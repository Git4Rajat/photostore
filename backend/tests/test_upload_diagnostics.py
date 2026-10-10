"""Upload timings observe behavior, including early returns, without payload logs."""
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import logging
from pathlib import Path
from types import SimpleNamespace

from flask import Flask, abort
import pytest

import app
import storage_utils
import upload_diagnostics as diagnostics


class Clock:
    now = 0.0

    def __call__(self):
        return self.now

    def action(self, milliseconds, result=None, error=None):
        def call(*args, **kwargs):
            self.now += milliseconds / 1000
            if error is not None:
                raise error
            return result
        return call


def records(caplog):
    return [json.loads(r.getMessage().removeprefix('upload timings '))
            for r in caplog.records if r.getMessage().startswith('upload timings ')]


@pytest.fixture
def upload_env(monkeypatch, caplog):
    clock = Clock()
    monkeypatch.setattr(diagnostics.time, 'monotonic', clock)
    monkeypatch.setattr(diagnostics, '_throughput', diagnostics.UploadThroughput(app.app.logger, clock=clock))
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(app, '_require_user_id', clock.action(2, ('library', None)))
    monkeypatch.setattr(app, '_require_library_context', clock.action(2, ('account', 'library', None)))
    monkeypatch.setattr(app, '_library_cleanup_block_reason', clock.action(3))
    monkeypatch.setattr(app, '_cleanup_failed_upload', clock.action(5))
    monkeypatch.setattr(app, 'reset_upload_tracking_and_reserve_blob', clock.action(7, 'blob'))
    monkeypatch.setattr(app, 'reset_upload_tracking_and_reserve_blobs_batch', clock.action(7, {}))
    monkeypatch.setattr(app, '_create_direct_upload_blob_url', clock.action(11, ('https://secret/?sig=credential', 'expiry')))
    monkeypatch.setattr(app, '_create_direct_thumbnail_upload_blob_url', clock.action(13, ('https://thumbnail/?sig=credential', 'expiry')))
    monkeypatch.setattr(app, 'read_pending_anonymous_blob', clock.action(5, 'blob'))
    monkeypatch.setattr(app, '_validate_client_blob_name', lambda value: None)
    blob = SimpleNamespace(get_blob_properties=clock.action(7, SimpleNamespace(size=10)))
    monkeypatch.setattr(app, 'blob_service_client', SimpleNamespace(get_blob_client=lambda **kw: blob))
    monkeypatch.setattr(app, 'finalize_uploaded_file', clock.action(11, ([], 'private.jpg')))
    monkeypatch.setattr(app, '_invalidate_metadata_scan_cache', lambda *a: None)
    monkeypatch.setattr(app, '_update_metadata_entity_fields', clock.action(13))
    monkeypatch.setattr(app, 'metadata_table_client', SimpleNamespace(get_entity=clock.action(17, {})))
    monkeypatch.setattr(app, 'apply_client_processing_results_for_file', clock.action(19, {}))
    monkeypatch.setattr(app, '_queue_upload_processing', clock.action(23))
    monkeypatch.setattr(app, '_mark_fresh_upload_activity', clock.action(29))
    monkeypatch.setattr(app, '_mark_upload_activity_heartbeat', clock.action(3))
    monkeypatch.setattr(app, '_queue_people_clustering_after_face_processing', clock.action(31))
    return clock, app.app.test_client()


def file_data(**extra):
    return {'filename': 'private.jpg', 'totalSize': 10, 'sha256': 'private-hash',
            'directToBlob': True, **extra}


