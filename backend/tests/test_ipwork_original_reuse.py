import json
import logging
import io
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from threading import Barrier

import pytest
from PIL import Image

import app
import ipwork_geo
import ipwork_image
import processing_diagnostics
import storage_utils
from tests.fakes import FakeTable


def source_row(**extra):
    return {'PartitionKey': 'u', 'RowKey': 'photo.raw', 'anonymousImageId': 'physical',
            'fileHash': 'source-hash', 'size': len(b'original'), 'processing_metadata': '{}',
            'exif_status': 'pending', 'map_detection_status': 'pending', **extra}


@pytest.fixture
def worker(monkeypatch):
    table = FakeTable()
    table.upsert_entity(source_row())
    for key, value in {'metadata_table_client': table, 'blob_service_client': object(),
                       'blob_image_container': 'images', 'embeddings_table_client': None,
                       'face_table_client': None, 'search_index_dirty_table_client': None}.items():
        monkeypatch.setitem(storage_utils._CTX, key, value)
    monkeypatch.setattr(app, 'claim_processing_lease', storage_utils.claim_processing_lease)
    monkeypatch.setattr(app, 'apply_client_processing_results_for_file', storage_utils.apply_client_processing_results_for_file)
    monkeypatch.setattr(app, '_get_metadata_entity', lambda u, f: table.get_entity(u, f))
    monkeypatch.setattr(app, '_upsert_job_status', lambda *a, **k: None)
    monkeypatch.setattr(app, 'IPWORK_REUSE_EXIF', True)
    monkeypatch.setattr(app, 'IPWORK_REUSE_METADATA_PACKET', False)
    monkeypatch.setattr(app, 'download_media_bytes', storage_utils.download_media_bytes)
    for step, processor in [('exif', ipwork_geo.process_exif), ('map_detection', ipwork_geo.process_geo)]:
        monkeypatch.setitem(app.IPWORK_STEP_PROCESSORS, step, processor)
    # An unqualified/legacy empty result still exercises the defensive fallback.
    monkeypatch.setitem(app.IPWORK_STEP_PROCESSORS, 'exif', lambda *a: {'hasData': False})
    monkeypatch.setattr(ipwork_geo, 'extract_exif_from_bytes', lambda *a: {})
    monkeypatch.setattr(storage_utils, '_refresh_semantic_fields', lambda *a: None)
    monkeypatch.setattr(storage_utils, '_extract_and_store_embeddings', lambda *a: None)
    monkeypatch.setattr(storage_utils, 'touch_user_search_indexes_state', lambda *a, **k: None)
    downloads, fallbacks = [], []
    monkeypatch.setattr(storage_utils, 'download_file_from_blob',
                        lambda container, blob: downloads.append((container, blob)) or b'original')
    monkeypatch.setattr(storage_utils, '_extract_server_exif_for_processing',
                        lambda data, name: fallbacks.append(data) or {})
    return table, downloads, fallbacks


@pytest.mark.parametrize('force_original', [False, True])
def test_empty_worker_exif_reuses_original_in_real_result_fallback(worker, caplog, force_original):
    caplog.set_level(logging.INFO, logger=processing_diagnostics._LOGGER.name)
    table, downloads, fallbacks = worker
    payload = {'filename': 'photo.raw', 'steps': ['exif', 'map_detection']}
    if force_original:
        payload['imageSource'] = 'original'
    assert app._handle_ipwork_queue_payload(payload, 'job', 'u') == 'done'
    assert downloads == [('images', 'physical')]
    assert fallbacks == [b'original']  # Extraction fallback is preserved; only the duplicate read disappears.
    stored = table.get_entity('u', 'photo.raw')
    assert stored['exif_status'] == 'no_data'
    assert stored['map_detection_status'] == 'no_data'
    assert stored['processing_lease_owner'] == ''
    events = [json.loads(r.getMessage().split('processing telemetry ', 1)[1])
              for r in caplog.records if r.getMessage().startswith('processing telemetry ')]
    assert len([e for e in events if e['event'] == 'media_download']) == 1
    reuse, = [e for e in events if e['event'] == 'media_reuse']
    assert reuse['source'] == 'image' and reuse['purpose'] == 'result_fallback'
    assert reuse['bytes'] == len(b'original')
    assert not any(value in json.dumps(reuse) for value in ('photo.raw', 'physical', 'source-hash'))
    assert ipwork_image.original_input('u', 'photo.raw', stored) is None


