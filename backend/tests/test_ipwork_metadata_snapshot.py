import io
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from PIL import Image

import app
import ipwork_geo
import processing_diagnostics
import storage_utils
from tests.fakes import FakeTable


@pytest.fixture
def inputs(monkeypatch):
    stream = io.BytesIO()
    Image.new('RGB', (40, 20), 'red').save(stream, 'JPEG')
    data = stream.getvalue()
    monkeypatch.setattr(app, 'IPWORK_REUSE_PREVIEW', True)
    monkeypatch.setattr(app, 'get_media_properties', lambda *a: {'size': 1000000})
    calls = []
    monkeypatch.setattr(app, 'download_media_bytes', lambda kind, name: calls.append((kind, name)) or data)
    monkeypatch.setitem(app.IPWORK_STEP_PROCESSORS, 'ocr', lambda *a: {'hasData': False})
    return calls


def snapshot(user='u', filename='photo.jpg', **extra):
    return {'PartitionKey': user, 'RowKey': filename, 'anonymousImageId': 'physical',
            'fileHash': 'hash', 'preview_status': 'done', **extra}


def events(caplog):
    prefix = 'processing telemetry '
    return [value for row in caplog.records if row.getMessage().startswith(prefix)
            and (value := json.loads(row.getMessage()[len(prefix):]))['event'] == 'vision_metadata_read']


def test_snapshot_avoids_input_get_and_is_not_mutated(monkeypatch, inputs, caplog):
    caplog.set_level(logging.INFO, logger=processing_diagnostics._LOGGER.name)
    row = snapshot()
    before = row.copy()
    monkeypatch.setattr(app, '_get_metadata_entity', lambda *a: pytest.fail('No second input read'))
    assert app._run_ipwork_steps('u', 'photo.jpg', ['ocr'], metadata_snapshot=row) == {'ocr': {'hasData': False}}
    assert row == before
    assert inputs == [('thumbnail', app._preview_cache_blob_name('physical'))]
    event, = events(caplog)
    assert event['source'] == 'lease_snapshot' and event['reads'] == 0


@pytest.mark.parametrize('row', [None, {}, [], snapshot(user='other'), snapshot(filename='other.jpg'),
    snapshot(processing_state='deleted'), snapshot(processingIsolated=True)])
def test_missing_or_mismatched_snapshot_keeps_one_input_read(monkeypatch, inputs, caplog, row):
    caplog.set_level(logging.INFO, logger=processing_diagnostics._LOGGER.name)
    reads = []
    monkeypatch.setattr(app, '_get_metadata_entity', lambda *a: reads.append(a) or snapshot())
    monkeypatch.setitem(app.IPWORK_STEP_PROCESSORS, 'face', lambda *a: {'hasData': False, 'faces': []})
    app._run_ipwork_steps('u', 'photo.jpg', ['ocr', 'face'], metadata_snapshot=row)
    assert reads == [('u', 'photo.jpg')]
    assert len(inputs) == 1
    event, = events(caplog)
    assert event['source'] == 'table' and event['reads'] == 1


def test_snapshot_supplies_stored_exif_without_image_or_table_read(monkeypatch, inputs):
    monkeypatch.setattr(app, 'IPWORK_REUSE_EXIF', True)
    monkeypatch.setitem(app.IPWORK_STEP_PROCESSORS, 'map_detection', ipwork_geo.process_geo)
    monkeypatch.setattr(app, '_get_metadata_entity', lambda *a: pytest.fail('No metadata GET'))
    row = snapshot(exif_status='no_data')
    assert app._run_ipwork_steps('u', 'photo.jpg', ['map_detection'], metadata_snapshot=row) == {
        'map_detection': {'hasData': False}}
    assert inputs == []


def test_packet_qualification_receives_the_claim_snapshot_without_table_read(monkeypatch, inputs):
    import upload_metadata
    monkeypatch.setattr(app, 'IPWORK_REUSE_METADATA_PACKET', True)
    monkeypatch.setitem(app.IPWORK_STEP_PROCESSORS, 'exif', ipwork_geo.process_exif)
    monkeypatch.setattr(app, '_get_metadata_entity', lambda *a: pytest.fail('No metadata GET'))
    row = snapshot(uploadMetadataPacket='packet-reference', size=12345)
    def extract(storage, filename, metadata, **kwargs):
        assert metadata == row and metadata is not row
        return {'Make': 'Camera'}
    monkeypatch.setattr(upload_metadata, 'extract_candidate', extract)
    result = app._run_ipwork_steps('u', 'photo.jpg', ['exif'], metadata_snapshot=row)
    assert result['exif']['data'] == {'Make': 'Camera'}
    assert app._ipwork_metrics_context.source_bytes == 12345
    assert inputs == []


def test_direct_calls_without_snapshot_do_not_reuse_previous_job(monkeypatch, inputs):
    reads = []
    monkeypatch.setattr(app, '_get_metadata_entity', lambda *a: reads.append(a) or snapshot(anonymousImageId='fresh'))
    app._run_ipwork_steps('u', 'photo.jpg', ['ocr'], metadata_snapshot=snapshot())
    app._run_ipwork_steps('u', 'photo.jpg', ['ocr'])
    assert reads == [('u', 'photo.jpg')]
    assert inputs[-1] == ('thumbnail', app._preview_cache_blob_name('fresh'))


