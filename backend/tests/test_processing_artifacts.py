import io
import json
from types import SimpleNamespace

import pytest
from PIL import Image
from azure.core.exceptions import ResourceExistsError, ResourceModifiedError, ResourceNotFoundError

import storage_utils
from processing_artifacts import publish, staging_name, derivative_blob
from processing_completion import StaleProcessingResult
from processing_fence import result_fence
from test_processing_fence import world


def jpeg(color='red', edge=40):
    out = io.BytesIO(); Image.new('RGB', (edge, edge), color).save(out, 'JPEG'); return out.getvalue()


class Blob:
    def __init__(self):
        self.data = None; self.version = 0; self.before_download = None
    def upload_blob(self, data, *, overwrite, content_settings):
        assert overwrite is False
        if self.data is not None: raise ResourceExistsError('exists')
        self.data = data; self.version += 1
    def get_blob_properties(self):
        if self.data is None: raise ResourceNotFoundError('missing')
        return SimpleNamespace(size=len(self.data), etag=str(self.version), content_settings=SimpleNamespace(content_type='image/jpeg'))
    def download_blob(self, *, offset, length, etag, match_condition):
        if self.before_download: self.before_download()
        if etag != str(self.version): raise ResourceModifiedError('changed')
        return SimpleNamespace(readall=lambda: self.data[offset:offset+length])


@pytest.fixture
def blobs(world, monkeypatch):
    objects = {}
    monkeypatch.setitem(storage_utils._CTX, 'blob_thumbnail_container', 'thumbs')
    monkeypatch.setattr(storage_utils, '_get_blob_client', lambda container, name: objects.setdefault(name, Blob()))
    return world, objects


def test_browser_staging_is_promoted_to_create_only_verified_content(blobs):
    (metadata, *_, fence), objects = blobs
    stage = staging_name('lib', 'photo', 'rev', 'attempt', 'thumbnail')
    staged = Blob(); staged.data = jpeg(); staged.version = 1; objects[stage] = staged
    row = metadata.get_entity('lib', 'photo.jpg')
    with result_fence(fence): receipt = publish(storage_utils, row, 'thumbnail', staged=True)
    assert receipt['blob'].startswith('derived/v1/') and receipt['blob'] != stage
    assert derivative_blob(row, 'thumbnail', 'photo') == receipt['blob']
    # A still-valid browser SAS can mutate staging, not the already-published image.
    original = objects[receipt['blob']].data
    staged.data = jpeg('blue'); staged.version += 1
    assert objects[receipt['blob']].data == original


def test_worker_retry_proves_existing_blob_without_overwrite(blobs):
    (metadata, *_, fence), objects = blobs
    row = metadata.get_entity('lib', 'photo.jpg')
    with result_fence(fence):
        first = publish(storage_utils, row, 'preview', data=jpeg())
        second = publish(storage_utils, row, 'preview', data=jpeg())
    assert first == second and objects[first['blob']].version == 1


def test_staging_changed_during_conditional_read_is_rejected(blobs):
    (metadata, *_, fence), objects = blobs
    stage = Blob(); stage.data = jpeg(); stage.version = 1
    stage.before_download = lambda: setattr(stage, 'version', 2)
    objects[staging_name('lib', 'photo', 'rev', 'attempt', 'thumbnail')] = stage
    row = metadata.get_entity('lib', 'photo.jpg')
    with result_fence(fence), pytest.raises(ResourceModifiedError): publish(storage_utils, row, 'thumbnail', staged=True)
    assert 'thumbnailArtifact' not in row