def test_original_handoff_is_scoped_nested_and_zero_copy():
    row = source_row()
    data = bytes(bytearray(b'original'))
    ipwork_image.remember_original_input('u', 'photo.raw', row, data)
    assert ipwork_image.original_input('u', 'photo.raw', row) is None
    with pytest.raises(RuntimeError, match='stop'):
        with ipwork_image.original_input_job('u', 'photo.raw'):
            ipwork_image.remember_original_input('u', 'photo.raw', row, data)
            assert ipwork_image.original_input('u', 'photo.raw', row) is data
            with ipwork_image.original_input_job('u', 'other.raw'):
                assert ipwork_image.original_input('u', 'photo.raw', row) is None
            assert ipwork_image.original_input('u', 'photo.raw', row) is data
            raise RuntimeError('stop')
    assert ipwork_image.original_input('u', 'photo.raw', row) is None


def test_copied_context_cannot_retain_job_cache_after_exit():
    row = source_row()
    with ipwork_image.original_input_job('u', 'photo.raw'):
        ipwork_image.remember_original_input('u', 'photo.raw', row, b'original')
        copied = copy_context()
    assert copied.run(ipwork_image.original_input, 'u', 'photo.raw', row) is None


@pytest.mark.parametrize('changed', [
    {'anonymousImageId': 'replacement'}, {'fileHash': 'changed'}, {'size': 9},
    {'PartitionKey': 'other'}, {'RowKey': 'other.raw'},
])
def test_original_handoff_rejects_changed_identity(changed):
    row = source_row()
    with ipwork_image.original_input_job('u', 'photo.raw'):
        ipwork_image.remember_original_input('u', 'photo.raw', row, b'original')
        assert ipwork_image.original_input('u', 'photo.raw', {**row, **changed}) is None
        assert ipwork_image.original_input('other', 'photo.raw', row) is None
        assert ipwork_image.original_input('u', 'other.raw', row) is None


@pytest.mark.parametrize('changed,data', [
    ({'anonymousImageId': '', 'fileHash': ''}, b'original'),
    ({'size': None}, b'original'), ({'size': True}, b'original'),
    ({'size': 0}, b'original'), ({'size': 9}, b'original'),
    ({}, bytearray(b'original')), ({}, b''),
    ({'PartitionKey': 'other'}, b'original'), ({'RowKey': 'other.raw'}, b'original'),
])
def test_original_handoff_requires_immutable_size_matched_source(changed, data):
    row = source_row(**changed)
    with ipwork_image.original_input_job('u', 'photo.raw'):
        ipwork_image.remember_original_input('u', 'photo.raw', row, data)
        assert ipwork_image.original_input('u', 'photo.raw', row) is None


@pytest.mark.parametrize('identity', [{'anonymousImageId': ''}, {'fileHash': ''}])
def test_original_handoff_supports_hash_or_physical_identity(identity):
    row = source_row(**identity)
    with ipwork_image.original_input_job('u', 'photo.raw'):
        ipwork_image.remember_original_input('u', 'photo.raw', row, b'original')
        assert ipwork_image.original_input('u', 'photo.raw', row) == b'original'


def test_concurrent_original_handoffs_do_not_share_bytes():
    barrier = Barrier(2)
    def run(user):
        row = source_row(PartitionKey=user)
        data = user.encode() * row['size']
        with ipwork_image.original_input_job(user, 'photo.raw'):
            ipwork_image.remember_original_input(user, 'photo.raw', row, data)
            barrier.wait(timeout=5)
            assert ipwork_image.original_input(user, 'photo.raw', row) is data
            assert ipwork_image.original_input('other', 'photo.raw', row) is None
            return data
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(run, ['a', 'b'])) == [b'aaaaaaaa', b'bbbbbbbb']


@pytest.mark.parametrize('origin,expected_reads', [('browser', 1), ('ipworker', 0)])
def test_only_worker_apply_can_consume_job_original(worker, origin, expected_reads):
    table, downloads, fallbacks = worker
    row = table.get_entity('u', 'photo.raw')
    with ipwork_image.original_input_job('u', 'photo.raw'):
        ipwork_image.remember_original_input('u', 'photo.raw', row, b'original')
        storage_utils.apply_client_processing_results_for_file('u', 'photo.raw', origin=origin,
            client_processing={'exif': {'hasData': False}}, claimed_steps=['exif'])
    assert len(downloads) == expected_reads
    assert fallbacks == [b'original']


