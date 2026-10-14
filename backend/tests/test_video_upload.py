import hashlib
import json
from types import SimpleNamespace

import pytest

import app
import storage_utils
from image_utils import VIDEO_EXTENSIONS
from routes import photos, upload
from upload_metadata import qualified_family, uploads_enabled
from video_metadata import decode_video_metadata, apply_browser_video_metadata, MAX_VIDEO_SOURCE_BYTES


def envelope(**changes):
    value = {'version': 1, 'kind': 'video-mediainfo-json', 'parser': 'mediainfo.js@0.3.8',
        'sourceSize': 1234, 'sourceSha256': 'a' * 64, 'tracks': [
            {'@type': 'General', 'Format': 'MPEG-4', 'Encoded_Date': '2024-01-02 03:04:05 UTC',
             'extra': {'com.apple.quicktime.location.ISO6709': '+00.0000+013.0000/'}},
            {'@type': 'Video', 'Width': 1920, 'Height': 1080, 'Duration': 2, 'FrameRate': 30, 'Rotation': 90}]}
    value.update(changes)
    packet = json.dumps(value).encode()
    return packet, {'version': 1, 'bytes': len(packet), 'sha256': hashlib.sha256(packet).hexdigest(), 'sourceSha256': 'a' * 64}


def test_video_metadata_maps_dimensions_date_and_zero_gps_without_losing_raw_fields():
    packet, descriptor = envelope()
    tags = decode_video_metadata(packet, descriptor, 1234)
    assert tags['ImageWidth'] == '1920' and tags['ImageLength'] == '1080'
    assert tags['GPS.LatitudeDecimal'] == '0.0' and tags['GPS.LongitudeDecimal'] == '13.0'
    assert tags['CreateDate'] == '2024-01-02 03:04:05 UTC'
    assert tags['Rotation'] == '90'
    assert tags['MediaInfo.Video.1.FrameRate'] == '30'
    assert tags['MediaInfo.General.0.extra.com.apple.quicktime.location.ISO6709'] == '+00.0000+013.0000/'


@pytest.mark.parametrize('changes', [
    {'version': True}, {'parser': 'unknown'}, {'sourceSize': 1235}, {'sourceSize': True},
    {'sourceSha256': 'b' * 64}, {'kind': 'tiff-ifd-ranges'}, {'tracks': []}, {'tracks': [{}]}, {'readBudgetReached': 'false'},
    {'tracks': [{'@type': 'General', 'Format': 'Audio'}]},
    {'tracks': [{'@type': 'Video', 'Width': float('nan')}]},
])
def test_invalid_video_packet_is_rejected(changes):
    packet, descriptor = envelope(**changes)
    with pytest.raises(ValueError):
        decode_video_metadata(packet, descriptor, 1234)


def test_video_packet_checksum_and_source_limit():
    packet, descriptor = envelope()
    with pytest.raises(ValueError):
        decode_video_metadata(packet + b' ', descriptor, 1234)
    packet, descriptor = envelope(sourceSize=MAX_VIDEO_SOURCE_BYTES + 1)
    with pytest.raises(ValueError):
        decode_video_metadata(packet, descriptor, MAX_VIDEO_SOURCE_BYTES + 1)


def test_bounded_video_metadata_remains_explicitly_marked_partial():
    packet, descriptor = envelope(readBudgetReached=True)
    assert decode_video_metadata(packet, descriptor, 1234)['MetadataReadBudgetReached'] == 'true'


def test_mediainfo_nested_conformance_details_are_bounded_but_not_discarded():
    nested = 'detail'
    for _ in range(9):
        nested = {'item': nested}
    packet, descriptor = envelope(tracks=[{'@type': 'General', 'Format': 'WebM', 'extra': nested}, {'@type': 'Video', 'Width': 120, 'Height': 68}])
    assert any(value == 'detail' for value in decode_video_metadata(packet, descriptor, 1234).values())
    for _ in range(4):
        nested = {'item': nested}
    packet, descriptor = envelope(tracks=[{'@type': 'General', 'Format': 'WebM', 'extra': nested}, {'@type': 'Video'}])
    with pytest.raises(ValueError, match='field limit'):
        decode_video_metadata(packet, descriptor, 1234)


