# -*- coding: utf-8 -*-

import base64
import json
import urllib.error
import urllib.parse
import urllib.request


class GarminApiError(RuntimeError):
    pass


def _garmin_timestamp(value):
    """Return Garmin milliseconds from /Date(123)/ or a numeric timestamp."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value)
    if text.startswith('/Date(') and text.endswith(')/'):
        text = text[6:-2]
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def _normalize_location(raw):
    if not isinstance(raw, dict):
        return None

    coordinate = raw.get('coordinate') or raw.get('Coordinate') or {}
    if not isinstance(coordinate, dict):
        coordinate = {}

    latitude = coordinate.get('latitude', coordinate.get('Latitude'))
    longitude = coordinate.get('longitude', coordinate.get('Longitude'))
    if latitude is None or longitude is None:
        return None

    return {
        'imei': str(raw.get('imei', raw.get('IMEI', ''))),
        'timestamp_ms': _garmin_timestamp(raw.get('timestamp', raw.get('Timestamp'))),
        'latitude': float(latitude),
        'longitude': float(longitude),
        'altitude_m': raw.get('altitude', raw.get('Altitude')),
        'speed_kmh': raw.get('speed', raw.get('Speed')),
        'course_deg': raw.get('course', raw.get('Course')),
        'gps_fix': raw.get('gpsFixStatus', raw.get('GPSFixStatus')),
        'text_message': raw.get('textMessage', raw.get('TextMessage', '')),
    }


class GarminClientBase:
    def __init__(self, endpoint, timeout=15):
        self.endpoint = endpoint.rstrip('/')
        self.timeout = timeout

    def _load_json(self, request):
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = response.read().decode('utf-8-sig')
                retry_after = response.headers.get('Retry-After')
                return json.loads(payload), retry_after
        except urllib.error.HTTPError as exc:
            detail = ''
            try:
                detail = exc.read().decode('utf-8', errors='replace')
            except Exception:
                pass
            retry_after = exc.headers.get('Retry-After') if exc.headers else None
            suffix = f' Retry-After: {retry_after}s.' if retry_after else ''
            raise GarminApiError(f'HTTP {exc.code}: {detail or exc.reason}.{suffix}') from exc
        except urllib.error.URLError as exc:
            raise GarminApiError(f'Network error: {exc.reason}') from exc
        except json.JSONDecodeError as exc:
            raise GarminApiError('Garmin returned invalid JSON.') from exc

    @staticmethod
    def _normalize_list(raw_locations, description):
        if not isinstance(raw_locations, list):
            raise GarminApiError(f'Garmin response does not contain a {description} list.')
        result = []
        for raw in raw_locations:
            item = _normalize_location(raw)
            if item:
                result.append(item)
        return result

    @classmethod
    def _extract_locations(cls, payload):
        if not isinstance(payload, dict):
            raise GarminApiError('Unexpected Garmin response.')
        raw_locations = payload.get('locations', payload.get('Locations', []))
        return cls._normalize_list(raw_locations, 'location')

    @classmethod
    def _extract_history(cls, payload):
        if not isinstance(payload, dict):
            raise GarminApiError('Unexpected Garmin response.')
        raw_locations = payload.get('historyItems', payload.get('HistoryItems', []))
        # Garmin can represent a valid no-history result as JSON null rather than
        # an empty array. No records for a requested period is not an API failure.
        if raw_locations is None:
            return []
        return cls._normalize_list(raw_locations, 'history')


class GarminV1Client(GarminClientBase):
    """Garmin IPC Inbound V1 client using HTTP Basic authentication."""

    def __init__(self, endpoint, username, password, timeout=15):
        super().__init__(endpoint, timeout)
        self.username = username
        self.password = password

    def _request(self, url):
        token = base64.b64encode(
            f'{self.username}:{self.password}'.encode('utf-8')
        ).decode('ascii')
        return urllib.request.Request(
            url,
            headers={
                'Authorization': f'Basic {token}',
                'Accept': 'application/json',
                'User-Agent': 'Inreach2QGIS/0.3',
            },
        )

    def last_known_locations(self, imeis):
        locations = []
        # V1 LastKnownLocation is documented/observed with singular IMEI.
        for imei in imeis:
            query = urllib.parse.urlencode({'IMEI': str(imei)})
            url = f'{self.endpoint}/Location.svc/LastKnownLocation?{query}'
            payload, _ = self._load_json(self._request(url))
            locations.extend(self._extract_locations(payload))
        return locations

    def history(self, imeis, start, end):
        query = urllib.parse.urlencode({
            'IMEIs': ','.join(str(x) for x in imeis),
            'Start': start,
            'End': end,
        })
        url = f'{self.endpoint}/Location.svc/History?{query}'
        payload, _ = self._load_json(self._request(url))
        return self._extract_history(payload)


class GarminV2Client(GarminClientBase):
    """Garmin IPC Inbound V2 client using X-API-Key authentication."""

    def __init__(self, endpoint, api_key, timeout=15):
        super().__init__(endpoint, timeout)
        self.api_key = api_key

    def _request(self, url):
        return urllib.request.Request(
            url,
            headers={
                'X-API-Key': self.api_key,
                'Accept': 'application/json',
                'User-Agent': 'Inreach2QGIS/0.3',
            },
        )

    def last_known_locations(self, imeis):
        query = urllib.parse.urlencode({'IMEI': ','.join(str(x) for x in imeis)})
        url = f'{self.endpoint}/Location/LastKnownLocation?{query}'
        payload, _ = self._load_json(self._request(url))
        return self._extract_locations(payload)

    def history(self, imeis, start, end):
        # Garmin's V2 documentation uses IMEI (singular parameter name), while
        # still allowing a comma-separated device list.
        query = urllib.parse.urlencode({
            'IMEI': ','.join(str(x) for x in imeis),
            'Start': start,
            'End': end,
        })
        url = f'{self.endpoint}/Location/History?{query}'
        payload, _ = self._load_json(self._request(url))
        return self._extract_history(payload)
