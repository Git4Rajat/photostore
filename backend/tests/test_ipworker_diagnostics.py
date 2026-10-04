"""Local-only diagnostics contracts: fake clocks, queues, and sweep phases."""
from concurrent.futures import Future, ThreadPoolExecutor
import json
import logging
from types import SimpleNamespace

import pytest

import app
import ipworker_metrics as metrics_module


class _StopLoop(BaseException):
    pass


@pytest.fixture
def clock(monkeypatch):
    now = SimpleNamespace(seconds=100.0)
    monkeypatch.setattr(app.time, 'monotonic', lambda: now.seconds)
    return now


@pytest.fixture
def memory_samples(monkeypatch):
    samples = []
    monkeypatch.setattr(app, '_log_ipwork_memory_sample', samples.append)
    return samples


def _metrics(caplog):
    prefix = 'ipwork throughput metrics='
    return [json.loads(record.getMessage()[len(prefix):])
            for record in caplog.records if record.getMessage().startswith(prefix)]


def test_window_60_second_cadence_reset_and_cumulative(clock, memory_samples, caplog):
    caplog.set_level(logging.INFO, logger=app.worker_logger.name)
    window = app._IpworkThroughputWindow()
    window.record('done', count=2)
    window.record('error')
    window.record('receive', duration_ms=12.3456)
    window.record('received', count=4)
    window.record('ack', duration_ms=3.4567)
    window.record('ack_failed', duration_ms=5.5555)

    clock.seconds = 159.999
    window.log(3)
    assert _metrics(caplog) == []
    assert memory_samples == []
    assert window.window['done'] == 2
    assert window.last_logged == 100

    clock.seconds = 160
    window.log(2)
    first, = _metrics(caplog)
    assert first['window_seconds'] == first['elapsed_seconds'] == 60
    assert first['in_flight'] == 2
    assert first['done_per_hour'] == 120
    assert first['window'] == first['cumulative']
    assert first['window']['error'] == 1
    assert first['window']['ack'] == first['window']['ack_failed'] == 1
    assert first['window']['received'] == 4
    assert first['window']['receive_ms'] == 12.346
    assert first['window']['ack_ms'] == 3.457
    assert first['window']['ack_failed_ms'] == 5.556
    assert first['window']['noop'] == first['window']['not_found'] == 0
    assert window.window == {}
    assert window.last_logged == 160

    window.record('noop')
    window.record('receive_failed', duration_ms=7.25)
    window.record('ack', duration_ms=2)
    clock.seconds = 219.999
    window.log(1)
    assert len(_metrics(caplog)) == 1
    clock.seconds = 220
    window.log(0)
    second = _metrics(caplog)[1]
    assert second['window_seconds'] == 60
    assert second['elapsed_seconds'] == 120
    assert second['window']['done'] == second['done_per_hour'] == 0
    assert second['window']['noop'] == second['window']['receive_failed'] == 1
    assert 'receive_ms' not in second['window']
    assert second['cumulative']['done'] == 2
    assert second['cumulative']['error'] == 1
    assert second['cumulative']['ack'] == 2
    assert second['cumulative']['ack_ms'] == 5.457
    assert second['cumulative']['receive_failed_ms'] == 7.25
    assert memory_samples == [2, 0]


@pytest.mark.parametrize('elapsed, expected_rate', [(0, 0), (5, 720)])
def test_forced_final_window_logs_before_cadence(clock, memory_samples, caplog, elapsed, expected_rate):
    caplog.set_level(logging.INFO, logger=app.worker_logger.name)
    window = app._IpworkThroughputWindow()
    window.record('done')
    clock.seconds += elapsed
    window.log(1, force=True)
    metrics, = _metrics(caplog)
    assert metrics['window_seconds'] == metrics['elapsed_seconds'] == elapsed
    assert metrics['done_per_hour'] == expected_rate
    assert metrics['window']['done'] == metrics['cumulative']['done'] == 1
    assert metrics['in_flight'] == 1
    assert memory_samples == [1]
    assert window.window == {}
    assert window.cumulative['done'] == 1


class _ImmediateExecutor:
    """Completed futures keep queue-loop tests deterministic without model threads."""
    def __init__(self, **kwargs):
        self.shutdown_calls = []

    def submit(self, function, *args):
        future = Future()
        try:
            future.set_result(function(*args))
        except Exception as exc:
            future.set_exception(exc)
        return future

    def shutdown(self, **kwargs):
        self.shutdown_calls.append(kwargs)


