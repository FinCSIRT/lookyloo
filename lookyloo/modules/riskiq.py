#!/usr/bin/env python3

from __future__ import annotations

import json

from datetime import date, datetime, timedelta
from typing import Any, TYPE_CHECKING
from urllib.parse import urlparse

from passivetotal import AccountClient, DnsRequest, WhoisRequest  # type: ignore[import-untyped]
from requests import Response

from ..default import ConfigError, get_homedir
from ..exceptions import ModuleError
from ..helpers import get_cache_directory

if TYPE_CHECKING:
    from ..capturecache import CaptureCache

from .abstractmodule import AbstractModule


class RiskIQError(ModuleError):

    def __init__(self, response: Response):
        self.response = response


class RiskIQ(AbstractModule):

    def module_init(self) -> bool:
        if not (self.config.get('user') and self.config.get('apikey')):
            self.logger.info('Missing credentials.')
            return False

        try:
            # Check if account is working
            test_client = AccountClient(username=self.config.get('user'),
                                        api_key=self.config.get('apikey'), exception_class=RiskIQError)
            details = test_client.get_account_details()
            self.client_dns = DnsRequest(username=self.config.get('user'),
                                         api_key=self.config.get('apikey'), exception_class=RiskIQError)
            self.client_whois = WhoisRequest(username=self.config.get('user'),
                                             api_key=self.config.get('apikey'), exception_class=RiskIQError)
        except RiskIQError as e:
            details = e.response.json()
            if 'message' in details:
                self.logger.warning(f'RiskIQ not available: {details["message"]}')
            else:
                self.logger.warning(f'RiskIQ not available: {details}')
            return False

        self.default_first_seen = self.config.get('default_first_seen_in_days', 5)

        self.storage_dir_riskiq = get_homedir() / 'riskiq'
        self.storage_dir_riskiq.mkdir(parents=True, exist_ok=True)
        return True

    def get_passivedns(self, query: str) -> dict[str, Any] | None:
        # The query can be IP or Hostname. For now, we only do it on domains.
        url_storage_dir = get_cache_directory(self.storage_dir_riskiq, query, 'pdns')
        if not url_storage_dir.exists():
            return None
        cached_entries = sorted(url_storage_dir.glob('*'), reverse=True)
        if not cached_entries:
            return None

        with cached_entries[0].open() as f:
            return json.load(f)

    def capture_default_trigger(self, cache: CaptureCache, /, *, force: bool=False,
                                auto_trigger: bool=False, as_admin: bool=False) -> dict[str, str]:
        '''Run the module on all the nodes up to the final redirect'''

        if error := super().capture_default_trigger(cache, force=force, auto_trigger=auto_trigger, as_admin=as_admin):
            return error

        if cache.url.startswith('file'):
            return {'error': 'RiskIQ does not support files.'}

        if cache.redirects:
            hostname = urlparse(cache.redirects[-1]).hostname
        else:
            hostname = urlparse(cache.url).hostname

        if not hostname:
            return {'error': 'No hostname found.'}

        self.__pdns_lookup(hostname, force)
        return {'success': 'Module triggered'}

    def __pdns_lookup(self, hostname: str, force: bool=False, first_seen: date | datetime | None=None) -> None:
        '''Lookup an hostname on RiskIQ Passive DNS
        Note: force means re-fetch the entry RiskIQ even if we already did it today
        '''
        if not self.available:
            raise ConfigError('RiskIQ not available, probably no API key')

        if first_seen is None:
            first_seen = date.today() - timedelta(days=self.default_first_seen)
        if isinstance(first_seen, datetime):
            first_seen = first_seen.date()

        url_storage_dir = get_cache_directory(self.storage_dir_riskiq, hostname, 'pdns')
        url_storage_dir.mkdir(parents=True, exist_ok=True)
        riskiq_file = url_storage_dir / date.today().isoformat()

        if not force and riskiq_file.exists():
            return

        pdns_info = self.client_dns.get_passive_dns(query=hostname, start=first_seen.isoformat())
        if not pdns_info or not pdns_info.get('results'):
            try:
                url_storage_dir.rmdir()
            except OSError:
                # Not empty.
                pass
            return
        pdns_info['results'] = sorted(pdns_info['results'], key=lambda k: k['lastSeen'], reverse=True)
        with riskiq_file.open('w') as _f:
            json.dump(pdns_info, _f)
