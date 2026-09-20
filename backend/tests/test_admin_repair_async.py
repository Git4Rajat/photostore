"""Coverage for the admin repair/rebuild actions moved off the backend
request thread onto the standalone clustering worker (see
app._enqueue_admin_repair_job). These used to run full-account face/photo
scans inline in the route handler -- expensive enough that backend's own
container had to be sized to cover a worst-case single request even though
ordinary gallery browsing never hits this code path. Routes now enqueue and
return a jobId; the worker dispatch (app._handle_clustering_queue_payload)
does the actual work and writes the result to the jobs table for
/api/admin/jobs/status to poll.
"""
from __future__ import annotations

import json

import pytest

import app
from routes.admin import (
    admin_dedupe_faces,
    admin_job_status,
    admin_purge_orphaned_photo_data,
    admin_rebuild_vector_index,
)


class _FakeTable:
    def __init__(self) -> None:
        self.rows: dict = {}

    def upsert_entity(self, entity):
        self.rows[(entity['PartitionKey'], entity['RowKey'])] = dict(entity)

    def get_entity(self, partition_key, row_key):
        try:
            return dict(self.rows[(partition_key, row_key)])
        except KeyError:
            raise Exception('not found')

    def query_entities(self, filter_str, select=None):
        return []


class _FakeQueue:
    def __init__(self) -> None:
        self.messages = []

    def send_message(self, content):
        self.messages.append(json.loads(content))


@pytest.fixture
def env(monkeypatch):
    table = _FakeTable()
    queue = _FakeQueue()
    monkeypatch.setattr(app, 'jobs_table_client', table)
    monkeypatch.setattr(app, 'clustering_queue_client', queue)
    monkeypatch.setattr(app, 'face_table_client', object())
    monkeypatch.setattr(app, 'person_table_client', object())
    monkeypatch.setattr(app, 'merge_table_client', object())
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('owner', None))
    return table, queue


def test_dedupe_route_enqueues_instead_of_running_inline(env, monkeypatch):
    table, queue = env
    called = []
    monkeypatch.setattr(app, '_dedupe_duplicate_faces', lambda *a, **k: called.append(k) or {'duplicateGroups': 0})

    with app.app.test_request_context(
        '/api/admin/people/dedupe-faces', method='POST',
        json={'repair': True, 'confirm': 'DEDUPE_FACES', 'dryRun': True},
    ):
        response = admin_dedupe_faces()
    body = response.get_json()

    assert body['status'] == 'queued'
    assert body['jobId']
    assert not called  # the heavy function must not run on the request thread
    assert len(queue.messages) == 1
    message = queue.messages[0]
    assert message['type'] == 'people_admin_repair'
    assert message['action'] == 'dedupe_faces'
    assert message['dryRun'] is True


def test_missing_confirmation_neither_enqueues_nor_runs(env, monkeypatch):
    _table, queue = env
    called = []
    monkeypatch.setattr(app, '_dedupe_duplicate_faces', lambda *a, **k: called.append(k))

    with app.app.test_request_context(
        '/api/admin/people/dedupe-faces', method='POST', json={'dryRun': True},
    ):
        response, status_code = admin_dedupe_faces()

    assert status_code == 403
    assert not called
    assert queue.messages == []


def test_purge_orphaned_dry_run_does_not_require_confirm_but_still_enqueues(env, monkeypatch):
    _table, queue = env
    called = []
    monkeypatch.setattr(app, '_purge_orphaned_photo_data', lambda *a, **k: called.append(k))

    with app.app.test_request_context(
        '/api/admin/photos/purge-orphaned-data', method='POST', json={'dryRun': True},
    ):
        response = admin_purge_orphaned_photo_data()
    body = response.get_json()

    assert body['status'] == 'queued'
    assert not called
    assert queue.messages[0]['action'] == 'purge_orphaned'
    assert queue.messages[0]['dryRun'] is True