def _prepare_queue(monkeypatch, queue):
    monkeypatch.setattr(app, 'IPWORKER_FACE_RECONCILE_BATCH_SIZE', 1)
    monkeypatch.setattr(app, 'queue_service_client', SimpleNamespace(get_queue_client=lambda name: queue))
    monkeypatch.setattr(app, '_register_ipwork_processors', lambda: None)
    monkeypatch.setattr(app, '_prewarm_ipwork_models', lambda: None)
    monkeypatch.setattr(app, '_ipwork_sweep_loop', lambda: None)
    monkeypatch.setattr(app, 'ThreadPoolExecutor', _ImmediateExecutor)
    monkeypatch.setattr(app, 'wait', lambda futures, **kwargs: (set(futures), set()))
    # Do not replace the process's real signal handlers for a fake-queue test.
    monkeypatch.setattr(app.signal, 'signal', lambda *args: None)


def _run_queue(monkeypatch, queue):
    _prepare_queue(monkeypatch, queue)

    def stop(_seconds):
        raise _StopLoop()

    monkeypatch.setattr(app.time, 'sleep', stop)
    with pytest.raises(_StopLoop):
        app.run_ipworker()


@pytest.mark.parametrize('outcome, dequeue_count, ack_failure, expected_ack', [
    ('done', 1, False, 1),
    ('noop', 1, False, 1),
    ('not_found', 1, False, 1),
    ('retry_exhausted', app.IPWORKER_MAX_RETRIES + 1, False, 1),
    ('error', 1, False, 0),
    ('raises', 1, False, 0),
    ('lease_busy', 1, False, 0),
    ('lease_busy', app.IPWORK_LEASE_RETRY_LIMIT, False, 1),
    ('done', 1, True, 0),
])
def test_queue_outcomes_are_distinct_from_ack_and_final_log(
        monkeypatch, clock, memory_samples, caplog, outcome, dequeue_count, ack_failure, expected_ack):
    caplog.set_level(logging.INFO, logger=app.worker_logger.name)
    message = SimpleNamespace(id='m1', content='{}', dequeue_count=dequeue_count)

    class Queue:
        def __init__(self):
            self.receives = 0
            self.deletes = []

        def create_queue(self):
            pass

        def receive_messages(self, **kwargs):
            self.receives += 1
            clock.seconds += 0.01
            return iter([message] if self.receives == 1 else [])

        def delete_message(self, msg):
            self.deletes.append(msg.id)
            clock.seconds += 0.02
            if ack_failure:
                raise RuntimeError('ack unavailable')

    def process(msg):
        if outcome == 'raises':
            raise RuntimeError('unexpected task failure')
        return outcome

    queue = Queue()
    monkeypatch.setattr(app, '_process_ipwork_message', process)
    _run_queue(monkeypatch, queue)

    metrics, = _metrics(caplog)
    counters = metrics['cumulative']
    assert counters == metrics['window']
    assert counters['error' if outcome == 'raises' else outcome] == 1
    assert counters['done'] == (outcome == 'done')
    assert counters['ack'] == expected_ack
    assert counters['ack_failed'] == int(ack_failure)
    assert counters['receive'] == queue.receives == 2
    assert counters['receive_ms'] == 20
    assert counters['receive_failed'] == 0
    assert counters['received'] == 1
    assert queue.deletes == (['m1'] if expected_ack or ack_failure else [])
    if expected_ack or ack_failure:
        assert counters['ack_failed_ms' if ack_failure else 'ack_ms'] == 20
    else:
        assert 'ack_ms' not in counters and 'ack_failed_ms' not in counters
    assert metrics['in_flight'] == 0
    assert metrics['window_seconds'] < 60
    # No per-completion memory sample: only the forced final window samples RSS.
    assert memory_samples == [0]


def test_lazy_receive_failure_counted_and_finally_logged(monkeypatch, clock, memory_samples, caplog):
    caplog.set_level(logging.INFO, logger=app.worker_logger.name)

    class Queue:
        def create_queue(self):
            pass

        def receive_messages(self, **kwargs):
            clock.seconds += 0.01
            yield SimpleNamespace(id='partial', content='{}')
            clock.seconds += 0.02
            raise RuntimeError('failed fetching next page')

    processed = []
    monkeypatch.setattr(app, '_process_ipwork_message', processed.append)
    _run_queue(monkeypatch, Queue())
    metrics, = _metrics(caplog)
    assert metrics['window']['receive_failed'] == 1
    assert metrics['window']['receive_failed_ms'] == 30
    assert metrics['window']['receive'] == metrics['window']['received'] == 0
    assert metrics['window']['ack'] == metrics['window']['done'] == 0
    assert processed == []
    assert memory_samples == [0]


