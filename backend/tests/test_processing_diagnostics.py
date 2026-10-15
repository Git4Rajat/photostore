"""Telemetry is bounded, privacy-preserving and never authoritative work state."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import logging
from types import SimpleNamespace
import uuid

import pytest

import processing_diagnostics as d


def records(caplog, event=None):
    prefix = 'processing telemetry '
    rows = [json.loads(r.getMessage()[len(prefix):]) for r in caplog.records
            if r.getMessage().startswith(prefix)]
    return [row for row in rows if event is None or row['event'] == event]


@pytest.fixture(autouse=True)
def telemetry(caplog):
    caplog.set_level(logging.INFO, logger=d._LOGGER.name)
    yield
    assert not d._WORKERS


def test_ids_are_opaque_and_untrusted_batch_strings_rejected(caplog):
    batch = str(uuid.uuid4())
    assert d.batch_id(batch) == batch.replace('-', '')
    for invalid in ('private.jpg', 'https://host/?sig=secret', 'x' * 10000, None, {}, []):
        assert d.batch_id(invalid) is None
    assert d.file_id('library', 'private.jpg') == d.file_id('library', 'private.jpg')
    assert d.file_id('other', 'private.jpg') != d.file_id('library', 'private.jpg')
    d.upload_event('library', 'private.jpg', {'uploadBatchId': batch}, size=42, duplicate=False, video=False)
    row, = records(caplog)
    assert row['batch_id'] == batch.replace('-', '') and row['bytes'] == 42
    assert 'private.jpg' not in json.dumps(row) and '"library"' not in json.dumps(row)


def test_emitter_ignores_payloads_nonfinite_and_urls_and_survives_logger_failure(monkeypatch, caplog):
    d.emit('test', payload={'secret': 'token'}, url='https://host/?sig=secret', nan=float('nan'), value=4)
    row, = records(caplog)
    assert not {'payload', 'url', 'nan'} & row.keys()
    assert row['value'] == 4
    monkeypatch.setattr(d._LOGGER, 'info', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('broken logger')))
    d.upload_event('u', 'private.mov', {}, size=10, duplicate=False, video=True)
    d.message_event('test', SimpleNamespace(content='bad'), role='vision')


def test_queue_age_and_redelivery_are_distinct_from_productive_results(caplog):
    message = SimpleNamespace(id='secret-queue-id', content=json.dumps({'user_id': 'private-user',
        'filename': 'private.mov', 'uploadBatchId': str(uuid.uuid4())}), dequeue_count=3,
        insertion_time=datetime.now(timezone.utc) - timedelta(seconds=25))
    d.message_event('vision_received', message, role='vision')
    row, = records(caplog)
    assert row['attempt'] == 3 and 25 <= row['enqueue_age_seconds'] < 27
    assert len(row['message_id']) == 32
    assert all(secret not in json.dumps(row) for secret in ('private.mov', 'private-user', 'secret-queue-id'))
    d.message_event('test', SimpleNamespace(content='[1]'), role='cluster')
    d.message_event('test', SimpleNamespace(content='invalid'), role='cluster')
    assert len(records(caplog)) == 1


def test_batch_context_is_thread_local_and_reset_on_error():
    batches = [str(uuid.uuid4()) for _ in range(8)]
    def run(value):
        def queue(user, filename):
            assert d.current_batch() == value.replace('-', '')
            raise ValueError('business failure')
        with pytest.raises(ValueError, match='business failure'):
            d.queued_call(queue, 'u', 'f', {'uploadBatchId': value})
        assert d.current_batch() is None
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(run, batches))


def test_per_step_events_inherit_delivery_identity_and_trace_is_reset(caplog):
    message = SimpleNamespace(id='queue', dequeue_count=2, content=json.dumps({
        'uploadBatchId': '01234567-89ab-4def-8123-456789abcdef'}))
    with d.trace(message):
        d.file_event('vision_step', 'u', 'photo.jpg', step='ocr')
    d.file_event('vision_step', 'u', 'photo.jpg', step='face')
    first, second = records(caplog)
    assert first['attempt'] == 2 and first['message_id'] == d.message_fields(message)['message_id']
    assert first['batch_id'] == '0123456789ab4def8123456789abcdef'
    assert 'message_id' not in second and 'batch_id' not in second


def test_state_accounting_is_exclusive_and_forced_report_includes_partial_window(caplog):
    now = SimpleNamespace(value=0)
    worker = d.Worker('cluster', clock=lambda: now.value)
    now.value = 5
    worker.state('idle')
    now.value = 8
    worker.state('accumulating')
    now.value = 18
    worker.state('processing')
    worker.count('applied_file_attempts', 2)
    worker.count('unbounded-user-label', 100)
    now.value = 22
    worker.report()
    row, = records(caplog, 'worker_state')
    assert row['startup_seconds'] == 5 and row['idle_seconds'] == 3
    assert row['accumulating_seconds'] == 10 and row['processing_seconds'] == 4
    assert row['elapsed_seconds'] == 22 and row['window_seconds'] == 22
    assert row['applied_file_attempts'] == 2 and 'unbounded-user-label' not in row


def test_worker_cleanup_and_exception_semantics(caplog):
    @d.worker('cluster')
    def run():
        d.ready('cluster', page_size=8)
        with d.phase('cluster', 'outer', state='processing'):
            with d.phase('cluster', 'inner', state='processing'):
                assert d._WORKERS['cluster'].active == 2
            assert d._WORKERS['cluster'].current == 'processing'
        assert d._WORKERS['cluster'].current == 'idle'
        raise ValueError('original exception')
    with pytest.raises(ValueError, match='original exception'):
        run()
    assert len(records(caplog, 'worker_first_work')) == 1
    assert records(caplog, 'worker_stop')
    assert records(caplog, 'worker_state')


def test_diagnostic_start_failure_still_runs_worker(monkeypatch):
    monkeypatch.setattr(d.Worker, 'start', lambda self: (_ for _ in ()).throw(RuntimeError('no thread')))
    assert d.worker('vision')(lambda: 'unchanged')() == 'unchanged'


def test_mixed_activity_and_cluster_face_counts(caplog):
    d.activity('private-user', [{'filename': 'p.jpg'}, {'filename': 'v.mov'}, None],
               lambda name: name.endswith('.mov'), phase='init')
    row, = records(caplog)
    assert row['photos'] == row['videos'] == 1
    d.cluster_applied('private-user', [('p.jpg', ['face1', 'face1', 'face2'])])
    row, = records(caplog, 'cluster_applied')
    assert row['faces'] == 2
    assert all(secret not in json.dumps(row) for secret in ('p.jpg', 'face1', 'private-user'))
