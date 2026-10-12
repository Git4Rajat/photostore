import io
from types import SimpleNamespace

import pytest
from PIL import Image

import app
import benchmark_upload as benchmark
import storage_utils


def entry(arm='derivatives'):
    return {'filename': f'rawbench-run-12345678-{arm}-00000.cr3', 'sha256': 'a' * 64,
            'blobName': 'blob-1', 'benchmark': {'runId': 'run-12345678', 'arm': arm}}


@pytest.fixture
def allowed(monkeypatch):
    monkeypatch.setenv('BENCHMARK_UPLOAD_EMAIL', 'owner@example.com')
    monkeypatch.setattr(app, '_resolve_session_payload', lambda required: ({'email': 'owner@example.com'}, None))


def test_normal_upload_unaffected_when_disabled(monkeypatch):
    monkeypatch.delenv('BENCHMARK_UPLOAD_EMAIL', raising=False)
    assert benchmark.authorize_entry(app, {}, 'camera.cr3') == {}


def test_benchmark_default_disabled(monkeypatch):
    monkeypatch.delenv('BENCHMARK_UPLOAD_EMAIL', raising=False)
    monkeypatch.setattr(app, '_resolve_session_payload', lambda required: ({'email': 'owner@example.com'}, None))
    with pytest.raises(PermissionError):
        benchmark.authorize_entry(app, entry(), entry()['filename'])


def test_only_allowlisted_authenticated_account(allowed, monkeypatch):
    monkeypatch.setattr(app, '_resolve_session_payload', lambda required: ({'email': 'someone@example.com'}, None))
    with pytest.raises(PermissionError):
        benchmark.authorize_entry(app, entry(), entry()['filename'])


@pytest.mark.parametrize('change', [{'filename': 'ordinary.cr3'}, {'sha256': ''}, {'benchmark': {'runId': '../escape', 'arm': 'derivatives'}}])
def test_benchmark_namespace_and_hash_required(allowed, change):
    value = {**entry(), **change}
    with pytest.raises(ValueError):
        benchmark.authorize_entry(app, value, value['filename'])


def test_authorized_marker_is_persistent(allowed):
    assert benchmark.authorize_entry(app, entry(), entry()['filename']) == {
        'processingIsolated': True, 'benchmarkRunId': 'run-12345678', 'benchmarkArm': 'derivatives'}


def test_finalization_requires_own_matching_reservation(allowed, monkeypatch):
    fields = benchmark.authorize_entry(app, entry(), entry()['filename'])
    row = {**fields, 'pendingAnonymousBlob': 'blob-1', 'upload_sha256_expected': 'a' * 64}
    monkeypatch.setattr(app, 'metadata_table_client', SimpleNamespace(get_entity=lambda **kw: row))
    benchmark.validate_finalization(app, 'lib', entry(), entry()['filename'])
    with pytest.raises(ValueError):
        benchmark.validate_finalization(app, 'lib', {**entry(), 'blobName': 'another-upload'}, entry()['filename'])


@pytest.mark.parametrize('queued', ['pending', 'failed', 'skipped', 'queued', 'running'])
def test_recovery_and_browser_poll_ignore_isolation(queued):
    row = {'RowKey': entry()['filename'], 'processingIsolated': True,
           **{f'{step}_status': queued for step in app.IPWORK_STEPS}}
    assert app._ipwork_sweep_eligible_steps(row) == []
    assert app._browser_processing_pending_item(row) is None


def test_enqueue_guards_even_when_forced(monkeypatch):
    monkeypatch.setattr(app, '_get_metadata_entity', lambda *args: {'processingIsolated': True})
    assert app._enqueue_processing_steps('lib', entry()['filename'], ['face'], force=True)['face']['status'] == 'skipped'
    assert app._queue_ipwork_processing('lib', entry()['filename'])['status'] == 'skipped'
    assert app._enqueue_preview_generation_job('lib', entry()['filename'])['status'] == 'skipped'
    assert app._queue_people_clustering_after_face_processing('lib', entry()['filename'], {'processingIsolated': True}) is None


