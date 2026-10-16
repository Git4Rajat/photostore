"""Regression test for the 2026-09-14 stuck-processing incident: 3 real
uploads on stcontainerapp-dv were left with every step permanently wedged at
'running' because _apply_client_processing_results only persists per-step
status via one final upsert, and nothing resets a step's status if an
exception is thrown before reaching it (release_processing_lease, called by
the caller's error path, only clears lease ownership, not step statuses).
"""
from __future__ import annotations

import pytest
import json

import storage_utils


class _ResourceNotFound(Exception):
    pass


class _FakeMetadataTable:
    def __init__(self) -> None:
        self.rows: dict = {}

    def upsert_entity(self, entity):
        self.rows[(entity['PartitionKey'], entity['RowKey'])] = dict(entity)

    def update_entity(self, entity, mode=None, *, etag=None, match_condition=None):
        key = (entity['PartitionKey'], entity['RowKey'])
        if key not in self.rows:
            raise _ResourceNotFound(f'{key} not found')
        self.rows[key] = dict(entity)

    def get_entity(self, partition_key, row_key):
        key = (partition_key, row_key)
        if key not in self.rows:
            raise _ResourceNotFound(f'{key} not found')
        return dict(self.rows[key])


def _seed_row(metadata: _FakeMetadataTable, user_id: str, filename: str, **overrides) -> None:
    row = {
        'PartitionKey': user_id,
        'RowKey': filename,
        'processing_metadata': '{}',
        **overrides,
    }
    metadata.upsert_entity(row)


@pytest.fixture
def metadata(monkeypatch):
    table = _FakeMetadataTable()
    monkeypatch.setitem(storage_utils._CTX, 'metadata_table_client', table)
    monkeypatch.setitem(storage_utils._CTX, 'blob_service_client', object())
    yield table


def test_exception_mid_apply_unsticks_running_steps_instead_of_wedging_forever(metadata, monkeypatch):
    user_id, filename = 'owner', '_MG_1337.CR3'
    # Mirrors claim_processing_lease's effect right before ipworker runs its
    # steps: every requested step is marked 'running', lease is held.
    _seed_row(
        metadata, user_id, filename,
        preview_status='running', thumbnail_status='done', exif_status='running',
        ocr_status='running', ai_vision_status='running', map_detection_status='running',
        face_status='running',
        processing_lease_owner='ipworker-job-1',
        processing_lease='ipworker-job-1',
        processing_lease_expires_at='2026-09-12T18:46:59+00:00',
    )

    def _boom(*args, **kwargs):
        raise RuntimeError('semantic index rebuild exploded')

    monkeypatch.setattr(storage_utils, '_apply_client_processing_results', _boom)

    with pytest.raises(RuntimeError, match='semantic index rebuild exploded'):
        storage_utils.apply_client_processing_results_for_file(
            user_id, filename,
            client_processing={'exif': {'hasData': True}},
            client_asset_id='ipworker:job-1',
            origin='ipworker',
            claimed_steps=['preview', 'exif', 'ocr', 'ai_vision', 'map_detection', 'face'],
        )

    stored = metadata.get_entity(user_id, filename)
    for step in ('preview', 'exif', 'ocr', 'ai_vision', 'map_detection', 'face'):
        assert stored[f'{step}_status'] == 'failed', f'{step}_status should be retryable, not stuck running'
    # A step this call never claimed responsibility for is left alone.
    assert stored['thumbnail_status'] == 'done'
    # The lease is released too, so a fresh claim can pick this photo back up.
    assert stored['processing_lease_owner'] == ''
    assert stored['processing_lease_expires_at'] == ''


def test_exception_with_no_running_steps_does_not_touch_the_row(metadata, monkeypatch):
    """A crash on a photo where every claimed step already resolved (e.g. the
    exception happened after the real work, in something unrelated) must not
    spuriously flip already-terminal statuses to 'failed'."""
    user_id, filename = 'owner', 'photo.jpg'
    _seed_row(metadata, user_id, filename, exif_status='done', ocr_status='no_data')

    def _boom(*args, **kwargs):
        raise RuntimeError('boom')

    monkeypatch.setattr(storage_utils, '_apply_client_processing_results', _boom)

    with pytest.raises(RuntimeError):
        storage_utils.apply_client_processing_results_for_file(
            user_id, filename,
            client_processing={},
            client_asset_id='ipworker:job-2',
            origin='ipworker',
            claimed_steps=['exif', 'ocr'],
        )

    stored = metadata.get_entity(user_id, filename)
    assert stored['exif_status'] == 'done'
    assert stored['ocr_status'] == 'no_data'


