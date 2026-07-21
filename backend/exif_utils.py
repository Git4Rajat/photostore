import io
import json
import os
import shutil
import subprocess
import tempfile
from ast import literal_eval
import re
from typing import Dict, Optional, Tuple

from PIL import ExifTags, Image

try:
    import piexif
except Exception:
    piexif = None

MAX_EXIF_VALUE_LENGTH = 512
MAX_EXIF_STORAGE_LENGTH = 60000
EXIFTOOL_TIMEOUT_SECONDS = int(os.getenv('EXIFTOOL_TIMEOUT_SECONDS', '20'))


def _stringify_exif_value(value) -> str:
    try:
        if isinstance(value, bytes):
            text = value.decode('utf-8', errors='replace')
        elif isinstance(value, (list, tuple)):
            text = ', '.join(_stringify_exif_value(item) for item in value)
        elif isinstance(value, dict):
            text = json.dumps({str(k): _stringify_exif_value(v) for k, v in value.items()}, ensure_ascii=False)
        else:
            text = str(value)
    except Exception:
        text = str(value)

    if len(text) > MAX_EXIF_VALUE_LENGTH:
        return f'{text[:MAX_EXIF_VALUE_LENGTH]}...'
    return text


def _rational_to_float(value) -> Optional[float]:
    try:
        if isinstance(value, tuple) and len(value) == 2 and value[1] != 0:
            return float(value[0]) / float(value[1])
        return float(value)
    except Exception:
        return None


def _parse_gps_component(value):
    if not value:
        return None
    if isinstance(value, str):
        cleaned = value.strip()
        try:
            value = literal_eval(cleaned)
        except Exception:
            numbers = re.findall(r'-?\d+(?:\.\d+)?', cleaned)
            if len(numbers) >= 6:
                value = tuple((float(numbers[i]), float(numbers[i + 1])) for i in range(0, 6, 2))
            elif len(numbers) >= 3:
                value = tuple(float(number) for number in numbers[:3])
            else:
                return None
    if isinstance(value, (list, tuple)) and len(value) >= 6 and not isinstance(value[0], (list, tuple)):
        return tuple((value[i], value[i + 1]) for i in range(0, 6, 2))
    return value


def _gps_dms_to_decimal(values, ref: str) -> Optional[float]:
    try:
        values = _parse_gps_component(values)
        if not values or len(values) < 3:
            return None
        degrees = _rational_to_float(values[0])
        minutes = _rational_to_float(values[1])
        seconds = _rational_to_float(values[2])
        if degrees is None or minutes is None or seconds is None:
            return None
        decimal = degrees + (minutes / 60.0) + (seconds / 3600.0)
        if ref in ('S', 'W'):
            decimal *= -1.0
        return round(decimal, 7)
    except Exception:
        return None


def extract_gps_decimal_from_exif(exif_data: Dict[str, str]) -> Tuple[str, str]:
    lat = exif_data.get('GPS.LatitudeDecimal', '')
    lon = exif_data.get('GPS.LongitudeDecimal', '')
    if lat and lon:
        return lat, lon

    lat_raw = exif_data.get('GPS.GPSLatitude', '') or exif_data.get('GPSLatitude', '') or exif_data.get('GPS.Latitude', '')
    lon_raw = exif_data.get('GPS.GPSLongitude', '') or exif_data.get('GPSLongitude', '') or exif_data.get('GPS.Longitude', '')
    lat_ref = (exif_data.get('GPS.GPSLatitudeRef', '') or exif_data.get('GPSLatitudeRef', '') or 'N').upper()[:1] or 'N'
    lon_ref = (exif_data.get('GPS.GPSLongitudeRef', '') or exif_data.get('GPSLongitudeRef', '') or 'E').upper()[:1] or 'E'

    lat_decimal = _parse_decimal_or_dms(lat_raw, lat_ref, -90.0, 90.0) if lat_raw else None
    lon_decimal = _parse_decimal_or_dms(lon_raw, lon_ref, -180.0, 180.0) if lon_raw else None
    if lat_decimal is None or lon_decimal is None:
        return '', ''

    return str(lat_decimal), str(lon_decimal)


def _parse_decimal_or_dms(value, ref: str, minimum: float, maximum: float) -> Optional[float]:
    try:
        parsed = float(value)
        if ref in ('S', 'W') and parsed > 0:
            parsed *= -1
        if minimum <= parsed <= maximum:
            return round(parsed, 7)
    except Exception:
        pass
    return _gps_dms_to_decimal(value, ref)


def _trim_exif_for_storage(exif: Dict[str, str]) -> Dict[str, str]:
    serialized = json.dumps(exif, ensure_ascii=False, separators=(',', ':'))
    if len(serialized) <= MAX_EXIF_STORAGE_LENGTH:
        return exif

    trimmed: Dict[str, str] = {}
    total_len = 2
    for key in sorted(exif.keys()):
        value = exif[key]
        item_len = len(key) + len(value) + 6
        if total_len + item_len > MAX_EXIF_STORAGE_LENGTH:
            break
        trimmed[key] = value
        total_len += item_len

    trimmed['EXIF.Truncated'] = 'true'
    return trimmed


