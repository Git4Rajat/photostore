"""Deterministic ordering + date helpers for the photo gallery.

Kept dependency-light (standard library only) so the gallery-ordering logic can
be unit tested in isolation, without pulling in the Flask / Azure / vision
stack. ``app.py`` imports and re-uses these helpers so production and tests
exercise exactly the same code.

The gallery bug this module fixes: photo order used to churn between loads
because the "upload date" sort key preferred ``last_processing_update`` (a
field rewritten on every background-processing step) whenever a row had no
persisted ``uploadDate``, and ties had no deterministic ordering. These helpers
sort on stable fields first (``last_processing_update`` remains only as a
last-resort fallback for legacy rows that predate persisted upload dates) and
always break ties by filename.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

# Sentinel used as the sort value for photos whose date is unknown. Kept
# timezone-aware so it never raises when compared against real, aware datetimes.
DATE_MIN = datetime.min.replace(tzinfo=timezone.utc)

# A capture time read off a clock whose zone was never recorded (EXIF with no
# OffsetTime*, a date in the filename). It compares and sorts as UTC, but it is
# the wall-clock time where the photo was taken, so it must be shown as is,
# never converted into the viewer's zone. ``capture_iso`` emits it without an
# offset for that reason. Identify it with ``tzinfo is FLOATING``.
FLOATING = timezone(timedelta(0), 'floating')


def parse_iso_date(value: str) -> Optional[datetime]:
    """Parse an ISO-8601 string into a timezone-aware datetime, or None."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return None


