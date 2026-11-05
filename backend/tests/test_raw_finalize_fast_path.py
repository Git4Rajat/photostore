"""RAW/HEIC finalize no longer eagerly downloads + decodes the file.

That eager work (_upload_needs_server_bytes' old codepath) was redundant with
ipworker's queued 'thumbnail'/'exif' steps (see _queue_upload_processing),
which call the exact same helpers asynchronously -- and it held a gunicorn
thread for as long as the RAW decode took (measured multi-second to ~60s on
real CR3 files in production). Video preparation is now browser-only too.
"""
from __future__ import annotations

import re

import pytest

import storage_utils


class _ResourceNotFound(Exception):
    pass


class _FakeTable:
    def __init__(self) -> None:
        self.rows: dict = {}

    def upsert_entity(self, entity):
        self.rows[(entity['PartitionKey'], entity['RowKey'])] = dict(entity)

    def get_entity(self, partition_key, row_key):
        key = (partition_key, row_key)
        if key not in self.rows:
            raise _ResourceNotFound(f'{key} not found')
        return dict(self.rows[key])

    def delete_entity(self, partition_key, row_key):
        self.rows.pop((partition_key, row_key), None)

    def query_entities(self, filter_str, **kwargs):
        m = re.match(r"PartitionKey eq '(.*)'$", filter_str.strip())
        pk = m.group(1) if m else ''
        return [dict(v) for (p, _), v in self.rows.items() if p == pk]


@pytest.fixture
def finalize_ctx(monkeypatch):
    metadata = _FakeTable()
    monkeypatch.setitem(storage_utils._CTX, 'metadata_table_client', metadata)
    monkeypatch.setitem(storage_utils._CTX, 'hash_index_table_client', _FakeTable())
    monkeypatch.setitem(storage_utils._CTX, 'filename_owners_table_client', _FakeTable())
    monkeypatch.setitem(storage_utils._CTX, 'image_names_table_client', None)
    monkeypatch.setitem(storage_utils._CTX, 'blob_service_client', object())
    monkeypatch.setitem(storage_utils._CTX, 'blob_image_container', 'images')
    monkeypatch.setitem(storage_utils._CTX, 'face_table_client', None)

    def _boom_download(*args, **kwargs):
        raise AssertionError('download_media_bytes should not be called for RAW with a trusted client hash')

    def _boom_thumbnail(*args, **kwargs):
        raise AssertionError('_create_server_thumbnail_for_upload should not run eagerly for RAW anymore')

    def _boom_exif(*args, **kwargs):
        raise AssertionError('_finalize_server_side_exif should not run eagerly for RAW anymore')

    monkeypatch.setattr(storage_utils, 'download_media_bytes', _boom_download)
    monkeypatch.setattr(storage_utils, '_create_server_thumbnail_for_upload', _boom_thumbnail)
    monkeypatch.setattr(storage_utils, '_finalize_server_side_exif', _boom_exif)
    storage_utils.invalidate_image_names_cache()
    yield metadata
    storage_utils.invalidate_image_names_cache()


def test_raw_finalize_skips_eager_download_and_thumbnail(finalize_ctx):
    file_hash = 'a' * 64
    # Would raise via the monkeypatches above if the old eager RAW path ran.
    duplicates, final_name = storage_utils.finalize_uploaded_file(
        'lib-A', 'photo.cr3', 'image/x-canon-cr3', client_sha256=file_hash,
    )
    assert final_name == 'photo.cr3'
    assert duplicates == []
    stored = finalize_ctx.get_entity('lib-A', 'photo.cr3')
    assert stored['fileHash'] == file_hash
    # thumbnail_status should be left 'pending' -- for ipworker's queued job to
    # pick up -- not eagerly marked 'done' by a server-generated thumbnail.
    assert stored.get('thumbnail_status') == 'pending'


def test_video_finalize_has_no_eager_path_even_without_browser_assets(finalize_ctx):
    assert storage_utils.finalize_uploaded_file(
        'lib-A', 'clip.mp4', 'video/mp4', client_sha256='b' * 64,
    ) == ([], 'clip.mp4')
    stored = finalize_ctx.get_entity('lib-A', 'clip.mp4')
    assert stored['fileHash'] == 'b' * 64
    assert stored['thumbnail_status'] == 'failed' and stored['exif_status'] == 'failed'
    assert stored['processing_complete'] is True