@pytest.mark.parametrize('origin,source,verified,status', [
    ('ipworker', 'browser_metadata_packet', True, 'no_data'),
    ('browser', 'browser_metadata_packet', True, 'done'),
    ('ipworker', 'other', True, 'done'),
    ('ipworker', 'browser_metadata_packet', False, 'done'),
])
def test_verified_absence_persists_dimensions_without_original_fallback(metadata, monkeypatch, origin, source, verified, status):
    user_id, filename = 'owner', 'plain.jpg'
    _seed_row(metadata, user_id, filename, exif_status='running')
    monkeypatch.setattr(storage_utils, 'download_media_bytes', lambda *_: pytest.fail('Unexpected original download'))
    monkeypatch.setattr(storage_utils.maps_utils, 'reverse_geocode', lambda *_: pytest.fail('Unexpected geocoding'))
    result = storage_utils.apply_client_processing_results_for_file(
        user_id, filename, client_processing={'exif': {
            'hasData': True, 'source': source, 'verifiedNoExif': verified,
            'data': {'FileType': 'JPEG', 'ImageWidth': '40', 'ImageHeight': '20'},
        }}, origin=origin, client_asset_id='test',
    )
    stored = metadata.get_entity(user_id, filename)
    assert stored['exif_status'] == status
    assert stored['map_detection_status'] == 'no_data'
    assert json.loads(stored['exifData'])['ImageWidth'] == '40'
    assert result is not None


def test_late_browser_no_data_cannot_redownload_verified_exif_absence(metadata, monkeypatch):
    user_id, filename = 'owner', 'plain.jpg'
    _seed_row(metadata, user_id, filename, exif_status='no_data', exifData='{"ImageWidth":"40"}',
        processing_metadata=json.dumps({'client_exif': {'source': 'ipworker', 'verifiedNoExif': True}}))
    monkeypatch.setattr(storage_utils, 'download_media_bytes', lambda *_: pytest.fail('Late browser result fetched original'))
    storage_utils.apply_client_processing_results_for_file(
        user_id, filename, client_processing={'exif': {'hasData': False}}, origin='browser',
    )
    stored = metadata.get_entity(user_id, filename)
    assert stored['exif_status'] == 'no_data'
    assert json.loads(stored['exifData'])['ImageWidth'] == '40'


@pytest.mark.parametrize('status,source,verified,locked', [
    ('no_data', 'ipworker', True, True), ('pending', 'ipworker', True, False),
    ('no_data', 'browser', True, False), ('no_data', 'ipworker', False, False),
])
def test_verified_absence_lock_requires_backend_provenance_and_allows_forced_repair(status, source, verified, locked):
    row = {'exif_status': status, 'processing_metadata': json.dumps({'client_exif': {
        'source': source, 'verifiedNoExif': verified}})}
    assert storage_utils._step_locked_done(row, 'exif') is locked


@pytest.mark.parametrize('result,status,code', [
    ({'hasData': False}, 'no_data', ''),
    ({'hasData': True, 'text': 'hello'}, 'done', ''),
    ({'hasData': False, 'error': 'engine error'}, 'failed', 'ocr_processing_failed'),
    ({'hasData': False, 'errorCode': 'ocr_decode_failed'}, 'failed', 'ocr_decode_failed'),
    ({'hasData': True, 'text': 'partial', 'error': 'engine error', 'errorCode': 'ocr_engine_failed'}, 'failed', 'ocr_engine_failed'),
    ({'hasData': False, 'errorCode': 'private filename or secret'}, 'failed', 'ocr_processing_failed'),
    ({'hasData': False, 'errorCode': {'untrusted': 'object'}}, 'failed', 'ocr_processing_failed'),
    ({'hasData': True}, 'failed', None),
])
def test_ocr_errors_are_not_verified_empty_results(metadata, monkeypatch, result, status, code):
    _seed_row(metadata, 'owner', 'photo.jpg', ocr_status='running')
    monkeypatch.setattr(storage_utils, 'download_media_bytes', lambda *_: pytest.fail('Unexpected original download'))
    storage_utils.apply_client_processing_results_for_file(
        'owner', 'photo.jpg', client_processing={'ocr': result}, origin='ipworker',
        client_asset_id='test', claimed_steps=['ocr'])
    stored = metadata.get_entity('owner', 'photo.jpg')
    assert stored['ocr_status'] == status
    if code is not None:
        assert json.loads(stored['processing_metadata'])['client_ocr']['errorCode'] == code


