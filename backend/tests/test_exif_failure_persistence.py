import json

import pytest

import storage_utils
from tests.fakes import FakeTable


@pytest.fixture
def metadata(monkeypatch):
    table = FakeTable()
    table.upsert_entity({'PartitionKey': 'owner', 'RowKey': 'photo.raw',
        'exif_status': 'running', 'map_detection_status': 'running', 'processing_metadata': '{}'})
    monkeypatch.setitem(storage_utils._CTX, 'metadata_table_client', table)
    monkeypatch.setitem(storage_utils._CTX, 'blob_service_client', object())
    monkeypatch.setattr(storage_utils, '_refresh_semantic_fields', lambda *_: None)
    monkeypatch.setattr(storage_utils, '_extract_and_store_embeddings', lambda *_: None)
    monkeypatch.setattr(storage_utils, 'touch_user_search_indexes_state', lambda *_, **__: None)
    return table


@pytest.mark.parametrize('code', ['exif_timeout', 'exif_parser_error', 'exif_unavailable', 'exif_unsupported', 'exif_storage_limit',
    'exif_execution_failed', 'exif_invalid_output', 'exif_read_failed', 'private parser output', {'bad': 'value'}])
def test_worker_extraction_failure_never_retries_in_apply_or_becomes_absence(metadata, monkeypatch, code):
    monkeypatch.setattr(storage_utils, 'download_media_bytes', lambda *_: pytest.fail('Repeated original read'))
    monkeypatch.setattr(storage_utils, '_extract_server_exif_for_processing', lambda *_: pytest.fail('Repeated extraction'))
    storage_utils.apply_client_processing_results_for_file('owner', 'photo.raw', origin='ipworker',
        client_processing={'exif': {'hasData': False, 'error': 'exif_extraction_failed', 'errorCode': code}})
    stored = metadata.get_entity('owner', 'photo.raw')
    assert stored['exif_status'] == stored['map_detection_status'] == 'failed'
    expected = code if isinstance(code, str) and code.startswith('exif_') else 'exif_processing_failed'
    assert json.loads(stored['processing_metadata'])['client_exif']['errorCode'] == expected
    assert 'private parser output' not in stored['processing_metadata']


@pytest.mark.parametrize('origin', ['ipworker', 'browser'])
def test_partial_result_with_error_cannot_be_marked_done(metadata, monkeypatch, origin):
    monkeypatch.setattr(storage_utils, 'download_media_bytes', lambda *_: pytest.fail('Unexpected read'))
    storage_utils.apply_client_processing_results_for_file('owner', 'photo.raw', origin=origin,
        client_processing={'exif': {'hasData': True, 'data': {'Make': 'Partial'}, 'error': 'failed'}})
    assert metadata.get_entity('owner', 'photo.raw')['exif_status'] == 'failed'


@pytest.mark.parametrize('apply_geo', [True, False])
def test_server_fallback_exception_is_failed_not_no_data(monkeypatch, apply_geo):
    def fail(*_):
        raise RuntimeError('private parser content')
    monkeypatch.setattr(storage_utils, '_extract_server_exif_for_processing', fail)
    monkeypatch.setattr(storage_utils, 'mark_step_no_data', lambda *_: pytest.fail('Unverified absence'))
    monkeypatch.setattr(storage_utils.maps_utils, 'reverse_geocode', lambda *_: pytest.fail('Unverified GPS'))
    row = {'exifData': '{"Make":"previous"}', 'exifCount': 1}
    result = storage_utils._apply_server_exif_fallback('owner', 'photo.raw', row, b'original',
        fallback_for='browser_unsupported', apply_geo=apply_geo)
    assert result == {'exif_status': 'failed', **({'map_detection_status': 'failed'} if apply_geo else {})}
    assert json.loads(row['exifData']) == {'Make': 'previous'}
    assert json.loads(row['processing_metadata'])['server_exif']['errorCode'] == 'exif_processing_failed'
    assert 'private parser content' not in row['processing_metadata']


def test_stored_failure_is_retryable(metadata, monkeypatch):
    storage_utils.apply_client_processing_results_for_file('owner', 'photo.raw', origin='ipworker',
        client_processing={'exif': {'hasData': False, 'errorCode': 'exif_parser_error'}})
    lease = storage_utils.claim_processing_lease('owner', 'photo.raw', 'retry', steps=['exif'])
    assert lease['statuses']['exifStatus'] == 'running'


