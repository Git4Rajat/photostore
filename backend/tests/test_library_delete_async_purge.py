"""Coverage for library_delete's data purge moving off the request thread.

_purge_library_data does a full row-by-row scan/delete across 6 tables --
the same worst-case-sizing shape as the admin repair actions (see
test_admin_repair_async.py). It fires essentially never (account
self-deletion with no other members), but for consistency it now enqueues
to the standalone clustering worker instead of running inline, same pattern
as _enqueue_admin_repair_job.
"""
from __future__ import annotations

import json

import pytest

import app
from routes.library import library_delete


class _FakeLibraryStore:
    def __init__(self) -> None:
        self.deleted_libraries: list[str] = []
        self.deleted_users: list[str] = []
        self.audits: list[tuple] = []

    def list_library_members(self, library_id):
        return [{'userId': 'owner'}]

    def audit(self, library_id, actor, action, target):
        self.audits.append((library_id, actor, action, target))

    def delete_all_invites(self, library_id):
        pass

    def delete_all_memberships(self, library_id):
        pass

    def delete_library(self, library_id):
        self.deleted_libraries.append(library_id)

    def delete_user(self, account_id):
        self.deleted_users.append(account_id)


class _FakeQueue:
    def __init__(self) -> None:
        self.messages = []

    def send_message(self, content):
        self.messages.append(json.loads(content))


@pytest.fixture
def env(monkeypatch):
    queue = _FakeQueue()
    store = _FakeLibraryStore()
    monkeypatch.setattr(app, 'clustering_queue_client', queue)
    monkeypatch.setattr(app, 'library_store', store)
    monkeypatch.setattr(app, '_require_owner_context', lambda *a, **k: ('owner', 'lib1', None))
    monkeypatch.setattr(app.password_auth, 'OWNER_USER_ID', 'root-owner')
    return queue, store


def test_library_delete_enqueues_purge_instead_of_running_inline(env, monkeypatch):
    queue, store = env
    called = []
    monkeypatch.setattr(app, '_purge_library_data', lambda *a, **k: called.append(a))

    with app.app.test_request_context('/api/library', method='DELETE'):
        response = library_delete()
    body = response.get_json()

    assert body['status'] == 'ok'
    assert store.deleted_libraries == ['lib1']
    assert store.deleted_users == ['owner']
    assert not called  # the heavy purge must not run on the request thread
    assert len(queue.messages) == 1
    message = queue.messages[0]
    assert message['type'] == 'library_delete_purge'
    assert message['libraryId'] == 'lib1'


def test_worker_dispatch_runs_the_real_purge(env, monkeypatch):
    _queue, _store = env
    calls = []
    monkeypatch.setattr(app, '_purge_library_data', lambda library_id: calls.append(('purge', library_id)))
    monkeypatch.setattr(app, 'invalidate_user_vector_index_cache', lambda library_id: calls.append(('vector', library_id)))
    monkeypatch.setattr(app, 'invalidate_user_lexical_index_cache', lambda library_id: calls.append(('lexical', library_id)))

    payload = {'user_id': 'lib1', 'libraryId': 'lib1', 'type': 'library_delete_purge'}
    app._handle_clustering_queue_payload(payload, '', 'lib1', 'library_delete_purge')

    assert calls == [('purge', 'lib1'), ('vector', 'lib1'), ('lexical', 'lib1')]


def test_worker_dispatch_purge_failure_does_not_raise(env, monkeypatch):
    monkeypatch.setattr(app, '_purge_library_data', lambda library_id: (_ for _ in ()).throw(RuntimeError('boom')))

    payload = {'user_id': 'lib1', 'libraryId': 'lib1', 'type': 'library_delete_purge'}
    # Must not raise -- a failed best-effort purge shouldn't crash the worker's
    # message loop.
    app._handle_clustering_queue_payload(payload, '', 'lib1', 'library_delete_purge')