def test_single_init_has_exact_timings_and_no_secrets(upload_env, caplog):
    _, client = upload_env
    response = client.post('/api/upload/init/', json=file_data(), headers={
        'X-Correlation-ID': 'caller-secret' * 1000, 'Authorization': 'Bearer private-token',
    })
    assert response.status_code == 200
    assert 'sig=credential' in response.json['blobUrl']  # response untouched
    record, = records(caplog)
    assert record['phase_ms'] == {'initialization': 5, 'cleanup': 5, 'tracking': 7, 'sas_creation': 24, 'enqueues': 3}
    assert record['total_ms'] == 44
    assert record['status'] == 200 and record['outcome'] == 'success'
    assert record['route'] == '/upload/init'
    assert len(record['correlation_id']) == 32
    assert all(c in '0123456789abcdef' for c in record['correlation_id'])
    text = json.dumps(record)
    for secret in ('private.jpg', 'private-hash', 'credential', 'https://', 'private-token', 'caller-secret'):
        assert secret not in text


@pytest.mark.parametrize('route', ['init', 'init-batch', 'finalize', 'finalize-batch', 'client-processing'])
@pytest.mark.parametrize('failure,status', [('auth', 401), ('cleanup', 409), ('validation', 400)])
def test_early_returns_logged_once(upload_env, monkeypatch, caplog, route, failure, status):
    clock, client = upload_env
    if failure == 'auth':
        monkeypatch.setattr(app, '_require_user_id', clock.action(2, (None, ({'error': 'denied'}, 401))))
        monkeypatch.setattr(app, '_require_library_context', clock.action(2, (None, None, ({'error': 'denied'}, 401))))
    elif failure == 'cleanup':
        monkeypatch.setattr(app, '_library_cleanup_block_reason', clock.action(3, 'cleanup'))
    response = client.post('/upload/' + route, json={})
    assert response.status_code == status
    record, = records(caplog)
    assert record['status'] == status and record['outcome'] == 'http_error'
    assert record['finalized_bytes'] == record['finalized_files'] == 0
    # init/init-batch now touch the activity heartbeat (enqueues, 3ms) right
    # after the cleanup-block check, before their own field validation --
    # so a validation failure on those two routes costs 3ms more than a
    # cleanup-block failure does. finalize/finalize-batch/client-processing
    # only touch the heartbeat deep in their success path, never reached by
    # any of these early-return failures.
    if failure == 'auth':
        expected_total = 2
    elif failure == 'cleanup':
        expected_total = 5
    else:
        expected_total = 8 if route in ('init', 'init-batch') else 5
    assert record['total_ms'] == expected_total
    assert diagnostics._CURRENT.get() is None


def test_caught_sas_503_is_not_success(upload_env, monkeypatch, caplog):
    clock, client = upload_env
    monkeypatch.setattr(app, '_create_direct_upload_blob_url', clock.action(11, error=RuntimeError('secret-SAS')))
    assert client.post('/upload/init', json=file_data()).status_code == 503
    record, = records(caplog)
    assert record['outcome'] == 'http_error' and record['status'] == 503
    assert record['phase_errors'] == {'sas_creation': 1}
    assert record['phase_ms']['sas_creation'] == 11
    assert record['total_ms'] == 31
    assert 'secret-SAS' not in json.dumps(record)


def test_init_batch_aggregates_success_and_failures(upload_env, monkeypatch, caplog):
    _, client = upload_env
    response = client.post('/upload/init-batch', json={'files': [file_data(), file_data(), {}]})
    assert response.status_code == 200 and len(response.json['results']) == 3
    record, = records(caplog)
    assert record['counts'] == {'files': 3, 'succeeded': 2, 'failed': 1}
    assert record['outcome'] == 'partial'
    assert record['phase_ms'] == {'initialization': 5, 'tracking': 7, 'sas_creation': 48, 'enqueues': 3}
    assert record['total_ms'] == 63


