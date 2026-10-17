import json
import io

import pytest

import app
import ipwork_geo
from PIL import Image


@pytest.fixture
def geo_ctx(monkeypatch):
    calls = []
    monkeypatch.setattr(app, 'IPWORK_REUSE_EXIF', True)
    monkeypatch.setattr(ipwork_geo.maps_utils, 'reverse_geocode', lambda lat, lon: {
        'city': 'Test City', 'region': 'Test Region', 'country': 'Test Country'})
    monkeypatch.setitem(app.IPWORK_STEP_PROCESSORS, 'map_detection', ipwork_geo.process_geo)
    monkeypatch.setattr(app, 'download_media_bytes', lambda kind, name: calls.append((kind, name)) or b'original')
    monkeypatch.setattr(ipwork_geo, 'extract_exif_from_bytes', lambda *_: {
        'GPS.LatitudeDecimal': '55.6', 'GPS.LongitudeDecimal': '13.0'})
    return calls


def test_completed_exif_avoids_original_download(monkeypatch, geo_ctx):
    monkeypatch.setattr(app, '_get_metadata_entity', lambda *_: {
        'exif_status': 'done', 'exifData': json.dumps({'Model': 'Camera', 'GPS.LatitudeDecimal': '55.6', 'GPS.LongitudeDecimal': '13.0'}),
        'latitude': '55.6', 'longitude': '13.0'})
    result = app._run_ipwork_steps('library', 'photo.CR3', ['map_detection'])
    assert result['map_detection']['latitude'] == 55.6
    assert result['map_detection']['city'] == 'Test City'
    assert geo_ctx == []


def test_same_job_exif_is_not_extracted_twice(monkeypatch, geo_ctx):
    monkeypatch.setattr(app, '_get_metadata_entity', lambda *_: {'anonymousImageId': 'physical'})
    reads = []
    monkeypatch.setattr(ipwork_geo, 'extract_exif_from_bytes', lambda *_: reads.append(True) or {
        'GPS.LatitudeDecimal': '55.6', 'GPS.LongitudeDecimal': '13.0'})
    monkeypatch.setitem(app.IPWORK_STEP_PROCESSORS, 'exif', ipwork_geo.process_exif)
    result = app._run_ipwork_steps('library', 'photo.CR3', ['exif', 'map_detection'])
    assert result['map_detection']['hasData'] is True
    assert reads == [True]
    assert geo_ctx == [('image', 'physical')]


@pytest.mark.parametrize('metadata', [
    {'exif_status': 'done', 'exifData': '{}'},
    {'exif_status': 'no_data'},
])
def test_confirmed_absent_gps_needs_no_image(monkeypatch, geo_ctx, metadata):
    monkeypatch.setattr(app, '_get_metadata_entity', lambda *_: metadata)
    assert app._run_ipwork_steps('library', 'photo.CR3', ['map_detection'])['map_detection'] == {'hasData': False}
    assert geo_ctx == []


@pytest.mark.parametrize('metadata', [
    {'exif_status': 'pending'},
    {'exif_status': 'failed'},
    {'exif_status': 'done', 'exifData': 'broken-json'},
    {'exif_status': 'done', 'exifData': '[]'},
    {'exif_status': 'done', 'latitude': 'NaN', 'longitude': '13'},
    {'exif_status': 'done', 'latitude': '91', 'longitude': '13'},
    {'exif_status': 'done', 'latitude': '55.6'},
    {'exif_status': 'done', 'latitude': '55.6', 'longitude': '13'},
    {'exif_status': 'done', 'exifData': '{}', 'latitude': '55.6', 'longitude': '13'},
    {'exif_status': 'done', 'exifData': {'GPS.LatitudeDecimal': 'NaN', 'GPS.LongitudeDecimal': '13'}},
    {'exif_status': 'done', 'exifData': {'GPS.GPSLatitude': 'invalid', 'GPS.GPSLongitude': '13'}},
    {'exif_status': 'done', 'exifData': {'GPS.LatitudeDecimal': '55.6', 'GPS.LongitudeDecimal': '13'}, 'latitude': '10', 'longitude': '20'},
])
def test_unusable_metadata_keeps_original_fallback(monkeypatch, geo_ctx, metadata):
    monkeypatch.setattr(app, '_get_metadata_entity', lambda *_: metadata)
    result = app._run_ipwork_steps('library', 'photo.CR3', ['map_detection'])
    assert result['map_detection']['hasData'] is True
    assert geo_ctx == [('image', 'photo.CR3')]


