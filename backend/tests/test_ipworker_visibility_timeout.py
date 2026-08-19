"""Unit test for the queue redelivery fix in run_ipworker: a single ipwork
pass (download + YOLO + MediaPipe + AdaFace + CLIP + tesseract OCR) can
exceed Azure Queue's default 30s visibility timeout, letting Azure redeliver
the same message to a second replica (ipworker runs maxReplicas=4) while the
first is still working it -- since the redelivered copy carries the same
jobId/lease owner, the processing-lease guard doesn't block the second
attempt, so two replicas can genuinely duplicate the full model pipeline on
one photo. The fix passes an explicit visibility_timeout on receive_messages;
this test just confirms it's actually wired through, since run_ipworker's
poll loop otherwise runs forever and isn't something to exercise end-to-end
in a unit test.
"""
from __future__ import annotations

import pytest

import app


class _StopLoop(BaseException):
    """Deliberately not Exception -- run_ipworker's poll loop catches plain
    Exception and keeps going, so escaping it after one iteration needs
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
    queue_client = _FakeQueueClient()
    monkeypatch.setattr(app, 'queue_service_client', _FakeQueueServiceClient(queue_client))

    def _stop_after_first_poll(_seconds):
        raise _StopLoop()

    monkeypatch.setattr(app.time, 'sleep', _stop_after_first_poll)

    with pytest.raises(_StopLoop):
        app.run_ipworker()

    assert len(queue_client.receive_calls) == 1
    assert queue_client.receive_calls[0]['visibility_timeout'] == app.IPWORKER_VISIBILITY_TIMEOUT_SECONDS