@pytest.mark.parametrize('stuck', [False, True])
def test_shutdown_final_sample_precedes_force_exit_and_keeps_in_flight_count(
        monkeypatch, clock, memory_samples, caplog, stuck):
    caplog.set_level(logging.INFO, logger=app.worker_logger.name)
    handlers = {}
    message = SimpleNamespace(id='shutdown', content='{}', dequeue_count=1)

    class Queue:
        def __init__(self):
            self.receives = 0
            self.deletes = []

        def create_queue(self):
            pass

        def receive_messages(self, **kwargs):
            self.receives += 1
            handlers[app.signal.SIGTERM](app.signal.SIGTERM, None)
            return [message]

        def delete_message(self, msg):
            self.deletes.append(msg.id)

    executor = _ImmediateExecutor()
    if stuck:
        monkeypatch.setattr(executor, 'submit', lambda *args: Future())

    def wait(futures, **kwargs):
        clock.seconds += 1
        return (set(), set(futures)) if stuck else (set(futures), set())

    queue = Queue()
    _prepare_queue(monkeypatch, queue)
    monkeypatch.setattr(app.signal, 'signal', lambda signum, handler: handlers.update({signum: handler}))
    monkeypatch.setattr(app, 'ThreadPoolExecutor', lambda **kwargs: executor)
    monkeypatch.setattr(app, 'wait', wait)
    monkeypatch.setattr(app, '_process_ipwork_message', lambda message: 'done')
    monkeypatch.setattr(app, 'IPWORKER_SHUTDOWN_GRACE_SECONDS', 1)
    exit_calls = []

    def force_exit(code):
        # Verify the final log/sample is emitted before os._exit, not after it.
        exit_calls.append((code, len(_metrics(caplog)), list(memory_samples)))

    monkeypatch.setattr(app.os, '_exit', force_exit)
    app.run_ipworker()
    metrics, = _metrics(caplog)
    assert metrics['in_flight'] == int(stuck)
    assert metrics['window']['received'] == 1
    assert metrics['window']['done'] == metrics['window']['ack'] == int(not stuck)
    assert metrics['window']['shutdown_grace_exhausted'] == int(stuck)
    assert memory_samples == [int(stuck)]
    assert queue.receives == 1
    assert queue.deletes == ([] if stuck else ['shutdown'])
    assert executor.shutdown_calls == [{'wait': not stuck, 'cancel_futures': stuck}]
    assert exit_calls == ([(0, 1, [1])] if stuck else [])


@pytest.mark.parametrize('result', [False, None, {'photosQueued': 3}])
def test_timed_sweep_phase_preserves_result_and_arguments(clock, caplog, result):
    caplog.set_level(logging.INFO, logger=app.worker_logger.name)
    calls = []

    def phase(*args, **kwargs):
        calls.append((args, kwargs))
        clock.seconds += 0.125
        return result

    assert app._timed_ipwork_sweep_phase('claim_lock', phase, 'owner', ttl_seconds=1200) is result
    assert calls == [(('owner',), {'ttl_seconds': 1200})]
    assert 'phase=claim_lock outcome=done duration_ms=125.000' in caplog.text


def test_timed_sweep_phase_logs_error_and_reraises_original(clock, caplog):
    caplog.set_level(logging.INFO, logger=app.worker_logger.name)
    error = RuntimeError('sweep failed')

    def phase():
        clock.seconds += 0.25
        raise error

    with pytest.raises(RuntimeError) as raised:
        app._timed_ipwork_sweep_phase('trash_purge', phase)
    assert raised.value is error
    assert 'phase=trash_purge outcome=error duration_ms=250.000' in caplog.text


