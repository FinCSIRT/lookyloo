#!/usr/bin/env python3

from __future__ import annotations

import json
import time
import logging
import logging.config
from collections import Counter
from datetime import date, timedelta
from typing import Any

from lacuscore import CaptureStatus as CaptureStatusCore, CaptureSettingsError
from lookyloo import Lookyloo
from lookyloo.exceptions import LacusUnreachable
from lookyloo.default import AbstractManager, get_config, get_homedir, safe_create_dir
from lookyloo.helpers import ParsedUserAgent, serialize_to_json, CaptureSettings
from pylacus import CaptureStatus as CaptureStatusPy

logging.config.dictConfig(get_config('logging'))


class Processing(AbstractManager):

    def __init__(self, loglevel: int | None=None):
        super().__init__(loglevel)
        self.script_name = 'processing'
        self.lookyloo = Lookyloo()

        self.use_own_ua = get_config('generic', 'use_user_agents_users')

    def _to_run_forever(self) -> None:
        if self.use_own_ua:
            self._build_ua_file()
        self._retry_failed_enqueue()

    def _build_ua_file(self) -> None:
        '''Build a file in a format compatible with the capture page'''
        yesterday = (date.today() - timedelta(days=1))
        self_generated_ua_file_path = get_homedir() / 'own_user_agents' / str(yesterday.year) / f'{yesterday.month:02}'
        safe_create_dir(self_generated_ua_file_path)
        self_generated_ua_file = self_generated_ua_file_path / f'{yesterday.isoformat()}.json'
        if self_generated_ua_file.exists():
            self.logger.debug(f'User-agent file for {yesterday} already exists.')
            return
        self.logger.info(f'Generating user-agent file for {yesterday}')
        entries = self.lookyloo.redis.zrevrange(f'user_agents|{yesterday.isoformat()}', 0, -1)
        if not entries:
            self.logger.info(f'No User-agent file for {yesterday} to generate.')
            return

        to_store: dict[str, Any] = {'by_frequency': []}
        uas = Counter([entry.split('|', 1)[1] for entry in entries])
        for ua, _ in uas.most_common():
            parsed_ua = ParsedUserAgent(ua)
            if not parsed_ua.platform or not parsed_ua.browser:
                continue
            platform_key = parsed_ua.platform
            if parsed_ua.platform_version:
                platform_key = f'{platform_key} {parsed_ua.platform_version}'
            browser_key = parsed_ua.browser
            if parsed_ua.version:
                browser_key = f'{browser_key} {parsed_ua.version}'
            if platform_key not in to_store:
                to_store[platform_key] = {}
            if browser_key not in to_store[platform_key]:
                to_store[platform_key][browser_key] = set()
            to_store[platform_key][browser_key].add(parsed_ua.string)
            to_store['by_frequency'].append({'os': platform_key,
                                             'browser': browser_key,
                                             'useragent': parsed_ua.string})
        with self_generated_ua_file.open('w') as f:
            json.dump(to_store, f, indent=2, default=serialize_to_json)

        # Remove the UA / IP mapping.
        self.lookyloo.redis.delete(f'user_agents|{yesterday.isoformat()}')
        self.logger.info(f'User-agent file for {yesterday} generated.')

    def _retry_failed_enqueue(self) -> None:
        '''If enqueuing failed, the settings are added, with a UUID in the 'to_capture key', and they have a UUID'''
        to_requeue: list[str] = []
        try:
            for uuid, _ in self.lookyloo.redis.zscan_iter('to_capture'):
                if self.lookyloo.redis.hget(uuid, 'not_queued') == '1':
                    # The capture is marked as not queued
                    to_requeue.append(uuid)
                elif self.lookyloo.lacus.get_capture_status(uuid) in [CaptureStatusPy.UNKNOWN, CaptureStatusCore.UNKNOWN]:
                    # The capture is unknown on lacus side. It might be a race condition.
                    # Let's retry a few times.
                    retry = 3
                    while retry > 0:
                        time.sleep(1)
                        if self.lookyloo.lacus.get_capture_status(uuid) not in [CaptureStatusPy.UNKNOWN, CaptureStatusCore.UNKNOWN]:
                            # Was a race condition, the UUID has been or is being processed by Lacus
                            self.logger.info(f'UUID {uuid} was only temporary unknown')
                            break
                        retry -= 1
                    else:
                        # UUID is still unknown
                        self.logger.info(f'UUID {uuid} is still unknown')
                        to_requeue.append(uuid)
        except LacusUnreachable:
            self.logger.warning('Lacus still unreachable, trying again later')
            return None

        for uuid in to_requeue:
            if self.lookyloo.redis.zscore('to_capture', uuid) is None:
                # The capture has been captured in the meantime.
                continue
            self.logger.info(f'Found a non-queued capture ({uuid}), retrying now.')
            # This capture couldn't be queued and we created the uuid locally
            try:
                if capture_settings := self.lookyloo.redis.hgetall(uuid):
                    query = CaptureSettings(**capture_settings)
                    try:
                        new_uuid = self.lookyloo.lacus.enqueue(
                            url=query.url,
                            document_name=query.document_name,
                            document=query.document,
                            # depth=query.depth,
                            browser=query.browser,
                            device_name=query.device_name,
                            user_agent=query.user_agent,
                            proxy=query.proxy,
                            general_timeout_in_sec=query.general_timeout_in_sec,
                            cookies=query.cookies,
                            headers=query.headers,
                            http_credentials=query.http_credentials,
                            viewport=query.viewport,
                            referer=query.referer,
                            rendered_hostname_only=query.rendered_hostname_only,
                            # force=query.force,
                            # recapture_interval=query.recapture_interval,
                            priority=query.priority,
                            uuid=uuid
                        )
                        if new_uuid != uuid:
                            # somehow, between the check and queuing, the UUID isn't UNKNOWN anymore, just checking that
                            self.logger.warning(f'Had to change the capture UUID (duplicate). Old: {uuid} / New: {new_uuid}')
                    except LacusUnreachable:
                        self.logger.warning('Lacus still unreachable.')
                        break
                    except Exception as e:
                        self.logger.warning(f'Still unable to enqueue capture: {e}')
                        break
                    else:
                        self.lookyloo.redis.hdel(uuid, 'not_queued')
                        self.logger.info(f'{uuid} enqueued.')
            except CaptureSettingsError as e:
                self.logger.error(f'Broken settings for {uuid} made their way in the cache, removing them: {e}')
                self.lookyloo.redis.zrem('to_capture', uuid)
                self.lookyloo.redis.delete(uuid)

            except Exception as e:
                self.logger.error(f'Unable to requeue {uuid}: {e}')


def main() -> None:
    p = Processing()
    p.run(sleep_in_sec=30)


if __name__ == '__main__':
    main()
