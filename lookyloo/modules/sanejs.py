#!/usr/bin/env python3

from __future__ import annotations

import json
import logging
from datetime import date
from collections.abc import Iterable

from pysanejs import SaneJS  # type: ignore[attr-defined]

from ..default import get_homedir, get_config, LookylooException


class SaneJavaScript():

    def __init__(self) -> None:
        self.logger = logging.getLogger(f'{self.__class__.__name__}')
        self.logger.setLevel(get_config('generic', 'loglevel'))
        self.config = get_config('modules', 'SaneJS')
        if not self.config.get('enabled'):
            self.logger.info('Not enabled.')
            self.available = False
            return

        self.client = SaneJS()

        if not self.client.is_up:
            self.logger.warning('Not up.')
            self.available = False

        self.storage_dir = get_homedir() / 'sanejs'
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.available = True

    def hashes_lookup(self, sha512: Iterable[str] | str, force: bool=False) -> dict[str, list[str]]:
        if not self.available:
            raise LookylooException('SaneJS is not available.')

        if isinstance(sha512, str):
            hashes: Iterable[str] = [sha512]
        else:
            hashes = sha512

        today_dir = self.storage_dir / date.today().isoformat()
        today_dir.mkdir(parents=True, exist_ok=True)
        sanejs_unknowns = today_dir / 'unknown'
        unknown_hashes = set()
        if sanejs_unknowns.exists():
            with sanejs_unknowns.open() as f:
                unknown_hashes = {line.strip() for line in f.readlines()}

        to_return: dict[str, list[str]] = {}

        if force:
            to_lookup = hashes
        else:
            to_lookup = [h for h in hashes if (h not in unknown_hashes
                                               and not (today_dir / h).exists())]
        has_new_unknown = False
        for h in to_lookup:
            try:
                response = self.client.sha512(h)
            except Exception as e:
                self.logger.warning(f'Something went wrong. Query: {h} - {e}')
                continue

            if 'error' in response:
                # Server not ready
                break
            if 'response' in response and response['response']:
                cached_path = today_dir / h
                with cached_path.open('w') as f:
                    json.dump(response['response'], f)
                to_return[h] = response['response']
            else:
                has_new_unknown = True
                unknown_hashes.add(h)

        for h in hashes:
            cached_path = today_dir / h
            if h in unknown_hashes or h in to_return:
                continue
            elif cached_path.exists():
                with cached_path.open() as f:
                    to_return[h] = json.load(f)

        if has_new_unknown:
            with sanejs_unknowns.open('w') as f:
                f.writelines(f'{h}\n' for h in unknown_hashes)

        return to_return
