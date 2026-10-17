"""ipworker's exif/geo step processors.

Mirrors the browser's client-side EXIF parser + Photon reverse-geocode call
(PhotoGallery.tsx) using the same exif_utils/maps_utils modules the backend's
own reactive server-side EXIF fallback already relies on
(storage_utils._apply_server_exif_fallback) -- no new dependencies needed, so
this module is safe to import even outside the ipworker image.
"""
from __future__ import annotations

import json
import math
from typing import Dict, Optional, Tuple

from exif_utils import ExifExtractionError, _exif_failure_category, extract_exif_from_bytes, extract_gps_decimal_from_exif
import maps_utils


def process_exif(user_id: str, filename: str, image_bytes: bytes) -> Dict:
    try:
        exif_data = extract_exif_from_bytes(image_bytes, filename)
        if 'EXIF.Truncated' in exif_data:
            raise ExifExtractionError('storage_limit')
        failure_category = _exif_failure_category(exif_data)
        if failure_category:
            raise ExifExtractionError(failure_category)
    except ExifExtractionError as exc:
        return {'hasData': False, 'error': 'exif_extraction_failed', 'errorCode': 'exif_' + exc.category}
    if not exif_data:
        return {'hasData': False, 'source': 'server_metadata', 'verifiedNoExif': True}
    return {'hasData': True, 'data': exif_data}


def geo_coordinates_from_exif(exif_data: Dict) -> Optional[Tuple[float, float]]:
    """None means no coordinates; present but unusable coordinates raise."""
    for keys, allowed in ((('GPS.GPSLatitudeRef', 'GPSLatitudeRef'), {'N', 'S'}),
                          (('GPS.GPSLongitudeRef', 'GPSLongitudeRef'), {'E', 'W'})):
        if any(key in exif_data and str(exif_data[key]).strip().upper()[:1] not in allowed for key in keys):
            raise ValueError('Invalid GPS reference')
    if any(isinstance(exif_data.get(key), bool) for key in (
            'GPS.LatitudeDecimal', 'GPS.LongitudeDecimal', 'GPSLatitude', 'GPSLongitude',
            'GPS.GPSLatitude', 'GPS.GPSLongitude', 'GPS.Latitude', 'GPS.Longitude')):
        raise ValueError('Invalid GPS coordinates')
    lat_str, lon_str = extract_gps_decimal_from_exif(exif_data)
    if not lat_str or not lon_str:
        if any(key in exif_data for key in (
                'GPS.LatitudeDecimal', 'GPS.LongitudeDecimal',
                'GPS.GPSLatitude', 'GPS.GPSLongitude', 'GPSLatitude', 'GPSLongitude',
                'GPS.Latitude', 'GPS.Longitude')):
            raise ValueError('Invalid GPS coordinates')
        return None
    lat, lon = float(lat_str), float(lon_str)
    if not math.isfinite(lat) or not math.isfinite(lon) or not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise ValueError('Invalid GPS coordinates')
    return lat, lon


def process_geo_from_exif(exif_data: Dict) -> Dict:
    coordinates = geo_coordinates_from_exif(exif_data)
    if coordinates is None:
        return {'hasData': False}
    lat, lon = coordinates
    place = maps_utils.reverse_geocode(lat, lon) or {}
    return {
        'hasData': True,
        'latitude': lat,
        'longitude': lon,
        'address': place.get('address', ''),
        'city': place.get('city', ''),
        'region': place.get('region', ''),
        'country': place.get('country', ''),
    }


def geo_exif_from_metadata(metadata: Dict) -> Optional[Dict]:
    """None requests original-file fallback; an empty dict means checked/no GPS."""
    status = str(metadata.get('exif_status') or '').lower()
    if status == 'no_data':
        return {}
    if status != 'done':
        return None
    try:
        if 'exifData' not in metadata:
            return None
        exif_data = metadata['exifData']
        if isinstance(exif_data, str):
            exif_data = json.loads(exif_data)
        if not isinstance(exif_data, dict):
            return None
        exif_data = dict(exif_data)
        lat, lon = metadata.get('latitude'), metadata.get('longitude')
        has_lat, has_lon = lat is not None and bool(str(lat).strip()), lon is not None and bool(str(lon).strip())
        if has_lat != has_lon:
            return None
        latitude, longitude = extract_gps_decimal_from_exif(exif_data)
        if latitude and longitude:
            lat_value, lon_value = float(latitude), float(longitude)
            if not math.isfinite(lat_value) or not math.isfinite(lon_value) or not -90 <= lat_value <= 90 or not -180 <= lon_value <= 180:
                return None
            if has_lat and has_lon and not (
                    math.isclose(float(lat), lat_value, rel_tol=0, abs_tol=1e-7)
                    and math.isclose(float(lon), lon_value, rel_tol=0, abs_tol=1e-7)):
                return None
        elif has_lat or has_lon or any(
                key in exif_data for key in ('GPSInfo', 'GPS.LatitudeDecimal', 'GPS.LongitudeDecimal',
                                            'GPS.GPSLatitude', 'GPS.GPSLongitude', 'GPSLatitude',
                                            'GPSLongitude', 'GPS.Latitude', 'GPS.Longitude')):
            # Top-level coordinates may survive a replaced file or a partial
            # client write. Require matching EXIF rather than declaring no GPS.
            return None
        return exif_data
    except (TypeError, ValueError, OverflowError):
        return None


def process_geo_from_metadata(metadata: Dict) -> Optional[Dict]:
    exif_data = geo_exif_from_metadata(metadata)
    return None if exif_data is None else process_geo_from_exif(exif_data)


def process_geo(user_id: str, filename: str, image_bytes: bytes) -> Dict:
    exif_result = process_exif(user_id, filename, image_bytes)
    if exif_result.get('error'):
        return exif_result
    return process_geo_from_exif(exif_result.get('data') or {})
