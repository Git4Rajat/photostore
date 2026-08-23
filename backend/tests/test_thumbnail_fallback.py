"""Unit tests for the reactive server-side thumbnail fallback.

Previously the backend unconditionally re-downloaded and re-thumbnailed every
HEIC/HEIF upload at finalize time, regardless of whether the browser's own
thumbnail (now decoded via heicDecodeWorker.ts) succeeded. The backend should
only step in when the browser's client-processing report actually says the
thumbnail step failed/was unsupported/timed out -- mirroring the existing
_apply_server_exif_fallback pattern -- and it must never re-download bytes it
doesn't end up needing.
"""
from __future__ import annotations

import base64
import io
import json
import re

import pytest
from PIL import Image

import storage_utils


class _ResourceNotFound(Exception):
    pass


class _FakeMetadataTable:
    """Fake Azure table supporting the point get/upsert storage_utils issues
    plus the RowKey/PartitionKey scans used by finalize/dedup lookups."""

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
        f = filter_str.strip()
        by_rowkey = re.match(r"RowKey eq '(.*)'$", f)
        if by_rowkey:
            rk = by_rowkey.group(1)
            return [dict(v) for (_, r), v in self.rows.items() if r == rk]
        by_pk = re.match(r"PartitionKey eq '(.*)'$", f)
        if by_pk:
            pk = by_pk.group(1)
            return [dict(v) for (p, _), v in self.rows.items() if p == pk]
        raise ValueError(f'Unsupported filter: {filter_str}')


def _tiny_jpeg_bytes() -> bytes:
    image = Image.new('RGB', (32, 32), color=(10, 120, 200))
    buffer = io.BytesIO()
    image.save(buffer, format='JPEG')
    return buffer.getvalue()


def _seed_row(metadata: _FakeMetadataTable, user_id: str, filename: str, **overrides) -> None:
    row = {
        'PartitionKey': user_id,
        'RowKey': filename,
        'thumbnail_status': 'pending',
        'processing_metadata': '{}',
        **overrides,
    }
    metadata.upsert_entity(row)


class _Calls:
    def __init__(self) -> None:
        self.downloads: list = []
        self.uploads: list = []


@pytest.fixture
def processing_ctx(monkeypatch):
    metadata = _FakeMetadataTable()
    monkeypatch.setitem(storage_utils._CTX, 'metadata_table_client', metadata)
    monkeypatch.setitem(storage_utils._CTX, 'blob_service_client', object())
    monkeypatch.setitem(storage_utils._CTX, 'blob_image_container', 'images')
    monkeypatch.setitem(storage_utils._CTX, 'face_table_client', None)
    # These write to real blob storage / a vector index / a text-embedding
    # model -- none of that is under test here, so replace with fakes that
    # record what was asked of them.
    calls = _Calls()
    monkeypatch.setattr(
        storage_utils, 'download_media_bytes',
        lambda kind, name: calls.downloads.append((kind, name)) or _tiny_jpeg_bytes(),
    )
    monkeypatch.setattr(
        storage_utils, 'upload_media_file',
        lambda kind, name, content, content_type: calls.uploads.append((kind, name, content_type)),
    )
    monkeypatch.setattr(storage_utils, 'refresh_user_vector_index', lambda *a, **k: None)
    monkeypatch.setattr(storage_utils, '_refresh_semantic_fields', lambda *a, **k: None)
    yield metadata, calls


# --- _client_report_needs_server_thumbnail_fallback: pure function ----------

@pytest.mark.parametrize('status', ['failed', 'unsupported', 'timeout'])
def test_needs_fallback_true_for_failure_statuses(status):
    report = [{'step': 'thumbnail', 'status': status, 'reason': 'unknown_error'}]
    assert storage_utils._client_report_needs_server_thumbnail_fallback(report) is True


@pytest.mark.parametrize('status', ['done', 'skipped'])
def test_needs_fallback_false_for_non_failure_statuses(status):
    report = [{'step': 'thumbnail', 'status': status, 'reason': 'unknown_error'}]
    assert storage_utils._client_report_needs_server_thumbnail_fallback(report) is False


def test_needs_fallback_false_when_no_thumbnail_item():
    report = [{'step': 'exif', 'status': 'failed', 'reason': 'unknown_error'}]
    assert storage_utils._client_report_needs_server_thumbnail_fallback(report) is False


# --- integration through apply_client_processing_results_for_file ----------

def test_server_generates_thumbnail_when_browser_reports_failure(processing_ctx):
    metadata, calls = processing_ctx
    user_id, filename = 'lib-A', 'photo.heic'
    _seed_row(metadata, user_id, filename)

    result = storage_utils.apply_client_processing_results_for_file(
        user_id,
        filename,
        client_processing={},
        client_processing_report=[{'step': 'thumbnail', 'status': 'failed', 'reason': 'unknown_error'}],
        client_asset_id='browser-photo.heic',
    )

    assert calls.downloads == [('image', filename)]
    assert len(calls.uploads) == 1
    assert calls.uploads[0][0] == 'thumbnail'
    assert result['thumbnail_status'] == 'done'
    stored = metadata.get_entity(user_id, filename)
    assert stored['thumbnail_status'] == 'done'
    processing = json.loads(stored['processing_metadata'])
    assert processing['server_thumbnail']['fallbackFor'] == 'browser_thumbnail_failed'


