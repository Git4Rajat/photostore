"""Local-only diagnostics contracts: fake clocks, queues, and sweep phases."""
from concurrent.futures import Future
import json
import logging
from types import SimpleNamespace

import pytest

import app


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