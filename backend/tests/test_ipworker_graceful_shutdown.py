"""Unit tests for run_ipworker's SIGTERM/SIGINT handling.

Background: Container Apps sends SIGTERM to scale an ipworker replica down,
not just on deploys -- KEDA recomputes the target replica count off the
shrinking visible-message count throughout a backlog drain, so this fires
constantly under load. Python's default SIGTERM disposition kills the
process immediately, with no chance to finish work already claimed off the
queue. That can strike between a message finishing its work (results
written) and the delete_message call that removes it from the queue,
orphaning a message that then gets endlessly redelivered every
IPWORKER_VISIBILITY_TIMEOUT_SECONDS forever, since the redelivered copy just
finds its own work already done and loops the same "almost delete it" race
again.

These tests send a real signal (via os.kill) into the running process rather
than reaching into run_ipworker's internals, since the whole point is to
prove the OS-level handler is actually wired up -- not just that some
boolean flag would gate the loop correctly if set.
"""
from __future__ import annotations

import os
import signal
import threading
import time

import pytest

import app


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


def _quiet_startup(monkeypatch) -> None:
    monkeypatch.setattr(app, '_prewarm_ipwork_models', lambda: None)
    monkeypatch.setattr(app, '_ipwork_sweep_loop', lambda: None)
    monkeypatch.setenv('IPWORKER_POLL_SECONDS', '0.05')


def test_sigterm_drains_in_flight_message_and_stops_fetching_new_work(monkeypatch):
    """A message already being processed when SIGTERM arrives should still
    get deleted; a second batch sitting behind it should never be claimed."""
    _quiet_startup(monkeypatch)
    in_flight_message = _FakeQueueMessage('in-flight')
    never_claimed_message = _FakeQueueMessage('never-claimed')
    queue_client = _FakeQueueClient(batches=[[in_flight_message], [never_claimed_message]])
    monkeypatch.setattr(app, 'queue_service_client', _FakeQueueServiceClient(queue_client))

    started = threading.Event()

    def fake_process(message):
        started.set()
        time.sleep(0.2)
        return 'done'

    monkeypatch.setattr(app, '_process_ipwork_message', fake_process)

    def send_sigterm_once_processing_started():
        assert started.wait(timeout=2), 'processing never started'
        os.kill(os.getpid(), signal.SIGTERM)

    sender = threading.Thread(target=send_sigterm_once_processing_started, daemon=True)
    sender.start()

    start_time = time.monotonic()
    app.run_ipworker()
    elapsed = time.monotonic() - start_time
    sender.join(timeout=2)

    assert queue_client.delete_calls == ['in-flight']
    assert 'never-claimed' not in queue_client.delete_calls
    # Only one receive_messages call should have gone through before the
    # signal landed and shutdown suppressed further fetching.
    assert len(queue_client.receive_calls) == 1
    # Should return well under the (default 25s) shutdown grace period since
    # the in-flight work finished on its own in ~0.2s.
    assert elapsed < 5


def test_shutdown_grace_period_force_exits_without_hanging(monkeypatch):
    """A message that's still running when the grace period elapses must
    not block run_ipworker from returning -- Container Apps' own SIGKILL is
    coming right after the grace window regardless, so hanging here just
    wastes the little cleanup time available."""
    _quiet_startup(monkeypatch)
    monkeypatch.setattr(app, 'IPWORKER_SHUTDOWN_GRACE_SECONDS', 0.15)
    stuck_message = _FakeQueueMessage('stuck')
    queue_client = _FakeQueueClient(batches=[[stuck_message]])
    monkeypatch.setattr(app, 'queue_service_client', _FakeQueueServiceClient(queue_client))

    started = threading.Event()

    def fake_process(message):
        started.set()
        time.sleep(2)  # deliberately outlasts the shutdown grace period
        return 'done'

    monkeypatch.setattr(app, '_process_ipwork_message', fake_process)

    exit_calls: list = []
    monkeypatch.setattr(app.os, '_exit', lambda code: exit_calls.append(code))

    def send_sigterm_once_processing_started():
        assert started.wait(timeout=2), 'processing never started'
        os.kill(os.getpid(), signal.SIGTERM)

    sender = threading.Thread(target=send_sigterm_once_processing_started, daemon=True)
    sender.start()

    start_time = time.monotonic()
    app.run_ipworker()
    elapsed = time.monotonic() - start_time
    sender.join(timeout=2)

    # Returned close to the grace period, not close to the 2s stuck task.
    assert elapsed < 1.5
    # The stuck message was never deleted -- it'll be redelivered after the
    # visibility timeout, which is correct (its work never finished).
    assert queue_client.delete_calls == []
    # The grace-exhausted path force-exits instead of relying on normal
    # interpreter shutdown to wait out the still-running thread.
    assert exit_calls == [0]
