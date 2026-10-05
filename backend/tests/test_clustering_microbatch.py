"""Queue microbatch boundaries preserve individual acknowledgements."""
from contextlib import contextmanager
import json
from types import SimpleNamespace

import app
import pytest


class Queue:
    def __init__(self, messages):
        self.messages = messages
        self.deleted = []
        self.received = []

    def receive_messages(self, **kwargs):
        self.received.append(kwargs)
        return self.messages[:kwargs['max_messages']]

    def update_message(self, message, **kwargs):
        return message

    def delete_message(self, message):
        self.deleted.append(message)


def message(filename, user='u', **kwargs):
    return SimpleNamespace(content=json.dumps(dict(type='people_incremental_assign',
        user_id=user, filename=filename, **kwargs)), dequeue_count=1, insertion_time=None)


@pytest.fixture
def setup(monkeypatch):
    calls, leases = [], []

    class Assigner:
        config = SimpleNamespace(io_concurrency=4)

        @contextmanager
        def batch(self, user, ids):
            leases.append((user, ids))

            def assign(filename, faces):
                calls.append((filename, faces))
                if filename == 'fail.jpg':
                    raise RuntimeError('write failed')
            yield assign

    monkeypatch.setattr(app, 'PEOPLE_ASSIGNMENT_ENGINE', 'faiss')
    monkeypatch.setattr(app, '_get_live_faiss_assigner', lambda: Assigner())
    monkeypatch.setattr(app, '_people_features_available', lambda: True)
    monkeypatch.setattr(app, '_get_metadata_entity', lambda *a: {'face_status': 'done'})
    monkeypatch.setattr(app, '_face_ids_awaiting_person_assignment', lambda u, f: [f])
    return calls, leases


def test_same_library_batch_shares_lease_and_acks_each_success(setup):
    calls, leases = setup
    messages = [message('a.jpg'), message('b.jpg'), message('c.jpg')]
    queue = Queue(messages)
    assert app._poll_clustering_queue_batch_once(queue, 'q', 5, batch_size=8)
    assert leases == [('u', ['a.jpg', 'b.jpg', 'c.jpg'])]
    assert calls == [(m, [m]) for m in ('a.jpg', 'b.jpg', 'c.jpg')]
    assert queue.deleted == messages
    assert queue.received[0]['max_messages'] == 8


def test_failed_filename_does_not_ack_or_drop_other_messages(setup):
    messages = [message('a.jpg'), message('fail.jpg'), message('c.jpg')]
    queue = Queue(messages)
    app._poll_clustering_queue_batch_once(queue, 'q', 5)
    assert queue.deleted == [messages[0], messages[2]]


def test_libraries_do_not_share_lease(setup):
    _, leases = setup
    messages = [message('a.jpg'), message('b.jpg'),
                message('c.jpg', 'other'), message('d.jpg', 'other')]
    queue = Queue(messages)
    app._poll_clustering_queue_batch_once(queue, 'q', 5)
    assert [user for user, ids in leases] == ['u', 'other']
    assert queue.deleted == messages


def test_batch_setup_failure_acknowledges_nothing(setup, monkeypatch):
    class Broken:
        config = SimpleNamespace(io_concurrency=4)

        @contextmanager
        def batch(self, *args):
            raise RuntimeError('lease busy')
            yield

    monkeypatch.setattr(app, '_get_live_faiss_assigner', lambda: Broken())
    queue = Queue([message('a.jpg'), message('b.jpg')])
    app._poll_clustering_queue_batch_once(queue, 'q', 5)
    assert queue.deleted == []


def test_preparation_error_only_retains_failed_message(setup, monkeypatch):
    messages = [message('a.jpg'), message('b.jpg')]
    queue = Queue(messages)
    original = app._prepare_incremental_assignment

    def prepare(payload, user):
        if payload['filename'] == 'a.jpg':
            raise RuntimeError('read unavailable')
        return original(payload, user)

    monkeypatch.setattr(app, '_prepare_incremental_assignment', prepare)
    app._poll_clustering_queue_batch_once(queue, 'q', 5)
    assert queue.deleted == [messages[1]]


