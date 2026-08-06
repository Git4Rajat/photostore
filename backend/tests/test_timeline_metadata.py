"""Dataset-driven tests for the timeline metadata aggregation.

Mirrors test_photo_ordering.py's style: exercises timeline_metadata directly
(standard-library only), so it runs without the Flask / Azure / vision stack.
"""
import json
from datetime import datetime, timezone

import timeline_metadata


def _exif(capture: str) -> str:
    return json.dumps({'DateTimeOriginal': capture})


def test_basic_day_month_year_bucketing_and_cumulative():
    dataset = [
        {'exifData': _exif('2024:03:15 10:00:00')},
        {'exifData': _exif('2024:03:15 11:30:00')},
        {'exifData': _exif('2024:03:20 09:00:00')},
        {'exifData': _exif('2024:07:01 08:00:00')},
        {'exifData': _exif('2023:12:25 12:00:00')},
    ]
    summary = timeline_metadata.build_timeline_summary(dataset)

    assert summary['years']['2024']['count'] == 4
    assert summary['years']['2024']['months']['03']['count'] == 3
    assert summary['years']['2024']['months']['03']['days']['15'] == 2
    assert summary['years']['2024']['months']['03']['days']['20'] == 1
    assert summary['years']['2024']['months']['07']['count'] == 1
    assert summary['years']['2023']['count'] == 1

    # Cumulative is oldest-year-first, running total.
    assert summary['cumulativeByYear'] == {'2023': 1, '2024': 5}
    assert summary['totalCount'] == 5
    assert summary['firstDate'] == '2023-12-25'
    assert summary['lastDate'] == '2024-07-01'
    assert summary['undatedCount'] == 0


def test_exif_absent_falls_back_to_upload_date():
    # No EXIF at all; must still bucket via uploadDate, matching the gallery's
    # default sort (ordering_utils.metadata_capture_datetime) and the
    # _capture_in_range fix that keeps /photos filtering consistent with this.
    dataset = [{'uploadDate': '2026-03-10T09:00:00+00:00', 'exifData': '{}'}]
    summary = timeline_metadata.build_timeline_summary(dataset)
    assert summary['years']['2026']['months']['03']['days']['10'] == 1
    assert summary['undatedCount'] == 0


def test_fully_undated_row_excluded_from_buckets_but_counted():
    dataset = [
        {'exifData': _exif('2024:01:01 00:00:00')},
        {},  # no exifData, no uploadDate, no upload_started_at, no last_processing_update
    ]
    summary = timeline_metadata.build_timeline_summary(dataset)
    assert summary['undatedCount'] == 1
    assert summary['totalCount'] == 2
    assert sum(y['count'] for y in summary['years'].values()) == 1


def test_future_dated_exif_clamps_into_today():
    frozen_now = datetime(2026, 8, 6, 12, 0, 0, tzinfo=timezone.utc)
    dataset = [{'exifData': _exif('2031:01:01 00:00:00')}]
    summary = timeline_metadata.build_timeline_summary(dataset, now=frozen_now)
    assert summary['lastDate'] == '2026-08-06'
    assert summary['years']['2026']['months']['08']['days']['06'] == 1
    assert '2031' not in summary['years']
    assert summary['today'] == '2026-08-06'


def test_multi_year_first_and_last_date():
    dataset = [
        {'exifData': _exif('2018:04:02 00:00:00')},
        {'exifData': _exif('2022:11:11 00:00:00')},
        {'exifData': _exif('2026:01:01 00:00:00')},
    ]
    summary = timeline_metadata.build_timeline_summary(dataset)
    assert summary['firstDate'] == '2018-04-02'
    assert summary['lastDate'] == '2026-01-01'


def test_empty_input_returns_empty_summary_without_error():
    summary = timeline_metadata.build_timeline_summary([])
    assert summary['years'] == {}
    assert summary['cumulativeByYear'] == {}
    assert summary['firstDate'] is None
    assert summary['lastDate'] is None
    assert summary['undatedCount'] == 0
    assert summary['totalCount'] == 0