@pytest.mark.parametrize('failure,status,phase', [
    ('missing', 404, 'blob_check'), ('size', 409, None),
    ('finalize', 500, 'finalize_metadata'), ('hash', 422, None),
])
def test_finalize_http_errors(upload_env, monkeypatch, caplog, failure, status, phase):
    clock, client = upload_env
    if failure in ('missing', 'size'):
        blob = SimpleNamespace(get_blob_properties=clock.action(7, SimpleNamespace(size=9),
            RuntimeError('secret blob URL') if failure == 'missing' else None))
        monkeypatch.setattr(app, 'blob_service_client', SimpleNamespace(get_blob_client=lambda **kw: blob))
    elif failure == 'finalize':
        monkeypatch.setattr(app, 'finalize_uploaded_file', clock.action(11, error=RuntimeError('secret hash')))
    else:
        monkeypatch.setattr(app, 'metadata_table_client', SimpleNamespace(get_entity=clock.action(17, {
            'upload_sha256_expected': 'secret', 'upload_sha256_match': False,
        })))
    assert client.post('/upload/finalize', json=file_data()).status_code == status
    record, = records(caplog)
    assert record['status'] == status and record['outcome'] == 'http_error'
    assert record['total_ms'] == {'missing': 17, 'size': 17, 'finalize': 28, 'hash': 58}[failure]
    assert record['phase_errors'] == ({phase: 1} if phase else {})
    assert record['finalized_bytes'] == record['finalized_files'] == 0
    assert diagnostics._throughput.window['finalized_bytes'] == 0


@pytest.mark.parametrize('batch', [False, True])
def test_finalize_success_aggregate_stages(upload_env, caplog, batch):
    _, client = upload_env
    item = file_data(clientProcessing={'ocr': 'private OCR text'})
    response = client.post('/upload/finalize-batch' if batch else '/upload/finalize',
                           json={'files': [item, item]} if batch else item)
    assert response.status_code == 200
    record, = records(caplog)
    multiplier = 2 if batch else 1
    assert record['phase_ms']['finalize_metadata'] == 11 * multiplier
    assert record['phase_ms']['client_processing'] == 19 * multiplier
    assert record['phase_ms']['metadata_read'] == 34 * multiplier
    assert record['phase_ms']['enqueues'] == 54 * multiplier + 29 + 3
    assert record['total_ms'] == 5 + (5 + 7 + 11 + 13 + 34 + 19 + 54) * multiplier + 29 + 3
    assert record['outcome'] == 'success'
    assert record['finalized_bytes'] == 10 * multiplier
    assert record['finalized_files'] == multiplier
    assert diagnostics._throughput.window['finalized_bytes'] == 10 * multiplier
    assert 'private OCR' not in json.dumps(record)


def test_finalize_batch_caught_failure_is_partial(upload_env, monkeypatch, caplog):
    clock, client = upload_env
    monkeypatch.setattr(app, 'finalize_uploaded_file', clock.action(11, error=RuntimeError('private failure')))
    response = client.post('/upload/finalize-batch', json={'files': [file_data(), file_data()]})
    assert response.status_code == 200
    record, = records(caplog)
    assert record['outcome'] == 'partial' and record['counts']['failed'] == 2
    assert record['phase_errors'] == {'finalize_metadata': 2}
    assert record['phase_ms']['finalize_metadata'] == 22
    assert record['finalized_bytes'] == record['finalized_files'] == 0


def test_partial_finalize_batch_only_counts_successful_verified_bytes(upload_env, monkeypatch, caplog):
    _, client = upload_env

    def finalize(user_id, filename, *a, **kw):
        if filename == 'bad.jpg':
            raise OSError('finalization failed')
        return [], filename

    monkeypatch.setattr(app, 'finalize_uploaded_file', finalize)
    response = client.post('/upload/finalize-batch', json={
        'files': [file_data(filename='good.jpg'), file_data(filename='bad.jpg'), {}]})
    assert response.status_code == 200
    record, = records(caplog)
    assert record['counts'] == {'files': 3, 'succeeded': 1, 'failed': 2}
    assert record['finalized_bytes'] == 10 and record['finalized_files'] == 1
    assert diagnostics._throughput.window['finalized_bytes'] == 10
    assert diagnostics._throughput.window['finalized_files'] == 1


