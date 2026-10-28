"""Unfinished upload reservations must not become gallery or search photos."""
import pytest

from catalog_metadata import CATALOG_ADMISSION_FIELDS, is_upload_reservation_only
import search_db
import storage_utils as su


@pytest.mark.parametrize('tracking', [
    {'pendingAnonymousBlob': 'reserved-uuid'},
    {'pendingAnonymousBlob': 'reserved-uuid', 'upload_started_at': '2026-10-27T19:21:30+00:00'},
])
def test_early_processing_claim_does_not_admit_an_unfinalized_upload(tracking):
    row = {'RowKey': '050697.png', **tracking, 'thumbnail_status': 'done',
           'exifData': '{"DateTimeOriginal":"2020:01:01 12:00:00"}',
           'processing_complete': True}
    assert is_upload_reservation_only(row)
    assert su._sort_index_row(row) is None
    assert su._access_index_row(row) is None
    assert search_db.row_record(row) is None


@pytest.mark.parametrize('finalized', [
    {},  # Legacy photo with no upload tracking.
    {'upload_started_at': '2020-01-02T00:00:00+00:00'},  # Legacy date fallback, no reservation.
    {'uploadDate': '2026-10-27T19:21:30+00:00'},
    {'anonymousImageId': 'permanent-uuid'},
    {'fileHash': 'hash'},
    {'perceptualHash': 'hash'},
    {'mimeType': 'image/png'},
    {'verification_status': 'failed'},
])
def test_legacy_and_finalized_photos_remain_visible_before_derivatives_finish(finalized):
    row = {'RowKey': 'photo.png', 'thumbnail_status': 'failed', 'preview_status': 'pending',
           'processing_complete': False, **finalized}
    if finalized and 'upload_started_at' not in finalized:
        # A new upload can reuse a row belonging to a finalized photo.
        row['pendingAnonymousBlob'] = 'new-reservation'
    assert not is_upload_reservation_only(row)
    assert su._sort_index_row(row)['RowKey'] == 'photo.png'
    assert su._access_index_row(row)['RowKey'] == 'photo.png'
    assert search_db.row_record(row)['filename'] == 'photo.png'


def test_projected_catalog_reads_include_admission_evidence():
    for fields in (su._SORT_INDEX_SOURCE_FIELDS, su._ACCESS_INDEX_SOURCE_FIELDS, su._STREAM_SELECT_FIELDS):
        assert set(CATALOG_ADMISSION_FIELDS) <= set(fields)
