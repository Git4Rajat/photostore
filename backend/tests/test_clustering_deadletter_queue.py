"""Unit tests for the clustering/library-ops dead-letter queue (Milestone 5).

Before this change, a message that exceeded CLUSTERING_WORKER_MAX_RETRIES /
LIBRARY_CLEAN_MAX_RETRIES was just deleted and the job marked 'failed' --
a genuinely poisoned payload was gone for good, leaving only the job-status
row's generic error string to go on. _poll_clustering_queue_once now
publishes the exhausted message's body (plus failure context) to a
dedicated dead-letter queue before deleting it from the source queue, so an
operator can inspect or manually replay it.
"""
from __future__ import annotations

import json

import pytest

import app


class _FakeMessage:
    def __init__(self, content, dequeue_count=1):
        self.content = content
        self.dequeue_count = dequeue_count
        self.insertion_time = None


class _FakeQueueClient:
    def __init__(self, messages=None):
        self.messages = list(messages or [])
        self.deleted = []
        self.sent = []

    def receive_messages(self, *, messages_per_page=1, max_messages=1, visibility_timeout=None):
        return list(self.messages[:1])

    def delete_message(self, message):
        self.deleted.append(message)
        if message in self.messages:
            self.messages.remove(message)

    def update_message(self, message, *, visibility_timeout=None):
        return message

    def send_message(self, content):
        self.sent.append(content)


@pytest.fixture(autouse=True)
def job_status_noop(monkeypatch):
    monkeypatch.setattr(app, '_upsert_job_status', lambda *a, **k: None)
    monkeypatch.setattr(app, 'library_store', None)


def test_exhausted_message_is_dead_lettered_before_delete():
    payload = {'jobId': 'job-1', 'user_id': 'u1', 'type': 'people_cluster', 'filename': 'photo.jpg'}
    message = _FakeMessage(json.dumps(payload), dequeue_count=6)
    source = _FakeQueueClient([message])
    deadletter = _FakeQueueClient()

    processed = app._poll_clustering_queue_once(source, 'photostore-clustering', max_retries=5, deadletter_queue_client=deadletter)

    assert processed is True
    assert source.deleted == [message]
    assert len(deadletter.sent) == 1
    dead_body = json.loads(deadletter.sent[0])
    assert dead_body['sourceQueue'] == 'photostore-clustering'
    assert dead_body['dequeueCount'] == 6
    assert dead_body['maxRetries'] == 5
    assert dead_body['jobId'] == 'job-1'
    assert dead_body['userId'] == 'u1'
    assert dead_body['jobType'] == 'people_cluster'
    assert dead_body['originalPayload'] == payload


def test_missing_deadletter_client_preserves_message_without_crashing():
    """Failed dead-letter configuration must not discard the only copy."""
    payload = {'jobId': 'job-2', 'user_id': 'u1', 'type': 'people_cluster'}
    message = _FakeMessage(json.dumps(payload), dequeue_count=10)
    source = _FakeQueueClient([message])

    processed = app._poll_clustering_queue_once(source, 'photostore-clustering', max_retries=5)

    assert processed is True
    assert source.deleted == []
    assert source.messages == [message]


def test_deadletter_send_failure_preserves_poison_message():
    """A storage outage must not permanently discard exhausted work."""
    payload = {'jobId': 'job-3', 'user_id': 'u1', 'type': 'people_cluster'}
    message = _FakeMessage(json.dumps(payload), dequeue_count=10)
    source = _FakeQueueClient([message])

    class _BrokenDeadletter(_FakeQueueClient):
        def send_message(self, content):
            raise RuntimeError('queue unavailable')

    processed = app._poll_clustering_queue_once(
        source, 'photostore-clustering', max_retries=5, deadletter_queue_client=_BrokenDeadletter(),
    )

    assert processed is True
    assert source.deleted == []
    assert source.messages == [message]


def test_message_under_retry_limit_is_not_dead_lettered():
    payload = {'jobId': 'job-4', 'user_id': 'u1', 'type': 'people_incremental_assign'}
    message = _FakeMessage(json.dumps(payload), dequeue_count=1)
    source = _FakeQueueClient([message])
    deadletter = _FakeQueueClient()

    app._poll_clustering_queue_once(source, 'photostore-clustering', max_retries=5, deadletter_queue_client=deadletter)

    assert deadletter.sent == []


def test_malformed_deadletter_preserves_original_body():
    message = _FakeMessage('{invalid JSON', dequeue_count=6)
    source, deadletter = _FakeQueueClient([message]), _FakeQueueClient()
    app._poll_clustering_queue_once(source, 'photostore-clustering', 5, deadletter)
    assert json.loads(deadletter.sent[0])['originalBody'] == message.content
    assert source.deleted == [message]


def test_incremental_assignment_exception_is_retried(monkeypatch):
    monkeypatch.setattr(app, '_people_features_available', lambda: True)
    monkeypatch.setattr(app, '_get_metadata_entity', lambda *args: {'face_status': 'done'})
    monkeypatch.setattr(app, '_face_ids_awaiting_person_assignment', lambda *args: ['f1'])
    def fail(*args):
        raise RuntimeError('storage outage')
    monkeypatch.setattr(app, '_assign_faces_to_people_incrementally', fail)
    message = _FakeMessage(json.dumps({'user_id': 'u1', 'type': 'people_incremental_assign',
                                       'filename': 'photo.jpg'}))
    source = _FakeQueueClient([message])
    app._poll_clustering_queue_once(source, 'photostore-clustering', 5)
    assert source.deleted == []
    assert source.messages == [message]
