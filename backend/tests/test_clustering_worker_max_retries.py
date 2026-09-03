"""Unit tests for run_clustering_worker's max-retry ceiling.

Confirmed live 2026-09-03: a batch of duplicate clustering jobs (see the
atomic-claim fix on _clustering_maintenance_due,
test_clustering_maintenance_cooldown.py) kept getting redelivered across
unrelated replica restarts/redeploys for hours -- active lease renewal
(test_clustering_worker_visibility_timeout.py) stops a *healthy* replica
from losing its message mid-job, but can't help a message that keeps being
redelivered for other reasons (repeated restarts, a payload that crashes
the whole process before any exception handler runs). Without a ceiling,
Azure just keeps redelivering such a message forever. See
test_ipworker_max_retries.py for the twin fix on the ipworker side.
"""
from __future__ import annotations

import json

import pytest

import app
from fakes import FakeTable


class _StopLoop(BaseException):
    """Deliberately not Exception -- run_clustering_worker's poll loop
    catches plain Exception and keeps going, so escaping it after one
    iteration needs something that bypasses that handler."""


class _FakeQueueMessage:
    def __init__(self, content: dict, dequeue_count: int = 0) -> None:
        self.id = 'm1'
        self.content = json.dumps(content)
        self.dequeue_count = dequeue_count


class _FakeQueueClient:
    def __init__(self, batches=None) -> None:
        self.delete_calls: list = []
        self._batches = list(batches or [])

    def create_queue(self):
        pass

    def receive_messages(self, **kwargs):
        if self._batches:
            return self._batches.pop(0)
        return []

    def delete_message(self, message):
        self.delete_calls.append(message.id)

    def update_message(self, message, **kwargs):
        # Defensive only -- the lease-renewal thread waits
        # CLUSTERING_WORKER_LEASE_RENEWAL_SECONDS (40s) before its first
        # renewal attempt and these tests finish in milliseconds, so this
        # shouldn't actually be hit; present in case of scheduler timing.
        return message


class _FakeQueueServiceClient:
    def __init__(self, queue_client) -> None:
        self._queue_client = queue_client

    def get_queue_client(self, name):
        return self._queue_client


@pytest.fixture(autouse=True)
def metadata_table(monkeypatch):
    table = FakeTable()
    monkeypatch.setattr(app, 'metadata_table_client', table)
    return table


@pytest.fixture(autouse=True)
def dispatch_spy(monkeypatch):
    calls = []
    monkeypatch.setattr(
        app, '_handle_clustering_queue_payload',
        lambda payload, job_id, user_id, job_type: calls.append((payload, job_id, user_id, job_type)),
    )
    return calls


def _run_one_poll(monkeypatch, queue_client) -> None:
    monkeypatch.setattr(app, 'queue_service_client', _FakeQueueServiceClient(queue_client))

    def _stop(_seconds):
        raise _StopLoop()

    monkeypatch.setattr(app.time, 'sleep', _stop)
    with pytest.raises(_StopLoop):
        app.run_clustering_worker()


def test_exceeding_max_retries_skips_dispatch_deletes_message_marks_failed(
    monkeypatch, metadata_table, dispatch_spy,
):
    message = _FakeQueueMessage(
        {'jobId': 'cluster:u1:job1', 'user_id': 'u1', 'type': 'people_cluster'},
        dequeue_count=app.CLUSTERING_WORKER_MAX_RETRIES + 1,
    )
    queue_client = _FakeQueueClient(batches=[[message]])

    _run_one_poll(monkeypatch, queue_client)

    assert dispatch_spy == []  # never reached the real pipeline
    assert queue_client.delete_calls == ['m1']
    row = metadata_table.get_entity('jobs', app._job_row_key('cluster:u1:job1'))
    assert row['status'] == 'failed'
    assert 'retries' in row['error'].lower()
    assert row['jobType'] == 'people_cluster'


def test_at_max_retries_still_dispatches_normally(monkeypatch, metadata_table, dispatch_spy):
    message = _FakeQueueMessage(
        {'jobId': 'cluster:u1:job1', 'user_id': 'u1', 'type': 'people_cluster'},
        dequeue_count=app.CLUSTERING_WORKER_MAX_RETRIES,
    )
    queue_client = _FakeQueueClient(batches=[[message]])

    _run_one_poll(monkeypatch, queue_client)

    assert len(dispatch_spy) == 1


def test_well_under_max_retries_dispatches_normally(monkeypatch, metadata_table, dispatch_spy):
    message = _FakeQueueMessage(
        {'jobId': 'cluster:u1:job1', 'user_id': 'u1', 'type': 'people_cluster'},
        dequeue_count=1,
    )
    queue_client = _FakeQueueClient(batches=[[message]])

    _run_one_poll(monkeypatch, queue_client)

    assert len(dispatch_spy) == 1