def test_source_changed_after_upload_does_not_publish_a_pointer(blobs, monkeypatch):
    (metadata, *_, fence), objects = blobs
    original = Blob.upload_blob
    def replace(blob, *a, **k):
        original(blob, *a, **k)
        metadata.rows['lib', 'photo.jpg']['processingFence'] = 'successor'
    monkeypatch.setattr(Blob, 'upload_blob', replace)
    row = metadata.get_entity('lib', 'photo.jpg')
    with result_fence(fence), pytest.raises(StaleProcessingResult): publish(storage_utils, row, 'thumbnail', data=jpeg())
    assert 'thumbnailArtifact' not in row
    assert any(blob.data for blob in objects.values())  # Unreferenced orphan is safe.


@pytest.mark.parametrize('data', [b'not jpeg', jpeg(edge=121)])
def test_invalid_thumbnail_never_becomes_a_pointer(blobs, data):
    (metadata, *_, fence), _ = blobs
    row = metadata.get_entity('lib', 'photo.jpg')
    with result_fence(fence), pytest.raises((ValueError, OSError)): publish(storage_utils, row, 'thumbnail', data=data)
    assert 'thumbnailArtifact' not in row


def test_serving_retains_previous_derivative_for_same_photo_but_rejects_cross_photo(blobs):
    (metadata, *_, fence), _ = blobs
    row = metadata.get_entity('lib', 'photo.jpg')
    with result_fence(fence): receipt = publish(storage_utils, row, 'thumbnail', data=jpeg())
    row['processingRevision'] = 'new-revision'
    assert derivative_blob(row, 'thumbnail', 'legacy') == receipt['blob']
    assert storage_utils._sort_index_row(row)['thumb'] == receipt['blob']
    assert storage_utils._access_index_row(row)['thumbnailArtifact'] == row['thumbnailArtifact']
    row['anonymousImageId'] = 'replacement'
    with pytest.raises(ValueError): derivative_blob(row, 'thumbnail', 'legacy')


@pytest.mark.parametrize('bad', ['photo', 'library', 'blob', 'missing', 'typed', 'json', 'list', 'null'])
def test_catalog_omits_invalid_receipts_without_weakening_completion_validation(blobs, bad):
    from processing_artifacts import published_name, validate_receipt
    (metadata, *_, fence), _ = blobs
    row = metadata.get_entity('lib', 'photo.jpg')
    with result_fence(fence):
        receipt = publish(storage_utils, row, 'thumbnail', data=jpeg())
    invalid = dict(receipt)
    if bad in ('photo', 'library'):
        invalid['photoId' if bad == 'photo' else 'library'] = 'other'
        invalid['blob'] = published_name(invalid['library'], invalid['photoId'], invalid['revision'], 'thumbnail', invalid['sha256'])
    elif bad == 'blob': invalid['blob'] = 'untrusted/path.jpg'
    elif bad == 'missing': del invalid['revision']
    elif bad == 'typed': invalid['library'] = 123
    raw = {'json': '{', 'list': '[]', 'null': 'null'}.get(bad, json.dumps(invalid))
    row['thumbnailArtifact'] = row['previewArtifact'] = raw
    assert storage_utils._sort_index_row(row)['thumb'] == row['anonymousImageId']
    projected = storage_utils._access_index_row(row)
    assert projected['RowKey'] == row['RowKey'] and projected['blobName'] == row['anonymousImageId']
    assert 'thumbnailArtifact' not in projected and 'previewArtifact' not in projected
    assert row['thumbnailArtifact'] == raw  # Projection never rewrites durable evidence.
    with pytest.raises((ValueError, KeyError, TypeError)):
        validate_receipt(row, 'thumbnail')


def test_receipt_detects_tampering_and_requires_the_current_processing_revision(blobs):
    from processing_artifacts import verify_receipt, validate_receipt
    (metadata, *_, fence), objects = blobs
    row = metadata.get_entity('lib', 'photo.jpg')
    with result_fence(fence): receipt = publish(storage_utils, row, 'thumbnail', data=jpeg())
    assert verify_receipt(storage_utils, row, 'thumbnail') == receipt
    objects[receipt['blob']].version += 1
    with pytest.raises(ValueError, match='no longer matches'): verify_receipt(storage_utils, row, 'thumbnail')
    row['processingRevision'] = 'new-source'
    with pytest.raises(ValueError, match='stale'): validate_receipt(row, 'thumbnail')


