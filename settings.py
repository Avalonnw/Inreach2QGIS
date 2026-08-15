# -*- coding: utf-8 -*-

import json
from datetime import date, timedelta

from qgis.core import QgsApplication, QgsProject, QgsSettings


ORG = 'Inreach2QGIS'
AUTH_PREFIX = 'Inreach2QGIS'
LEGACY_PREFIX = 'GarminInReachTracker'
LEGACY_AUTH_KEY = 'GarminInReachTracker/api_key'
V2_KEY_INITIALIZED = f'{ORG}/v2_api_key_initialized'

DEFAULT_V1_ENDPOINT = 'https://aus-enterprise.inreach.garmin.com/ipcinbound/V1'
DEFAULT_V2_ENDPOINT = 'https://ipcinbound.inreachapp.com/api'

TRACK_COLOR_PALETTE = (
    '#2563EB',
    '#DC2626',
    '#16A34A',
    '#9333EA',
    '#EA580C',
    '#0891B2',
    '#DB2777',
    '#65A30D',
)


def default_track_color(imei):
    """Return a stable default colour derived from an IMEI."""
    text = str(imei or '')
    value = sum((index + 1) * ord(char) for index, char in enumerate(text))
    return TRACK_COLOR_PALETTE[value % len(TRACK_COLOR_PALETTE)]


def normalize_track_color(value, imei=''):
    """Return #RRGGBB, falling back to a stable colour for the IMEI."""
    text = str(value or '').strip().upper()
    if len(text) == 7 and text.startswith('#'):
        try:
            int(text[1:], 16)
        except ValueError:
            pass
        else:
            return text
    return default_track_color(imei)