def _normalize_exiftool_key(key: str) -> str:
    cleaned = str(key or '').strip()
    if not cleaned:
        return ''
    if ':' in cleaned:
        group, tag = cleaned.split(':', 1)
        if group.upper() == 'GPS':
            return f'GPS.{tag}'
        return tag
    return cleaned


def _extract_exiftool_from_path(path: str) -> Dict[str, str]:
    exiftool = shutil.which('exiftool')
    if not exiftool:
        return {}
    try:
        result = subprocess.run(
            [exiftool, '-j', '-G1', '-n', path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=EXIFTOOL_TIMEOUT_SECONDS,
            check=False,
        )
    except Exception:
        return {}
    if result.returncode != 0 or not result.stdout:
        return {}
    try:
        parsed = json.loads(result.stdout.decode('utf-8', errors='replace'))
    except Exception:
        return {}
    if not isinstance(parsed, list) or not parsed or not isinstance(parsed[0], dict):
        return {}

    exif: Dict[str, str] = {}
    for raw_key, raw_value in parsed[0].items():
        key = _normalize_exiftool_key(str(raw_key))
        if not key or key in {'SourceFile', 'FileName', 'Directory'}:
            continue
        value = _stringify_exif_value(raw_value)
        if key.startswith('GPS.'):
            exif[key] = value
        elif key not in exif:
            exif[key] = value

    lat = exif.get('GPSLatitude') or exif.get('GPS.GPSLatitude') or exif.get('GPS.Latitude')
    lon = exif.get('GPSLongitude') or exif.get('GPS.GPSLongitude') or exif.get('GPS.Longitude')
    lat_ref = (exif.get('GPSLatitudeRef') or exif.get('GPS.GPSLatitudeRef') or 'N').upper()[:1] or 'N'
    lon_ref = (exif.get('GPSLongitudeRef') or exif.get('GPS.GPSLongitudeRef') or 'E').upper()[:1] or 'E'
    lat_decimal = _valid_decimal_from_exiftool(lat, lat_ref, -90.0, 90.0)
    lon_decimal = _valid_decimal_from_exiftool(lon, lon_ref, -180.0, 180.0)
    if lat_decimal and lon_decimal:
        exif['GPS.LatitudeDecimal'] = lat_decimal
        exif['GPS.LongitudeDecimal'] = lon_decimal

    return _trim_exif_for_storage(exif)


def _valid_decimal_from_exiftool(value, ref: str, minimum: float, maximum: float) -> str:
    try:
        parsed = float(value)
    except Exception:
        return ''
    if ref in {'S', 'W'} and parsed > 0:
        parsed *= -1
    if parsed < minimum or parsed > maximum:
        return ''
    return str(round(parsed, 7))


def extract_exif_from_path(path: str) -> Dict[str, str]:
    exif = _extract_exiftool_from_path(path)
    if exif:
        return exif
    try:
        with Image.open(path) as image:
            return extract_exif_from_image(image)
    except Exception:
        return {}


def extract_exif_from_bytes(image_bytes: bytes, filename: str = '') -> Dict[str, str]:
    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            return extract_exif_from_image(image)
    except Exception:
        pass
    if not image_bytes:
        return {}
    ext = filename.rsplit('.', 1)[-1].lower() if filename and '.' in filename else 'raw'
    suffix = f'.{ext}' if ext else '.raw'
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix) as temp_file:
            temp_file.write(image_bytes)
            temp_file.flush()
            return extract_exif_from_path(temp_file.name)
    except Exception:
        return {}