def test_no_fallback_when_thumbnail_already_done(processing_ctx):
    metadata, calls = processing_ctx
    user_id, filename = 'lib-A', 'photo.heic'
    _seed_row(
        metadata, user_id, filename,
        thumbnail_status='done',
        processing_metadata=json.dumps({'client_thumbnail': {'source': 'browser'}}),
    )

    # A later reprocessing pass reports failure again for the same file --
    # must not stomp on or redo a thumbnail that already exists.
    storage_utils.apply_client_processing_results_for_file(
        user_id,
        filename,
        client_processing={},
        client_processing_report=[{'step': 'thumbnail', 'status': 'failed', 'reason': 'unknown_error'}],
        client_asset_id='browser-photo.heic',
    )

    assert calls.downloads == []
    assert calls.uploads == []
    assert metadata.get_entity(user_id, filename)['thumbnail_status'] == 'done'


def test_no_download_when_thumbnail_succeeds_via_direct_upload(processing_ctx):
    metadata, calls = processing_ctx
    user_id, filename = 'lib-A', 'photo.jpg'
    _seed_row(metadata, user_id, filename)

    storage_utils.apply_client_processing_results_for_file(
        user_id,
        filename,
        client_processing={},
        client_processing_report=[{'step': 'thumbnail', 'status': 'done', 'reason': 'done'}],
        client_asset_id='browser-photo.jpg',
        thumbnail_already_uploaded=True,
    )

    # The report says the browser already succeeded (and already uploaded the
    # thumbnail directly) -- nothing here should need the original bytes.
    assert calls.downloads == []
    assert calls.uploads == []
    assert metadata.get_entity(user_id, filename)['thumbnail_status'] == 'done'


def test_no_download_when_report_has_no_steps(processing_ctx):
    metadata, calls = processing_ctx
    user_id, filename = 'lib-A', 'photo.jpg'
    _seed_row(metadata, user_id, filename)

    storage_utils.apply_client_processing_results_for_file(
        user_id,
        filename,
        client_processing={},
        client_processing_report=[],
        client_asset_id='browser-photo.jpg',
    )

    assert calls.downloads == []
    assert calls.uploads == []


def test_single_download_serves_both_exif_and_thumbnail_fallback(processing_ctx):
    """When a report needs BOTH the EXIF and thumbnail fallback, the lazy
    get_image_bytes() must fetch once and reuse the cached bytes, not
    download twice."""
    metadata, calls = processing_ctx
    user_id, filename = 'lib-A', 'photo.heic'
    _seed_row(metadata, user_id, filename)

    storage_utils.apply_client_processing_results_for_file(
        user_id,
        filename,
        client_processing={},
        client_processing_report=[
            {'step': 'thumbnail', 'status': 'failed', 'reason': 'unknown_error'},
            {'step': 'exif', 'status': 'unsupported', 'reason': 'unknown_error'},
        ],
        client_asset_id='browser-photo.heic',
    )

    assert calls.downloads == [('image', filename)]
    assert len(calls.uploads) == 1  # only the thumbnail upload; EXIF has no blob upload


def test_missing_source_blob_fails_gracefully_instead_of_500(processing_ctx, monkeypatch):
    """A genuinely missing source blob (e.g. a lost upload reservation) used
    to propagate download_media_bytes's ResourceNotFoundError straight out of
    apply_client_processing_results_for_file uncaught -- turning "the source
    is gone" into a 500 on the whole /upload/client-processing request
    instead of the steps just resolving to 'failed' like any other terminal
    failure."""
    metadata, calls = processing_ctx
    user_id, filename = 'lib-A', 'photo.cr3'
    _seed_row(metadata, user_id, filename)

    def _raise_not_found(kind, name):
        calls.downloads.append((kind, name))
        raise storage_utils.ResourceNotFoundError('The specified blob does not exist.')

    monkeypatch.setattr(storage_utils, 'download_media_bytes', _raise_not_found)

    result = storage_utils.apply_client_processing_results_for_file(
        user_id,
        filename,
        client_processing={},
        client_processing_report=[
            {'step': 'thumbnail', 'status': 'failed', 'reason': 'unknown_error'},
            {'step': 'exif', 'status': 'unsupported', 'reason': 'unknown_error'},
        ],
        client_asset_id='browser-photo.cr3',
    )

    assert calls.uploads == []
    assert result['thumbnail_status'] == 'failed'
    stored = metadata.get_entity(user_id, filename)
    assert stored['thumbnail_status'] == 'failed'
    assert stored['exif_status'] == 'failed'