class SettingsStore:
    def __init__(self):
        self.settings = QgsSettings()

    # ---------- global connection ----------
    def protocol(self):
        return self.settings.value(f'{ORG}/protocol', 'v2', type=str)

    def set_protocol(self, value):
        self.settings.setValue(f'{ORG}/protocol', value)

    def endpoint_v1(self):
        return self.settings.value(f'{ORG}/endpoint_v1', DEFAULT_V1_ENDPOINT, type=str)

    def set_endpoint_v1(self, value):
        self.settings.setValue(f'{ORG}/endpoint_v1', value.strip())

    def endpoint_v2(self):
        return self.settings.value(f'{ORG}/endpoint_v2', DEFAULT_V2_ENDPOINT, type=str)

    def set_endpoint_v2(self, value):
        self.settings.setValue(f'{ORG}/endpoint_v2', value.strip())

    def refresh_seconds(self):
        return self.settings.value(f'{ORG}/refresh_seconds', 60, type=int)

    def set_refresh_seconds(self, value):
        self.settings.setValue(f'{ORG}/refresh_seconds', int(value))

    def fresh_minutes(self):
        return self.settings.value(f'{ORG}/fresh_minutes', 15, type=int)

    def set_fresh_minutes(self, value):
        self.settings.setValue(f'{ORG}/fresh_minutes', int(value))

    def stale_minutes(self):
        return self.settings.value(f'{ORG}/stale_minutes', 60, type=int)

    def set_stale_minutes(self, value):
        self.settings.setValue(f'{ORG}/stale_minutes', int(value))

    # ---------- encrypted credentials ----------
    def _auth(self):
        return QgsApplication.authManager()

    def _unlock_auth(self):
        auth = self._auth()
        if auth.isDisabled():
            raise RuntimeError(auth.disabledMessage())
        if not auth.setMasterPassword(True):
            raise RuntimeError('QGIS authentication database is locked.')
        return auth

    def _load_secret(self, key):
        try:
            auth = self._unlock_auth()
        except RuntimeError:
            return ''
        value = auth.authSetting(f'{AUTH_PREFIX}/{key}', '', True)
        return str(value) if value is not None else ''

    def _save_secret(self, key, value):
        auth = self._unlock_auth()
        if not auth.storeAuthSetting(f'{AUTH_PREFIX}/{key}', value or '', True):
            raise RuntimeError('Could not store credentials in the QGIS authentication database.')

    def v1_username(self):
        return self._load_secret('v1_username')

    def v1_password(self):
        return self._load_secret('v1_password')

    def v2_api_key(self):
        value = self._load_secret('v2_api_key')
        initialized = self.settings.value(V2_KEY_INITIALIZED, False, type=bool)
        if initialized or value:
            return value

        # One-time compatibility fallback for the first Garmin inReach Tracker prototype.
        # Once the v0.2+ key has been explicitly saved (even as empty), never resurrect
        # the legacy credential.
        try:
            auth = self._unlock_auth()
            legacy = auth.authSetting(LEGACY_AUTH_KEY, '', True)
            return str(legacy) if legacy is not None else ''
        except RuntimeError:
            return ''

    def set_v1_credentials(self, username, password):
        self._save_secret('v1_username', username)
        self._save_secret('v1_password', password)

    def set_v2_api_key(self, api_key):
        self._save_secret('v2_api_key', api_key)
        self.settings.setValue(V2_KEY_INITIALIZED, True)

    # ---------- global device registry ----------
    def devices(self):
        raw = self.settings.value(f'{ORG}/devices', '', type=str)
        if not raw:
            raw = self.settings.value(f'{LEGACY_PREFIX}/devices_json', '[]', type=str)
        try:
            items = json.loads(raw)
        except Exception:
            items = []
        result = []
        seen = set()
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            imei = str(item.get('imei', '')).strip()
            name = str(item.get('name', '')).strip()
            if not imei or imei in seen:
                continue
            seen.add(imei)
            result.append({
                'name': name or imei,
                'imei': imei,
                'color': normalize_track_color(item.get('color', ''), imei),
            })
        return result

    def set_devices(self, devices):
        clean = []
        seen = set()
        for item in devices:
            imei = str(item.get('imei', '')).strip()
            name = str(item.get('name', '')).strip()
            if not imei or imei in seen:
                continue
            seen.add(imei)
            clean.append({
                'name': name or imei,
                'imei': imei,
                'color': normalize_track_color(item.get('color', ''), imei),
            })
        self.settings.setValue(f'{ORG}/devices', json.dumps(clean, ensure_ascii=False))

    # ---------- current QGIS project ----------
    def project_enabled(self):
        value, ok = QgsProject.instance().readBoolEntry(ORG, 'enabled', False)
        return bool(value) if ok else False

    def set_project_enabled(self, enabled):
        QgsProject.instance().writeEntry(ORG, 'enabled', bool(enabled))
        QgsProject.instance().setDirty(True)

    def project_imeis(self):
        values, ok = QgsProject.instance().readListEntry(ORG, 'imeis', [])
        return [str(x) for x in values] if ok else []

    def set_project_imeis(self, imeis):
        QgsProject.instance().writeEntry(ORG, 'imeis', [str(x) for x in imeis])
        QgsProject.instance().setDirty(True)

    def project_history_enabled(self):
        value, ok = QgsProject.instance().readBoolEntry(ORG, 'history_enabled', False)
        return bool(value) if ok else False

    def set_project_history_enabled(self, enabled):
        QgsProject.instance().writeEntry(ORG, 'history_enabled', bool(enabled))
        QgsProject.instance().setDirty(True)

    def project_history_start(self):
        default = (date.today() - timedelta(days=1)).isoformat()
        value, ok = QgsProject.instance().readEntry(ORG, 'history_start', default)
        return str(value) if ok and value else default

    def project_history_end(self):
        default = date.today().isoformat()
        value, ok = QgsProject.instance().readEntry(ORG, 'history_end', default)
        return str(value) if ok and value else default

    def set_project_history_range(self, start_date, end_date):
        QgsProject.instance().writeEntry(ORG, 'history_start', str(start_date))
        QgsProject.instance().writeEntry(ORG, 'history_end', str(end_date))
        QgsProject.instance().setDirty(True)
