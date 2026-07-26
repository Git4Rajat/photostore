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
from datetime import datetime, timezone
from typing import Dict, List, Optional

# Sentinel used as the sort value for photos whose date is unknown. Kept
# timezone-aware so it never raises when compared against real, aware datetimes.
DATE_MIN = datetime.min.replace(tzinfo=timezone.utc)


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
    raw = (
        exif_data.get('DateTimeOriginal')
        or exif_data.get('DateTime')
        or exif_data.get('CreateDate')
        or exif_data.get('MediaCreateDate')
        or exif_data.get('TrackCreateDate')
        or ''
    )
    if not raw:
        return None
    # exiftool video dates may carry a timezone offset (e.g. 2024:01:02 10:00:00+05:30).
    for fmt in ('%Y:%m:%d %H:%M:%S', '%Y:%m:%d %H:%M:%S%z'):
        try:
            parsed = datetime.strptime(raw[:26], fmt)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except Exception:
            continue
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


def metadata_capture_datetime(metadata: Dict) -> Optional[datetime]:
    """Return the capture time, falling back to the upload time.

    Implements the product rule "if a photo has no capture date, use its upload
    date as the capture date". Returns None only when neither is known.
    """
    return parse_capture_date(_exif_dict(metadata)) or metadata_upload_datetime(metadata)


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
        ordered.sort(key=lambda name: numeric_key(name, 'rating'), reverse=True)
    elif sort == 'likes':
        ordered.sort(key=lambda name: numeric_key(name, 'likes'), reverse=True)
    elif sort == 'capture':
        ordered.sort(key=capture_key, reverse=True)
    else:  # 'date' and any unknown value: most recently uploaded first
        ordered.sort(key=upload_key, reverse=True)

    return ordered