def test_late_ocr_error_cannot_overwrite_completed_text(metadata):
    _seed_row(metadata, 'owner', 'photo.jpg', ocr_status='done', ocrText='verified text')
    storage_utils.apply_client_processing_results_for_file(
        'owner', 'photo.jpg', client_processing={'ocr': {'hasData': False, 'error': 'late failure'}},
        origin='ipworker')
    stored = metadata.get_entity('owner', 'photo.jpg')
    assert stored['ocr_status'] == 'done'
    assert stored['ocrText'] == 'verified text'


@pytest.mark.parametrize('result,status,code', [
    ({'hasData': False}, 'no_data', ''),
    ({'hasData': False, 'latitude': None, 'longitude': ''}, 'no_data', ''),
    ({'hasData': False, 'error': 'geocoder failed'}, 'failed', 'geo_processing_failed'),
    ({'hasData': False, 'errorCode': 'secret or URL'}, 'failed', 'geo_processing_failed'),
    ({'hasData': False, 'errorCode': {'untrusted': True}}, 'failed', 'geo_processing_failed'),
    ({'hasData': True, 'latitude': 10, 'longitude': 20, 'error': 'partial'}, 'failed', 'geo_processing_failed'),
    ({'hasData': True}, 'failed', 'geo_invalid_result'),
    ({}, 'failed', 'geo_invalid_result'),
    ({'hasData': False, 'latitude': 10}, 'failed', 'geo_invalid_result'),
    ({'hasData': True, 'latitude': 'NaN', 'longitude': 20}, 'failed', 'geo_invalid_result'),
    ({'hasData': True, 'latitude': 91, 'longitude': 20}, 'failed', 'geo_invalid_result'),
    ({'hasData': True, 'latitude': True, 'longitude': 20}, 'failed', 'geo_invalid_result'),
    ('malformed result', 'failed', 'geo_invalid_result'),
])
def test_geo_failures_are_not_no_gps_results(metadata, monkeypatch, result, status, code):
    _seed_row(metadata, 'owner', 'photo.jpg', map_detection_status='running')
    monkeypatch.setattr(storage_utils, 'download_media_bytes', lambda *_: pytest.fail('Unexpected original download'))
    monkeypatch.setattr(storage_utils.maps_utils, 'reverse_geocode', lambda *_: pytest.fail('Unexpected geocoding'))
    storage_utils.apply_client_processing_results_for_file(
        'owner', 'photo.jpg', client_processing={'map_detection': result}, origin='ipworker',
        client_asset_id='test', claimed_steps=['map_detection'])
    stored = metadata.get_entity('owner', 'photo.jpg')
    assert stored['map_detection_status'] == status
    assert json.loads(stored['processing_metadata'])['client_map_detection']['errorCode'] == code
    assert 'secret or URL' not in stored['processing_metadata']
    assert 'latitude' not in stored and 'longitude' not in stored
    lease = storage_utils.claim_processing_lease('owner', 'photo.jpg', 'retry', steps=['map_detection'])
    assert lease['statuses']['map_detectionStatus'] == ('running' if status == 'failed' else 'no_data')