@pytest.mark.parametrize('location,expected', [
    ('55.6000\u00b0N 13.0000\u00b0E', ('55.6', '13.0')),
    ('0.0000\u00b0S 0.0000\u00b0W', ('-0.0', '-0.0')),
    ('55.6000\u00b0N 13.0000\u00b0E 1025.858m', ('55.6', '13.0')),
    ('55.6000\u00b0S 13.0000\u00b0W -12.5m', ('-55.6', '-13.0')),
    ('90\u00b0N 180\u00b0E +0m', ('90.0', '180.0')),
    ('91\u00b0N 13\u00b0E 10m', None),
    ('55.6\u00b0N 13\u00b0E NaNm', None),
    ('55.6\u00b0N 13\u00b0E 10m trailing', None),
    ('91.0000\u00b0N 181.0000\u00b0E', None), ('invalid', None),
])
def test_mediainfo_recorded_location_and_camera_fields(location, expected):
    packet, descriptor = envelope(tracks=[{'@type': 'General', 'Format': 'MPEG-4', 'Recorded_Location': location,
        'Encoded_Hardware_CompanyName': 'Apple', 'Encoded_Hardware_Name': 'iPhone 15'}, {'@type': 'Video', 'Width': 100, 'Height': 200}])
    tags = decode_video_metadata(packet, descriptor, 1234)
    assert tags['Make'] == 'Apple' and tags['Model'] == 'iPhone 15'
    if expected:
        assert (tags['GPS.LatitudeDecimal'], tags['GPS.LongitudeDecimal']) == expected
    else:
        assert 'GPS.LatitudeDecimal' not in tags


@pytest.mark.parametrize('extension', sorted(VIDEO_EXTENSIONS))
def test_video_capability_does_not_depend_on_image_feature_flag(monkeypatch, extension):
    monkeypatch.setenv('BROWSER_UPLOAD_PREPARATION', 'false')
    assert uploads_enabled('clip.' + extension)
    assert qualified_family('clip.' + extension)
    assert not uploads_enabled('photo.jpg')


@pytest.mark.parametrize('with_altitude', [False, True])
def test_video_packet_reads_only_etag_bound_sidecar_and_applies_browser_provenance(monkeypatch, with_altitude):
    changes = {'tracks': [
        {'@type': 'General', 'Format': 'MPEG-4', 'Recorded_Location': '55.6000\u00b0N 13.0000\u00b0E 1025.858m'},
        {'@type': 'Video', 'Width': 100, 'Height': 200},
    ]} if with_altitude else {}
    packet, descriptor = envelope(**changes)
    candidate = {**descriptor, 'sourceSize': 1234, 'physical': 'physical', 'etag': 'etag-1'}
    row = {'fileHash': 'a' * 64, 'size': 1234, 'anonymousImageId': 'physical', 'uploadMetadataPacket': json.dumps(candidate)}
    reads, applies = [], []
    def download(**kwargs):
        reads.append(kwargs)
        return SimpleNamespace(readall=lambda: packet)
    monkeypatch.setitem(storage_utils._CTX, 'blob_thumbnail_container', 'thumbnails')
    monkeypatch.setattr(storage_utils, '_get_blob_client', lambda container, name:
        SimpleNamespace(download_blob=download) if (container, name) == ('thumbnails', 'metadata/physical.bin') else pytest.fail('Original read'))
    fake = SimpleNamespace(metadata_table_client=SimpleNamespace(get_entity=lambda **_: row),
        apply_client_processing_results_for_file=lambda *args, **kwargs: applies.append((args, kwargs)) or {'exif_status': 'done'})
    assert apply_browser_video_metadata(fake, 'library', 'clip.mov', 'asset') == {'exif_status': 'done'}
    assert reads[0]['length'] == len(packet) and reads[0]['etag'] == 'etag-1'
    assert applies[0][1]['origin'] == 'browser_video_metadata'
    assert applies[0][1]['client_processing']['map_detection'] == {
        'hasData': True, 'latitude': '55.6' if with_altitude else '0.0', 'longitude': '13.0',
    }
    row['anonymousImageId'] = 'replacement'
    with pytest.raises(ValueError):
        apply_browser_video_metadata(fake, 'library', 'clip.mov', 'asset')
    assert len(reads) == 1


def test_video_finalization_uses_hash_without_downloading_or_decoding(monkeypatch):
    rows = []
    monkeypatch.setattr(storage_utils, '_require_context', lambda: None)
    monkeypatch.setitem(storage_utils._CTX, 'metadata_table_client', SimpleNamespace(upsert_entity=lambda row: rows.append(dict(row))))
    monkeypatch.setattr(storage_utils, 'download_media_bytes', lambda *_: pytest.fail('Video original download'))
    monkeypatch.setattr(storage_utils, '_create_server_thumbnail_for_upload', lambda *_: pytest.fail('Backend thumbnail'))
    monkeypatch.setattr(storage_utils, '_finalize_server_side_exif', lambda *_, **__: pytest.fail('Backend EXIF'))
    monkeypatch.setattr(storage_utils, 'detect_duplicates', lambda *_, **__: [])
    monkeypatch.setattr(storage_utils, '_resolve_filename_for_upload', lambda _user, filename, _hash: filename)
    monkeypatch.setattr(storage_utils, 'get_or_create_metadata', lambda *_: {'PartitionKey': 'library', 'RowKey': 'clip.mp4'})
    for name in ('touch_user_search_indexes_state', '_store_hash_index', '_store_filename_owner'):
        monkeypatch.setattr(storage_utils, name, lambda *_, **__: None)
    assert storage_utils.finalize_uploaded_file('library', 'clip.mp4', 'video/mp4', client_sha256='a' * 64) == ([], 'clip.mp4')
    assert rows[0]['videoBrowserOnly'] is True and rows[0]['processing_complete'] is True
    assert rows[0]['thumbnail_status'] == 'failed' and rows[0]['exif_status'] == 'failed'
    with pytest.raises(ValueError, match='SHA-256'):
        storage_utils.finalize_uploaded_file('library', 'clip.mp4', 'video/mp4')