def test_worker_geo_does_not_parse_again_after_same_job_exif_failure(monkeypatch):
    import app
    import ipwork_geo
    from exif_utils import ExifExtractionError
    parses, downloads = [], []
    def extract(*_):
        parses.append(True)
        raise ExifExtractionError('parser_error')
    monkeypatch.setattr(app, 'IPWORK_REUSE_EXIF', True)
    monkeypatch.setattr(app, '_get_metadata_entity', lambda *_: {'exif_status': 'running'})
    monkeypatch.setattr(app, 'download_media_bytes', lambda *_: downloads.append(True) or b'original')
    monkeypatch.setitem(app.IPWORK_STEP_PROCESSORS, 'exif', ipwork_geo.process_exif)
    monkeypatch.setitem(app.IPWORK_STEP_PROCESSORS, 'map_detection', ipwork_geo.process_geo)
    monkeypatch.setattr(ipwork_geo, 'extract_exif_from_bytes', extract)
    monkeypatch.setattr(ipwork_geo.maps_utils, 'reverse_geocode', lambda *_: pytest.fail('Unverified GPS'))
    result = app._run_ipwork_steps('owner', 'photo.raw', ['exif', 'map_detection'])
    assert result['exif']['errorCode'] == 'exif_parser_error'
    assert result['map_detection']['error'] == 'exif_dependency_failed'
    assert len(parses) == len(downloads) == 1


@pytest.mark.parametrize('server_fallback', [True, False])
def test_exif_failure_cannot_downgrade_completed_geo(metadata, monkeypatch, server_fallback):
    row = metadata.get_entity('owner', 'photo.raw')
    row.update(map_detection_status='done', latitude='10', longitude='20')
    metadata.upsert_entity(row)
    if server_fallback:
        from exif_utils import ExifExtractionError
        def fail(*_):
            raise ExifExtractionError('timeout')
        monkeypatch.setattr(storage_utils, '_extract_server_exif_for_processing', fail)
        updates = storage_utils._apply_server_exif_fallback('owner', 'photo.raw', row, b'original', fallback_for='test')
        assert updates == {'exif_status': 'failed'}
    else:
        storage_utils.apply_client_processing_results_for_file('owner', 'photo.raw', origin='ipworker',
            client_processing={'exif': {'hasData': False, 'errorCode': 'exif_timeout'}})
        stored = metadata.get_entity('owner', 'photo.raw')
        assert stored['exif_status'] == 'failed'
        assert stored['map_detection_status'] == 'done'


@pytest.mark.parametrize('exif', [
    {'GPSLatitude': '10'}, {'GPSLatitude': 'broken', 'GPSLongitude': '20'},
    {'GPS.LatitudeDecimal': 'NaN', 'GPS.LongitudeDecimal': '20'},
])
def test_server_fallback_malformed_gps_is_failed_not_absent(monkeypatch, exif):
    monkeypatch.setattr(storage_utils, '_extract_server_exif_for_processing', lambda *_: exif)
    monkeypatch.setattr(storage_utils, 'mark_step_no_data', lambda *_: pytest.fail('Unverified absence'))
    monkeypatch.setattr(storage_utils.maps_utils, 'reverse_geocode', lambda *_: pytest.fail('Invalid GPS'))
    row = {}
    updates = storage_utils._apply_server_exif_fallback('owner', 'photo.raw', row, b'original', fallback_for='test')
    assert updates == {'exif_status': 'done', 'map_detection_status': 'failed'}
    assert json.loads(row['exifData']) == exif
    assert 'latitude' not in row and 'longitude' not in row


@pytest.mark.parametrize('origin', ['ipworker', 'browser'])
def test_only_trusted_worker_empty_result_can_skip_reextraction(metadata, monkeypatch, origin):
    reads = []
    monkeypatch.setattr(storage_utils, 'download_media_bytes', lambda *_: reads.append(True) or b'original')
    monkeypatch.setattr(storage_utils, '_extract_server_exif_for_processing', lambda *_: {})
    storage_utils.apply_client_processing_results_for_file('owner', 'photo.raw', origin=origin,
        client_processing={'exif': {'hasData': False, 'source': 'server_metadata', 'verifiedNoExif': True}})
    stored = metadata.get_entity('owner', 'photo.raw')
    assert stored['exif_status'] == stored['map_detection_status'] == 'no_data'
    assert len(reads) == (0 if origin == 'ipworker' else 1)


