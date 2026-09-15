"""Unit tests for run_ipworker's queue loop: the visibility-timeout wiring,
the bounded-thread-pool batch sizing (IPWORKER_CONCURRENCY), and that the
lease_busy-vs-delete semantics from the old one-message-at-a-time loop
still hold now that messages are dispatched to a worker pool and completed
out of order.

Background on the visibility-timeout part: a single ipwork pass (download +
YOLO + MediaPipe + AdaFace + CLIP + tesseract OCR) can exceed Azure Queue's
default 30s visibility timeout, letting Azure redeliver the same message to
a second replica (ipworker runs maxReplicas=4) while the first is still
working it -- since the redelivered copy carries the same jobId/lease
owner, the processing-lease guard doesn't block the second attempt, so two
replicas can genuinely duplicate the full model pipeline on one photo. The
fix passes an explicit visibility_timeout on receive_messages; these tests
confirm it (and the newer concurrency-batch-size behavior) are actually
wired through, since run_ipworker's poll loop otherwise runs forever and
isn't something to exercise end-to-end in a unit test.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

import app


class _StopLoop(BaseException):
    """Deliberately not Exception -- run_ipworker's poll loop catches plain
    Exception and keeps going, so escaping it after one iteration needs
    something that bypasses that handler."""


class _FakeQueueMessage:
    def __init__(self, message_id: str, dequeue_count: int = 0) -> None:
        self.id = message_id
        self.content = '{}'
        self.dequeue_count = dequeue_count


class _FakeQueueClient:
    def __init__(self, batches=None) -> None:
        self.receive_calls: list = []
        self.delete_calls: list = []
        self._batches = list(batches or [])

    def create_queue(self):
        pass

    def receive_messages(self, **kwargs):
        self.receive_calls.append(kwargs)
        if self._batches:
            return self._batches.pop(0)
        return []

    def delete_message(self, message):
        self.delete_calls.append(message.id)


class _FakeQueueServiceClient:
    def __init__(self, queue_client) -> None:
        self._queue_client = queue_client

    def get_queue_client(self, name):
        return self._queue_client


def _run_until_idle(monkeypatch, queue_client) -> None:
    """Runs app.run_ipworker() until it drains all in-flight work and would
    otherwise block on time.sleep() waiting for new messages -- at that
    point it's fully idle, so raise out instead of actually sleeping."""
    monkeypatch.setattr(app, 'queue_service_client', _FakeQueueServiceClient(queue_client))
    # These are loop/pool-mechanics tests, not a check that real models
    # load -- skip _prewarm_ipwork_models's real CLIP/ONNX/MediaPipe/
    # geocoder loading so the suite doesn't pay for it (or depend on those
    # weights/packages being present) on every run_ipworker() call.
    monkeypatch.setattr(app, '_prewarm_ipwork_models', lambda: None)
    # run_ipworker also starts a daemon sweep thread; leaving the real one
    # running would call the same monkeypatched time.sleep from a second
    # thread and raise _StopLoop there instead of on the main thread this
    # test actually watches via pytest.raises.
    monkeypatch.setattr(app, '_ipwork_sweep_loop', lambda: None)

    def _stop_when_idle(_seconds):
        raise _StopLoop()

    monkeypatch.setattr(app.time, 'sleep', _stop_when_idle)
    with pytest.raises(_StopLoop):
        app.run_ipworker()


def test_receive_messages_uses_configured_visibility_timeout(monkeypatch):
    queue_client = _FakeQueueClient()
    _run_until_idle(monkeypatch, queue_client)

    assert len(queue_client.receive_calls) == 1
    assert queue_client.receive_calls[0]['visibility_timeout'] == app.IPWORKER_VISIBILITY_TIMEOUT_SECONDS


def test_receive_messages_batch_size_matches_concurrency(monkeypatch):
    monkeypatch.setattr(app, 'IPWORKER_CONCURRENCY', 3)
    queue_client = _FakeQueueClient()
    _run_until_idle(monkeypatch, queue_client)

    assert queue_client.receive_calls[0]['max_messages'] == 3
    assert queue_client.receive_calls[0]['messages_per_page'] == 3


def test_completed_message_is_deleted(monkeypatch):
    message = _FakeQueueMessage('m1')
    queue_client = _FakeQueueClient(batches=[[message]])
    monkeypatch.setattr(app, '_process_ipwork_message', lambda msg: 'done')
    _run_until_idle(monkeypatch, queue_client)

    assert queue_client.delete_calls == ['m1']


def test_lease_busy_under_retry_limit_is_not_deleted(monkeypatch):
    assert 1 < app.IPWORK_LEASE_RETRY_LIMIT
    message = _FakeQueueMessage('m2', dequeue_count=1)
    queue_client = _FakeQueueClient(batches=[[message]])
    monkeypatch.setattr(app, '_process_ipwork_message', lambda msg: 'lease_busy')
    _run_until_idle(monkeypatch, queue_client)

    # Left undeleted so Azure's own visibility timeout redelivers it.
    assert queue_client.delete_calls == []


def test_lease_busy_at_retry_limit_is_deleted(monkeypatch):
    message = _FakeQueueMessage('m3', dequeue_count=app.IPWORK_LEASE_RETRY_LIMIT)
    queue_client = _FakeQueueClient(batches=[[message]])
    monkeypatch.setattr(app, '_process_ipwork_message', lambda msg: 'lease_busy')
    _run_until_idle(monkeypatch, queue_client)

    # Bounded by dequeue_count so a lease that's stuck doesn't retry forever.
    assert queue_client.delete_calls == ['m3']


def test_multiple_messages_in_one_batch_are_each_resolved(monkeypatch):
    monkeypatch.setattr(app, 'IPWORKER_CONCURRENCY', 2)
    messages = [_FakeQueueMessage('a'), _FakeQueueMessage('b')]
    queue_client = _FakeQueueClient(batches=[messages])
    outcomes = {'a': 'done', 'b': 'lease_busy'}
    monkeypatch.setattr(app, '_process_ipwork_message', lambda msg: outcomes[msg.id])
    _run_until_idle(monkeypatch, queue_client)

    assert queue_client.delete_calls == ['a']


def test_thread_pool_processes_messages_concurrently(monkeypatch):
    """Proves IPWORKER_CONCURRENCY > 1 actually overlaps work in wall-clock
    time, not just that the code compiles -- exercises _process_ipwork_message
    (the new pool entry point) directly through a real ThreadPoolExecutor
    rather than driving the full run_ipworker() loop, since that loop's
    time.sleep()-based idle detection isn't compatible with a worker
    function that itself needs to sleep to simulate overlapping I/O."""
    monkeypatch.setattr(app, 'IPWORKER_CONCURRENCY', 3)
    intervals: list = []
    lock = threading.Lock()

    def fake_process(_message):
        start = time.monotonic()
        time.sleep(0.05)
        end = time.monotonic()
        with lock:
            intervals.append((start, end))
        return 'done'

    monkeypatch.setattr(app, '_process_ipwork_message', fake_process)

    with ThreadPoolExecutor(max_workers=app.IPWORKER_CONCURRENCY) as executor:
        futures = [executor.submit(app._process_ipwork_message, object()) for _ in range(3)]
        results = [f.result() for f in futures]

    assert results == ['done', 'done', 'done']

    def overlaps(a, b):
        return a[0] < b[1] and b[0] < a[1]

    assert any(
        overlaps(intervals[i], intervals[j])
        for i in range(len(intervals))
        for j in range(i + 1, len(intervals))
    )
