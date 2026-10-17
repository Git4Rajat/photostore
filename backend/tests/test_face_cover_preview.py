import io
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from PIL import Image, ImageOps

import app
import ipwork_image
import processing_diagnostics as diagnostics
import storage_utils
from tests.fakes import FakeTable


def jpeg(orientation=1):
    image = Image.new('RGB', (200, 100))
    for box, color in [((0, 0, 100, 50), 'red'), ((100, 0, 200, 50), 'lime'),
                       ((0, 50, 100, 100), 'blue'), ((100, 50, 200, 100), 'yellow')]:
        image.paste(color, box)
    stream = io.BytesIO()
    exif = Image.Exif()
    exif[274] = orientation
    image.save(stream, 'JPEG', quality=95, exif=exif)
    return stream.getvalue()


def rows(caplog, event):
    prefix = 'processing telemetry '
    return [value for record in caplog.records if record.getMessage().startswith(prefix)
            and (value := json.loads(record.getMessage()[len(prefix):]))['event'] == event]


def test_crop_input_lifetime_identity_and_immutable_handoff():
    data = jpeg()
    metadata = {'anonymousImageId': 'blob', 'fileHash': 'hash'}
    with ipwork_image.crop_input_job('u', 'photo.cr3'):
        ipwork_image.remember_crop_preview('u', 'photo.cr3', metadata, data, 200, 100)
        source = ipwork_image.crop_preview('u', 'photo.cr3', metadata)
        assert source() is data
        assert ipwork_image.crop_preview('other', 'photo.cr3', metadata) is None
        assert ipwork_image.crop_preview('u', 'other.cr3', metadata) is None
        for changed in ({**metadata, 'fileHash': 'changed'}, {**metadata, 'anonymousImageId': 'changed'}):
            assert ipwork_image.crop_preview('u', 'photo.cr3', changed) is None
        with ipwork_image.crop_input_job('u', 'next.jpg'):
            assert ipwork_image.crop_preview('u', 'photo.cr3', metadata) is None
        assert ipwork_image.crop_preview('u', 'photo.cr3', metadata) is source
    assert ipwork_image.crop_preview('u', 'photo.cr3', metadata) is None
    assert source() is data  # A background consumer remains valid after context cleanup.


@pytest.mark.parametrize('metadata,data,width,height', [
    ({}, jpeg(), 200, 100), ({'fileHash': 'h'}, b'bad', 200, 100),
    ({'fileHash': 'h'}, b'\xff\xd8' + b'0' * 1_500_000, 200, 100),
    ({'fileHash': 'h'}, jpeg(), 2049, 100), ({'fileHash': 'h'}, jpeg(), 200, 0),
])
def test_crop_preview_guards(metadata, data, width, height):
    with ipwork_image.crop_input_job('u', 'a'):
        ipwork_image.remember_crop_preview('u', 'a', metadata, data, width, height)
        assert ipwork_image.crop_preview('u', 'a', metadata) is None


def test_crop_inputs_are_isolated_between_concurrent_jobs():
    barrier = Barrier(2)
    def run(user):
        data = jpeg()
        with ipwork_image.crop_input_job(user, 'same.jpg'):
            metadata = {'anonymousImageId': user}
            ipwork_image.remember_crop_preview(user, 'same.jpg', metadata, data, 200, 100)
            barrier.wait(timeout=5)
            assert ipwork_image.crop_preview(user, 'same.jpg', metadata)().startswith(b'\xff\xd8')
            assert ipwork_image.crop_preview('other', 'same.jpg', metadata) is None
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(run, ['a', 'b']))