def test_real_empty_worker_result_parses_only_once_through_saving(metadata, monkeypatch):
    import app
    import ipwork_geo
    import io
    from PIL import Image
    stream = io.BytesIO()
    Image.new('RGB', (40, 20), 'red').save(stream, 'JPEG')
    data = stream.getvalue()
    downloads, parses = [], []
    extract = ipwork_geo.extract_exif_from_bytes
    monkeypatch.setattr(app, '_get_metadata_entity', lambda *_: metadata.get_entity('owner', 'photo.raw'))
    monkeypatch.setattr(app, 'download_media_bytes', lambda *_: downloads.append(True) or data)
    monkeypatch.setattr(app, 'IPWORK_REUSE_EXIF', True)
    monkeypatch.setattr(ipwork_geo, 'extract_exif_from_bytes', lambda *a: parses.append(True) or extract(*a))
    monkeypatch.setitem(app.IPWORK_STEP_PROCESSORS, 'exif', ipwork_geo.process_exif)
    monkeypatch.setitem(app.IPWORK_STEP_PROCESSORS, 'map_detection', ipwork_geo.process_geo)
    monkeypatch.setattr(storage_utils, 'download_media_bytes', lambda *_: pytest.fail('Second original read'))
    monkeypatch.setattr(storage_utils, '_extract_server_exif_for_processing', lambda *_: pytest.fail('Second extraction'))
    payload = app._run_ipwork_steps('owner', 'photo.raw', ['exif', 'map_detection'])
    storage_utils.apply_client_processing_results_for_file('owner', 'photo.raw', origin='ipworker', client_processing=payload)
    assert len(parses) == len(downloads) == 1
    assert metadata.get_entity('owner', 'photo.raw')['map_detection_status'] == 'no_data'


@pytest.mark.parametrize('data', [
    {'GPS.LatitudeDecimal': 'NaN', 'GPS.LongitudeDecimal': '20'},
    {'GPSLatitude': '10'}, {'GPSLatitude': '1 75 2', 'GPSLongitude': '20'},
])
def test_packet_gps_validation_without_explicit_geo_never_claims_absence(metadata, monkeypatch, data):
    monkeypatch.setattr(storage_utils, 'download_media_bytes', lambda *_: pytest.fail('Unexpected original'))
    monkeypatch.setattr(storage_utils.maps_utils, 'reverse_geocode', lambda *_: pytest.fail('Invalid GPS'))
    storage_utils.apply_client_processing_results_for_file('owner', 'photo.raw', origin='ipworker',
        client_processing={'exif': {'hasData': True, 'source': 'browser_metadata_packet', 'data': data}})
    row = metadata.get_entity('owner', 'photo.raw')
    assert row['exif_status'] == 'done'
    assert row['map_detection_status'] == 'failed'
    assert 'latitude' not in row and 'longitude' not in row


def test_native_worker_metadata_does_not_lose_late_keys_to_the_browser_limit(metadata, monkeypatch):
    data = {f'Field{i}': str(i) for i in range(250)}
    data.update({'GPS.LatitudeDecimal': '0', 'GPS.LongitudeDecimal': '0'})
    monkeypatch.setattr(storage_utils, 'download_media_bytes', lambda *_: pytest.fail('Unexpected original'))
    storage_utils.apply_client_processing_results_for_file('owner', 'photo.raw', origin='ipworker',
        client_processing={'exif': {'hasData': True, 'data': data},
                           'map_detection': {'hasData': False, 'error': 'deferred'}})
    row = metadata.get_entity('owner', 'photo.raw')
    assert json.loads(row['exifData'])['Field249'] == '249'
    assert float(json.loads(row['exifData'])['GPS.LatitudeDecimal']) == 0.0


@pytest.mark.parametrize('data,expected', [
    ({'GPS.LatitudeDecimal': 0, 'GPS.LongitudeDecimal': 0}, (0., 0.)),
    ({'GPSLatitude': 0, 'GPSLongitude': 0}, (0., 0.)),
    ({'GPSLatitude': ((55, 1), (36, 1), (0, 1)), 'GPSLongitude': 13}, (55.6, 13.)),
    ({'GPSLatitude': '1 75 2', 'GPSLongitude': '20'}, None),
    ({'GPSLatitude': 10, 'GPSLongitude': 20, 'GPSLatitudeRef': 'X'}, None),
    ({'GPS.LatitudeDecimal': True, 'GPS.LongitudeDecimal': False}, None),
])
def test_shared_gps_validation_numeric_zero_dms_and_references(data, expected):
    from ipwork_geo import geo_coordinates_from_exif
    if expected is None:
        with pytest.raises(ValueError):
            geo_coordinates_from_exif(data)
    else:
        assert geo_coordinates_from_exif(data) == expected


def test_truncated_metadata_is_a_storage_limit_failure(monkeypatch):
    import ipwork_geo
    monkeypatch.setattr(ipwork_geo, 'extract_exif_from_bytes', lambda *_: {'Make': 'Partial', 'EXIF.Truncated': 'true'})
    assert ipwork_geo.process_exif('owner', 'photo.raw', b'original')['errorCode'] == 'exif_storage_limit'