def test_zero_coordinates_are_valid(monkeypatch):
    monkeypatch.setattr(ipwork_geo.maps_utils, 'reverse_geocode', lambda *_: {})
    result = ipwork_geo.process_geo_from_metadata({'exif_status': 'done', 'latitude': 0, 'longitude': 0,
                                                  'exifData': {'GPS.LatitudeDecimal': '0', 'GPS.LongitudeDecimal': '0'}})
    assert result == {'hasData': True, 'latitude': 0.0, 'longitude': 0.0, 'address': '', 'city': '', 'region': '', 'country': ''}


def test_disabled_reuse_preserves_original_path(monkeypatch, geo_ctx):
    monkeypatch.setattr(app, 'IPWORK_REUSE_EXIF', False)
    monkeypatch.setattr(app, '_get_metadata_entity', lambda *_: {'exif_status': 'done', 'latitude': '55.6', 'longitude': '13'})
    assert app._run_ipwork_steps('library', 'photo.CR3', ['map_detection'])['map_detection']['hasData'] is True
    assert geo_ctx == [('image', 'photo.CR3')]


def test_preview_and_completed_exif_avoid_original_download(monkeypatch, geo_ctx):
    stream = io.BytesIO()
    Image.new('RGB', (40, 20), 'red').save(stream, 'JPEG')
    preview = stream.getvalue()
    monkeypatch.setattr(app, 'IPWORK_REUSE_PREVIEW', True)
    monkeypatch.setattr(app, '_get_metadata_entity', lambda *_: {
        'anonymousImageId': 'physical', 'preview_status': 'done', 'exif_status': 'done',
        'latitude': '55.6', 'longitude': '13',
        'exifData': {'GPS.LatitudeDecimal': '55.6', 'GPS.LongitudeDecimal': '13'}})
    monkeypatch.setattr(app, 'get_media_properties', lambda *_: {'size': 10_000_000})
    monkeypatch.setattr(app, 'download_media_bytes', lambda kind, name: geo_ctx.append((kind, name)) or preview)
    monkeypatch.setitem(app.IPWORK_STEP_PROCESSORS, 'ocr', lambda _, __, data: {'hasData': True, 'received': data})
    result = app._run_ipwork_steps('library', 'photo.CR3', ['ocr', 'map_detection'])
    assert result['ocr']['received'] == preview
    assert result['map_detection']['hasData'] is True
    assert geo_ctx == [('thumbnail', app._preview_cache_blob_name('physical'))]


def test_packet_exif_and_geo_do_not_fetch_original(monkeypatch, geo_ctx):
    import upload_metadata
    monkeypatch.setattr(app, 'IPWORK_REUSE_METADATA_PACKET', True)
    monkeypatch.setitem(app.IPWORK_STEP_PROCESSORS, 'exif', ipwork_geo.process_exif)
    monkeypatch.setattr(app, '_get_metadata_entity', lambda *_: {'uploadMetadataPacket': 'candidate'})
    monkeypatch.setattr(upload_metadata, 'extract_candidate', lambda *_, **kwargs: {
        'Model': 'Camera', 'GPS.LatitudeDecimal': '55.6', 'GPS.LongitudeDecimal': '13'})
    result = app._run_ipwork_steps('library', 'photo.CR3', ['exif', 'map_detection'])
    assert result['exif']['source'] == 'browser_metadata_packet'
    assert result['map_detection']['city'] == 'Test City'
    assert geo_ctx == []


def test_verified_exif_absence_packet_skips_original_and_geocoding(monkeypatch, geo_ctx):
    import upload_metadata
    monkeypatch.setattr(app, 'IPWORK_REUSE_METADATA_PACKET', True)
    monkeypatch.setitem(app.IPWORK_STEP_PROCESSORS, 'exif', ipwork_geo.process_exif)
    monkeypatch.setattr(app, '_get_metadata_entity', lambda *_: {'uploadMetadataPacket': 'candidate'})
    def extract(*_, **kwargs):
        kwargs['on_outcome']('reused_no_exif')
        return {'FileType': 'JPEG', 'ImageWidth': '40', 'ImageHeight': '20'}
    monkeypatch.setattr(upload_metadata, 'extract_candidate', extract)
    monkeypatch.setattr(ipwork_geo.maps_utils, 'reverse_geocode', lambda *_: pytest.fail('Absent GPS was geocoded'))
    result = app._run_ipwork_steps('library', 'plain.jpg', ['exif', 'map_detection'])
    assert result['exif']['verifiedNoExif'] is True
    assert result['exif']['data']['ImageWidth'] == '40'
    assert result['map_detection'] == {'hasData': False}
    assert geo_ctx == []