def test_real_result_cas_publishes_verified_pointer_with_status_and_lease_release(blobs, monkeypatch):
    import base64
    (metadata, *_, fence), objects = blobs
    monkeypatch.setenv('UPLOAD_WAVE_SOURCE_COMPLETION_ENABLED', 'true')
    monkeypatch.setitem(storage_utils._CTX, 'source_completion_delivery', None)
    monkeypatch.setitem(storage_utils._CTX, 'embeddings_table_client', None)
    monkeypatch.setattr(storage_utils, '_refresh_semantic_fields', lambda *a: None)
    result = storage_utils.apply_client_processing_results_for_file('lib', 'photo.jpg',
        lease_owner='owner', expected_processing_revision='rev', expected_processing_fence='attempt',
        claimed_steps=['thumbnail'], client_processing={'thumbnail': {'data': base64.b64encode(jpeg()).decode()}})
    assert result['thumbnail_status'] == 'done'
    stored = metadata.get_entity('lib', 'photo.jpg')
    receipt = json.loads(stored['thumbnailArtifact'])
    assert objects[receipt['blob']].data == jpeg()
    assert stored['processing_lease_owner'] == ''
    assert metadata.update_calls == 1


def test_unverified_client_done_report_cannot_commit_success_in_fenced_mode(blobs, monkeypatch):
    (metadata, *_, fence), _ = blobs
    monkeypatch.setenv('UPLOAD_WAVE_SOURCE_COMPLETION_ENABLED', 'true')
    monkeypatch.setitem(storage_utils._CTX, 'source_completion_delivery', None)
    monkeypatch.setitem(storage_utils._CTX, 'embeddings_table_client', None)
    monkeypatch.setattr(storage_utils, '_refresh_semantic_fields', lambda *a: None)
    result = storage_utils.apply_client_processing_results_for_file('lib', 'photo.jpg',
        lease_owner='owner', expected_processing_revision='rev', expected_processing_fence='attempt',
        claimed_steps=['thumbnail'], client_processing={'thumbnail': {'data': 'invalid'}},
        client_processing_report=[{'step': 'thumbnail', 'status': 'done'}])
    assert result['thumbnail_status'] == 'failed'
    assert not result.get('processingCompletionPending')


@pytest.mark.parametrize('converted', [
    None, b'', b'undecodable raw', b'raw' * 600_000, jpeg(edge=2049), jpeg()[:100],
])
def test_invalid_server_preview_is_committed_as_unrecoverable_without_publication(blobs, monkeypatch, converted):
    # Deterministic on these bytes (same conversion, same input, every time),
    # so this resolves to terminal 'unrecoverable' on the first attempt
    # instead of a retryable 'failed'.
    (metadata, *_, fence), objects = blobs
    monkeypatch.setenv('UPLOAD_WAVE_SOURCE_COMPLETION_ENABLED', 'true')
    monkeypatch.setitem(storage_utils._CTX, 'source_completion_delivery', None)
    monkeypatch.setitem(storage_utils._CTX, 'embeddings_table_client', None)
    monkeypatch.setattr(storage_utils, '_refresh_semantic_fields', lambda *a: None)
    monkeypatch.setattr(storage_utils, 'download_media_bytes', lambda *a: b'original raw')
    monkeypatch.setattr(storage_utils, 'convert_image_to_jpeg', lambda *a: converted)
    result = storage_utils.apply_client_processing_results_for_file('lib', 'photo.jpg',
        lease_owner='owner', expected_processing_revision='rev', expected_processing_fence='attempt',
        claimed_steps=['preview'], origin='ipworker',
        client_processing={'preview': {'hasData': False, 'error': 'source_undecodable'}})
    stored = metadata.get_entity('lib', 'photo.jpg')
    assert result['preview_status'] == stored['preview_status'] == 'unrecoverable'
    assert stored['processing_lease_owner'] == ''
    assert not stored.get('previewArtifact') and not objects
    assert not stored.get('processingCompletionPending')


