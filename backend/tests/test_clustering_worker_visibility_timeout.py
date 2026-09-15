"""Unit test for the queue redelivery fix in run_clustering_worker: a full
DBSCAN pass can exceed Azure Queue's default 30s visibility timeout, causing
the same message to be redelivered (and duplicate-processed) before the
finally-block delete runs. The fix passes an explicit, generous
visibility_timeout on receive_messages -- this test just confirms it's
actually wired through, since run_clustering_worker's poll loop otherwise
runs forever and isn't something to exercise end-to-end in a unit test.
"""
from __future__ import annotations

import pytest

import app


class _StopLoop(BaseException):
    """Deliberately not Exception -- run_clustering_worker's poll loop catches
    plain Exception and keeps going, so escaping it after one iteration needs
    something that bypasses that handler."""


class _FakeQueueClient:
    def __init__(self) -> None:
        self.receive_calls: list = []

    def create_queue(self):
        pass

    def receive_messages(self, **kwargs):
        self.receive_calls.append(kwargs)
        return []


class _FakeQueueServiceClient:
    def __init__(self, queue_client) -> None:
        self._queue_client = queue_client

    def get_queue_client(self, name):
        return self._queue_client


def test_receive_messages_uses_configured_visibility_timeout(monkeypatch):
    # run_clustering_worker polls the priority library-ops queue and the
    # general clustering queue every cycle (see LIBRARY_OPS_QUEUE_NAME) --
    # both resolve to this same fake client here since the test only cares
    # that whichever queue is hit, the visibility_timeout is wired through.
    queue_client = _FakeQueueClient()
    monkeypatch.setattr(app, 'queue_service_client', _FakeQueueServiceClient(queue_client))

    def _stop_after_first_poll(_seconds):
        raise _StopLoop()

    monkeypatch.setattr(app.time, 'sleep', _stop_after_first_poll)

    with pytest.raises(_StopLoop):
        app.run_clustering_worker()

    assert len(queue_client.receive_calls) == 2  # library-ops poll, then clustering poll
    assert all(
        call['visibility_timeout'] == app.CLUSTERING_WORKER_VISIBILITY_TIMEOUT_SECONDS
        for call in queue_client.receive_calls
    )
