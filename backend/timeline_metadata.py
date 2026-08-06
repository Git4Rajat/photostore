"""Aggregation of photo metadata into a compact year/month/day timeline summary.

Kept dependency-light (standard library only) so it can be unit tested in
isolation, without pulling in the Flask / Azure stack — mirrors
``ordering_utils.py``. ``app.py`` imports and reuses this to serve
``/photos/timeline``.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Dict, List, Optional

from ordering_utils import metadata_capture_datetime


def build_timeline_summary(metadata_rows: List[Dict], *, now: Optional[datetime] = None) -> Dict:
    """Bucket photo metadata rows into a year/month/day count summary.

    Each row's date is resolved via ``metadata_capture_datetime`` (EXIF capture
    date, falling back to upload date) — the same function the gallery's
    default sort already uses, so timeline counts always agree with what the
    gallery shows. Rows with neither are "undated": excluded from the buckets
    but counted separately so the UI can surface them (they remain searchable,
    just not navigable via the timeline).

    Dates after ``now`` (default: real current time) are clamped into today's
    bucket — a bad camera clock must not extend the timeline into a fantasy
    future year. ``now`` is injectable so tests can freeze "today".
    """
    current = now or datetime.now(timezone.utc)
    today = current.date()

    years: Dict[str, Dict] = {}
    undated_count = 0
    min_date: Optional[date] = None
    max_date: Optional[date] = None

    for row in metadata_rows:
        captured = metadata_capture_datetime(row)
        if captured is None:
            undated_count += 1
            continue

        day = captured.date()
        if day > today:
            day = today

        year_key = f'{day.year:04d}'
        month_key = f'{day.month:02d}'
        day_key = f'{day.day:02d}'

        year_bucket = years.setdefault(year_key, {'count': 0, 'months': {}})
        month_bucket = year_bucket['months'].setdefault(month_key, {'count': 0, 'days': {}})

        year_bucket['count'] += 1
        month_bucket['count'] += 1
        month_bucket['days'][day_key] = month_bucket['days'].get(day_key, 0) + 1

        if min_date is None or day < min_date:
            min_date = day
        if max_date is None or day > max_date:
            max_date = day

    cumulative_by_year: Dict[str, int] = {}
    running = 0
    for year_key in sorted(years.keys()):
        running += years[year_key]['count']
        cumulative_by_year[year_key] = running

    return {
        'years': years,
        'cumulativeByYear': cumulative_by_year,
        'firstDate': min_date.isoformat() if min_date else None,
        'lastDate': max_date.isoformat() if max_date else None,
        'today': today.isoformat(),
        'undatedCount': undated_count,
        'totalCount': len(metadata_rows),
    }