def test_geo_error_overrides_same_payload_exif_no_gps_status(metadata, monkeypatch):
    _seed_row(metadata, 'owner', 'photo.jpg', exif_status='running', map_detection_status='running')
    monkeypatch.setattr(storage_utils, 'download_media_bytes', lambda *_: pytest.fail('Unexpected original download'))
    storage_utils.apply_client_processing_results_for_file('owner', 'photo.jpg', origin='ipworker', client_processing={
        'exif': {'hasData': True, 'source': 'browser_metadata_packet', 'data': {'FileType': 'JPEG'}},
        'map_detection': {'hasData': False, 'error': 'processing_failed'},
    })
    assert metadata.get_entity('owner', 'photo.jpg')['map_detection_status'] == 'failed'


@pytest.mark.parametrize('fallback', ['no_data', 'unsupported'])
@pytest.mark.parametrize('exif_data', [{}, {'Make': 'Test'}, {
    'GPS.LatitudeDecimal': '10', 'GPS.LongitudeDecimal': '20',
}])
def test_geo_error_survives_server_exif_fallback(metadata, monkeypatch, fallback, exif_data):
    _seed_row(metadata, 'owner', 'photo.jpg', exif_status='running', map_detection_status='running')
    monkeypatch.setitem(storage_utils._CTX, 'queue_map_on_upload', False)
    monkeypatch.setattr(storage_utils.vision_utils, 'encode_text_embedding', lambda *_: [])
    downloads = []
    def download(*_):
        downloads.append(True)
        return b'original'
    monkeypatch.setattr(storage_utils, 'download_media_bytes', download)
    monkeypatch.setattr(storage_utils, '_extract_server_exif_for_processing', lambda *_: exif_data)
    monkeypatch.setattr(storage_utils.maps_utils, 'reverse_geocode', lambda *_: pytest.fail('Duplicate geocoding'))
    payload = {'map_detection': {'hasData': False, 'error': 'processing_failed'}}
    if fallback == 'no_data':
        payload['exif'] = {'hasData': False}
    storage_utils.apply_client_processing_results_for_file(
        'owner', 'photo.jpg', origin='ipworker', client_processing=payload,
        client_processing_report=[{'step': 'exif', 'status': 'unsupported'}] if fallback == 'unsupported' else None,
    )
    stored = metadata.get_entity('owner', 'photo.jpg')
    assert stored['exif_status'] == ('done' if exif_data else 'no_data')
    assert stored['map_detection_status'] == 'failed'
    assert json.loads(stored['processing_metadata'])['client_map_detection']['errorCode'] == 'geo_processing_failed'
    assert len(downloads) == 1  # Only the existing EXIF fallback reads the original.


def test_explicit_geo_success_does_not_trigger_duplicate_exif_geocoding(metadata, monkeypatch):
    _seed_row(metadata, 'owner', 'photo.jpg', exif_status='running', map_detection_status='running')
    monkeypatch.setitem(storage_utils._CTX, 'queue_map_on_upload', False)
    monkeypatch.setattr(storage_utils, 'download_media_bytes', lambda *_: pytest.fail('Unexpected original download'))
    monkeypatch.setattr(storage_utils.maps_utils, 'reverse_geocode', lambda *_: pytest.fail('Duplicate geocoding'))
    storage_utils.apply_client_processing_results_for_file('owner', 'photo.jpg', origin='ipworker', client_processing={
        'exif': {'hasData': True, 'source': 'browser_metadata_packet',
                 'data': {'GPS.LatitudeDecimal': '10', 'GPS.LongitudeDecimal': '20'}},
        'map_detection': {'hasData': True, 'latitude': 10, 'longitude': 20, 'city': 'Ready', 'country': 'Test'},
    })
    stored = metadata.get_entity('owner', 'photo.jpg')
    assert stored['map_detection_status'] == 'done'
    assert stored['locationCity'] == 'Ready'


@pytest.mark.parametrize('result', [{'hasData': False, 'error': 'late failure'}, {}, 'bad'])
def test_late_geo_failure_cannot_replace_completed_location(metadata, monkeypatch, result):
    _seed_row(metadata, 'owner', 'photo.jpg', map_detection_status='done', latitude='10.0', longitude='20.0',
              locationCity='Verified City', locationCountry='Verified Country')
    monkeypatch.setattr(storage_utils, 'download_media_bytes', lambda *_: pytest.fail('Unexpected original download'))
    monkeypatch.setattr(storage_utils.maps_utils, 'reverse_geocode', lambda *_: pytest.fail('Unexpected geocoding'))
    storage_utils.apply_client_processing_results_for_file('owner', 'photo.jpg', origin='ipworker',
        client_processing={'map_detection': result})
    stored = metadata.get_entity('owner', 'photo.jpg')
    assert stored['map_detection_status'] == 'done'
    assert stored['locationCity'] == 'Verified City'
    assert stored['latitude'] == '10.0' and stored['longitude'] == '20.0'