@pytest.mark.parametrize('orientation', range(1, 9))
@pytest.mark.parametrize('rotation', [0, 90, 180, 270])
def test_preview_crop_preserves_orientation_and_manual_rotation(monkeypatch, caplog, orientation, rotation):
    caplog.set_level(logging.INFO, logger=diagnostics._LOGGER.name)
    original = jpeg(orientation)
    with Image.open(io.BytesIO(original)) as image:
        normalized = ImageOps.exif_transpose(image).convert('RGB')
        width, height = normalized.size
        stream = io.BytesIO()
        normalized.save(stream, 'JPEG', quality=95)
    preview = stream.getvalue()
    faces = FakeTable()
    bbox = {'left': 20, 'top': 20, 'width': 30, 'height': 30}
    faces.upsert_entity({'PartitionKey': 'u', 'RowKey': 'face', 'bbox': json.dumps(bbox),
                        'imageWidth': width, 'imageHeight': height})
    monkeypatch.setitem(storage_utils._CTX, 'face_table_client', faces)
    monkeypatch.setattr(storage_utils, 'get_media_properties', lambda *a: None)
    uploaded = []
    monkeypatch.setattr(storage_utils, 'upload_media_file', lambda *a: uploaded.append(a[2]))
    # A JPEG preview under a RAW filename must not invoke RAW conversion.
    monkeypatch.setattr('image_utils.convert_image_to_jpeg', lambda *a: pytest.fail('No RAW decode for preview'))
    source = ipwork_image.CropPreview(preview, 'blob', 'hash')
    storage_utils._warm_face_crops_for_photo('u', 'photo.cr3', ['face'], source, rotation)
    expected = storage_utils.crop_face_thumbnail(original, 'photo.jpg', bbox, width, height, rotation)
    assert len(uploaded) == 1
    with Image.open(io.BytesIO(uploaded[0])) as actual, Image.open(io.BytesIO(expected)) as reference:
        assert actual.size == reference.size
        a, b = actual.getpixel((actual.width // 2, actual.height // 2)), reference.getpixel((reference.width // 2, reference.height // 2))
        assert max(abs(x-y) for x, y in zip(a, b)) <= 15
    event, = rows(caplog, 'face_cover_result')
    assert event['source'] == 'preview' and event['generated'] == 1


@pytest.mark.parametrize('failure', [False, True])
def test_common_download_telemetry_records_bytes_errors_and_privacy(monkeypatch, caplog, failure):
    caplog.set_level(logging.INFO, logger=diagnostics._LOGGER.name)
    monkeypatch.setitem(storage_utils._CTX, 'blob_image_container', 'images')
    def download(*args):
        if failure:
            raise RuntimeError('private error with credential')
        return b'12345'
    monkeypatch.setattr(storage_utils, 'download_file_from_blob', download)
    with diagnostics.media_download_scope('face_cover', user='private-user', filename='private.jpg'):
        if failure:
            with pytest.raises(RuntimeError):
                storage_utils.download_media_bytes('image', 'secret-blob')
        else:
            assert storage_utils.download_media_bytes('image', 'secret-blob') == b'12345'
    event, = rows(caplog, 'media_download')
    assert event['purpose'] == 'face_cover' and event['source'] == 'image'
    assert event['bytes'] == (0 if failure else 5)
    assert event['outcome'] == ('error' if failure else 'success')
    assert event['milliseconds'] >= 0
    assert all(value not in json.dumps(event) for value in ['private-user', 'private.jpg', 'secret-blob', 'credential'])
    assert diagnostics.media_download_context() == {}


@pytest.mark.parametrize('origin,changed,expected_preview', [
    ('ipworker', False, True), ('ipworker', True, False), ('browser', False, False),
])
def test_real_result_application_selects_only_matching_worker_preview(monkeypatch, origin, changed, expected_preview):
    data = jpeg()
    metadata = {'PartitionKey': 'u', 'RowKey': 'photo.cr3', 'anonymousImageId': 'blob',
                'fileHash': 'hash', 'processing_metadata': '{}', 'face_status': 'running', 'rotation': 90}
    table = FakeTable()
    monkeypatch.setitem(storage_utils._CTX, 'metadata_table_client', table)
    monkeypatch.setitem(storage_utils._CTX, 'blob_service_client', object())
    for name in ['_refresh_semantic_fields', '_extract_and_store_embeddings', 'touch_user_search_indexes_state',
                 'mark_step_done', 'mark_step_no_data']:
        monkeypatch.setattr(storage_utils, name, lambda *a, **k: None)
    monkeypatch.setattr(storage_utils, '_store_client_face_entities', lambda *a, **k: ['face'])
    selected = []
    monkeypatch.setattr(storage_utils, '_start_face_crop_warming', lambda *a: selected.append(a))
    def original():
        return b'original'
    face = {'embedding': [0.1, 0.2, 0.3], 'confidence': 0.99,
            'bbox': {'left': 10, 'top': 10, 'width': 30, 'height': 30}, 'imageWidth': 200, 'imageHeight': 100}
    with ipwork_image.crop_input_job('u', 'photo.cr3'):
        ipwork_image.remember_crop_preview('u', 'photo.cr3', metadata, data, 200, 100)
        if changed:
            metadata['fileHash'] = 'replacement'
        storage_utils._apply_client_processing_results('u', 'photo.cr3', metadata, original,
            {'face': {'hasData': True, 'faces': [face], 'rawFaceCount': 1}}, None, 'job',
            origin=origin, claimed_steps=['face'])
    assert len(selected) == 1
    source, rotation = selected[0][3:]
    assert isinstance(source, ipwork_image.CropPreview) is expected_preview
    assert rotation == 90
    assert source() == (data if expected_preview else b'original')


@pytest.mark.parametrize('force_original', [False, True])
def test_worker_handoff_and_download_categories(monkeypatch, caplog, force_original):
    caplog.set_level(logging.INFO, logger=diagnostics._LOGGER.name)
    data = jpeg()
    row = {'anonymousImageId': 'blob', 'fileHash': 'hash', 'preview_status': 'done'}
    monkeypatch.setattr(app, 'claim_processing_lease', lambda *a, **k: {'statuses': {'faceStatus': 'pending'}})
    monkeypatch.setattr(app, '_get_metadata_entity', lambda *a: row)
    monkeypatch.setattr(app, 'get_media_properties', lambda *a: {'size': 1_000_000})
    monkeypatch.setattr(app, '_upsert_job_status', lambda *a, **k: None)
    monkeypatch.setattr(app, '_queue_people_clustering_after_face_processing', lambda *a: None)
    monkeypatch.setattr(app, 'IPWORK_REUSE_PREVIEW', True)
    monkeypatch.setattr(app, 'IPWORK_SHARED_DECODE', True)
    monkeypatch.setitem(storage_utils._CTX, 'blob_image_container', 'images')
    monkeypatch.setitem(storage_utils._CTX, 'blob_thumbnail_container', 'thumbnails')
    calls = []
    def download(container, blob):
        calls.append(container)
        return data
    monkeypatch.setattr(storage_utils, 'download_file_from_blob', download)
    monkeypatch.setattr(app, 'download_media_bytes', storage_utils.download_media_bytes)
    monkeypatch.setitem(app.IPWORK_STEP_PROCESSORS, 'face', lambda *a: {'hasData': False, 'faces': []})
    held = []
    def apply(*args, **kwargs):
        held.append(ipwork_image.crop_preview('u', 'photo.cr3', row))
        return {'face_status': 'no_data'}
    monkeypatch.setattr(app, 'apply_client_processing_results_for_file', apply)
    payload = {'filename': 'photo.cr3', 'steps': ['face']}
    if force_original:
        payload['imageSource'] = 'original'
    assert app._handle_ipwork_queue_payload(payload, 'job', 'u') == 'done'
    assert calls == (['images'] if force_original else ['thumbnails'])
    assert (held[0] is not None) is (not force_original)
    if held[0]:
        assert held[0]() == data
    assert ipwork_image.crop_preview('u', 'photo.cr3', row) is None
    event, = rows(caplog, 'media_download')
    assert event['purpose'] == 'inference' and event['bytes'] == len(data)
    assert event['source'] == ('image' if force_original else 'thumbnail')


def test_background_download_keeps_trace_and_purpose(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=diagnostics._LOGGER.name)
    from types import SimpleNamespace
    trace = SimpleNamespace(id='private-message', content='{}', dequeue_count=2)
    threads = []
    import threading
    real_thread = threading.Thread
    def create_thread(**kwargs):
        thread = real_thread(**kwargs)
        threads.append(thread)
        return thread
    monkeypatch.setattr(storage_utils.threading, 'Thread', create_thread)
    monkeypatch.setattr(storage_utils, '_FACE_CROP_WARM_SLOTS', threading.BoundedSemaphore(1))
    def warm(*args):
        diagnostics.media_download('image', 123, 1)
    monkeypatch.setattr(storage_utils, '_warm_face_crops_for_photo', warm)
    with diagnostics.trace(trace), diagnostics.media_download_scope('inference', user='u', filename='photo.jpg'):
        assert storage_utils._start_face_crop_warming('u', 'photo.jpg', ['face'], lambda: b'', 0)
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    event, = rows(caplog, 'media_download')
    assert event['purpose'] == 'face_cover'
    assert event['attempt'] == 2 and event['message_id'] == diagnostics.message_fields(trace)['message_id']
    assert event['file_id'] == diagnostics.file_id('u', 'photo.jpg')


def test_download_diagnostics_failure_does_not_change_download(monkeypatch):
    monkeypatch.setitem(storage_utils._CTX, 'blob_image_container', 'images')
    monkeypatch.setattr(storage_utils, 'download_file_from_blob', lambda *a: b'valid')
    monkeypatch.setattr(diagnostics._LOGGER, 'info', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('logger')))
    assert storage_utils.download_media_bytes('image', 'blob') == b'valid'


@pytest.mark.parametrize('purpose', ['face_cover', 'result_fallback'])
def test_apply_original_callback_preserves_background_purpose(monkeypatch, caplog, purpose):
    caplog.set_level(logging.INFO, logger=diagnostics._LOGGER.name)
    table = FakeTable()
    table.upsert_entity({'PartitionKey': 'u', 'RowKey': 'photo.jpg', 'anonymousImageId': 'blob'})
    monkeypatch.setitem(storage_utils._CTX, 'metadata_table_client', table)
    monkeypatch.setitem(storage_utils._CTX, 'blob_service_client', object())
    monkeypatch.setitem(storage_utils._CTX, 'blob_image_container', 'images')
    monkeypatch.setattr(storage_utils, 'download_file_from_blob', lambda *a: b'original')
    monkeypatch.setattr(storage_utils, 'refresh_metadata_entity', lambda *a: None)
    def apply(user, filename, metadata, get_bytes, *a, **k):
        with diagnostics.media_download_scope(purpose):
            assert get_bytes() == get_bytes() == b'original'
    monkeypatch.setattr(storage_utils, '_apply_client_processing_results', apply)
    storage_utils.apply_client_processing_results_for_file('u', 'photo.jpg')
    event, = rows(caplog, 'media_download')
    assert event['purpose'] == purpose and event['bytes'] == len(b'original')


@pytest.mark.parametrize('failed', [False, True])
def test_metadata_packet_reads_are_accounted_even_when_validation_fails(monkeypatch, caplog, failed):
    import hashlib
    from types import SimpleNamespace
    import upload_metadata
    caplog.set_level(logging.INFO, logger=diagnostics._LOGGER.name)
    packet = b'bad-packet'
    candidate = {'version': 1, 'bytes': len(packet), 'sha256': hashlib.sha256(packet).hexdigest(),
                 'sourceSha256': 'a' * 64, 'physical': 'blob', 'sourceSize': 100, 'etag': 'etag'}
    row = {'uploadMetadataPacket': json.dumps(candidate), 'fileHash': 'a' * 64, 'anonymousImageId': 'blob', 'size': 100}
    def read():
        if failed:
            raise RuntimeError('private storage failure')
        return packet
    storage = SimpleNamespace(_CTX={'blob_thumbnail_container': 'thumbnails'},
        _get_blob_client=lambda *a: SimpleNamespace(download_blob=lambda **k: SimpleNamespace(readall=read)))
    with diagnostics.media_download_scope('inference', user='u', filename='photo.dng'):
        assert upload_metadata.extract_candidate(storage, 'photo.dng', row) is None
    event, = rows(caplog, 'media_download')
    assert event['purpose'] == event['source'] == 'metadata_packet'
    assert event['bytes'] == (0 if failed else len(packet))
    assert event['outcome'] == ('error' if failed else 'success')


def test_background_preview_crop_runs_after_inference_image_is_closed(monkeypatch):
    import threading
    data = jpeg()
    faces = FakeTable()
    faces.upsert_entity({'PartitionKey': 'u', 'RowKey': 'face',
        'bbox': json.dumps({'left': 10, 'top': 10, 'width': 40, 'height': 40}),
        'imageWidth': 200, 'imageHeight': 100})
    monkeypatch.setitem(storage_utils._CTX, 'face_table_client', faces)
    monkeypatch.setattr(storage_utils, 'get_media_properties', lambda *a: None)
    monkeypatch.setattr(storage_utils, 'download_media_bytes', lambda *a: pytest.fail('Reuse must not download'))
    monkeypatch.setattr(storage_utils, '_FACE_CROP_WARM_SLOTS', threading.BoundedSemaphore(1))
    uploaded = []
    monkeypatch.setattr(storage_utils, 'upload_media_file', lambda *a: uploaded.append(a[2]))
    release = threading.Event()
    threads = []
    real_thread = threading.Thread
    real_warm = storage_utils._warm_face_crops_for_photo
    def create_thread(**kwargs):
        thread = real_thread(**kwargs)
        threads.append(thread)
        return thread
    def delayed(*args):
        assert release.wait(timeout=5)
        real_warm(*args)
    monkeypatch.setattr(storage_utils.threading, 'Thread', create_thread)
    monkeypatch.setattr(storage_utils, '_warm_face_crops_for_photo', delayed)
    try:
        with ipwork_image.crop_input_job('u', 'photo.cr3'), ipwork_image.image_job(True):
            with ipwork_image.rgb_image(data) as pixels:
                row = {'anonymousImageId': 'blob'}
                ipwork_image.remember_crop_preview('u', 'photo.cr3', row, data, pixels.width, pixels.height)
                source = ipwork_image.crop_preview('u', 'photo.cr3', row)
                assert storage_utils._start_face_crop_warming('u', 'photo.cr3', ['face'], source, 0)
        with pytest.raises(ValueError):
            pixels.tobytes()
    finally:
        release.set()
        for thread in threads:
            thread.join(timeout=5)
            assert not thread.is_alive()
    assert len(uploaded) == 1
    with Image.open(io.BytesIO(uploaded[0])) as crop:
        assert 0 < crop.width <= 512 and 0 < crop.height <= 512
