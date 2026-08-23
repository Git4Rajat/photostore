"""Unit tests for _cleanup_failed_upload's completed-vs-abandoned heuristic.

kickOffThumbnailForFile fires an early, thumbnail-only processing claim
concurrently with the raw upload itself (before finalize), so a row can have
thumbnail_status='running' even though the upload was then abandoned before
finalize_uploaded_file ever ran. has_completed_metadata used to treat
thumbnail_status as proof the upload completed, which routed these genuinely
abandoned rows into the "tracking cleared, row kept" branch instead of being
deleted -- leaving a permanently broken (blank thumbnail, never finalized)
photo tile in the gallery forever, and blocking a clean re-upload of the same
filename from starting fresh.
"""
from __future__ import annotations

import pytest

import app
from fakes import FakeTable


@pytest.fixture(autouse=True)
def metadata_table(monkeypatch):
    table = FakeTable()
    monkeypatch.setattr(app, 'metadata_table_client', table)
    # Not under test here -- avoid touching the real filesystem/blob storage.
    monkeypatch.setattr(app, '_delete_upload_temp_files_for_filename', lambda *a, **k: ([], []))
    monkeypatch.setattr(app, '_delete_photo_blobs_if_present', lambda *a, **k: [])
    monkeypatch.setattr(app, 'delete_image_name_mapping', lambda *a, **k: None)
    monkeypatch.setattr(app, 'original_filename_for_anonymous_id', lambda *a, **k: None)
    return table


def _seed_row(table, user_id, filename, **overrides):
    row = {'PartitionKey': user_id, 'RowKey': filename, **overrides}
    table.upsert_entity(row)


def test_never_finalized_upload_is_deleted_not_just_tracking_cleared(metadata_table):
    """The core bug: a row claimed for thumbnail-only processing before the
    raw upload was abandoned (no anonymousImageId, no fileHash -- finalize
    never ran) must be fully deleted so a fresh re-upload starts clean."""
    _seed_row(
        metadata_table, 'lib-A', 'IMG_9001.jpeg',
        thumbnail_status='running',
        processing_lease_owner='browser-abc',
        processing_lease_expires_at='2026-08-19T21:00:00+00:00',
        pendingAnonymousBlob='pending-uuid-123',
    )

    result = app._cleanup_failed_upload('lib-A', 'IMG_9001.jpeg')

    assert result['metadataAction'] == 'deleted'
    with pytest.raises(Exception):
        metadata_table.get_entity('lib-A', 'IMG_9001.jpeg')


def test_finalized_upload_only_clears_tracking_fields(metadata_table):
    """A row that genuinely finished uploading (anonymousImageId stamped at
    finalize) but then stalled during processing must NOT be deleted on a
    fresh re-upload attempt -- only the transient upload-tracking fields."""
    _seed_row(
        metadata_table, 'lib-A', 'IMG_9434.jpeg',
        thumbnail_status='running',
        exif_status='running',
        anonymousImageId='real-anon-uuid',
        fileHash='abc123',
        mimeType='image/jpeg',
        pendingAnonymousBlob='stale-should-be-cleared',
    )

    result = app._cleanup_failed_upload('lib-A', 'IMG_9434.jpeg')

    assert result['metadataAction'] == 'trackingCleared'
    stored = metadata_table.get_entity('lib-A', 'IMG_9434.jpeg')
    assert 'pendingAnonymousBlob' not in stored
    assert stored['anonymousImageId'] == 'real-anon-uuid'
    assert stored['thumbnail_status'] == 'running'


def test_row_with_no_upload_tracking_is_left_alone(metadata_table):
    """A fully processed, unrelated photo with no in-flight upload state at
    all shouldn't be touched just because a fresh /upload/init for the same
    filename happens to run cleanup."""
    _seed_row(
        metadata_table, 'lib-A', 'photo.jpg',
        thumbnail_status='done',
        anonymousImageId='real-anon-uuid',
    )

    result = app._cleanup_failed_upload('lib-A', 'photo.jpg')

    assert result['metadataAction'] == 'none'
    assert metadata_table.get_entity('lib-A', 'photo.jpg')['thumbnail_status'] == 'done'


def test_stale_pre_rename_row_deletes_row_but_preserves_shared_blob(metadata_table, monkeypatch):
    """A cross-tenant filename clash at finalize (_resolve_filename_for_upload)
    renames the upload and only updates the row under the NEW name -- this row,
    still keyed by the original filename, is left behind with the same
    pendingAnonymousBlob UUID that's now live under the renamed row's
    anonymousImageId. It must NOT delete that shared blob (it would break the
    other, successfully finalized row) even though it looks identical to a
    genuinely abandoned upload; the stale row itself should still go."""
    _seed_row(
        metadata_table, 'lib-A', 'IMG_0051.heic',
        pendingAnonymousBlob='shared-uuid-789',
    )
    delete_calls = []
    monkeypatch.setattr(app, '_delete_photo_blobs_if_present', lambda *a, **k: delete_calls.append(a) or [])
    monkeypatch.setattr(
        app, 'original_filename_for_anonymous_id',
        lambda user_id, anon_id: 'IMG_0051-ed20808c.heic' if anon_id == 'shared-uuid-789' else None,
    )

    result = app._cleanup_failed_upload('lib-A', 'IMG_0051.heic')

    assert delete_calls == []
    assert result['metadataAction'] == 'deleted'
    with pytest.raises(Exception):
        metadata_table.get_entity('lib-A', 'IMG_0051.heic')