def test_valid_server_preview_publishes_verified_artifact(blobs, monkeypatch):
    (metadata, *_, fence), objects = blobs
    monkeypatch.setattr(storage_utils, 'convert_image_to_jpeg', lambda *a: jpeg())
    row = metadata.get_entity('lib', 'photo.jpg')
    with result_fence(fence):
        result = storage_utils._apply_server_preview_fallback('lib', 'photo.jpg', row,
            b'original raw', fallback_for='client_preview_failed')
    assert result['preview_status'] == 'done'
    receipt = json.loads(row['previewArtifact'])
    assert objects[receipt['blob']].data == jpeg()


@pytest.mark.parametrize('error', [OSError('storage unavailable'), StaleProcessingResult('replaced')])
def test_server_preview_publication_errors_still_propagate(monkeypatch, error):
    monkeypatch.setattr(storage_utils, 'convert_image_to_jpeg', lambda *a: jpeg())
    def fail(*a, **k):
        raise error
    monkeypatch.setattr(storage_utils, '_publish_processing_derivative', fail)
    with pytest.raises(type(error)):
        storage_utils._apply_server_preview_fallback('lib', 'photo.jpg', {},
            b'original raw', fallback_for='client_preview_failed')


def test_sort_and_access_projections_preserve_versioned_derivative_pointers(blobs, monkeypatch):
    (metadata, *_, fence), _ = blobs
    row = metadata.get_entity('lib', 'photo.jpg')
    with result_fence(fence):
        receipt = publish(storage_utils, row, 'thumbnail', data=jpeg())
        publish(storage_utils, row, 'preview', data=jpeg())
    row.update(thumbnail_status='done', preview_status='done')
    assert storage_utils._sort_index_row(row)['thumb'] == receipt['blob']
    projected = storage_utils._access_index_row(row)
    monkeypatch.setattr(storage_utils, '_load_access_index_manifest', lambda *a: {'sourceVersion': 'versioned-artifacts'})
    monkeypatch.setattr(storage_utils, '_load_access_index_blob', lambda *a: SimpleNamespace(rows=[projected]))
    storage_utils._ACCESS_LOOKUP_CACHE.clear()
    compact = storage_utils.lookup_access_entries('lib', ['photo.jpg'])['photo.jpg']
    assert derivative_blob(compact, 'thumbnail', 'wrong') == receipt['blob']
    assert compact['previewArtifact'] == row['previewArtifact']
    assert storage_utils.metadata_index_kinds({'thumbnailArtifact': row['thumbnailArtifact']}) >= {'sort', 'access'}


@pytest.mark.parametrize('kind', ['thumbnail', 'preview', 'image'])
def test_access_url_uses_exact_derivative_pointer_and_original_identity(blobs, monkeypatch, kind):
    import app
    from routes.photos import photo_access_url
    (metadata, *_, fence), _ = blobs
    row = metadata.get_entity('lib', 'photo.jpg')
    with result_fence(fence):
        thumb = publish(storage_utils, row, 'thumbnail', data=jpeg())
        preview = publish(storage_utils, row, 'preview', data=jpeg())
    row.update(thumbnail_status='done', preview_status='done')
    monkeypatch.setattr(app, '_require_user_id', lambda: ('lib', None))
    monkeypatch.setattr(app, '_get_metadata_entity', lambda *a: row)
    monkeypatch.setattr(app, 'blob_service_client', object())
    monkeypatch.setattr(app, 'account_name', 'account')
    monkeypatch.setattr(app, '_create_stable_read_sas_url', lambda container, name, **kwargs: ('https://storage/' + name, 'later'))
    with app.app.test_request_context('/api/photos/access/' + kind + '/photo.jpg'):
        response = photo_access_url(kind, 'photo.jpg')
    expected = {'thumbnail': thumb['blob'], 'preview': preview['blob'], 'image': 'photo'}[kind]
    assert response.get_json()['url'] == 'https://storage/' + expected