@pytest.mark.parametrize('filename,filetype', [('profile.jpg', 'JPEG'), ('profile.webp', 'Extended WEBP')])
def test_non_camera_packet_metadata_skips_original_and_geocoding_without_claiming_exif_absence(monkeypatch, geo_ctx, filename, filetype):
    import upload_metadata
    monkeypatch.setattr(app, 'IPWORK_REUSE_METADATA_PACKET', True)
    monkeypatch.setitem(app.IPWORK_STEP_PROCESSORS, 'exif', ipwork_geo.process_exif)
    monkeypatch.setattr(app, '_get_metadata_entity', lambda *_: {'uploadMetadataPacket': 'candidate'})
    def extract(*_, **kwargs):
        kwargs['on_outcome']('reused')
        return {'FileType': filetype, 'ImageWidth': '40', 'ImageHeight': '20', 'Software': 'Editor'}
    monkeypatch.setattr(upload_metadata, 'extract_candidate', extract)
    monkeypatch.setattr(ipwork_geo.maps_utils, 'reverse_geocode', lambda *_: pytest.fail('Absent GPS was geocoded'))
    result = app._run_ipwork_steps('library', filename, ['exif', 'map_detection'])
    assert result['exif']['data']['Software'] == 'Editor'
    assert 'verifiedNoExif' not in result['exif']
    assert result['map_detection'] == {'hasData': False}
    assert geo_ctx == []


def test_packet_fallback_does_not_make_visual_processors_use_original(monkeypatch, geo_ctx):
    import upload_metadata
    monkeypatch.setattr(app, 'IPWORK_REUSE_METADATA_PACKET', True)
    monkeypatch.setattr(app, 'IPWORK_REUSE_PREVIEW', True)
    monkeypatch.setitem(app.IPWORK_STEP_PROCESSORS, 'exif', ipwork_geo.process_exif)
    monkeypatch.setattr(app, '_get_metadata_entity', lambda *_: {
        'anonymousImageId': 'physical', 'uploadMetadataPacket': 'bad', 'preview_status': 'done'})
    monkeypatch.setattr(upload_metadata, 'extract_candidate', lambda *_, **kwargs: None)
    stream = io.BytesIO()
    Image.new('RGB', (40, 20), 'red').save(stream, 'JPEG')
    preview = stream.getvalue()
    monkeypatch.setattr(app, 'get_media_properties', lambda *_: {'size': 10_000_000})
    monkeypatch.setattr(app, 'download_media_bytes', lambda kind, name: geo_ctx.append((kind, name)) or (preview if kind == 'thumbnail' else b'original'))
    monkeypatch.setitem(app.IPWORK_STEP_PROCESSORS, 'ocr', lambda _, __, data: {'hasData': True, 'received': data})
    result = app._run_ipwork_steps('library', 'photo.CR3', ['exif', 'ocr', 'map_detection'])
    assert result['ocr']['received'] == preview
    assert result['map_detection']['hasData'] is True
    assert geo_ctx == [('image', 'physical'), ('thumbnail', app._preview_cache_blob_name('physical'))]


def test_explicit_original_policy_bypasses_all_reuse(monkeypatch, geo_ctx):
    import upload_metadata
    monkeypatch.setattr(app, 'IPWORK_REUSE_METADATA_PACKET', True)
    monkeypatch.setattr(app, 'IPWORK_REUSE_PREVIEW', True)
    monkeypatch.setitem(app.IPWORK_STEP_PROCESSORS, 'exif', ipwork_geo.process_exif)
    monkeypatch.setattr(app, '_get_metadata_entity', lambda *_: {
        'anonymousImageId': 'physical', 'uploadMetadataPacket': 'candidate', 'preview_status': 'done'})
    monkeypatch.setattr(upload_metadata, 'extract_candidate', lambda *_, **kwargs: pytest.fail('Packet reused during repair'))
    monkeypatch.setitem(app.IPWORK_STEP_PROCESSORS, 'ocr', lambda _, __, data: {'hasData': True, 'received': data})
    result = app._run_ipwork_steps('library', 'photo.CR3', ['exif', 'ocr', 'map_detection'], use_original=True)
    assert result['ocr']['received'] == b'original'
    assert result['map_detection']['hasData'] is True
    assert geo_ctx == [('image', 'physical')]