def test_byte_totals_are_not_clipped_by_file_counter_cap(upload_env, monkeypatch, caplog):
    clock, client = upload_env
    size = 8000000
    blob = SimpleNamespace(get_blob_properties=clock.action(7, SimpleNamespace(size=size)))
    monkeypatch.setattr(app, 'blob_service_client', SimpleNamespace(get_blob_client=lambda **kw: blob))
    assert client.post('/upload/finalize', json=file_data(totalSize=size)).status_code == 200
    record, = records(caplog)
    assert record['finalized_bytes'] == diagnostics._throughput.window['finalized_bytes'] == size
    assert record['finalized_files'] == 1


def test_init_and_client_processing_do_not_credit_upload_bytes(upload_env):
    _, client = upload_env
    assert client.post('/upload/init', json=file_data()).status_code == 200
    assert client.post('/upload/init-batch', json={'files': [file_data()]}).status_code == 200
    assert client.post('/upload/client-processing', json=file_data(clientProcessing={'ocr': {}})).status_code == 200
    assert diagnostics._throughput.window['requests'] == 3
    assert diagnostics._throughput.window['finalized_bytes'] == diagnostics._throughput.window['finalized_files'] == 0


@pytest.mark.parametrize('deleted,status', [(True, 410), (False, 500)])
def test_client_processing_errors(upload_env, monkeypatch, caplog, deleted, status):
    clock, client = upload_env
    monkeypatch.setattr(app, 'apply_client_processing_results_for_file', clock.action(19,
        error=RuntimeError('Photo deleted: private' if deleted else 'private result')))
    assert client.post('/upload/client-processing', json=file_data()).status_code == status
    record, = records(caplog)
    assert record['outcome'] == 'http_error' and record['status'] == status
    assert record['total_ms'] == 24 and record['phase_errors'] == {'client_processing': 1}
    assert 'private' not in json.dumps(record)


def test_client_processing_success_and_caught_enqueue_failure(upload_env, monkeypatch, caplog):
    clock, client = upload_env
    monkeypatch.setattr(app, '_queue_people_clustering_after_face_processing',
                        clock.action(31, error=RuntimeError('private queue failure')))
    response = client.post('/upload/client-processing', json=file_data(claimedSteps=['ocr', 'face'],
        clientProcessing={'ocr': 'secret results'}))
    assert response.status_code == 200 and response.json['accepted']
    record, = records(caplog)
    assert record['counts'] == {'files': 1, 'steps': 2}
    assert record['outcome'] == 'degraded' and record['phase_errors'] == {'enqueues': 1}
    assert record['total_ms'] == 55


def test_context_phase_preserves_exception_and_aggregates_bounded_metrics():
    clock = Clock()
    timing = diagnostics.UploadTiming('/upload/init', clock)
    token = diagnostics._CURRENT.set(timing)
    original = ValueError('private exception')
    try:
        assert diagnostics.timed_call('tracking', clock.action(10, 'value')) == 'value'
        with pytest.raises(ValueError) as caught:
            diagnostics.timed_call('tracking', clock.action(20, error=original))
        assert caught.value is original
        diagnostics.timed_call('untrusted phase' * 1000, clock.action(5))
        diagnostics.upload_count('files', 10**20)
        diagnostics.upload_count('untrusted key', 'secret')
        assert timing.phase_ms == {'tracking': pytest.approx(30)}
        assert timing.phase_errors == {'tracking': 1}
        assert timing.counters == {'files': 1000000}
    finally:
        diagnostics._CURRENT.reset(token)
    diagnostics.timed_call('tracking', clock.action(40))
    assert timing.phase_ms['tracking'] == pytest.approx(30)