@pytest.mark.parametrize('lock_claimed, failure_phase', [
    (False, None), (True, None), (True, 'claim_lock'), (True, 'stale_processing'),
    (True, 'tag_embedding_indexes'), (True, 'trash_purge'),
])
def test_sweep_loop_consumes_outcomes_without_changing_control_flow(
        monkeypatch, clock, caplog, lock_claimed, failure_phase):
    caplog.set_level(logging.INFO, logger=app.worker_logger.name)
    calls = []
    sleep_calls = []

    def sleep(seconds):
        sleep_calls.append(seconds)
        if len(sleep_calls) == 2:
            raise _StopLoop()

    def phase(name, result):
        def run(*args, **kwargs):
            calls.append(name)
            clock.seconds += 0.01
            if name == failure_phase:
                raise RuntimeError('phase failed')
            return result
        return run

    monkeypatch.setattr(app.time, 'sleep', sleep)
    monkeypatch.setattr(app, '_try_claim_ipwork_sweep_lock', phase('claim_lock', lock_claimed))
    monkeypatch.setattr(app, '_sweep_stale_processing_into_ipwork', phase(
        'stale_processing', {'photosQueued': 2, 'stepsQueued': 3, 'libraries': 1}))
    monkeypatch.setattr(app, '_sweep_tag_embedding_indexes', phase(
        'tag_embedding_indexes', {'librariesChecked': 1, 'indexesAvailable': 1}))
    monkeypatch.setattr(app, '_run_trash_purge_sweep', phase(
        'trash_purge', {'photosPurged': 1, 'albumsPurged': 0, 'libraries': 1}))
    with pytest.raises(_StopLoop):
        app._ipwork_sweep_loop()

    expected = ['claim_lock', 'stale_processing', 'tag_embedding_indexes', 'trash_purge']
    if not lock_claimed:
        expected = expected[:1]
    elif failure_phase:
        expected = expected[:expected.index(failure_phase) + 1]
    assert calls == expected
    for name in expected:
        outcome = 'error' if name == failure_phase else 'done'
        assert f'phase={name} outcome={outcome} duration_ms=10.000' in caplog.text
    if lock_claimed and not failure_phase:
        assert 'released 2 stale photo(s), 3 step(s), across 1 librar(y/ies)' in caplog.text
        assert 'tag-embedding sweep: 1/1 librar(y/ies) have a usable index' in caplog.text
        assert 'trash sweep: purged 1 photo(s) and 0 album(s)' in caplog.text
    assert ('ipwork sweep iteration failed' in caplog.text) == bool(failure_phase)
    assert sleep_calls == [min(60, app.IPWORK_SWEEP_INTERVAL_SECONDS), app.IPWORK_SWEEP_INTERVAL_SECONDS]


def test_histogram_upper_bounds_overflow_and_fixed_size():
    histogram = metrics_module.Histogram()
    for _ in range(10000):
        histogram.observe(501)
    histogram.observe(600001)
    for invalid in (-1, float('nan'), float('inf')):
        histogram.observe(invalid)
    summary = histogram.snapshot()
    assert summary['count'] == 10001
    assert summary['p50_upper_bound'] == summary['p95_upper_bound'] == summary['p99_upper_bound'] == 750
    assert len(histogram.counts) == len(metrics_module.LATENCY_MS) + 1
    assert summary['bucket_counts'][-1] == 1
    assert summary['bucket_upper_bounds'][-1] is None
    tail = metrics_module.Histogram()
    tail.observe(600001)
    assert tail.snapshot()['p99_upper_bound'] is None
    assert metrics_module.Histogram().snapshot()['p50_upper_bound'] is None


def test_concurrent_records_and_atomic_window_reset(clock):
    collector = metrics_module.Metrics(2, clock=lambda: clock.seconds)

    def record_many(_):
        for _ in range(1000):
            collector.record('done')
            collector.observe('task', 123)
            collector.record('filename-untrusted')
            collector.observe('unbounded-label', 10)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(record_many, range(4)))
    clock.seconds += 60
    first = collector.snapshot(60, 60)
    second = collector.snapshot(0, 60)
    assert first['window']['done'] == first['cumulative']['done'] == 4000
    assert first['histograms']['window']['task']['count'] == 4000
    assert second['window']['done'] == 0
    assert second['histograms']['window'] == {}
    assert second['histograms']['cumulative']['task']['count'] == 4000
    assert 'filename-untrusted' not in collector.cumulative
    assert len(collector.histograms[0]) == len(metrics_module.TIMINGS) + 1


def test_slot_integration_preparation_idle_and_windows(clock):
    collector = metrics_module.Metrics(2, clock=lambda: clock.seconds)
    clock.seconds += 2  # idle
    collector.state(preparing=8)
    clock.seconds += 3  # prep only
    collector.state(preparing=0, ready=8, active_delta=1)
    clock.seconds += 4  # one slot
    collector.state(active_delta=1)
    clock.seconds += 5  # both slots
    first = collector.snapshot(14, 14)
    window = first['utilization']['window']
    assert window['slot_seconds'] == 14
    assert window['slot_utilization'] == 0.5
    assert window['idle_seconds'] == 2
    assert window['preparation_only_seconds'] == window['preparing_seconds'] == 3
    assert window['ready_seconds'] == 9
    clock.seconds += 2
    collector.state(active_delta=-2, ready=0)
    clock.seconds += 1
    second = collector.snapshot(3, 17)
    assert second['utilization']['window']['slot_seconds'] == 4
    assert second['utilization']['cumulative']['slot_seconds'] == 18
    assert second['utilization']['window']['idle_seconds'] == 1
    assert second['loop'] == {'active_tasks': 0, 'preparing': 0, 'ready': 0}