def test_geo_successful_retry_clears_failure_diagnostic(metadata, monkeypatch):
    _seed_row(metadata, 'owner', 'photo.jpg', map_detection_status='running')
    monkeypatch.setattr(storage_utils, 'download_media_bytes', lambda *_: pytest.fail('Unexpected original download'))
    for result in [{'hasData': False, 'error': 'processing_failed'},
                   {'hasData': True, 'latitude': 0, 'longitude': 0, 'city': 'Recovered', 'country': 'Test'}]:
        storage_utils.apply_client_processing_results_for_file('owner', 'photo.jpg', origin='ipworker',
            client_processing={'map_detection': result})
    stored = metadata.get_entity('owner', 'photo.jpg')
    assert stored['map_detection_status'] == 'done'
    assert stored['latitude'] == '0.0' and stored['longitude'] == '0.0'
    assert stored['locationCity'] == 'Recovered'
    assert json.loads(stored['processing_metadata'])['client_map_detection']['errorCode'] == ''


def test_geo_processor_exception_persists_failed_and_retries_from_stored_exif(metadata, monkeypatch):
    import app
    import ipwork_geo
    _seed_row(metadata, 'owner', 'photo.jpg', exif_status='done', map_detection_status='running',
              exifData=json.dumps({'GPS.LatitudeDecimal': '10', 'GPS.LongitudeDecimal': '20'}))
    monkeypatch.setattr(app, 'IPWORK_REUSE_EXIF', True)
    monkeypatch.setitem(app.IPWORK_STEP_PROCESSORS, 'map_detection', ipwork_geo.process_geo)
    monkeypatch.setattr(app, '_get_metadata_entity', lambda user, name: metadata.get_entity(user, name))
    monkeypatch.setattr(app, 'download_media_bytes', lambda *_: pytest.fail('Unexpected original download'))
    monkeypatch.setattr(storage_utils, 'download_media_bytes', lambda *_: pytest.fail('Unexpected original download'))
    calls = []
    def geocode(*_):
        calls.append(True)
        if len(calls) == 1:
            raise RuntimeError('Geocoder unavailable')
        return {'city': 'Recovered', 'country': 'Test'}
    monkeypatch.setattr(ipwork_geo.maps_utils, 'reverse_geocode', geocode)
    result = app._run_ipwork_steps('owner', 'photo.jpg', ['map_detection'])
    assert result['map_detection']['error'] == 'processing_failed'
    storage_utils.apply_client_processing_results_for_file('owner', 'photo.jpg', origin='ipworker',
        client_processing=result, claimed_steps=['map_detection'])
    stored = metadata.get_entity('owner', 'photo.jpg')
    assert stored['map_detection_status'] == 'failed'
    assert json.loads(stored['processing_metadata'])['client_map_detection']['errorCode'] == 'geo_processing_failed'
    lease = storage_utils.claim_processing_lease('owner', 'photo.jpg', 'retry', steps=['map_detection'])
    assert lease['statuses']['map_detectionStatus'] == 'running'
    result = app._run_ipwork_steps('owner', 'photo.jpg', ['map_detection'])
    storage_utils.apply_client_processing_results_for_file('owner', 'photo.jpg', origin='ipworker',
        client_processing=result, claimed_steps=['map_detection'])
    stored = metadata.get_entity('owner', 'photo.jpg')
    assert stored['map_detection_status'] == 'done'
    assert stored['locationCity'] == 'Recovered'
    assert 'map_detection' not in app._ipwork_sweep_eligible_steps(stored)
    assert json.loads(stored['processing_metadata'])['client_map_detection']['errorCode'] == ''
    assert len(calls) == 2
