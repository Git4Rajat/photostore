import io
import json
import math
import os
import shutil
import subprocess
import tempfile
import threading
import warnings
from ast import literal_eval
from contextlib import contextmanager
import re
from typing import Dict, Iterable, Optional, Tuple

from PIL import ExifTags, Image

try:
    import piexif
except Exception:
    piexif = None

MAX_EXIF_VALUE_LENGTH = 512
MAX_EXIF_STORAGE_LENGTH = 60000
EXIFTOOL_TIMEOUT_SECONDS = int(os.getenv('EXIFTOOL_TIMEOUT_SECONDS', '20'))
_PILLOW_METADATA_LOCK = threading.RLock()


class ExifExtractionError(RuntimeError):
    """Extraction failed; category and message never include parser output."""

    CATEGORIES = frozenset({
        'unavailable', 'timeout', 'parser_error', 'invalid_output',
        'execution_failed', 'read_failed', 'unsupported', 'storage_limit',
    })

    def __init__(self, category: str):
        if not isinstance(category, str) or category not in self.CATEGORIES:
            raise ValueError('Unknown EXIF extraction category')
        self.category = category
        super().__init__(f'EXIF extraction failed: {self.category}')


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
        if (not all(math.isfinite(value) for value in (degrees, minutes, seconds))
                or degrees < 0 or not 0 <= minutes < 60 or not 0 <= seconds < 60):
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
    if lat is not None and lon is not None and str(lat).strip() and str(lon).strip():
        return str(lat), str(lon)

    def present(*keys):
        return next((exif_data[key] for key in keys if key in exif_data
                     and exif_data[key] is not None and str(exif_data[key]).strip()), '')
    lat_raw = present('GPS.GPSLatitude', 'GPSLatitude', 'GPS.Latitude')
    lon_raw = present('GPS.GPSLongitude', 'GPSLongitude', 'GPS.Longitude')
    lat_ref = (exif_data.get('GPS.GPSLatitudeRef', '') or exif_data.get('GPSLatitudeRef', '') or 'N').upper()[:1] or 'N'
    lon_ref = (exif_data.get('GPS.GPSLongitudeRef', '') or exif_data.get('GPSLongitudeRef', '') or 'E').upper()[:1] or 'E'

    lat_decimal = _parse_decimal_or_dms(lat_raw, lat_ref, -90.0, 90.0) if str(lat_raw).strip() else None
    lon_decimal = _parse_decimal_or_dms(lon_raw, lon_ref, -180.0, 180.0) if str(lon_raw).strip() else None
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


def _exif_failure_category(exif_data: Dict) -> str:
    parser_error = False
    for raw_key, value in exif_data.items():
        key = _normalize_exiftool_key(raw_key)
        if key == 'Error':
            if value in ('Unknown file type', 'Unsupported RAW file type', 'Unsupported file type'):
                return 'unsupported'
            parser_error = True
        elif key == 'Warning' and value == 'Unsupported file type':
            return 'unsupported'
    return 'parser_error' if parser_error else ''


def _exiftool_fields(obj: Dict) -> Dict[str, str]:
    """Normalize an `exiftool -j -G1 -n` JSON object into stored EXIF fields.

    No failure/empty raises -- callers decide how to treat an empty result. This
    is the single normalization shared by the server's own extraction and by
    browser-supplied ExifTool JSON, so a browser ExifTool (same version) yields
    byte-identical stored fields.
    """
    exif: Dict[str, str] = {}
    for raw_key, raw_value in obj.items():
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
    return exif


def _normalize_exiftool_parsed(obj: Dict, *, returncode: int = 0) -> Dict[str, str]:
    """Shared normalization: failure-category + fields + non-empty check + trim."""
    failure_category = _exif_failure_category(obj)
    if failure_category:
        raise ExifExtractionError(failure_category)
    if returncode != 0:
        raise ExifExtractionError('parser_error')
    exif = _exiftool_fields(obj)
    if not exif:
        raise ExifExtractionError('invalid_output')
    return _trim_exif_for_storage(exif)


def classify_browser_exiftool(obj) -> str:
    """Failure category ('unsupported' | 'parser_error' | '') for a browser-
    supplied `exiftool -j -G1 -n` JSON object. Identical to the server's own
    verdict because it is the same ExifTool output. '' means usable (the file
    may still legitimately carry no EXIF -- see browser_exiftool_fields)."""
    if not isinstance(obj, dict):
        return 'invalid_output'
    return _exif_failure_category(obj)


def browser_exiftool_fields(obj) -> Dict[str, str]:
    """Stored EXIF fields from a browser-supplied ExifTool JSON object, empty if
    the file genuinely carries no EXIF. Caller must call classify_browser_exiftool
    first and handle 'unsupported'/'parser_error' before using this."""
    if not isinstance(obj, dict):
        return {}
    return _trim_exif_for_storage(_exiftool_fields(obj))