@pytest.mark.parametrize('platform, peak, expected_peak', [('linux', 4096, 4194304), ('darwin', 4194304, 4194304)])
def test_resource_units_current_peak_and_cpu(monkeypatch, platform, peak, expected_peak):
    from io import StringIO
    monkeypatch.setattr(metrics_module.sys, 'platform', platform)
    monkeypatch.setattr(metrics_module, 'resource', SimpleNamespace(
        RUSAGE_SELF=0, getrusage=lambda _: SimpleNamespace(ru_maxrss=peak)))
    monkeypatch.setattr(metrics_module.time, 'process_time', lambda: 3.25)
    monkeypatch.setattr('builtins.open', lambda *a, **kw: StringIO('100 20 0 0'))
    monkeypatch.setattr(metrics_module.os, 'sysconf', lambda _: 4096)
    calls = []

    def mach_rss():
        calls.append(True)
        return 81920

    monkeypatch.setattr(metrics_module, '_darwin_current_rss_bytes', mach_rss)
    sample = metrics_module.resource_sample()
    assert sample == {'process_cpu_seconds': 3.25, 'current_rss_bytes': 81920,
                      'peak_rss_bytes': expected_peak}
    assert bool(calls) == (platform == 'darwin')


def test_resource_failure_is_null_and_cpu_delta_is_process_wide(monkeypatch, clock):
    monkeypatch.setattr(metrics_module, 'resource', None)
    monkeypatch.setattr(metrics_module.sys, 'platform', 'linux')
    monkeypatch.setattr('builtins.open', lambda *a, **kw: (_ for _ in ()).throw(OSError()))
    cpu = SimpleNamespace(seconds=10)
    monkeypatch.setattr(metrics_module.time, 'process_time', lambda: cpu.seconds)
    collector = metrics_module.Metrics(2, clock=lambda: clock.seconds)
    cpu.seconds += 90
    clock.seconds += 60
    sample = collector.snapshot(60, 60)['resources']
    assert sample['window_cpu_seconds'] == 90
    assert sample['window_cpu_cores'] == 1.5
    assert sample['current_rss_bytes'] is sample['peak_rss_bytes'] is None


def test_identity_is_allowlisted_bounded_and_contains_no_secret(monkeypatch):
    monkeypatch.setenv('SESSION_SECRET', 'must-not-log-this')
    monkeypatch.setenv('BLOB_CONNECTION_STRING', 'must-not-log-this-either')
    monkeypatch.setenv('CONTAINER_APP_REVISION', 'r' * 1000)
    identity = metrics_module.replica_identity()
    assert len(identity['container_app_revision']) == 128
    assert 'must-not-log' not in json.dumps(identity)
    assert set(identity) == {'container_app_name', 'container_app_revision', 'container_app_replica_name', 'hostname'}


def test_step_and_failed_download_timing_without_storage_changes(monkeypatch, clock):
    collector = metrics_module.Metrics(2, clock=lambda: clock.seconds)
    monkeypatch.setattr(app._ipwork_metrics_context, 'collector', collector, raising=False)
    monkeypatch.setattr(app, '_get_metadata_entity', lambda *a: {})
    downloads = []

    def download(*args):
        downloads.append(args)
        clock.seconds += 0.02
        return b'photo'

    def step(*args):
        clock.seconds += 0.03
        return {'hasData': False}

    monkeypatch.setattr(app, 'download_media_bytes', download)
    monkeypatch.setattr(app, 'IPWORK_STEP_PROCESSORS', {'ocr': step, 'face': step})
    results = app._run_ipwork_steps('u', 'f', ['ocr', 'face'])
    assert results == {'ocr': {'hasData': False}, 'face': {'hasData': False}}
    assert len(downloads) == 1
    summary = collector.snapshot(1, 1)['histograms']['window']
    assert summary['download']['count'] == 1
    assert summary['download']['sum'] == 20
    assert summary['step_ocr']['sum'] == summary['step_face']['sum'] == 30

    def failed_download(*args):
        clock.seconds += 0.04
        raise OSError('download failure')

    monkeypatch.setattr(app, 'download_media_bytes', failed_download)
    assert app._run_ipwork_steps('u', 'f', ['face'])['face']['error'] == 'download_failed'
    summary = collector.snapshot(1, 2)['histograms']['window']
    assert summary['download']['sum'] == 40
    assert 'step_face' not in summary


