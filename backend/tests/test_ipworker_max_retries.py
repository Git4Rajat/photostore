"""Unit tests for _process_ipwork_message's max-retry ceiling.

Distinct from IPWORK_LEASE_RETRY_LIMIT (test_ipworker_visibility_timeout.py),
which only bounds the lease_busy race. This bounds *every* outcome: a
message whose processing reliably crashes the whole replica (e.g. a
corrupt/poison image) never reaches _process_ipwork_message's own except
block, so without this it would be redelivered by Azure forever, each
attempt burning a full replica's worth of compute. See
CLUSTERING_WORKER_MAX_RETRIES's twin fix in
test_clustering_worker_max_retries.py -- same production incident
(2026-09-03) motivated both.
"""
from __future__ import annotations

import json

import pytest

import app
from fakes import FakeTable


class _FakeQueueMessage:
    def __init__(self, content: dict, dequeue_count: int = 0) -> None:
        self.id = 'm1'
        self.content = json.dumps(content)
        self.dequeue_count = dequeue_count


@pytest.fixture(autouse=True)
def metadata_table(monkeypatch):
    table = FakeTable()
    monkeypatch.setattr(app, 'metadata_table_client', table)
    return table


@pytest.fixture(autouse=True)
def dispatch_spy(monkeypatch):
    calls = []
    monkeypatch.setattr(
        app, '_handle_ipwork_queue_payload',
        lambda payload, job_id, user_id: calls.append((payload, job_id, user_id)) or 'done',
    )
    return calls


def test_exceeding_max_retries_skips_dispatch_and_marks_job_failed(metadata_table, dispatch_spy):
    message = _FakeQueueMessage(
        {'jobId': 'ipwork:u1:f1', 'user_id': 'u1', 'filename': 'f1.jpg'},
        dequeue_count=app.IPWORKER_MAX_RETRIES + 1,
    )

    outcome = app._process_ipwork_message(message)

    assert outcome == 'done'
    assert dispatch_spy == []  # never reached the real pipeline
    row = metadata_table.get_entity('jobs', app._job_row_key('ipwork:u1:f1'))
    assert row['status'] == 'failed'
    assert 'retries' in row['error'].lower()


def test_at_max_retries_still_dispatches_normally(metadata_table, dispatch_spy):
    message = _FakeQueueMessage(
        {'jobId': 'ipwork:u1:f1', 'user_id': 'u1', 'filename': 'f1.jpg'},
        dequeue_count=app.IPWORKER_MAX_RETRIES,
    )

    outcome = app._process_ipwork_message(message)

    assert outcome == 'done'
    assert len(dispatch_spy) == 1


def test_well_under_max_retries_dispatches_normally(metadata_table, dispatch_spy):
    message = _FakeQueueMessage(
        {'jobId': 'ipwork:u1:f1', 'user_id': 'u1', 'filename': 'f1.jpg'},
        dequeue_count=1,
    )

    outcome = app._process_ipwork_message(message)

    assert outcome == 'done'
    assert len(dispatch_spy) == 1