@pytest.mark.parametrize('extension', ['mp4', 'crm', 'nev', 'braw', 'r3d', 'mxf'])
def test_video_worker_and_forced_repair_never_queue_or_download_original(monkeypatch, extension):
    filename = 'clip.' + extension
    monkeypatch.setattr(app, 'download_media_bytes', lambda *_: pytest.fail('Video original download'))
    monkeypatch.setattr(app, '_get_metadata_entity', lambda *_: pytest.fail('Video work should stop before metadata lookup'))
    assert app._queue_ipwork_processing('library', filename, use_original=True)['reason'] == 'video_browser_only'
    assert app._enqueue_preview_generation_job('library', filename)['reason'] == 'video_browser_only'
    assert app._enqueue_processing_steps('library', filename, ['thumbnail', 'exif'], force=True)['exif']['reason'] == 'video_browser_only'
    result = app._run_ipwork_steps('library', filename, ['thumbnail', 'exif', 'map_detection'], use_original=True)
    assert all(row['error'] == 'video_browser_only' for row in result.values())
    row = {'RowKey': filename, 'thumbnail_status': 'pending', 'exif_status': 'failed'}
    assert app._ipwork_sweep_eligible_steps(row) == []
    assert app._browser_processing_pending_item(row) is None
    jobs = []
    monkeypatch.setattr(app, '_upsert_job_status', lambda *args: jobs.append(args))
    monkeypatch.setattr(app, 'claim_processing_lease', lambda *_, **__: pytest.fail('Stale video job must not claim lease'))
    assert app._handle_ipwork_queue_payload({'filename': filename, 'steps': ['thumbnail']}, 'old-job', 'library') == 'noop'
    assert jobs == [('old-job', 'library', 'ipwork', 'done')]


def test_rejected_video_thumbnail_is_terminal_and_never_calls_fallback(monkeypatch):
    import benchmark_upload
    updates = []
    monkeypatch.setitem(storage_utils._CTX, 'metadata_table_client', SimpleNamespace(upsert_entity=lambda _: None))
    monkeypatch.setattr(benchmark_upload, 'verify_jpeg', lambda *_, **__: (_ for _ in ()).throw(ValueError('Direct derivative byte limit')))
    monkeypatch.setattr(storage_utils, '_update_metadata_fields', lambda *args: updates.append(args[-1]))
    monkeypatch.setattr(storage_utils, 'touch_user_search_indexes_state', lambda *_, **__: None)
    row = {'processing_metadata': '{}', 'thumbnail_status': 'failed'}
    storage_utils._apply_client_processing_results('library', 'clip.mp4', row,
        lambda: pytest.fail('Original download'), {'thumbnail': {'alreadyUploaded': True}}, [], 'asset')
    assert row['thumbnail_status'] == 'failed'


def test_video_metadata_info_does_not_download_original_if_dimensions_missing(monkeypatch):
    monkeypatch.setattr(app, '_require_user_id', lambda: ('library', None))
    monkeypatch.setattr(app, '_get_metadata_entity', lambda *_: {'RowKey': 'clip.mp4'})
    monkeypatch.setattr(app, 'download_media_bytes', lambda *_: pytest.fail('Video info fallback'))
    with app.app.test_request_context('/api/photos/clip.mp4/metadata'):
        assert app.app.make_response(photos.get_photo_metadata('clip.mp4')).status_code == 200


@pytest.mark.parametrize('batch', [False, True])
def test_video_finalization_reports_missing_hash_without_fallback(monkeypatch, batch):
    monkeypatch.setattr(app, '_require_library_context', lambda: ('account', 'library', None))
    monkeypatch.setattr(app, '_library_cleanup_block_reason', lambda *_: None)
    monkeypatch.setattr(app, '_mark_fresh_upload_activity', lambda *_: None)
    monkeypatch.setattr(app, '_mark_upload_activity_heartbeat', lambda *_: None)
    monkeypatch.setattr(app, 'finalize_uploaded_file', lambda *_, **__: pytest.fail('Missing hash'))
    item = {'filename': 'clip.mp4', 'totalSize': 1234}
    with app.app.test_request_context('/upload/finalize-batch' if batch else '/upload/finalize', method='POST',
                                     json={'files': [item]} if batch else item):
        response = upload.finalize_upload_batch() if batch else upload.finalize_direct_upload()
    if batch:
        assert response.get_json()['results'][0]['error'] == 'Video uploads require a client SHA-256'
    else:
        assert response[1] == 400