def test_worker_dispatch_runs_the_real_handler_and_records_result(env, monkeypatch):
    table, _queue = env
    seen_kwargs = {}

    def _fake_dedupe(user_id, *, dry_run):
        seen_kwargs['user_id'] = user_id
        seen_kwargs['dry_run'] = dry_run
        return {'duplicateGroups': 3, 'deletedFaces': 5}

    monkeypatch.setattr(app, '_dedupe_duplicate_faces', _fake_dedupe)

    job_id = 'cluster:owner:abc123'
    payload = {'jobId': job_id, 'user_id': 'owner', 'type': 'people_admin_repair', 'action': 'dedupe_faces', 'dryRun': False}
    app._handle_clustering_queue_payload(payload, job_id, 'owner', 'people_admin_repair')

    assert seen_kwargs == {'user_id': 'owner', 'dry_run': False}
    row = table.get_entity('owner', job_id)
    assert row['status'] == 'done'
    assert json.loads(row['result']) == {'duplicateGroups': 3, 'deletedFaces': 5}


def test_worker_dispatch_unknown_action_fails_cleanly(env):
    table, _queue = env
    job_id = 'cluster:owner:unknown-action'
    payload = {'jobId': job_id, 'user_id': 'owner', 'type': 'people_admin_repair', 'action': 'not_a_real_action'}
    app._handle_clustering_queue_payload(payload, job_id, 'owner', 'people_admin_repair')

    row = table.get_entity('owner', job_id)
    assert row['status'] == 'failed'
    assert 'unknown repair action' in row['error']


def test_worker_dispatch_handler_exception_is_caught_and_recorded_as_failed(env, monkeypatch):
    table, _queue = env

    def _boom(user_id, *, dry_run):
        raise RuntimeError('storage exploded')

    monkeypatch.setattr(app, '_suppress_suspicious_faces', _boom)

    job_id = 'cluster:owner:boom'
    payload = {'jobId': job_id, 'user_id': 'owner', 'type': 'people_admin_repair', 'action': 'suppress_suspicious', 'dryRun': True}
    app._handle_clustering_queue_payload(payload, job_id, 'owner', 'people_admin_repair')

    row = table.get_entity('owner', job_id)
    assert row['status'] == 'failed'


def test_vector_index_rebuild_route_enqueues_then_worker_populates_result(env, monkeypatch):
    table, queue = env

    class _FakeSnapshot:
        row_keys = ['a', 'b', 'c']
        source_version = 'v1'
        embedding_version = 'adaface1'
        updated_at = '2026-09-16T00:00:00+00:00'

    monkeypatch.setattr(app, 'refresh_user_vector_index', lambda user_id, **kwargs: _FakeSnapshot())

    with app.app.test_request_context(
        '/api/admin/vector-index/rebuild', method='POST',
        json={'repair': True, 'confirm': 'REBUILD_VECTOR_INDEX'},
    ):
        response = admin_rebuild_vector_index()
    body = response.get_json()
    assert body['status'] == 'queued'
    job_id = body['jobId']
    assert queue.messages[0]['type'] == 'vector_index_rebuild'

    app._handle_clustering_queue_payload(queue.messages[0], job_id, 'owner', 'vector_index_rebuild')

    with app.app.test_request_context(f'/api/admin/jobs/status?jobId={job_id}'):
        status_response = admin_job_status()
    status_body = status_response.get_json()
    assert status_body['status'] == 'done'
    assert status_body['result']['status'] == 'rebuilt'
    assert status_body['result']['rowCount'] == 3


def test_vector_index_rebuild_worker_handles_no_embeddings(env, monkeypatch):
    table, queue = env
    monkeypatch.setattr(app, 'refresh_user_vector_index', lambda user_id, **kwargs: None)

    job_id = 'cluster:owner:vecempty'
    payload = {'jobId': job_id, 'user_id': 'owner', 'type': 'vector_index_rebuild'}
    app._handle_clustering_queue_payload(payload, job_id, 'owner', 'vector_index_rebuild')

    row = table.get_entity('owner', job_id)
    assert row['status'] == 'done'
    result = json.loads(row['result'])
    assert result['status'] == 'empty'
    assert result['rowCount'] == 0


def test_admin_job_status_unknown_for_missing_job(env):
    with app.app.test_request_context('/api/admin/jobs/status?jobId=does-not-exist'):
        response = admin_job_status()
    assert response.get_json() == {'status': 'unknown'}


def test_admin_job_status_hides_other_users_jobs(env, monkeypatch):
    table, _queue = env
    job_id = 'cluster:someone-else:xyz'
    app._upsert_job_status(job_id, 'someone-else', 'people_admin_repair', 'done', result={'duplicateGroups': 1})

    with app.app.test_request_context(f'/api/admin/jobs/status?jobId={job_id}'):
        response = admin_job_status()
    assert response.get_json() == {'status': 'unknown'}