def test_benchmark_finalize_retains_isolation_without_overwriting_dedup(finalize_ctx, monkeypatch):
    name = 'rawbench-run-12345678-originals-00000.cr3'
    finalize_ctx.upsert_entity({'PartitionKey': 'lib-A', 'RowKey': name,
                               'processingIsolated': True, 'benchmarkRunId': 'run-12345678'})
    monkeypatch.setattr(storage_utils, '_store_hash_index', lambda *args: pytest.fail('Benchmark replaced dedup index'))
    storage_utils.finalize_uploaded_file('lib-A', name, 'image/x-canon-cr3', client_sha256='a' * 64,
                                        anonymous_blob_name='blob-1')
    row = finalize_ctx.get_entity('lib-A', name)
    assert row['processingIsolated'] is True
    assert row['processing_complete'] is True
    assert all(row[f'{step}_status'] == 'skipped' for step in storage_utils.PROCESSING_STEPS)


def test_video_benchmark_retains_isolation_provenance(finalize_ctx):
    import json
    name = 'rawbench-run-12345678-originals-00000.mp4'
    finalize_ctx.upsert_entity({'PartitionKey': 'lib-A', 'RowKey': name,
                               'processingIsolated': True, 'benchmarkRunId': 'run-12345678'})
    storage_utils.finalize_uploaded_file('lib-A', name, 'video/mp4', client_sha256='a' * 64,
                                        anonymous_blob_name='blob-1')
    row = finalize_ctx.get_entity('lib-A', name)
    assert json.loads(row['processing_metadata'])['isolation']['reason'] == 'upload_only_benchmark'
    assert all(row[f'{step}_status'] == 'skipped' for step in storage_utils.PROCESSING_STEPS)


@pytest.mark.parametrize('new_hash,new_photo', [('b' * 64, 'old-photo'), ('a' * 64, 'new-photo')])
def test_replacement_finalize_clears_previous_source_derivative_receipts(finalize_ctx, new_hash, new_photo):
    from processing_generation import allocate_generation
    row = dict(PartitionKey='lib-A', RowKey='photo.cr3', fileHash='a' * 64,
               anonymousImageId='old-photo', thumbnailArtifact='old-thumbnail',
               previewArtifact='old-preview', artifactReceipt='old-manifest',
               processingCompletionOutbox='[]')
    row.update(allocate_generation('old-revision', photo_id='old-photo', file_hash='a' * 64, is_video=False))
    finalize_ctx.upsert_entity(row)
    storage_utils.finalize_uploaded_file('lib-A', 'photo.cr3', 'image/x-canon-cr3',
                                        client_sha256=new_hash, anonymous_blob_name=new_photo)
    stored = finalize_ctx.get_entity('lib-A', 'photo.cr3')
    assert stored['processingRevision'] != 'old-revision'
    assert stored['anonymousImageId'] == new_photo
    assert stored['thumbnailArtifact'] == stored['previewArtifact'] == stored['artifactReceipt'] == ''


def test_same_source_finalize_retry_preserves_derivative_receipts(finalize_ctx):
    from processing_generation import allocate_generation
    row = dict(PartitionKey='lib-A', RowKey='photo.cr3', fileHash='a' * 64,
               anonymousImageId='photo', thumbnailArtifact='existing-thumbnail',
               previewArtifact='existing-preview', artifactReceipt='existing-manifest')
    row.update(allocate_generation('revision', photo_id='photo', file_hash='a' * 64, is_video=False))
    finalize_ctx.upsert_entity(row)
    storage_utils.finalize_uploaded_file('lib-A', 'photo.cr3', 'image/x-canon-cr3',
                                        client_sha256='a' * 64, anonymous_blob_name='photo')
    stored = finalize_ctx.get_entity('lib-A', 'photo.cr3')
    assert stored['processingRevision'] == 'revision'
    for field in ('thumbnailArtifact', 'previewArtifact', 'artifactReceipt'):
        assert stored[field] == row[field]
