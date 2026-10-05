"""Unit test for run_clustering_worker's SIGTERM/SIGINT handling.

Background: KEDA sends SIGTERM to scale a clustering-worker replica down
(queueLength=1 recomputes the target replica count constantly, not just on
deploys), and the loop previously had no signal handler at all -- Python's
default SIGTERM disposition kills the process immediately, mid-
cluster_user_faces() if one happens to be running. The queue message
survives (still invisible for the rest of its visibility timeout) so the
job isn't lost forever, but the jobs-table row is stranded at 'running'
until /jobs/status's stale-cutoff flags it 'failed' -- a spurious failure
even though the work would have finished fine.

This test sends a real signal (via os.kill) into the running process to
prove the OS-level handler is actually wired up, mirroring
test_ipworker_graceful_shutdown.py's approach for the sibling worker.
"""
from __future__ import annotations

import os
import signal
import threading
import time
from types import SimpleNamespace

import app
import pytest


class _FakeQueueMessage:
    def __init__(self, message_id: str) -> None:
        self.id = message_id
        self.content = '{}'


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


def test_sigterm_finishes_in_flight_message_and_stops_polling(monkeypatch):
    """A message already being processed when SIGTERM arrives should still
    get deleted; a second batch sitting behind it should never be claimed."""
    monkeypatch.setenv('CLUSTERING_WORKER_POLL_SECONDS', '0.05')
    in_flight_message = _FakeQueueMessage('in-flight')
    never_claimed_message = _FakeQueueMessage('never-claimed')
    queue_client = _FakeQueueClient(batches=[[in_flight_message], [never_claimed_message]])
    monkeypatch.setattr(app, 'queue_service_client', _FakeQueueServiceClient(queue_client))

    started = threading.Event()

    def fake_handle(payload, job_id, user_id, job_type):
        started.set()
        time.sleep(0.2)

    monkeypatch.setattr(app, '_handle_clustering_queue_payload', fake_handle)

    def send_sigterm_once_processing_started():
        assert started.wait(timeout=2), 'processing never started'
        os.kill(os.getpid(), signal.SIGTERM)

    sender = threading.Thread(target=send_sigterm_once_processing_started, daemon=True)
    sender.start()

    start_time = time.monotonic()
    app.run_clustering_worker()
    elapsed = time.monotonic() - start_time
    sender.join(timeout=2)

    assert queue_client.delete_calls == ['in-flight']
    assert 'never-claimed' not in queue_client.delete_calls
    # Only one receive_messages call should have gone through before the
    # signal landed and the loop condition stopped further polling. Holds
    # even with run_clustering_worker's priority library-ops queue check
    # added (both resolve to this same fake client): finding a message on
    # the first (library-ops) poll short-circuits the second (clustering)
    # poll for that iteration, and the SIGTERM lands during that message's
    # processing, stopping the loop before a second iteration ever starts.
    assert len(queue_client.receive_calls) == 1
    # Returns promptly since the in-flight work finished on its own in ~0.2s
    # -- no grace-period wait needed for this (synchronous, one-at-a-time)
    # loop, unlike run_ipworker's thread-pool drain.
    assert elapsed < 2


@pytest.mark.parametrize('batch_size', [1, 8])
def test_shutdown_drains_inflight_group_before_final_checkpoint(monkeypatch, batch_size):
    monkeypatch.setenv('CLUSTERING_WORKER_BATCH_SIZE', str(batch_size))
    queue = _FakeQueueClient()
    monkeypatch.setattr(app, 'queue_service_client', _FakeQueueServiceClient(queue))
    handlers, events = {}, []
    monkeypatch.setattr(app.signal, 'signal', lambda sig, handler: handlers.update({sig: handler}))

    def finish_group(*args, **kwargs):
        events.append('started')
        handlers[signal.SIGTERM](signal.SIGTERM, None)
        events.extend(['durable', 'acknowledged'])
        return True

    def poll_single(client, name, *args):
        if name == app.LIBRARY_OPS_QUEUE_NAME:
            return False
        return finish_group()

    monkeypatch.setattr(app, '_poll_clustering_queue_once', poll_single)
    monkeypatch.setattr(app, '_poll_clustering_queue_batch_once', finish_group)
    monkeypatch.setattr(app, '_live_faiss_assigner', SimpleNamespace(
        final_checkpoint=lambda: events.append('final_checkpoint')))
    app.run_clustering_worker()
    assert events == ['started', 'durable', 'acknowledged', 'final_checkpoint']


def test_shutdown_without_runtime_never_initializes_adapter(monkeypatch):
    queue = _FakeQueueClient()
    monkeypatch.setattr(app, 'queue_service_client', _FakeQueueServiceClient(queue))
    monkeypatch.setattr(app, '_live_faiss_assigner', None)
    monkeypatch.setattr(app.time, 'sleep', lambda *a: pytest.fail('shutdown must not idle-wait'))
    handlers = {}
    monkeypatch.setattr(app.signal, 'signal', lambda sig, handler: handlers.update({sig: handler}))
    monkeypatch.setattr(app, '_get_live_faiss_assigner', lambda: pytest.fail('must not initialize'))

    def poll(*args):
        handlers[signal.SIGTERM](signal.SIGTERM, None)
        return False

    monkeypatch.setattr(app, '_poll_clustering_queue_once', poll)
    app.run_clustering_worker()


def test_shutdown_checkpoint_error_does_not_break_exit(monkeypatch, caplog):
    queue = _FakeQueueClient()
    monkeypatch.setattr(app, 'queue_service_client', _FakeQueueServiceClient(queue))
    handlers = {}
    monkeypatch.setattr(app.signal, 'signal', lambda sig, handler: handlers.update({sig: handler}))

    def poll(*args):
        handlers[signal.SIGTERM](signal.SIGTERM, None)
        return True

    def fail():
        raise OSError('share disconnected')

    monkeypatch.setattr(app, '_poll_clustering_queue_once', poll)
    monkeypatch.setattr(app, '_live_faiss_assigner', SimpleNamespace(final_checkpoint=fail))
    app.run_clustering_worker()
    assert 'Final clustering checkpoint failed' in caplog.text