def test_inference_uses_a_copy_not_a_mutating_caller_row(monkeypatch, inputs):
    row = snapshot()
    def ocr(*args):
        row['anonymousImageId'] = 'changed'
        return {'hasData': False}
    monkeypatch.setitem(app.IPWORK_STEP_PROCESSORS, 'ocr', ocr)
    monkeypatch.setitem(app.IPWORK_STEP_PROCESSORS, 'exif', lambda *a: {'hasData': False})
    monkeypatch.setattr(app, '_get_metadata_entity', lambda *a: pytest.fail('No metadata GET'))
    app._run_ipwork_steps('u', 'photo.jpg', ['ocr', 'exif'], metadata_snapshot=row)
    assert inputs == [('thumbnail', app._preview_cache_blob_name('physical')), ('image', 'physical')]


def test_queue_payload_cannot_supply_the_reused_snapshot(monkeypatch, inputs):
    reads = []
    monkeypatch.setattr(app, 'claim_processing_lease', lambda *a, **k: {'statuses': {}})
    monkeypatch.setattr(app, '_get_metadata_entity', lambda *a: reads.append(a) or snapshot())
    monkeypatch.setattr(app, '_upsert_job_status', lambda *a, **k: None)
    monkeypatch.setattr(app, 'apply_client_processing_results_for_file', lambda *a, **k: {})
    assert app._handle_ipwork_queue_payload({'filename': 'photo.jpg', 'steps': ['ocr'],
        'metadata_snapshot': snapshot(anonymousImageId='untrusted')}, 'job', 'u') == 'done'
    assert reads == [('u', 'photo.jpg')]
    assert inputs == [('thumbnail', app._preview_cache_blob_name('physical'))]


def test_parallel_jobs_do_not_share_metadata(monkeypatch):
    barrier = Barrier(2)
    monkeypatch.setattr(app, 'IPWORK_REUSE_PREVIEW', False)
    monkeypatch.setattr(app, '_get_metadata_entity', lambda *a: pytest.fail('No metadata GET'))
    def download(kind, name):
        barrier.wait(timeout=5)
        return name.encode()
    monkeypatch.setattr(app, 'download_media_bytes', download)
    monkeypatch.setitem(app.IPWORK_STEP_PROCESSORS, 'ocr', lambda u, f, data: {'hasData': True, 'text': data.decode()})
    def run(user):
        return app._run_ipwork_steps(user, 'same.jpg', ['ocr'], metadata_snapshot=snapshot(
            user=user, filename='same.jpg', anonymousImageId=user))['ocr']['text']
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(run, ['a', 'b'])) == ['a', 'b']


class ReadCountingTable(FakeTable):
    def __init__(self):
        super().__init__()
        self.reads = 0

    def get_entity(self, *a, **k):
        self.reads += 1
        return super().get_entity(*a, **k)


@pytest.mark.parametrize('force_original', [False, True])
@pytest.mark.parametrize('concurrent_change', ['rating', 'deleted'])
@pytest.mark.parametrize('provide_snapshot', [False, True])
def test_real_lease_input_reuse_keeps_fresh_apply_read(monkeypatch, inputs, force_original, concurrent_change, provide_snapshot):
    table = ReadCountingTable()
    table.upsert_entity(snapshot(ocr_status='pending', processing_metadata='{}', rating=1))
    monkeypatch.setitem(storage_utils._CTX, 'metadata_table_client', table)
    monkeypatch.setitem(storage_utils._CTX, 'blob_service_client', object())
    def claim(*a, **k):
        lease = storage_utils.claim_processing_lease(*a, **k)
        if not provide_snapshot:
            lease.pop('entity')
        return lease
    monkeypatch.setattr(app, 'claim_processing_lease', claim)
    monkeypatch.setattr(app, 'release_processing_lease', lambda *a, **k: None)
    monkeypatch.setattr(app, '_upsert_job_status', lambda *a, **k: None)
    monkeypatch.setattr(app, 'metadata_table_client', table)
    # Keep the real application entry point's fresh get/deletion guard; isolate
    # downstream persistence already covered by its race/recovery tests.
    monkeypatch.setattr(app, 'apply_client_processing_results_for_file', storage_utils.apply_client_processing_results_for_file)
    monkeypatch.setattr(storage_utils, '_apply_client_processing_results', lambda *a, **k: None)
    monkeypatch.setattr(storage_utils, 'refresh_metadata_entity', lambda *a, **k: None)
    def process(*args):
        assert table.reads == (1 if provide_snapshot else 2)
        table.rows[('u', 'photo.jpg')].update(
            {'rating': 5} if concurrent_change == 'rating' else {'processing_state': 'deleted'})
        return {'hasData': False}
    monkeypatch.setitem(app.IPWORK_STEP_PROCESSORS, 'ocr', process)
    payload = {'filename': 'photo.jpg', 'steps': ['ocr']}
    if force_original:
        payload['imageSource'] = 'original'
    if concurrent_change == 'deleted':
        with pytest.raises(RuntimeError, match='deleted'):
            app._handle_ipwork_queue_payload(payload, 'job', 'u')
    else:
        assert app._handle_ipwork_queue_payload(payload, 'job', 'u') == 'done'
        assert table.rows[('u', 'photo.jpg')]['rating'] == 5
    # Snapshot reuse removes exactly the input read, not the fresh apply read.
    assert table.reads == (2 if provide_snapshot else 3)
    assert inputs[0][0] == ('image' if force_original else 'thumbnail')