def test_worker_cannot_claim_isolated_photo(monkeypatch):
    table = SimpleNamespace(get_entity=lambda **kw: {'processingIsolated': True})
    monkeypatch.setitem(storage_utils._CTX, 'metadata_table_client', table)
    monkeypatch.setitem(storage_utils._CTX, 'blob_service_client', object())
    with pytest.raises(storage_utils.PhotoNotFoundError, match='isolated'):
        storage_utils.claim_processing_lease('lib', entry()['filename'], 'worker', steps=['face'])


def jpeg_storage(width, height, *, size_override=None, content_type='image/jpeg'):
    buffer = io.BytesIO()
    Image.new('RGB', (width, height), 'red').save(buffer, format='JPEG')
    content = buffer.getvalue()
    reads = []
    properties = SimpleNamespace(size=size_override or len(content), etag='version-1',
                                 content_settings=SimpleNamespace(content_type=content_type))

    def download(**kwargs):
        reads.append(kwargs)
        assert kwargs['length'] <= 1_500_000
        assert kwargs['etag'] == 'version-1'
        return SimpleNamespace(readall=lambda: content)

    client = SimpleNamespace(get_blob_properties=lambda: properties, download_blob=download)
    storage = SimpleNamespace(_CTX={'blob_thumbnail_container': 'thumbs'}, _get_blob_client=lambda *args: client)
    return storage, reads, len(content)


def test_direct_jpeg_is_bounded_and_verified():
    storage, reads, length = jpeg_storage(2048, 1024)
    assert benchmark.verify_jpeg(storage, 'preview/blob.jpg', byte_cap=1_500_000, edge_cap=2048) == length
    assert reads[0]['length'] == length


@pytest.mark.parametrize('width,height,size,mime', [(2049, 1, None, 'image/jpeg'), (2, 2, 1_500_001, 'image/jpeg'), (2, 2, None, 'image/png')])
def test_bad_direct_jpeg_rejected(width, height, size, mime):
    storage, reads, _ = jpeg_storage(width, height, size_override=size, content_type=mime)
    with pytest.raises(ValueError):
        benchmark.verify_jpeg(storage, 'preview/blob.jpg', byte_cap=1_500_000, edge_cap=2048)
    if size or mime != 'image/jpeg':
        assert not reads


def test_isolated_reports_never_fall_back_to_original_or_accept_ai(monkeypatch):
    monkeypatch.setitem(storage_utils._CTX, 'metadata_table_client', object())
    row = {'processingIsolated': True, 'anonymousImageId': 'blob-1', 'processing_metadata': '{}',
           'preview_status': 'skipped', 'thumbnail_status': 'skipped', 'face_status': 'skipped'}
    updates = []
    monkeypatch.setattr(storage_utils, '_update_metadata_fields', lambda *args: updates.append(args[-1]))
    storage_utils._apply_client_processing_results('lib', 'photo.cr3', row,
        lambda: pytest.fail('Original RAW was read'),
        {'preview': {'hasData': False}, 'face': {'faces': []}, 'exif': {'hasData': False}},
        [{'step': 'thumbnail', 'status': 'failed'}, {'step': 'face', 'status': 'done'}], 'asset')
    assert row['face_status'] == 'skipped'
    assert row['preview_status'] == 'skipped'
    assert updates and updates[0]['thumbnail_status'] == 'skipped'


def test_isolated_direct_derivatives_persist_without_reupload(monkeypatch):
    monkeypatch.setitem(storage_utils._CTX, 'metadata_table_client', object())
    storage, _, _ = jpeg_storage(120, 80)
    monkeypatch.setattr(storage_utils, '_get_blob_client', storage._get_blob_client)
    monkeypatch.setitem(storage_utils._CTX, 'blob_thumbnail_container', 'thumbs')
    row = {'processingIsolated': True, 'anonymousImageId': 'blob-1', 'processing_metadata': '{}',
           'preview_status': 'skipped', 'thumbnail_status': 'skipped'}
    updates = []
    monkeypatch.setattr(storage_utils, '_update_metadata_fields', lambda *args: updates.append(args[-1]))
    storage_utils._apply_client_processing_results('lib', 'photo.cr3', row,
        lambda: pytest.fail('Original RAW was read'), {'preview': {'alreadyUploaded': True}},
        [{'step': 'thumbnail', 'status': 'done'}], 'asset')
    assert updates[0]['preview_status'] == 'done'
    assert updates[0]['thumbnail_status'] == 'done'
