"""finalize-batch with the browser's real report shape must persist EXIF and GPS."""
import json
from types import SimpleNamespace

import pytest

import app
import storage_utils
from fakes import FakeTable
from routes import upload


@pytest.fixture
def finalize(monkeypatch):
    metadata = FakeTable()
    metadata.upsert_entity({'PartitionKey': 'library', 'RowKey': 'renamed.cr3',
        'processing_metadata': '{}', 'exif_status': 'pending', 'map_detection_status': 'pending'})
    monkeypatch.setitem(storage_utils._CTX, 'metadata_table_client', metadata)
    monkeypatch.setitem(storage_utils._CTX, 'embeddings_table_client', FakeTable())
    monkeypatch.setitem(storage_utils._CTX, 'blob_service_client', object())
    monkeypatch.setitem(storage_utils._CTX, 'queue_map_on_upload', False)
    monkeypatch.setattr(storage_utils, 'touch_user_search_indexes_state', lambda *a, **k: None)
    monkeypatch.setattr(storage_utils, 'download_media_bytes', lambda *a: pytest.fail('Original read'))
    monkeypatch.setattr(storage_utils, '_reverse_geocode_fallback', lambda *a: {'city': 'Test City', 'country': 'Test Country'})
    monkeypatch.setattr(app, '_require_library_context', lambda *a, **k: ('account', 'library', None))
    monkeypatch.setattr(app, '_require_user_id', lambda: ('library', None))
    monkeypatch.setattr(app, '_library_cleanup_block_reason', lambda *_: None)
    for name in ('_mark_fresh_upload_activity', '_mark_upload_activity_heartbeat', '_invalidate_metadata_scan_cache',
                 '_queue_people_clustering_after_face_processing', 'enqueue_index_build', '_queue_upload_processing'):
        monkeypatch.setattr(app, name, lambda *args, **kwargs: None)
    monkeypatch.setattr(app, '_update_metadata_entity_fields', lambda *args, **kwargs: None)
    monkeypatch.setattr(app, '_validate_client_blob_name', lambda value: value)
    monkeypatch.setattr(app, 'blob_service_client', SimpleNamespace(get_blob_client=lambda **kwargs:
        SimpleNamespace(get_blob_properties=lambda: SimpleNamespace(size=100))))
    monkeypatch.setattr(app, 'metadata_table_client', metadata)
    monkeypatch.setattr(app, 'finalize_uploaded_file', lambda *a, **k: ([], 'renamed.cr3'))
    monkeypatch.setattr(app, 'apply_client_processing_results_for_file', storage_utils.apply_client_processing_results_for_file)
    monkeypatch.setattr(upload, 'stamp_candidate', lambda *a: None)
    monkeypatch.setattr(upload, 'record_receipt', lambda *a, **k: None)
    return metadata


def test_finalize_batch_with_browser_exif_report_persists_gps(finalize):
    item = {
        'filename': 'photo.cr3', 'blobName': 'physical', 'uploadId': 'id', 'totalSize': 100, 'sha256': 'a' * 64,
        'clientAssetId': 'ddf07989-d788-4ecb-a8b5-94bf671771a2',
        'clientProcessing': {'exif': {
            'source': 'browser_exiftool', 'verifiedComplete': True,
            'exifToolJson': {'EXIF:Make': 'Test', 'Composite:GPSLatitude': 10.5, 'Composite:GPSLongitude': 20.25},
        }},
        'clientProcessingReport': [{'step': 'exif', 'status': 'done', 'reason': 'done',
                                    'durationMs': 549, 'runtime': 'browser-exiftool-wasm'}],
    }
    with app.app.test_request_context('/upload/finalize-batch', method='POST', json={'files': [item]}):
        response = upload.finalize_upload_batch()
    assert response.status_code == 200
    row = finalize.rows[('library', 'renamed.cr3')]
    assert json.loads(row['exifData'])['Make'] == 'Test'
    assert float(row['latitude']) == pytest.approx(10.5)
    assert row['exif_status'] == 'done'
    assert row['map_detection_status'] == 'done'


def test_finalize_batch_keeps_apples_live_photo_identifier_and_offers_the_photo_for_pairing(finalize, monkeypatch):
    offered = []
    monkeypatch.setattr(app, 'attach_live_photo_after_upload', lambda *args: offered.append(args) or [])
    item = {
        'filename': 'photo.cr3', 'blobName': 'physical', 'uploadId': 'id', 'totalSize': 100, 'sha256': 'a' * 64,
        'clientAssetId': 'ddf07989-d788-4ecb-a8b5-94bf671771a2',
        'clientProcessing': {'exif': {
            'source': 'browser_exiftool', 'verifiedComplete': True,
            'exifToolJson': {'EXIF:Make': 'Apple', 'Apple:ContentIdentifier': 'E0CE855B-1F69-4913-90C3-687C6EF0C359'},
        }},
        'clientProcessingReport': [{'step': 'exif', 'status': 'done', 'reason': 'done',
                                    'durationMs': 549, 'runtime': 'browser-exiftool-wasm'}],
    }
    with app.app.test_request_context('/upload/finalize-batch', method='POST', json={'files': [item]}):
        assert upload.finalize_upload_batch().status_code == 200
    exif = json.loads(finalize.rows[('library', 'renamed.cr3')]['exifData'])
    assert exif['ContentIdentifier'] == 'E0CE855B-1F69-4913-90C3-687C6EF0C359'
    assert [args[:2] for args in offered] == [('library', 'renamed.cr3')]
    assert json.loads(offered[0][2]['exifData'])['ContentIdentifier'] == 'E0CE855B-1F69-4913-90C3-687C6EF0C359'


def test_a_failing_live_photo_pairing_never_fails_the_upload(finalize, monkeypatch):
    def boom(*args):
        raise RuntimeError('table outage')
    monkeypatch.setattr(app, 'attach_live_photo_after_upload', boom)
    item = {'filename': 'photo.cr3', 'blobName': 'physical', 'uploadId': 'id', 'totalSize': 100, 'sha256': 'a' * 64}
    with app.app.test_request_context('/upload/finalize-batch', method='POST', json={'files': [item]}):
        response = upload.finalize_upload_batch()
    assert response.status_code == 200
    assert response.get_json()['results'][0].get('complete') is True
