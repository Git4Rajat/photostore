"""Dataset-driven tests for deterministic gallery ordering.

These reproduce the "gallery shows a different shuffle every load" bug and lock
in the fix. They exercise ``ordering_utils`` directly (standard-library only),
so they run without the Flask / Azure / vision stack installed.

The dataset below deliberately mixes the states a real library contains:
  * photos with a persisted uploadDate,
  * photos with only upload_started_at (the /upload/init state, before the
    finalize fix persisted uploadDate),
  * photos with EXIF capture dates and photos without,
  * photos with no date information at all,
  * ties (same timestamp) that must resolve deterministically.
"""
import json

import ordering_utils


def _exif(capture: str) -> str:
    """Serialize an EXIF map the way metadata rows store it (a JSON string)."""
    return json.dumps({'DateTimeOriginal': capture})


# A photo library snapshot. RowKey (filename) order below is intentionally NOT
# the display order for any sort, so a passing test proves ordering comes from
# the sort keys, not from however the rows happen to be listed.
DATASET = {
    # Recent upload, older capture. uploadDate present.
    'p_a.jpg': {'uploadDate': '2026-01-05T09:00:00+00:00', 'exifData': _exif('2020:06:01 12:00:00')},
    # Most recent upload. uploadDate present.
    'p_b.jpg': {'uploadDate': '2026-03-10T09:00:00+00:00', 'exifData': _exif('2019:01:01 08:00:00')},
    # No uploadDate yet (pre-finalize row) but a stable upload_started_at; no EXIF.
    'p_c.jpg': {'upload_started_at': '2026-02-01T09:00:00+00:00'},
    # No date information at all.
    'p_d.jpg': {},
    # Ties p_b on upload date; no EXIF (capture falls back to upload date).
    'p_e.jpg': {'uploadDate': '2026-03-10T09:00:00+00:00'},
}

ENTRIES = list(DATASET.keys())


def _order(sort, entries=None, data=None):
    return ordering_utils.order_photo_entries(entries or ENTRIES, data or DATASET, sort)


def test_recent_upload_first():
    # 'date' sort = most recently uploaded first. Ties (p_b/p_e) break by
    # filename ascending; the dateless photo (p_d) sinks to the bottom.
    assert _order('date') == ['p_b.jpg', 'p_e.jpg', 'p_c.jpg', 'p_a.jpg', 'p_d.jpg']


def test_capture_chronology_with_upload_fallback():
    # 'capture' sort = most recently captured first. Photos without EXIF fall
    # back to their upload date (p_c via upload_started_at, p_e via uploadDate),
    # so they still slot into the chronology instead of disappearing.
    assert _order('capture') == ['p_e.jpg', 'p_c.jpg', 'p_a.jpg', 'p_b.jpg', 'p_d.jpg']


def test_order_is_independent_of_input_row_order():
    # The table backend gives no ordering guarantee across loads; the output must
    # not depend on the order rows arrive in.
    for rotation in range(len(ENTRIES)):
        shuffled = ENTRIES[rotation:] + ENTRIES[:rotation]
        assert _order('date', entries=shuffled) == _order('date')
        assert _order('capture', entries=shuffled) == _order('capture')


def test_processing_updates_do_not_reshuffle_gallery():
    # Regression for the root bug: the upload-date sort key must ignore
    # last_processing_update, which is rewritten on every background-processing
    # step. Stamping every row with a churning value must not change the order.
    baseline = _order('date')
    churned = {
        name: {**row, 'last_processing_update': f'2026-07-26T10:{idx:02d}:00+00:00'}
        for idx, (name, row) in enumerate(DATASET.items())
    }
    assert ordering_utils.order_photo_entries(ENTRIES, churned, 'date') == baseline


def test_upload_date_preferred_over_upload_started_at():
    row = {
        'uploadDate': '2026-05-01T00:00:00+00:00',
        'upload_started_at': '2020-01-01T00:00:00+00:00',
    }
    assert ordering_utils.metadata_upload_datetime(row).year == 2026


def test_unknown_sort_falls_back_to_recent_upload():
    assert _order('banana') == _order('date')


def test_repeated_calls_are_stable():
    first = _order('date')
    for _ in range(5):
        assert _order('date') == first