def extract_exif_from_image(image: Image.Image) -> Dict[str, str]:
    exif: Dict[str, str] = {}
    raw_exif = image.getexif()
    if not raw_exif:
        return exif

    gps_lat = None
    gps_lon = None

    for tag_id, value in raw_exif.items():
        tag_name = ExifTags.TAGS.get(tag_id, str(tag_id))
        if tag_name == 'GPSInfo':
            gps_items = None
            if isinstance(value, dict):
                gps_items = value
            elif hasattr(value, 'items'):
                try:
                    gps_items = dict(value.items())
                except Exception:
                    gps_items = None
            elif isinstance(value, int):
                try:
                    pointed_ifd = raw_exif.get_ifd(value)
                    if pointed_ifd and hasattr(pointed_ifd, 'items'):
                        gps_items = dict(pointed_ifd.items())
                except Exception:
                    gps_items = None

            if gps_items:
                for gps_tag, gps_value in gps_items.items():
                    gps_name = ExifTags.GPSTAGS.get(gps_tag, str(gps_tag))
                    exif[f'GPS.{gps_name}'] = _stringify_exif_value(gps_value)

                lat = _gps_dms_to_decimal(gps_items.get(2), str(gps_items.get(1, 'N')).upper()[:1] or 'N')
                lon = _gps_dms_to_decimal(gps_items.get(4), str(gps_items.get(3, 'E')).upper()[:1] or 'E')
                if lat is not None and lon is not None:
                    gps_lat, gps_lon = lat, lon
                    exif['GPS.LatitudeDecimal'] = str(lat)
                    exif['GPS.LongitudeDecimal'] = str(lon)
            else:
                exif['GPSInfo'] = _stringify_exif_value(value)
        else:
            exif[tag_name] = _stringify_exif_value(value)

    if gps_lat is None or gps_lon is None:
        try:
            gps_ifd = raw_exif.get_ifd(0x8825)
        except Exception:
            gps_ifd = None

        if gps_ifd:
            for gps_tag, gps_value in gps_ifd.items():
                gps_name = ExifTags.GPSTAGS.get(gps_tag, str(gps_tag))
                exif[f'GPS.{gps_name}'] = _stringify_exif_value(gps_value)

            lat = _gps_dms_to_decimal(gps_ifd.get(2), str(gps_ifd.get(1, 'N')).upper()[:1] or 'N')
            lon = _gps_dms_to_decimal(gps_ifd.get(4), str(gps_ifd.get(3, 'E')).upper()[:1] or 'E')
            if lat is not None and lon is not None:
                exif['GPS.LatitudeDecimal'] = str(lat)
                exif['GPS.LongitudeDecimal'] = str(lon)

    if (gps_lat is None or gps_lon is None) and piexif is not None:
        try:
            exif_blob = image.info.get('exif')
            if exif_blob:
                exif_dict = piexif.load(exif_blob)
                gps_ifd = exif_dict.get('GPS', {}) or {}

                lat_raw = gps_ifd.get(piexif.GPSIFD.GPSLatitude)
                lon_raw = gps_ifd.get(piexif.GPSIFD.GPSLongitude)
                lat_ref_raw = gps_ifd.get(piexif.GPSIFD.GPSLatitudeRef, b'N')
                lon_ref_raw = gps_ifd.get(piexif.GPSIFD.GPSLongitudeRef, b'E')

                if isinstance(lat_ref_raw, (bytes, bytearray)):
                    lat_ref = lat_ref_raw.decode('ascii', errors='ignore').upper()[:1] or 'N'
                else:
                    lat_ref = str(lat_ref_raw).upper()[:1] or 'N'

                if isinstance(lon_ref_raw, (bytes, bytearray)):
                    lon_ref = lon_ref_raw.decode('ascii', errors='ignore').upper()[:1] or 'E'
                else:
                    lon_ref = str(lon_ref_raw).upper()[:1] or 'E'

                lat = _gps_dms_to_decimal(lat_raw, lat_ref) if lat_raw else None
                lon = _gps_dms_to_decimal(lon_raw, lon_ref) if lon_raw else None

                if lat is not None and lon is not None:
                    exif['GPS.GPSLatitudeRef'] = lat_ref
                    exif['GPS.GPSLongitudeRef'] = lon_ref
                    exif['GPS.GPSLatitude'] = _stringify_exif_value(lat_raw)
                    exif['GPS.GPSLongitude'] = _stringify_exif_value(lon_raw)
                    exif['GPS.LatitudeDecimal'] = str(lat)
                    exif['GPS.LongitudeDecimal'] = str(lon)
        except Exception:
            pass

    return _trim_exif_for_storage(exif)


def parse_exif_data(exif_raw: str) -> Dict[str, str]:
    if not exif_raw:
        return {}
    try:
        parsed = json.loads(exif_raw)
        if isinstance(parsed, dict):
            return {str(k): str(v) for k, v in parsed.items()}
    except Exception:
        pass
    return {}


def exif_summary(exif_data: Dict[str, str]) -> Dict[str, str]:
    return {
        'camera': exif_data.get('Model', ''),
        'lens': exif_data.get('LensModel', '') or exif_data.get('LensID', '') or exif_data.get('Lens', ''),
        'capturedAt': (
            exif_data.get('DateTimeOriginal', '')
            or exif_data.get('CreateDate', '')
            or exif_data.get('DateTime', '')
            or exif_data.get('ModifyDate', '')
        ),
        'fNumber': exif_data.get('FNumber', ''),
        'exposureTime': exif_data.get('ExposureTime', '') or exif_data.get('ShutterSpeed', ''),
        'iso': exif_data.get('ISOSpeedRatings', '') or exif_data.get('PhotographicSensitivity', '') or exif_data.get('ISO', ''),
        'focalLength': exif_data.get('FocalLength', '') or exif_data.get('FocalLengthIn35mmFormat', ''),
    }