def test_completion_delivery_retains_exact_old_derivative_receipt_across_new_generation(blobs):
    from processing_completion import STEPS, prepare_completion, relay_completion
    from test_upload_waves import AtomicTable
    from upload_waves import ShadowWaves
    (metadata, *_, fence), _ = blobs
    row = metadata.get_entity('lib', 'photo.jpg')
    with result_fence(fence): receipt = publish(storage_utils, row, 'thumbnail', data=jpeg())
    row.update({step + '_status': 'no_data' for step in STEPS})
    row.update(thumbnail_status='done', processing_lease_owner='', processing_lease_expires_at='')
    prepare_completion(row)
    row['processingRevision'] = 'successor'
    row.pop('thumbnailArtifact')
    metadata.upsert_entity(row)
    inbox = ShadowWaves(AtomicTable(), 'lib')
    assert relay_completion(metadata, 'lib', 'photo.jpg', inbox.accept_source_completion)
    stored = inbox._page('source-completion!')[0]
    payload = json.loads(stored['payload'])
    assert payload['revision'] == 'rev'
    assert payload['derivatives']['thumbnail'] == receipt
    assert not stored['completionQualified']


def test_proxy_thumbnail_preserves_legacy_physical_name_mapping(monkeypatch):
    import app
    from routes.photos import proxy_thumbnail
    monkeypatch.setattr(app, '_require_user_id', lambda: ('lib', None))
    monkeypatch.setattr(app, '_get_metadata_entity', lambda *a: {'RowKey': 'old.jpg'})
    monkeypatch.setattr(app, '_resolve_media_blob_name', lambda *a: 'old-physical-id')
    monkeypatch.setattr(app, 'get_media_properties', lambda kind, name: {'content_type': 'image/jpeg', 'size': 1})
    names = []
    monkeypatch.setattr(app, '_stream_media_response', lambda kind, name, **kwargs: names.append(name) or 'streamed')
    with app.app.test_request_context('/api/photos/thumbnail/old.jpg'):
        assert proxy_thumbnail('old.jpg') == 'streamed'
    assert names == ['old-physical-id']


def test_unfenced_legacy_publication_keeps_its_existing_content_type(monkeypatch):
    writes = []
    monkeypatch.setattr(storage_utils, 'upload_media_file', lambda *a: writes.append(a))
    storage_utils._publish_processing_derivative({'RowKey': 'legacy.png'}, 'thumbnail', b'legacy-png',
        filename='legacy.png', content_type='image/png')
    assert writes == [('thumbnail', 'legacy.png', b'legacy-png', 'image/png')]


@pytest.mark.parametrize('kind', ['thumbnail', 'preview'])
@pytest.mark.parametrize('invalid', ['photo', 'revision', 'malformed', 'list', 'null'])
def test_prepared_promotion_replaces_stale_receipts_using_current_photo_bytes(blobs, kind, invalid):
    from processing_artifacts import promote_prepared, verify_receipt, published_name
    (metadata, *_, fence), objects = blobs
    row = metadata.get_entity('lib', 'photo.jpg')
    with result_fence(fence):
        previous = publish(storage_utils, row, kind, data=jpeg('red'))
    old_blob = previous['blob']
    if invalid in ('malformed', 'list', 'null'):
        row[kind + 'Artifact'] = {'malformed': 'not-json', 'list': '[]', 'null': 'null'}[invalid]
    else:
        stale = dict(previous)
        stale['photoId' if invalid == 'photo' else 'revision'] = 'previous-source'
        stale['blob'] = published_name(stale['library'], stale['photoId'], stale['revision'], kind, stale['sha256'])
        row[kind + 'Artifact'] = json.dumps(stale)
    row[kind + '_status'] = 'done'
    prepared = Blob(); prepared.data = jpeg('blue'); prepared.version = 1
    objects['photo' if kind == 'thumbnail' else 'preview/photo.jpg'] = prepared
    with result_fence(fence):
        promote_prepared(storage_utils, row)
        current = verify_receipt(storage_utils, row, kind)
    assert current['photoId'] == fence.photo and current['revision'] == fence.revision
    assert current['sha256'] != previous['sha256']
    assert objects[current['blob']].data == jpeg('blue')
    assert objects[old_blob].data == jpeg('red')