def test_clock_and_logger_failures_are_best_effort(upload_env, monkeypatch):
    _, client = upload_env
    def broken(*args, **kwargs):
        raise RuntimeError('diagnostics unavailable')
    monkeypatch.setattr(diagnostics.time, 'monotonic', broken)
    monkeypatch.setattr(app.app.logger, 'info', broken)
    assert client.post('/upload/init', json=file_data()).status_code == 200
    assert client.post('/upload/init', json={}).status_code == 400
    assert diagnostics._CURRENT.get() is None


def test_requests_have_distinct_server_ids_and_no_phase_leak(upload_env, caplog):
    _, client = upload_env
    assert client.post('/upload/init', json=file_data()).status_code == 200
    assert client.post('/upload/init', json={}).status_code == 400
    first, second = records(caplog)
    assert first['correlation_id'] != second['correlation_id']
    assert second['phase_ms'] == {'initialization': 5, 'enqueues': 3}
    assert second['total_ms'] == 8 and second['phase_errors'] == {}


@pytest.mark.parametrize('handled', [False, True])
def test_exception_status_uses_response_or_teardown(monkeypatch, caplog, handled):
    server = Flask(__name__)
    server.testing = True
    server.teardown_request(diagnostics.upload_teardown)
    caplog.set_level(logging.INFO)
    clock = Clock()
    monkeypatch.setattr(diagnostics.time, 'monotonic', clock)
    original = RuntimeError('private exception text')
    @server.post('/test')
    @diagnostics.instrument_upload('/upload/init')
    def route():
        return diagnostics.timed_call('tracking', clock.action(20, error=original))
    if handled:
        server.register_error_handler(RuntimeError, lambda exc: ({'error': 'unavailable'}, 503))
        assert server.test_client().post('/test').status_code == 503
    else:
        with pytest.raises(RuntimeError) as caught:
            server.test_client().post('/test')
        assert caught.value is original
    record, = records(caplog)
    assert record['outcome'] == 'exception'
    assert record['status'] == (503 if handled else 500)
    assert record['total_ms'] == 20
    assert diagnostics._CURRENT.get() is None


def test_http_exception_keeps_http_outcome(caplog):
    server = Flask(__name__)
    caplog.set_level(logging.INFO)
    @server.post('/test')
    @diagnostics.instrument_upload('/upload/init')
    def route():
        abort(413)
    assert server.test_client().post('/test').status_code == 413
    record, = records(caplog)
    assert record['status'] == 413 and record['outcome'] == 'http_error'


def test_storage_subphases_roll_into_request(monkeypatch):
    clock = Clock()
    timing = diagnostics.UploadTiming('/upload/finalize', clock)
    monkeypatch.setattr(storage_utils, '_require_context', lambda: None)
    monkeypatch.setitem(storage_utils._CTX, 'metadata_table_client', SimpleNamespace(upsert_entity=clock.action(13)))
    monkeypatch.setattr(storage_utils, 'detect_duplicates', clock.action(5, []))
    monkeypatch.setattr(storage_utils, '_resolve_filename_for_upload', clock.action(7, 'private.jpg'))
    monkeypatch.setattr(storage_utils, 'get_or_create_metadata', clock.action(11, {}))
    monkeypatch.setattr(storage_utils, 'touch_user_search_indexes_state', clock.action(17))
    monkeypatch.setattr(storage_utils, '_store_hash_index', clock.action(19))
    monkeypatch.setattr(storage_utils, '_store_filename_owner', clock.action(23))
    token = diagnostics._CURRENT.set(timing)
    try:
        assert storage_utils.finalize_uploaded_file('library', 'private.jpg', 'image/jpeg',
            client_sha256='private hash') == ([], 'private.jpg')
    finally:
        diagnostics._CURRENT.reset(token)
    assert timing.phase_ms == {'dedup': pytest.approx(5), 'name_resolution': pytest.approx(7),
                              'metadata_read': pytest.approx(11), 'persistence': pytest.approx(72)}