@pytest.mark.parametrize('kind, expected', [
    ('good', 'productive_completed'), ('error', 'completed_with_step_error'),
    ('failure_stage', 'completed_with_step_error'), ('malformed', 'completed_result_unknown'),
    ('already', 'already_processed'), ('stale', 'productive_completed'),
    ('backend_failed', 'completed_with_step_error'),
])
def test_completion_semantics_do_not_guess_new_uploads(monkeypatch, clock, kind, expected):
    collector = metrics_module.Metrics(2, clock=lambda: clock.seconds)
    monkeypatch.setattr(app._ipwork_metrics_context, 'collector', collector, raising=False)
    status = 'done' if kind in {'already', 'stale'} else 'pending'
    monkeypatch.setattr(app, 'claim_processing_lease', lambda *a, **kw: {'statuses': {'faceStatus': status}})
    monkeypatch.setattr(app, 'release_processing_lease', lambda *a, **kw: None)
    monkeypatch.setattr(app, '_upsert_job_status', lambda *a, **kw: None)
    monkeypatch.setattr(app, '_get_metadata_entity', lambda *a: {})
    monkeypatch.setattr(app, '_browser_processing_face_version_stale', lambda _: kind == 'stale')
    monkeypatch.setattr(app, '_queue_people_clustering_after_face_processing', lambda *a: None)
    face = {'hasData': False, 'faces': []}
    if kind == 'error':
        face['error'] = 'detector_failed'
    elif kind == 'failure_stage':
        face['faceFailureStage'] = 'detect'
    elif kind == 'malformed':
        face = None
    monkeypatch.setattr(app, '_run_ipwork_steps', lambda *a: {'face': face})
    # Historical successful face status cannot override an executed face error.
    monkeypatch.setattr(app, 'apply_client_processing_results_for_file', lambda *a, **kw: {'face_status': 'failed' if kind == 'backend_failed' else 'done'})
    outcome = app._handle_ipwork_queue_payload({'filename': 'f', 'steps': ['face']}, 'j', 'u')
    counters = collector.snapshot(1, 1)['window']
    assert outcome == ('noop' if kind == 'already' else 'done')
    assert counters[expected] == 1
    assert counters['productive_completed'] == int(kind in {'good', 'stale'})
    assert counters['eligibility_reprocessing'] == int(kind == 'stale')
    assert counters['eligibility_unknown'] == int(kind not in {'already', 'stale'})


def test_real_retry_exhaustion_acks_without_completion_or_milestone(monkeypatch, clock, memory_samples, caplog):
    caplog.set_level(logging.INFO, logger=app.worker_logger.name)
    message = SimpleNamespace(id='exhausted', dequeue_count=app.IPWORKER_MAX_RETRIES + 1,
                              content=json.dumps({'user_id': 'u', 'jobId': 'j', 'filename': 'f', 'steps': ['face']}))
    statuses, triggers = [], []
    monkeypatch.setattr(app, '_upsert_job_status', lambda *a, **kw: statuses.append((a, kw)))
    monkeypatch.setattr(app, '_handle_ipwork_queue_payload', lambda *a: pytest.fail('must not dispatch exhausted job'))
    monkeypatch.setattr(app, '_trigger_tools_index_rebuild', triggers.append)
    monkeypatch.setattr(app, 'IPWORKER_INDEX_REBUILD_MILESTONE', 1)

    class Queue:
        receives = 0
        deletes = []

        def create_queue(self):
            pass

        def receive_messages(self, **kw):
            self.receives += 1
            return [message] if self.receives == 1 else []

        def delete_message(self, msg):
            self.deletes.append(msg.id)

    queue = Queue()
    _run_queue(monkeypatch, queue)
    summary, = _metrics(caplog)
    counters = summary['window']
    assert counters['retry_exhausted'] == counters['ack'] == 1
    assert counters['done'] == counters['productive_completed'] == 0
    assert queue.deletes == ['exhausted'] and triggers == []
    assert statuses[0][0][-1] == 'failed'
    histograms = summary['histograms']['window']
    assert histograms['retry_depth']['count'] == histograms['task']['count'] == 1
    assert histograms['receipt_to_start']['count'] == histograms['receipt_to_ack']['count'] == 1


