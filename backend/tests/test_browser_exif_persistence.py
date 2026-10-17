"""A browser-origin authoritative EXIF verdict must persist its fields, not only its status."""
import json

import pytest

import storage_utils
from fakes import FakeTable


@pytest.fixture
def browser_apply(monkeypatch):
    metadata = FakeTable()
    metadata.upsert_entity({'PartitionKey': 'private-user', 'RowKey': 'private.jpg',
        'processing_metadata': '{}', 'processing_lease_owner': 'worker', 'processing_lease': 'worker',
        'exif_status': 'running', 'map_detection_status': 'running'})
    monkeypatch.setitem(storage_utils._CTX, 'metadata_table_client', metadata)
    monkeypatch.setitem(storage_utils._CTX, 'embeddings_table_client', FakeTable())
    monkeypatch.setitem(storage_utils._CTX, 'blob_service_client', object())
    monkeypatch.setitem(storage_utils._CTX, 'queue_map_on_upload', False)
    monkeypatch.setattr(storage_utils, 'touch_user_search_indexes_state', lambda *a, **k: None)
    monkeypatch.setattr(storage_utils, 'download_media_bytes', lambda *a: pytest.fail('Original read'))
    monkeypatch.setattr(storage_utils, '_reverse_geocode_fallback', lambda *a: {'city': 'Test City', 'country': 'Test Country'})
    return metadata


def test_browser_exif_verdict_persists_exif_data_and_gps(browser_apply):
    storage_utils.apply_client_processing_results_for_file(
        'private-user', 'private.jpg', origin='browser', lease_owner='worker',
        client_processing={'exif': {
            'source': 'browser_exiftool', 'verifiedComplete': True,
            'exifToolJson': {'EXIF:Make': 'Test', 'Composite:GPSLatitude': 10.5, 'Composite:GPSLongitude': 20.25},
        }})
    row = browser_apply.rows[('private-user', 'private.jpg')]
    assert row['exif_status'] == 'done'
    assert json.loads(row['exifData'])['Make'] == 'Test'
    assert row['exifCount'] > 0
    assert float(row['latitude']) == pytest.approx(10.5)
    assert float(row['longitude']) == pytest.approx(20.25)
    assert row['processing_lease_owner'] == ''


def test_browser_exif_verdict_with_done_report_item_still_persists(browser_apply):
    storage_utils.apply_client_processing_results_for_file(
        'private-user', 'private.jpg', origin='browser', lease_owner='worker',
        client_processing={'exif': {
            'source': 'browser_exiftool', 'verifiedComplete': True,
            'exifToolJson': {'EXIF:Make': 'Test', 'Composite:GPSLatitude': 10.5, 'Composite:GPSLongitude': 20.25},
        }},
        client_processing_report=[{'step': 'exif', 'status': 'done', 'reason': 'done', 'runtime': 'browser-exiftool-wasm'}])
    row = browser_apply.rows[('private-user', 'private.jpg')]
    assert json.loads(row['exifData'])['Make'] == 'Test'
    assert float(row['latitude']) == pytest.approx(10.5)
    assert row['exif_status'] == 'done'
    from ipwork_geo import geo_exif_from_metadata
    assert geo_exif_from_metadata(dict(row)) is not None