def test_fresh_apply_source_change_does_not_reuse_old_original(worker, monkeypatch):
    table, downloads, fallbacks = worker
    run = app._run_ipwork_steps
    def replace_after_inference(*args, **kwargs):
        result = run(*args, **kwargs)
        table.rows[('u', 'photo.raw')].update(anonymousImageId='replacement', fileHash='new-hash')
        return result
    monkeypatch.setattr(app, '_run_ipwork_steps', replace_after_inference)
    assert app._handle_ipwork_queue_payload({'filename': 'photo.raw', 'steps': ['exif']}, 'job', 'u') == 'done'
    assert downloads == [('images', 'physical'), ('images', 'replacement')]
    assert fallbacks == [b'original']


def test_failed_worker_job_clears_original_handoff(worker, monkeypatch):
    table, downloads, _ = worker
    run = app._run_ipwork_steps
    def delete_after_inference(*args, **kwargs):
        result = run(*args, **kwargs)
        table.rows[('u', 'photo.raw')]['processing_state'] = 'deleted'
        return result
    monkeypatch.setattr(app, '_run_ipwork_steps', delete_after_inference)
    with pytest.raises(RuntimeError, match='deleted'):
        app._handle_ipwork_queue_payload({'filename': 'photo.raw', 'steps': ['exif']}, 'job', 'u')
    assert downloads == [('images', 'physical')]
    assert ipwork_image.original_input('u', 'photo.raw', source_row()) is None


def test_next_delivery_downloads_again_instead_of_cross_job_cache(worker):
    table, downloads, fallbacks = worker
    for job in ('first', 'second'):
        table.upsert_entity(source_row())
        assert app._handle_ipwork_queue_payload({'filename': 'photo.raw', 'steps': ['exif']}, job, 'u') == 'done'
    assert downloads == [('images', 'physical'), ('images', 'physical')]
    assert fallbacks == [b'original', b'original']


def test_preview_only_job_still_downloads_no_original(worker, monkeypatch):
    table, downloads, fallbacks = worker
    table.rows[('u', 'photo.raw')]['preview_status'] = 'done'
    monkeypatch.setitem(storage_utils._CTX, 'blob_thumbnail_container', 'thumbnails')
    monkeypatch.setattr(app, 'IPWORK_REUSE_PREVIEW', True)
    monkeypatch.setattr(app, 'get_media_properties', lambda *a: {'size': len(b'original')})
    monkeypatch.setitem(app.IPWORK_STEP_PROCESSORS, 'ocr', lambda *a: {'hasData': False})
    stream = io.BytesIO()
    Image.new('RGB', (24, 12), 'red').save(stream, 'JPEG')
    preview = stream.getvalue()
    def download(container, blob):
        assert container == 'thumbnails'
        downloads.append((container, blob))
        return preview
    monkeypatch.setattr(storage_utils, 'download_file_from_blob', download)
    assert app._handle_ipwork_queue_payload({'filename': 'photo.raw', 'steps': ['ocr']}, 'job', 'u') == 'done'
    assert downloads == [('thumbnails', app._preview_cache_blob_name('physical'))]
    assert fallbacks == []


def test_background_getter_owns_reference_after_worker_context_exit(worker, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=processing_diagnostics._LOGGER.name)
    table, downloads, _ = worker
    getters = []
    monkeypatch.setattr(storage_utils, '_apply_client_processing_results',
        lambda user, name, row, get_bytes, *a, **k: getters.append(get_bytes))
    data = bytes(bytearray(b'original'))
    with ipwork_image.original_input_job('u', 'photo.raw'):
        ipwork_image.remember_original_input('u', 'photo.raw', table.get_entity('u', 'photo.raw'), data)
        storage_utils.apply_client_processing_results_for_file('u', 'photo.raw', origin='ipworker')
    assert ipwork_image.original_input('u', 'photo.raw', source_row()) is None
    def background():
        with processing_diagnostics.media_download_scope('face_cover', user='u', filename='photo.raw',
                context={'message_id': 'opaque-message', 'batch_id': 'opaque-batch', 'attempt': 1}):
            return getters[0]()
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(background).result(timeout=5) is data
    assert downloads == []
    assert any('"purpose":"face_cover"' in r.getMessage() and '"event":"media_reuse"' in r.getMessage()
               for r in caplog.records)
    assert any('"message_id":"opaque-message"' in r.getMessage() and '"event":"media_reuse"' in r.getMessage()
               for r in caplog.records)