def epoch_millis_to_iso(value: object) -> Optional[str]:
    """Convert a client-supplied epoch-millisecond timestamp (JS ``File.lastModified``)
    into an ISO string for storage, or None if missing/invalid."""
    try:
        millis = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if millis <= 0:
        return None
    try:
        return datetime.fromtimestamp(millis / 1000, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _exif_dict(metadata: Dict) -> Dict[str, str]:
    """Return the parsed EXIF map for a metadata row.

    ``exifData`` is stored as a JSON string but may already be a dict in tests.
    """
    raw = metadata.get('exifData', '{}')
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    try:
        parsed = json.loads(raw or '{}')
        if isinstance(parsed, dict):
            return {str(k): str(v) for k, v in parsed.items()}
    except Exception:
        pass
    return {}


def parse_capture_date(exif_data: Dict[str, str]) -> Optional[datetime]:
    """Extract the capture time from EXIF fields, or None when absent."""
    if not exif_data:
        return None
    # QuickTime's "CreationDate" (com.apple.quicktime.creationdate) is
    # checked ahead of the container-level CreateDate/MediaCreateDate/
    # TrackCreateDate atoms: those get silently rewritten to the
    # export/transfer time whenever a video is re-saved, shared, or
    # re-encoded (AirDrop, WhatsApp, "Save Video", etc.), while
    # CreationDate is the local-time-with-offset field iOS writes and
    # is what Photos.app itself displays -- it survives that far more
    # reliably.
    for key in (
        'DateTimeOriginal', 'DateTime', 'CreationDate',
        'CreateDate', 'MediaCreateDate', 'TrackCreateDate',
    ):
        parsed = _parse_capture_value(exif_data.get(key))
        if parsed is None:
            continue
        if parsed.tzinfo is None:
            offset = next((o for o in map(_parse_offset, (exif_data.get(k) for k in _EXIF_OFFSET_KEYS.get(key, ())))
                           if o is not None), None)
            parsed = parsed.replace(tzinfo=offset or (FLOATING if key in _FLOATING_KEYS else timezone.utc))
        return parsed
    return None


# EXIF writes DateTimeOriginal/DateTime/CreateDate as the camera's local time
# with no zone; since EXIF 2.31 the zone sits in a separate OffsetTime* tag
# (iPhones always write it). Reading the time as UTC without it put every such
# photo hours away from the videos shot beside it (theirs carry an offset) and
# pushed late-evening shots onto the next day.
_EXIF_OFFSET_KEYS = {
    'DateTimeOriginal': ('OffsetTimeOriginal', 'OffsetTime', 'OffsetTimeDigitized'),
    'DateTime': ('OffsetTime', 'OffsetTimeOriginal', 'OffsetTimeDigitized'),
    'CreateDate': ('OffsetTimeDigitized', 'OffsetTimeOriginal', 'OffsetTime'),
}
# EXIF local times whose zone is unknown without that tag. Naive container
# atoms (CreateDate/MediaCreateDate/... from exiftool on a video) are UTC by the
# QuickTime spec, so they stay UTC.
_FLOATING_KEYS = ('DateTimeOriginal', 'DateTime')
_OFFSET = re.compile(r'^([+-])(\d{2}):?(\d{2})$')


def _parse_offset(value: object) -> Optional[timezone]:
    match = _OFFSET.match(str(value or '').strip())
    if not match:
        return None
    hours, minutes = int(match[2]), int(match[3])
    if hours > 14 or minutes > 59:
        return None
    delta = timedelta(hours=hours, minutes=minutes)
    return timezone(-delta if match[1] == '-' else delta)


# Dates that apps and cameras put in the filename when the file itself carries
# no capture date: Android/Pixel/DJI "IMG_20240105_101112", "PXL_20231231_235959123",
# "dji_fly_20220910_160714_5_...", macOS "Screenshot 2024-01-05 at 10.11.12",
# WhatsApp exports "PHOTO-2024-01-05-10-11-12" and "IMG-20231112-WA0001" (date only).
_FILENAME_DATES = (
    re.compile(r'(?<![0-9])((?:19|20)[0-9]{2})([0-9]{2})([0-9]{2})[_-]([0-9]{2})([0-9]{2})([0-9]{2})(?:[0-9]{1,3})?(?![0-9])'),
    re.compile(r'(?<![0-9])((?:19|20)[0-9]{2})-([0-9]{2})-([0-9]{2})(?:[ _T-]|\s+at\s+)([0-9]{2})[.:-]([0-9]{2})[.:-]([0-9]{2})(?![0-9])'),
    re.compile(r'(?<![0-9])((?:19|20)[0-9]{2})([0-9]{2})([0-9]{2})-WA[0-9]+', re.IGNORECASE),
)
_MIN_FILENAME_YEAR = 1990


def parse_filename_capture_date(filename: str, *, now: Optional[datetime] = None) -> Optional[datetime]:
    """The capture time an app or camera wrote into the filename, as wall-clock time (FLOATING), or None."""
    name = str(filename or '')
    latest = (now or datetime.now(timezone.utc)).year + 1
    for pattern in _FILENAME_DATES:
        match = pattern.search(name)
        if not match:
            continue
        parts = [int(p) for p in match.groups()] + [0, 0, 0]
        try:
            parsed = datetime(*parts[:6], tzinfo=FLOATING)
        except ValueError:
            continue
        if _MIN_FILENAME_YEAR <= parsed.year <= latest:
            return parsed
    return None


def capture_iso(value: Optional[datetime]) -> Optional[str]:
    """ISO string for a capture time: with its offset when the zone is known, without one when it is FLOATING."""
    if value is None:
        return None
    if value.tzinfo is FLOATING:
        return value.replace(tzinfo=None).isoformat()
    return value.isoformat()


# Container atoms with no clock set store seconds since the 1904 QuickTime
# epoch, which mediainfo reports as e.g. "1904-01-01 00:33:24 UTC". Anything
# before this is a placeholder, not a capture date.
_MIN_PLAUSIBLE_CAPTURE_YEAR = 1970


def _parse_capture_value(value: object) -> Optional[datetime]:
    raw = str(value or '').strip()
    if not raw:
        return None

    # exiftool variants can include timezone offsets and/or fractional seconds,
    # e.g. "2024:01:02 10:00:00+05:30" or
    # "2024:01:02 10:00:00.123456+00:00". The browser's mediainfo video reader
    # emits dash dates with a trailing "UTC", e.g. "2025-01-27 22:37:42 UTC".
    if raw.endswith(' UTC'):
        raw = f"{raw[:-4]}+00:00"
    elif raw.endswith('Z'):
        raw = f"{raw[:-1]}+00:00"

    for fmt in (
        '%Y:%m:%d %H:%M:%S',
        '%Y:%m:%d %H:%M:%S%z',
        '%Y:%m:%d %H:%M:%S.%f',
        '%Y:%m:%d %H:%M:%S.%f%z',
        '%Y-%m-%d %H:%M:%S',
        '%Y-%m-%d %H:%M:%S%z',
        '%Y-%m-%d %H:%M:%S.%f',
        '%Y-%m-%d %H:%M:%S.%f%z',
    ):
        try:
            parsed = datetime.strptime(raw, fmt)
        except Exception:
            continue
        if parsed.year < _MIN_PLAUSIBLE_CAPTURE_YEAR:
            return None
        # Naive stays naive: parse_capture_date decides its zone from the key and the offset tags.
        return parsed
    return None


def metadata_upload_datetime(metadata: Dict) -> Optional[datetime]:
    """Return the upload time for a photo, or None if truly unknown.

    Prefers the stable fields: the persisted ``uploadDate``, then
    ``upload_started_at`` (written once at upload init). Only when neither
    exists — legacy rows uploaded before finalize started persisting
    ``uploadDate`` — does it fall back to ``last_processing_update``. That field
    is rewritten on every processing step, so it must never outrank the stable
    fields (that caused the gallery to reshuffle while processing ran), but for
    legacy rows whose processing finished long ago it is static and roughly
    matches the upload time; without it they would all collapse to DATE_MIN and
    degrade into filename order (which reads as oldest-to-newest).
    """
    return parse_iso_date(
        str(
            metadata.get('uploadDate')
            or metadata.get('upload_started_at')
            or metadata.get('last_processing_update')
            or ''
        )
    )


def metadata_client_last_modified_datetime(metadata: Dict) -> Optional[datetime]:
    """Return the uploading device's own file-modified time, if the client sent one.

    Populated at finalize from the browser File API's ``lastModified`` (see
    ``epoch_millis_to_iso``). For a photo with no EXIF -- e.g. downloaded from
    the web, where the capture date was never embedded in the first place --
    this is a best-effort proxy for "when this photo was added to the user's
    library" on the device it was uploaded from, which reads as more
    meaningful gallery position than the upload instant. Reliability varies by
    OS/export path (some desktop download flows just stamp it with download
    time), so it is a fallback tier, not a substitute for real EXIF.
    """
    return parse_iso_date(str(metadata.get('clientLastModified') or ''))


def metadata_capture_datetime(metadata: Dict) -> Optional[datetime]:
    """Return the capture time, falling back to the upload time.

    Implements the product rule "if a photo has no capture date, use its upload
    date as the capture date", with two intermediate fallbacks: a date the
    camera or app wrote into the filename (DJI, Android, WhatsApp and screenshot
    exports carry no EXIF date but name the file after the moment), then the
    client's own file-modified time (see
    ``metadata_client_last_modified_datetime``), which for a download or an
    AirDrop is the transfer time, not the capture. Returns None only when none
    is known.
    """
    return (
        parse_capture_date(_exif_dict(metadata))
        or parse_filename_capture_date(str(metadata.get('RowKey') or ''))
        or metadata_client_last_modified_datetime(metadata)
        or metadata_upload_datetime(metadata)
    )


def order_photo_entries(
    entries: List[str],
    metadata_map: Dict[str, Dict],
    sort: str,
) -> List[str]:
    """Order photo filenames for gallery display, deterministically.

    The primary key is the requested ``sort``; ties always break by filename
    ascending. Because the tie-break is explicit, repeated loads return an
    identical sequence regardless of the order the table backend happened to
    return rows in — which is what stops the gallery from showing a different
    shuffle every time it loads.

    Supported sorts: ``date`` (default, most recently uploaded first),
    ``capture`` (most recently captured first, upload date as fallback),
    ``rating``, ``likes``, ``location``.
    """
    # Stable base: filename ascending. Python's sort is stable, so this becomes
    # the deterministic tie-break for every primary sort applied below.
    ordered = sorted(entries)

    def meta(name: str) -> Dict:
        return metadata_map.get(name, {}) or {}

    def upload_key(name: str) -> datetime:
        return metadata_upload_datetime(meta(name)) or DATE_MIN

    def capture_key(name: str) -> datetime:
        return metadata_capture_datetime(meta(name)) or DATE_MIN

    def numeric_key(name: str, field: str) -> float:
        try:
            return float(meta(name).get(field, 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    if sort == 'location':
        ordered.sort(key=lambda name: name.lower())
    elif sort == 'rating':
        # Matches search_db.SearchDatabase._LIST_ORDER['rating']: rating DESC,
        # likes DESC, capture_ts DESC, filename ASC.
        ordered.sort(key=lambda name: (numeric_key(name, 'rating'), numeric_key(name, 'likes'), capture_key(name)),
                     reverse=True)
    elif sort == 'likes':
        # Matches _LIST_ORDER['likes']: likes DESC, rating DESC, capture_ts DESC, filename ASC.
        ordered.sort(key=lambda name: (numeric_key(name, 'likes'), numeric_key(name, 'rating'), capture_key(name)),
                     reverse=True)
    elif sort == 'capture':
        ordered.sort(key=capture_key, reverse=True)
    else:  # 'date' and any unknown value: most recently uploaded first
        ordered.sort(key=upload_key, reverse=True)

    return ordered