def test_pending_visibility_renewal_hands_off_latest_receipt(setup, monkeypatch):
    import threading

    messages = [message('a.jpg'), message('b.jpg')]
    renewed = threading.Event()

    class RenewingQueue(Queue):
        def update_message(self, msg, **kwargs):
            replacement = SimpleNamespace(**vars(msg), receipt='renewed') if not hasattr(msg, 'receipt') else msg
            renewed.set()
            return replacement

    queue = RenewingQueue(messages)
    original = app._prepare_incremental_assignment

    def prepare(payload, user):
        assert renewed.wait(timeout=5), 'pending messages were not renewed'
        return original(payload, user)

    monkeypatch.setattr(app, 'CLUSTERING_WORKER_LEASE_RENEWAL_SECONDS', 0.01)
    monkeypatch.setattr(app, '_prepare_incremental_assignment', prepare)
    app._poll_clustering_queue_batch_once(queue, 'q', 5)
    assert len(queue.deleted) == 2
    assert all(msg.receipt == 'renewed' for msg in queue.deleted)


@pytest.fixture
def real_staged(monkeypatch):
    import faiss_assignment
    from test_faiss_assignment import Harness
    from faiss_assignment import AssignmentConfig

    faiss_assignment.invalidate()
    h = Harness(config=AssignmentConfig(io_concurrency=2, coalesce_writes=True))
    h.face('a.jpg')
    h.face('b.jpg')
    monkeypatch.setattr(app, 'PEOPLE_ASSIGNMENT_ENGINE', 'faiss')
    monkeypatch.setattr(app, '_get_live_faiss_assigner', lambda: h.assigner)
    monkeypatch.setattr(app, '_prepare_incremental_assignment',
                        lambda payload, user: (payload['filename'], [payload['filename']]))
    yield h
    faiss_assignment.invalidate()


def test_staged_queue_ack_waits_for_all_table_commits_and_projection(real_staged):
    h = real_staged

    class CheckingQueue(Queue):
        def delete_message(self, msg):
            assert h.people.transactions and h.members.transactions and h.faces.transactions
            filename = json.loads(msg.content)['filename']
            assert ('u', filename) in h.metadata
            super().delete_message(msg)

    queue = CheckingQueue([message('a.jpg'), message('b.jpg')])
    app._poll_clustering_queue_batch_once(queue, 'q', 5)
    assert queue.deleted == queue.messages
    assert len(h.people.transactions[0]) == 1


@pytest.mark.parametrize('table', ['people', 'members', 'faces'])
def test_staged_queue_partial_commit_retains_entire_group(real_staged, table):
    from azure.core.exceptions import HttpResponseError
    import faiss_assignment

    h = real_staged
    getattr(h, table).fail_write = HttpResponseError('response lost after commit')
    getattr(h, table).commit_then_fail = True
    queue = Queue([message('a.jpg'), message('b.jpg')])
    app._poll_clustering_queue_batch_once(queue, 'q', 5)
    assert not queue.deleted
    assert faiss_assignment._ACTIVE is None
    app._poll_clustering_queue_batch_once(queue, 'q', 5)
    assert queue.deleted == queue.messages


def test_staged_queue_metadata_failure_retains_only_affected_filename(real_staged):
    h = real_staged

    def metadata(user, filename):
        if filename == 'a.jpg':
            raise RuntimeError('projection offline')
        h.metadata.append((user, filename))

    h.assigner.metadata_callback = metadata
    messages = [message('a.jpg'), message('b.jpg')]
    queue = Queue(messages)
    app._poll_clustering_queue_batch_once(queue, 'q', 5)
    assert queue.deleted == [messages[1]]
    assert all(row['personId'] for row in h.faces.rows.values())


def test_owned_retry_projects_without_new_writes_or_index(real_staged):
    import faiss_assignment
    h = real_staged
    h.faces.rows['u', 'a.jpg']['personId'] = 'alice'
    h.faces.rows['u', 'b.jpg']['personId'] = 'alice'
    queue = Queue([message('a.jpg'), message('b.jpg')])
    app._poll_clustering_queue_batch_once(queue, 'q', 5)
    assert queue.deleted == queue.messages
    assert not h.people.transactions and not h.faces.transactions
    assert faiss_assignment._ACTIVE is None
    assert sorted(h.metadata) == [('u', 'a.jpg'), ('u', 'b.jpg')]