"""ipworker's exif/geo step processors.

Mirrors the browser's client-side EXIF parser + Photon reverse-geocode call
(PhotoGallery.tsx) using the same exif_utils/maps_utils modules the backend's
own reactive server-side EXIF fallback already relies on
(storage_utils._apply_server_exif_fallback) -- no new dependencies needed, so
this module is safe to import even outside the ipworker image.
"""
from __future__ import annotations

from typing import Dict, Optional

from exif_utils import extract_exif_from_bytes, extract_gps_decimal_from_exif
import maps_utils


def process_exif(user_id: str, filename: str, image_bytes: bytes) -> Optional[Dict]:
    exif_data = extract_exif_from_bytes(image_bytes, filename)
    if not exif_data:
        return {'hasData': False}
    return {'hasData': True, 'data': exif_data}


def process_geo(user_id: str, filename: str, image_bytes: bytes) -> Optional[Dict]:
    # Re-extracts EXIF rather than sharing process_exif's result -- the two
    # steps are dispatched independently by _run_ipwork_steps (app.py), and
    # exiftool's subprocess cost is small next to face/vision inference, so
    # this isn't worth a cross-step cache for now.
    exif_data = extract_exif_from_bytes(image_bytes, filename)
    lat_str, lon_str = extract_gps_decimal_from_exif(exif_data)
    if not lat_str or not lon_str:
        return {'hasData': False}
    lat, lon = float(lat_str), float(lon_str)
    place = maps_utils.reverse_geocode(lat, lon) or {}
    return {
        'hasData': True,
        'latitude': lat,
        'longitude': lon,
        'address': place.get('address', ''),
        'city': place.get('city', ''),
        'country': place.get('country', ''),
    }