def test_storage_client_processing_persistence_subphases(monkeypatch):
    clock = Clock()
    timing = diagnostics.UploadTiming('/upload/client-processing', clock)
    monkeypatch.setattr(storage_utils, '_require_context', lambda: None)
    monkeypatch.setitem(storage_utils._CTX, 'metadata_table_client',
                        SimpleNamespace(get_entity=clock.action(7, {})))
    def apply(*args, **kwargs):
        storage_utils.update_processing_status('library', 'private.jpg', 'ocr', 'done')
    monkeypatch.setattr(storage_utils, '_apply_client_processing_results', apply)
    monkeypatch.setattr(storage_utils, '_flush_processing_status_batch', clock.action(11))
    monkeypatch.setattr(storage_utils, 'refresh_metadata_entity', clock.action(13))
    token = diagnostics._CURRENT.set(timing)
    try:
        assert storage_utils.apply_client_processing_results_for_file('library', 'private.jpg') == {}
    finally:
        diagnostics._CURRENT.reset(token)
    assert timing.phase_ms == {'metadata_read': pytest.approx(7), 'persistence': pytest.approx(24)}


def throughput_records(caplog):
    prefix = 'upload throughput metrics='
    return [json.loads(r.getMessage().removeprefix(prefix))
            for r in caplog.records if r.getMessage().startswith(prefix)]


@pytest.mark.parametrize('elapsed', [0, 30, 120])
def test_upload_rates_use_actual_replica_time_and_reset_idle_windows(caplog, elapsed):
    caplog.set_level(logging.INFO)
    clock = Clock()
    reporter = diagnostics.UploadThroughput(app.app.logger, clock=clock)
    reporter.record_request(200, finalized_files=2, finalized_bytes=3000000)
    reporter.record_request(500, finalized_files=9, finalized_bytes=9000000)
    reporter.log()
    assert throughput_records(caplog) == []
    clock.now = elapsed
    reporter.log(force=True)
    first, = throughput_records(caplog)
    assert first['window'] == first['cumulative'] == {
        'requests': 2, 'request_errors': 1, 'finalized_files': 2, 'finalized_bytes': 3000000}
    assert first['finalized_mb_per_hour'] == (round(3 * 3600 / elapsed, 3) if elapsed else 0)
    assert first['process_mb_per_hour'] == first['finalized_mb_per_hour']
    assert first['workers_per_replica'] == 1
    assert first['window_seconds'] == first['elapsed_seconds'] == elapsed
    clock.now += 60
    reporter.log()
    second = throughput_records(caplog)[1]
    assert second['window']['finalized_bytes'] == second['finalized_mb_per_hour'] == 0
    assert second['cumulative'] == first['cumulative']
    assert second['process_instance'] == first['process_instance']


def test_multiple_processes_do_not_claim_a_replica_rate(caplog):
    caplog.set_level(logging.INFO)
    clock = Clock()
    reporter = diagnostics.UploadThroughput(app.app.logger, clock=clock, workers_per_replica=2)
    reporter.record_request(200, finalized_files=1, finalized_bytes=1000000)
    clock.now = 60
    reporter.log()
    record, = throughput_records(caplog)
    assert record['finalized_mb_per_hour'] is None
    assert record['process_mb_per_hour'] == 60
    assert record['workers_per_replica'] == 2


def test_concurrent_uploads_and_snapshots_do_not_lose_or_duplicate_bytes(caplog):
    caplog.set_level(logging.INFO)
    clock = Clock()
    reporter = diagnostics.UploadThroughput(app.app.logger, clock=clock)

    def upload():
        for _ in range(500):
            reporter.record_request(200, finalized_files=1, finalized_bytes=8000000)

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(upload) for _ in range(4)]
        reporter.log(force=True)
        for future in futures:
            future.result()
    clock.now = 60
    reporter.log(force=True)
    samples = throughput_records(caplog)
    assert sum(s['window']['finalized_bytes'] for s in samples) == 2000 * 8000000
    assert sum(s['window']['finalized_files'] for s in samples) == 2000
    assert samples[-1]['cumulative']['finalized_bytes'] == 2000 * 8000000


