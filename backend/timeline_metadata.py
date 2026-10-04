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


class TimelineAccumulator:
    """Streaming form of build_timeline_summary: feed rows one at a time with
    add() and call summary() at the end -- constant memory (a few counters per
    day), so the index build never has to hold the library in a list."""

    def __init__(self, *, now: Optional[datetime] = None) -> None:
        self._today = (now or datetime.now(timezone.utc)).date()
        self._years: Dict[str, Dict] = {}
        self._undated = 0
        self._future = 0
        self._total = 0
        self._min: Optional[date] = None
        self._max: Optional[date] = None

    def add(self, row: Dict) -> None:
        self._total += 1
        captured = metadata_capture_datetime(row)
        if captured is None:
            self._undated += 1
            return
        day = captured.date()
        if day > self._today:
            self._future += 1
            return
        year_bucket = self._years.setdefault(f'{day.year:04d}', {'count': 0, 'months': {}})
        month_bucket = year_bucket['months'].setdefault(f'{day.month:02d}', {'count': 0, 'days': {}})
        year_bucket['count'] += 1
        month_bucket['count'] += 1
        day_key = f'{day.day:02d}'
        month_bucket['days'][day_key] = month_bucket['days'].get(day_key, 0) + 1
        if self._min is None or day < self._min:
            self._min = day
        if self._max is None or day > self._max:
            self._max = day

    def summary(self) -> Dict:
        cumulative_by_year: Dict[str, int] = {}
        running = 0
        for year_key in sorted(self._years.keys()):
            running += self._years[year_key]['count']
            cumulative_by_year[year_key] = running
        return {
            'years': self._years,
            'cumulativeByYear': cumulative_by_year,
            'firstDate': self._min.isoformat() if self._min else None,
            'lastDate': self._max.isoformat() if self._max else None,
            'today': self._today.isoformat(),
            'undatedCount': self._undated,
            'futureCount': self._future,
            'totalCount': self._total,
        }


def build_timeline_summary(metadata_rows, *, now: Optional[datetime] = None) -> Dict:
    """Bucket photo metadata rows into a year/month/day count summary.

    Each row's date is resolved via ``metadata_capture_datetime`` (EXIF capture
    date, falling back to upload date) — the same function the gallery's
    default sort already uses, so timeline counts always agree with what the
    gallery shows. Rows with neither are "undated": excluded from the buckets
    but counted separately so the UI can surface them (they remain searchable,
    just not navigable via the timeline).

    Dates after ``now`` (default: real current time) are a bad camera clock,
    not a real future photo — they're dropped entirely (counted in
    ``futureCount``, not folded into today's bucket) so they can't inflate
    today's count or otherwise distort the timeline. ``now`` is injectable so
    tests can freeze "today". Accepts any iterable (list or stream).
    """
    accumulator = TimelineAccumulator(now=now)
    for row in metadata_rows:
        accumulator.add(row)
    return accumulator.summary()