@pytest.mark.parametrize('kind', ['thumbnail', 'preview'])
def test_prepared_promotion_preserves_valid_receipt_without_reading_preparation(blobs, monkeypatch, kind):
    from processing_artifacts import promote_prepared
    (metadata, *_, fence), _ = blobs
    row = metadata.get_entity('lib', 'photo.jpg')
    with result_fence(fence):
        publish(storage_utils, row, kind, data=jpeg())
        row[kind + '_status'] = 'done'
        original = row[kind + 'Artifact']
        monkeypatch.setattr(storage_utils, '_get_blob_client', lambda *a: pytest.fail('Reread valid prepared artifact'))
        promote_prepared(storage_utils, row)
    assert row[kind + 'Artifact'] == original


@pytest.mark.parametrize('kind', ['thumbnail', 'preview'])
def test_stale_receipt_repair_rejects_invalid_prepared_bytes(blobs, kind):
    from processing_artifacts import promote_prepared
    (metadata, *_, fence), objects = blobs
    row = metadata.get_entity('lib', 'photo.jpg')
    row.update({kind + '_status': 'done', kind + 'Artifact': '{}'})
    prepared = Blob(); prepared.data = b'not-an-image'; prepared.version = 1
    objects['photo' if kind == 'thumbnail' else 'preview/photo.jpg'] = prepared
    with result_fence(fence), pytest.raises((ValueError, OSError)):
        promote_prepared(storage_utils, row)
    assert row[kind + 'Artifact'] == '{}'
    assert not any(name.startswith('derived/') for name in objects)


def test_capture_can_reuse_just_published_bytes_but_other_attempts_must_verify(blobs, monkeypatch):
    from processing_artifacts import verify_receipt
    (metadata, *_, fence), _ = blobs
    row = metadata.get_entity('lib', 'photo.jpg')
    original = storage_utils._get_blob_client
    reads = []
    monkeypatch.setattr(storage_utils, '_get_blob_client', lambda *a: (reads.append(a), original(*a))[1])
    with result_fence(fence):
        receipt = publish(storage_utils, row, 'thumbnail', data=jpeg())
        reads.clear()
        assert verify_receipt(storage_utils, row, 'thumbnail', reuse_published=True) == receipt
        assert not reads
        # Ordinary verification still checks the blob even inside this attempt.
        verify_receipt(storage_utils, row, 'thumbnail')
        assert reads
    with result_fence(fence):
        reads.clear()
        verify_receipt(storage_utils, row, 'thumbnail', reuse_published=True)
        assert reads  # Reentering even the same fence cannot reuse old trust.


def test_modified_receipt_cannot_use_publication_verification_cache(blobs):
    from processing_artifacts import verify_receipt
    (metadata, *_, fence), _ = blobs
    row = metadata.get_entity('lib', 'photo.jpg')
    with result_fence(fence):
        receipt = publish(storage_utils, row, 'thumbnail', data=jpeg())
        row['thumbnailArtifact'] = json.dumps(dict(receipt, etag='another-version'))
        with pytest.raises(ValueError, match='no longer matches'):
            verify_receipt(storage_utils, row, 'thumbnail', reuse_published=True)