@pytest.mark.parametrize('status, exception, expected_bytes', [(200, False, 5000000), (503, False, 0), (200, True, 0)])
def test_request_emission_credits_bytes_once_only_for_accepted_results(caplog, monkeypatch, status, exception, expected_bytes):
    caplog.set_level(logging.INFO)
    clock = Clock()
    reporter = diagnostics.UploadThroughput(app.app.logger, clock=clock)
    monkeypatch.setattr(diagnostics, '_throughput', reporter)
    timing = diagnostics.UploadTiming('/upload/finalize', clock)
    token = diagnostics._CURRENT.set(timing)
    try:
        diagnostics.upload_finalized(5000000)
    finally:
        diagnostics._CURRENT.reset(token)
    timing.emit(app.app.logger, status, exception=exception)
    timing.emit(app.app.logger, status, exception=exception)
    assert reporter.window['finalized_bytes'] == expected_bytes
    assert reporter.window['requests'] == 1
    assert len(records(caplog)) == 1


def test_upload_reporter_start_is_once_and_stop_emits_short_final_window(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    clock = Clock()
    monkeypatch.setattr(diagnostics.time, 'monotonic', clock)
    monkeypatch.setattr(diagnostics, '_throughput', None)
    threads = []

    class Thread:
        def __init__(self, **kwargs):
            assert kwargs['daemon'] is True
            threads.append(self)

        def start(self):
            pass

        def join(self, *, timeout):
            assert timeout == 1

    monkeypatch.setattr(diagnostics.threading, 'Thread', Thread)
    diagnostics.start_upload_throughput(app.app.logger)
    reporter = diagnostics._throughput
    diagnostics.start_upload_throughput(app.app.logger)
    assert len(threads) == 1
    reporter.record_request(200, finalized_files=1, finalized_bytes=2000000)
    clock.now = 5
    diagnostics.stop_upload_throughput()
    diagnostics.stop_upload_throughput()
    record, = throughput_records(caplog)
    assert record['window_seconds'] == 5 and record['finalized_mb_per_hour'] == 1440
    assert reporter.stopping.is_set()
    assert diagnostics._throughput is None


def test_periodic_reporter_emits_idle_samples_without_requests(caplog):
    caplog.set_level(logging.INFO)
    clock = Clock()
    reporter = diagnostics.UploadThroughput(app.app.logger, clock=clock)

    def wait(seconds):
        assert seconds == 60
        clock.now += seconds
        return clock.now > 120

    reporter.stopping = SimpleNamespace(wait=wait)
    reporter._run()
    samples = throughput_records(caplog)
    assert len(samples) == 2
    assert all(s['window_seconds'] == 60 and s['finalized_mb_per_hour'] == 0 for s in samples)


@pytest.mark.parametrize('role', ['upload', 'core', 'vision'])
def test_gunicorn_hooks_only_report_for_upload_role(monkeypatch, role):
    path = Path(__file__).resolve().parents[1] / 'gunicorn.conf.py'
    spec = importlib.util.spec_from_file_location('upload_gunicorn_config', path)
    config = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(config)
    monkeypatch.setenv('APP_ROLE', role)
    calls = []
    monkeypatch.setattr(diagnostics, 'start_upload_throughput', lambda logger, **kw: calls.append(('start', kw)))
    monkeypatch.setattr(diagnostics, 'stop_upload_throughput', lambda: calls.append(('stop', {})))
    worker = SimpleNamespace(log=app.app.logger, cfg=SimpleNamespace(workers=1))
    config.post_worker_init(worker)
    config.worker_exit(None, worker)
    assert calls == ([('start', {'workers_per_replica': 1}), ('stop', {})] if role == 'upload' else [])