def _extract_exiftool_from_path(path: str, *, scan_jpeg: bool = False) -> Dict[str, str]:
    exiftool = shutil.which('exiftool')
    if not exiftool:
        raise ExifExtractionError('unavailable')
    try:
        # ExtractEmbedded keeps JPEG parsing past SOS in ExifTool 13.55.
        # Packet extraction leaves this off to preserve its qualified behavior.
        result = subprocess.run(
            [exiftool, '-j', '-G1', '-n', *(['-ee'] if scan_jpeg else []), path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=EXIFTOOL_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise ExifExtractionError('timeout') from None
    except FileNotFoundError:
        raise ExifExtractionError('unavailable') from None
    except OSError:
        raise ExifExtractionError('execution_failed') from None
    if not result.stdout:
        raise ExifExtractionError('parser_error' if result.returncode else 'invalid_output')
    try:
        parsed = json.loads(result.stdout.decode('utf-8', errors='replace'))
    except (UnicodeError, json.JSONDecodeError):
        raise ExifExtractionError('parser_error' if result.returncode else 'invalid_output') from None
    if not isinstance(parsed, list) or len(parsed) != 1 or not isinstance(parsed[0], dict):
        raise ExifExtractionError('parser_error' if result.returncode else 'invalid_output')
    return _normalize_exiftool_parsed(parsed[0], returncode=result.returncode)


def _exiftool_has_only_warning(path: str, expected: str) -> bool:
    """Check every diagnostic before exempting a known, benign warning.

    Ordinary JSON output hides duplicate Warning tags. Family 4 makes them
    unique without changing the existing metadata extraction/normalization.
    """
    exiftool = shutil.which('exiftool')
    if not exiftool:
        return False
    try:
        result = subprocess.run(
            [exiftool, '-j', '-G4', '-Warning', '-Error', path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=EXIFTOOL_TIMEOUT_SECONDS, check=False,
        )
        if result.returncode != 0 or result.stderr:
            return False
        parsed = json.loads(result.stdout.decode('utf-8'))
        if not isinstance(parsed, list) or len(parsed) != 1 or not isinstance(parsed[0], dict):
            return False
        warnings = []
        for key, value in parsed[0].items():
            if key == 'SourceFile':
                continue
            if key.rsplit(':', 1)[-1] != 'Warning':
                return False
            warnings.append(value)
        return bool(warnings) and all(value == expected for value in warnings)
    except Exception:
        return False


def _exiftool_has_only_warnings(path: str, expected: Iterable[str]) -> bool:
    """Match an exact, nonempty warning multiset, including duplicate counts."""
    exiftool = shutil.which('exiftool')
    if not exiftool:
        return False
    try:
        if isinstance(expected, (str, bytes)):
            return False
        expected_values = list(expected)
        if not expected_values or not all(isinstance(value, str) for value in expected_values):
            return False
        result = subprocess.run(
            [exiftool, '-j', '-G4', '-Warning', '-Error', path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=EXIFTOOL_TIMEOUT_SECONDS, check=False,
        )
        if result.returncode != 0 or result.stderr:
            return False
        parsed = json.loads(result.stdout.decode('utf-8'))
        if not isinstance(parsed, list) or len(parsed) != 1 or not isinstance(parsed[0], dict):
            return False
        warnings = []
        for key, value in parsed[0].items():
            if key == 'SourceFile':
                continue
            if key.rsplit(':', 1)[-1] != 'Warning' or not isinstance(value, str):
                return False
            warnings.append(value)
        return sorted(warnings) == sorted(expected_values)
    except Exception:
        return False


def _valid_decimal_from_exiftool(value, ref: str, minimum: float, maximum: float) -> str:
    try:
        parsed = float(value)
    except Exception:
        return ''
    if ref in {'S', 'W'} and parsed > 0:
        parsed *= -1
    if not math.isfinite(parsed) or parsed < minimum or parsed > maximum:
        return ''
    return str(round(parsed, 7))


_SAFE_EXT_RE = re.compile(r'[a-z0-9]{1,10}')


def _safe_temp_suffix(filename: str, default_ext: str) -> str:
    """Derive a ``NamedTemporaryFile`` suffix from a (possibly attacker-
    controlled) filename's extension, restricted to a plain alphanumeric
    charset so a crafted filename (e.g. containing ``/`` or ``..``) can't
    inject a path separator into the resulting temp-file path."""
    ext = filename.rsplit('.', 1)[-1].lower() if filename and '.' in filename else default_ext
    if not _SAFE_EXT_RE.fullmatch(ext or ''):
        ext = default_ext
    return f'.{ext}'


@contextmanager
def _pillow_metadata_read():
    # Python 3.12 warning hooks are global. Serialize our captures and attribute
    # warnings only to the parser thread, forwarding every warning as usual.
    with _PILLOW_METADATA_LOCK:
        parser_thread = threading.get_ident()
        metadata_warning = False
        with warnings.catch_warnings():
            warnings.simplefilter('always', UserWarning)
            previous_showwarning = warnings.showwarning

            def showwarning(message, category, filename, lineno, file=None, line=None):
                nonlocal metadata_warning
                if threading.get_ident() == parser_thread and issubclass(category, UserWarning):
                    metadata_warning = True
                previous_showwarning(message, category, filename, lineno, file=file, line=line)

            warnings.showwarning = showwarning
            try:
                yield
            except Exception:
                raise ExifExtractionError('read_failed') from None
            if metadata_warning:
                raise ExifExtractionError('read_failed')


def extract_exif_from_path(path: str) -> Dict[str, str]:
    """Pillow can recover metadata from a missing tool, but cannot prove absence."""
    tool_error = None
    try:
        with open(path, 'rb') as source:
            is_jpeg = source.read(2) == b'\xff\xd8'
    except OSError:
        is_jpeg = False
    try:
        exif = (_extract_exiftool_from_path(path, scan_jpeg=True) if is_jpeg
                else _extract_exiftool_from_path(path))
    except ExifExtractionError as exc:
        if exc.category != 'unavailable':
            raise
        tool_error = exc
        exif = None
    if exif:
        return exif
    try:
        with _pillow_metadata_read(), Image.open(path) as image:
            pillow_exif = _extract_exif_from_image(image)
    except ExifExtractionError:
        if tool_error is not None:
            raise tool_error from None
        raise ExifExtractionError('read_failed') from None
    if pillow_exif:
        return pillow_exif
    raise tool_error or ExifExtractionError('read_failed')


def extract_exif_from_bytes(image_bytes: bytes, filename: str = '') -> Dict[str, str]:
    """JPEG/PNG require complete container extraction, not early Pillow tags.

    This costs a temporary copy and subprocess for metadata-free originals. JPEG
    uses -ee to scan APP1 after SOS; neither Pillow nor ExifTool defaults prove
    absence from early JPEG headers. Packet extraction keeps its default command.
    """
    if not image_bytes:
        raise ExifExtractionError('read_failed')
    is_png = image_bytes.startswith(b'\x89PNG\r\n\x1a\n')
    is_jpeg = image_bytes.startswith(b'\xff\xd8')
    # PNG getexif() can decode IDAT to discover trailing EXIF. ExifTool walks
    # the complete container without decoding pixels or holding the Pillow lock.
    if not is_png and not is_jpeg:
        try:
            with _pillow_metadata_read(), Image.open(io.BytesIO(image_bytes)) as image:
                pillow_exif = _extract_exif_from_image(image)
            if pillow_exif:
                return pillow_exif
        except ExifExtractionError:
            pass
    suffix = _safe_temp_suffix(filename, 'raw')
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix) as temp_file:
            temp_file.write(image_bytes)
            temp_file.flush()
            if image_bytes.startswith(b'\xff\xd8'):
                return _extract_exiftool_from_path(temp_file.name, scan_jpeg=True)
            return _extract_exiftool_from_path(temp_file.name)
    except ExifExtractionError:
        raise
    except OSError:
        raise ExifExtractionError('read_failed') from None


def extract_exif_from_image(image: Image.Image) -> Dict[str, str]:
    with _pillow_metadata_read():
        return _extract_exif_from_image(image)


def _read_referenced_exif_ifd(raw_exif, tag_id):
    if tag_id not in raw_exif:
        return {}
    ifd = raw_exif.get_ifd(tag_id)
    if not ifd:
        raise ExifExtractionError('read_failed')
    return ifd


def _extract_exif_from_image(image: Image.Image) -> Dict[str, str]:
    if image.format == 'PNG':
        # A partially read PNG cannot establish metadata absence; callers need
        # the original bytes/path so ExifTool can also inspect chunks after IDAT.
        raise ExifExtractionError('read_failed')
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

    # IFD0 (the loop above) only carries a handful of tags (Make, Model,
    # Orientation, the top-level "DateTime" modify-date, ...). The bulk of
    # capture metadata — DateTimeOriginal, DateTimeDigitized, ExposureTime,
    # FNumber, ISO, LensModel, etc. — lives in the separate Exif SubIFD that
    # tag 0x8769 ("ExifOffset") points to, and getexif() does not merge it in
    # automatically. Without this, DateTimeOriginal was silently absent for
    # every photo, so capture-date sort fell back to upload date even when
    # the file had a real EXIF-original date.
    exif_sub_ifd = _read_referenced_exif_ifd(raw_exif, 0x8769)
    if exif_sub_ifd:
        for tag_id, value in exif_sub_ifd.items():
            tag_name = ExifTags.TAGS.get(tag_id, str(tag_id))
            if tag_name not in exif:
                exif[tag_name] = _stringify_exif_value(value)

    if gps_lat is None or gps_lon is None:
        gps_ifd = _read_referenced_exif_ifd(raw_exif, 0x8825)

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