@pytest.mark.parametrize('reason', ['preparation_failed', 'visibility_budget'])
def test_deferred_wave_metrics_leave_receipts_unacked(monkeypatch, clock, memory_samples, caplog, reason):
    caplog.set_level(logging.INFO, logger=app.worker_logger.name)
    messages = [SimpleNamespace(id=str(i), content='{}', dequeue_count=1000) for i in range(2)]

    class Queue:
        receives = 0

        def create_queue(self):
            pass

        def receive_messages(self, **kw):
            self.receives += 1
            return messages if self.receives == 1 else []

        def delete_message(self, message):
            pytest.fail('deferred message ACKed')

    _prepare_queue(monkeypatch, Queue())
    monkeypatch.setattr(app, 'IPWORKER_FACE_RECONCILE_BATCH_SIZE', 8)

    def prepare(*args):
        if reason == 'preparation_failed':
            clock.seconds += 0.1
            raise OSError('scan failed')
        clock.seconds += app.IPWORKER_VISIBILITY_TIMEOUT_SECONDS / 2 + 1

    monkeypatch.setattr(app, '_prepare_ipwork_face_indexes', prepare)
    monkeypatch.setattr(app, '_process_ipwork_message', lambda *a: pytest.fail('deferred message processed'))
    monkeypatch.setattr(app.time, 'sleep', lambda _: (_ for _ in ()).throw(_StopLoop()))
    with pytest.raises(_StopLoop):
        app.run_ipworker()
    summary = _metrics(caplog)[-1]
    assert summary['cumulative']['defer_' + reason] == 2
    assert summary['cumulative']['ack'] == 0
    assert summary['histograms']['cumulative']['retry_depth']['bucket_counts'][-1] == 2
    assert summary['histograms']['cumulative']['wave']['count'] == 1
    assert summary['histograms']['cumulative']['preparation']['count'] == 1


@pytest.mark.parametrize('preparation', [False, True])
def test_watchdog_counters_oldest_task_and_no_invented_latency(
        monkeypatch, clock, memory_samples, caplog, preparation):
    import faulthandler
    caplog.set_level(logging.INFO, logger=app.worker_logger.name)
    monkeypatch.setattr(faulthandler, 'dump_traceback', lambda **kw: None)
    messages = [SimpleNamespace(id=str(i), content='{}', dequeue_count=1) for i in range(2)]

    class Queue:
        def create_queue(self):
            pass

        def receive_messages(self, **kw):
            return messages

        def delete_message(self, message):
            pytest.fail('watchdog must never ACK incomplete work')

    class BlockedExecutor:
        def __init__(self, **kw):
            pass

        def submit(self, *args):
            return Future()

        def shutdown(self, **kw):
            assert kw == {'wait': False, 'cancel_futures': True}

    _prepare_queue(monkeypatch, Queue())
    monkeypatch.setattr(app, 'IPWORKER_CONCURRENCY', 2)
    monkeypatch.setattr(app, 'IPWORKER_FACE_RECONCILE_BATCH_SIZE', 8 if preparation else 1)
    monkeypatch.setattr(app, 'IPWORKER_TASK_TIMEOUT_SECONDS', 120)
    monkeypatch.setattr(app, 'IPWORKER_VISIBILITY_TIMEOUT_SECONDS', 300)
    monkeypatch.setattr(app, 'ThreadPoolExecutor', BlockedExecutor)
    exits = []
    monkeypatch.setattr(app.os, '_exit', exits.append)

    def wait(futures, **kw):
        clock.seconds += 121
        return set(), set(futures)

    monkeypatch.setattr(app, 'wait', wait)
    app.run_ipworker()
    summary = _metrics(caplog)[-1]
    assert exits == [1]
    assert summary['cumulative']['watchdog_preparation'] == int(preparation)
    assert summary['cumulative']['watchdog_tasks'] == (0 if preparation else 2)
    assert summary['loop']['preparing'] == (2 if preparation else 0)
    assert summary['loop']['oldest_task_seconds'] == (0 if preparation else 121)
    assert 'task' not in summary['histograms']['cumulative']
    assert 'wave' not in summary['histograms']['cumulative']
    assert summary['cumulative']['ack'] == summary['cumulative']['done'] == 0


