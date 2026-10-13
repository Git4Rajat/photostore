from types import SimpleNamespace

import pytest

import app
from routes import upload


@pytest.fixture
def context(monkeypatch):
    events = []
    monkeypatch.setattr(app, '_require_library_context', lambda *args, **kwargs: ('account', 'library', None))
    monkeypatch.setattr(app, '_require_user_id', lambda: ('library', None))
    monkeypatch.setattr(app, '_library_cleanup_block_reason', lambda *_: None)
    for name in ('_mark_fresh_upload_activity', '_mark_upload_activity_heartbeat', '_invalidate_metadata_scan_cache',
                 '_queue_people_clustering_after_face_processing', 'enqueue_index_build'):
        monkeypatch.setattr(app, name, lambda *args, **kwargs: None)
    monkeypatch.setattr(app, '_update_metadata_entity_fields', lambda *args: events.append('stamp'))
    monkeypatch.setattr(app, '_validate_client_blob_name', lambda value: value)
    monkeypatch.setattr(app, 'blob_service_client', SimpleNamespace(get_blob_client=lambda **kwargs:
        SimpleNamespace(get_blob_properties=lambda: SimpleNamespace(size=100))))
    monkeypatch.setattr(app, 'metadata_table_client', SimpleNamespace(get_entity=lambda **kwargs: {}))
    monkeypatch.setattr(app, 'finalize_uploaded_file', lambda *args, **kwargs: (events.append('original') or [], 'renamed.cr3'))
    monkeypatch.setattr(app, 'apply_client_processing_results_for_file', lambda *args, **kwargs: events.append('assets') or {})
    monkeypatch.setattr(upload, 'stamp_candidate', lambda *args: events.append(('packet', args[2], args[4])))
    monkeypatch.setattr(app, '_queue_upload_processing', lambda *args: events.append('queue'))
    return events


@pytest.mark.parametrize('batch', [False, True])
def test_assets_and_candidate_are_applied_before_queue_even_after_rename(context, batch):
    item = {'filename': 'photo.cr3', 'blobName': 'physical', 'uploadId': 'id', 'totalSize': 100, 'sha256': 'a' * 64,
            'clientProcessing': {'preview': {'alreadyUploaded': True}}, 'clientMetadata': {'version': 1}}
    with app.app.test_request_context('/upload/finalize-batch' if batch else '/upload/finalize', method='POST',
                                     json={'files': [item]} if batch else item):
        response = upload.finalize_upload_batch() if batch else upload.finalize_direct_upload()
    assert response.status_code == 200
    assert context.index('original') < context.index('assets') < context.index(('packet', 'renamed.cr3', 'physical')) < context.index('queue')


@pytest.mark.parametrize('batch', [False, True])
def test_bad_packet_does_not_fail_original_finalization(context, monkeypatch, batch):
    monkeypatch.setattr(upload, 'stamp_candidate', lambda *args: (_ for _ in ()).throw(ValueError('bad packet')))
    item = {'filename': 'photo.cr3', 'blobName': 'physical', 'totalSize': 100, 'clientMetadata': {'bad': True}}
    with app.app.test_request_context('/upload/finalize-batch' if batch else '/upload/finalize', method='POST',
                                     json={'files': [item]} if batch else item):
        response = upload.finalize_upload_batch() if batch else upload.finalize_direct_upload()
    assert response.status_code == 200
    assert 'queue' in context


@pytest.mark.parametrize('enabled', [False, True])
@pytest.mark.parametrize('batch', [False, True])
def test_metadata_sas_is_capability_gated(context, monkeypatch, enabled, batch):
    monkeypatch.setenv('BROWSER_UPLOAD_PREPARATION', str(enabled).lower())
    monkeypatch.setattr(app, '_cleanup_failed_upload', lambda *_: None)
    monkeypatch.setattr(app, 'reset_upload_tracking_and_reserve_blob', lambda *args, **kwargs: 'physical')
    monkeypatch.setattr(app, 'reset_upload_tracking_and_reserve_blobs_batch', lambda *args: {'photo.cr3': 'physical'})
    monkeypatch.setattr(app, '_create_direct_upload_blob_url', lambda name: ('original-url', 'expiry'))
    urls = []
    monkeypatch.setattr(app, '_create_direct_thumbnail_upload_blob_url', lambda name: (urls.append(name) or 'url-' + name, 'expiry'))
    item = {'filename': 'photo.cr3', 'totalSize': 100, 'sha256': 'a' * 64, 'directToBlob': True, 'includePreview': True, 'includeMetadata': True}
    with app.app.test_request_context('/upload/init-batch' if batch else '/upload/init', method='POST',
                                     json={'files': [item]} if batch else item):
        response = upload.init_upload_batch() if batch else upload.init_upload()
    value = response.get_json()['results'][0] if batch else response.get_json()
    assert value['uploadPreparationEnabled'] is enabled
    assert value['metadataBlobUrl'] == ('url-metadata/physical.bin' if enabled else None)
    assert ('metadata/physical.bin' in urls) is enabled
