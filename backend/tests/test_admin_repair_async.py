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
from azure.core.exceptions import ResourceNotFoundError

import app
from routes.admin import (
    admin_dedupe_faces,
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
            raise ResourceNotFoundError('not found')

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


def test_vector_index_rebuild_route_uses_dedicated_worker(env, monkeypatch):
    import storage_utils
    import automatic_index_updates
    from tests.test_automatic_index_updates import Table
    from types import SimpleNamespace
    from tests.test_index_priming import _dedicated_queue
    _table, legacy_queue = env
    queue = _dedicated_queue(monkeypatch)
    monkeypatch.setenv('APP_ROLE', 'index-worker')
    monkeypatch.setitem(storage_utils._CTX, 'search_index_dirty_table_client', Table())
    monkeypatch.setattr(automatic_index_updates, 'artifact_base', lambda user, kind: 'v1')
    monkeypatch.setattr(automatic_index_updates, 'wave_pending', lambda user: False)
    built = []
    monkeypatch.setattr(storage_utils, 'refresh_user_vector_index',
                        lambda uid, **kw: built.append(uid) or SimpleNamespace(
                            row_keys=['a', 'b', 'c'], source_version='v1',
                            embedding_version='adaface1', updated_at='2026-10-23T00:00:00Z'))
    with app.app.test_request_context(
        '/api/admin/vector-index/rebuild', method='POST',
        json={'repair': True, 'confirm': 'REBUILD_VECTOR_INDEX'},
    ):
        response, status = admin_rebuild_vector_index()
    body = response.get_json()
    assert status == 202 and body['status'] == 'queued'
    assert not built and not legacy_queue.messages
    payload = queue.sent[0]
    assert payload['type'] == 'index_build' and payload['scope'] == 'vector'
    app._handle_clustering_queue_payload(payload, body['jobId'], 'owner', 'index_build')
    assert built == ['owner']


def test_legacy_vector_repair_forwards_to_the_index_build(env, monkeypatch):
    table, _queue = env
    monkeypatch.setenv('APP_ROLE', 'cluster')
    monkeypatch.setattr(app, 'refresh_user_vector_index',
                        lambda *a, **kw: pytest.fail('cluster must not build vectors'))
    monkeypatch.setattr(app, 'enqueue_index_build', lambda *a, **kw: 'queued')
    job_id = 'cluster:owner:vecempty'
    payload = {'jobId': job_id, 'user_id': 'owner', 'type': 'vector_index_rebuild'}
    app._handle_clustering_queue_payload(payload, job_id, 'owner', 'vector_index_rebuild')
    assert table.get_entity('owner', job_id)['forwardedTo']




def test_purge_orphaned_removes_the_purged_photos_from_albums_and_dirties_indexes(monkeypatch):
    rows = [{'PartitionKey': 'owner', 'RowKey': name, 'processing_state': 'done'} for name in ('kept.jpg', 'gone.jpg')]
    deleted = []

    class _Container:
        def list_blobs(self):
            return [type('B', (), {'name': 'kept.jpg'})()]

    class _Blobs:
        def get_container_client(self, _):
            return _Container()

    class _Meta:
        def query_entities(self, *a, **k):
            return list(rows)

        def delete_entity(self, partition_key, row_key):
            deleted.append(row_key)

    removed, touched = [], []
    monkeypatch.setattr(app, 'blob_service_client', _Blobs())
    monkeypatch.setattr(app, 'metadata_table_client', _Meta())
    monkeypatch.setattr(app, 'face_table_client', None)
    monkeypatch.setattr(app, '_blob_name_from_metadata', lambda row, filename: filename)
    monkeypatch.setattr(app, '_batch_remove_filenames_from_albums', lambda uid, names: removed.append(set(names)))
    monkeypatch.setattr(app, '_invalidate_metadata_scan_cache', lambda uid: None)
    monkeypatch.setattr(app, 'touch_user_search_indexes_state', lambda uid, filenames=None, **k: touched.append(set(filenames)))

    result = app._purge_orphaned_photo_data('owner', dry_run=False)

    assert deleted == ['gone.jpg']
    assert result['orphanedMetadataDeleted'] == 1
    assert removed == [{'gone.jpg'}]
    assert touched == [{'gone.jpg'}]

    removed.clear(); touched.clear(); deleted.clear()
    app._purge_orphaned_photo_data('owner', dry_run=True)
    assert deleted == [] and removed == [] and touched == []