def test_exact_receipt_task_ack_and_wave_boundaries(monkeypatch, clock, memory_samples, caplog):
    caplog.set_level(logging.INFO, logger=app.worker_logger.name)
    message = SimpleNamespace(id='bounded', content='{}', dequeue_count=2)

    class Queue:
        receives = 0

        def create_queue(self):
            pass

        def receive_messages(self, **kw):
            self.receives += 1
            clock.seconds += 0.01
            return [message] if self.receives == 1 else []

        def delete_message(self, message):
            clock.seconds += 0.04

    _prepare_queue(monkeypatch, Queue())
    monkeypatch.setattr(app, 'IPWORKER_FACE_RECONCILE_BATCH_SIZE', 8)
    monkeypatch.setattr(app, 'IPWORKER_CONCURRENCY', 2)
    monkeypatch.setattr(app, '_prepare_ipwork_face_indexes', lambda *a: setattr(clock, 'seconds', clock.seconds + 0.1))

    def process(message):
        clock.seconds += 0.2
        return 'done'

    monkeypatch.setattr(app, '_process_ipwork_message', process)
    monkeypatch.setattr(app.time, 'sleep', lambda _: (_ for _ in ()).throw(_StopLoop()))
    with pytest.raises(_StopLoop):
        app.run_ipworker()
    summary, = _metrics(caplog)
    hist = summary['histograms']['window']
    assert hist['receive']['sum'] == 20
    assert hist['receipt_to_start']['sum'] == hist['preparation']['sum'] == 100
    assert hist['task']['sum'] == 200
    assert hist['ack']['sum'] == 40
    assert hist['receipt_to_ack']['sum'] == 340
    assert hist['wave']['sum'] == 350
    utilization = summary['utilization']['window']
    assert utilization['slot_seconds'] == 0.2
    assert utilization['preparation_only_seconds'] == 0.1
    assert utilization['idle_seconds'] == 0.06  # queue receive and ACK, not inference
    assert summary['loop'] == {'active_tasks': 0, 'preparing': 0, 'ready': 0,
                               'oldest_task_seconds': 0, 'preparation_seconds': 0}


def test_unsupported_payload_is_noop_not_completion():
    assert app._process_ipwork_message(SimpleNamespace(content='[]', dequeue_count=1)) == 'noop'


def test_window_resets_concurrently_without_losing_observations(clock):
    collector = metrics_module.Metrics(2, clock=lambda: clock.seconds)

    def record(_):
        for _ in range(1000):
            collector.observe('task', 1)

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(record, i) for i in range(4)]
        first = collector.snapshot(1, 1)
        for future in futures:
            future.result()
    second = collector.snapshot(1, 2)
    observed = sum(summary['histograms']['window'].get('task', {}).get('count', 0)
                   for summary in (first, second))
    assert observed == second['histograms']['cumulative']['task']['count'] == 4000


def test_apply_exception_records_latency_but_not_productive_completion(monkeypatch, clock):
    collector = metrics_module.Metrics(2, clock=lambda: clock.seconds)
    monkeypatch.setattr(app._ipwork_metrics_context, 'collector', collector, raising=False)
    monkeypatch.setattr(app, 'claim_processing_lease', lambda *a, **kw: {'statuses': {}})
    releases = []
    monkeypatch.setattr(app, 'release_processing_lease', lambda *a, **kw: releases.append(a))
    monkeypatch.setattr(app, '_upsert_job_status', lambda *a, **kw: None)
    monkeypatch.setattr(app, '_run_ipwork_steps', lambda *a: {'face': {'hasData': True, 'faces': []}})

    def failed_apply(*a, **kw):
        clock.seconds += 0.25
        raise OSError('transient persistence failure')

    monkeypatch.setattr(app, 'apply_client_processing_results_for_file', failed_apply)
    message = SimpleNamespace(content=json.dumps({'filename': 'f', 'user_id': 'u', 'jobId': 'j', 'steps': ['face']}),
                              dequeue_count=1)
    assert app._process_ipwork_message(message) == 'error'
    summary = collector.snapshot(1, 1)
    assert summary['histograms']['window']['apply']['sum'] == 250
    assert summary['window']['productive_completed'] == 0
    assert releases == [('u', 'f', 'ipworker-j')]


def test_populated_histogram_transport_records_stay_bounded(clock, memory_samples, caplog):
    caplog.set_level(logging.INFO, logger=app.worker_logger.name)
    window = app._IpworkThroughputWindow()
    for name in metrics_module.TIMINGS:
        window.collector.observe(name, 1234)
    clock.seconds += 60
    window.log(0)
    metrics, = _metrics(caplog)
    assert metrics['histograms']['window']['task']['p95_upper_bound'] == 2000
    assert 'bucket_counts' not in metrics['histograms']['window']['task']
    details = [r.getMessage() for r in caplog.records if r.getMessage().startswith('ipwork latency histogram metrics=')]
    assert len(details) == len(metrics_module.TIMINGS)
    assert all(len(r.getMessage().encode()) < 12000 for r in caplog.records)


def test_missing_metrics_scope_is_safe_and_does_not_capture_api_calls(monkeypatch):
    monkeypatch.setattr(app._ipwork_metrics_context, 'collector', None, raising=False)
    app._ipwork_metric('done')
    assert app._ipwork_timed_call('apply', lambda: 'same-result') == 'same-result'