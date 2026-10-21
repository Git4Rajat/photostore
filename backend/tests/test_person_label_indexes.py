"""Labels schedule scoped index maintenance without waiting for propagation."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import app
from routes import people
from tests.fakes import FakeTable


@pytest.fixture
def label_env(monkeypatch):
    table = FakeTable()
    table.upsert_entity({'PartitionKey': 'u1', 'RowKey': 'person-1', 'faceIds': '["face-1"]'})
    env = SimpleNamespace(
        table=table, affected={'photo.jpg'}, queued=[], dirty=[], flushed=[],
        light=False, propagate=Mock(return_value={'status': 'queued', 'jobId': 'propagate-1'}),
        enqueue=app.enqueue_index_build, mark_dirty=app.mark_people_dirty,
        flush=app.storage_utils_module._flush_dirty_filename_buffer,
    )
    monkeypatch.setattr(app, '_require_user_id', lambda: ('u1', None))
    monkeypatch.setattr(app, '_people_features_available', lambda: True)
    monkeypatch.setattr(app, 'person_table_client', table)

    def update(user, person_id, changes):
        try:
            row = table.get_entity(user, person_id)
        except Exception:
            return False
        table.upsert_entity({**row, **changes})
        return True

    monkeypatch.setattr(app, '_update_person_entity', update)
    monkeypatch.setattr(people, '_confirm_faces_for_person', lambda *a: env.affected)
    monkeypatch.setattr(app, '_update_person_rep_embedding', lambda *a: None)
    monkeypatch.setattr(app, '_rebuild_metadata_faces_for_filenames', Mock())
    monkeypatch.setattr(app, 'mark_people_dirty', lambda user, ids: env.dirty.extend(ids))
    monkeypatch.setattr(app.storage_utils_module, '_flush_dirty_filename_buffer',
                        lambda user, kind: env.flushed.append((user, kind)))
    monkeypatch.setattr(app, 'light_index_build_needed', lambda user: env.light)
    monkeypatch.setattr(app, 'enqueue_index_build',
                        lambda user, **kw: env.queued.append((user, kw)) or 'queued')
    monkeypatch.setattr(app, '_enqueue_propagate_job', env.propagate)
    return env


def label(name='Alice', person_id='person-1'):
    with app.app.test_request_context('/api/persons/test/label', method='POST', json={'name': name}):
        return people.label_person(person_id)


def test_label_queues_people_search_refresh_before_propagation(label_env):
    env = label_env

    def propagate(*args):
        assert env.queued == [('u1', {'reason': 'person-label', 'scope': 'people'})]
        assert env.dirty == ['person-1']
        assert env.flushed == [('u1', kind) for kind in ('lexical', 'sort', 'access')]
        assert env.table.get_entity('u1', 'person-1')['name'] == 'Alice'
        return {'status': 'queued', 'jobId': 'propagate-1'}

    env.propagate.side_effect = propagate
    response = label()
    assert response.get_json()['propagateJobId'] == 'propagate-1'
    app._rebuild_metadata_faces_for_filenames.assert_called_once_with('u1', {'photo.jpg'})


def test_label_queues_light_refresh_only_when_photo_metadata_dirtied_sort(label_env):
    label_env.light = True
    assert label().get_json()['success'] is True
    assert [kw['scope'] for _, kw in label_env.queued] == ['people', 'light']


@pytest.mark.parametrize('name', ['', 'Unnamed'])
def test_clearing_a_name_still_schedules_indexes_without_propagation(label_env, name):
    assert label(name).get_json()['success'] is True
    assert [kw['scope'] for _, kw in label_env.queued] == ['people']
    label_env.propagate.assert_not_called()


def test_person_without_photos_still_refreshes_people_not_sort(label_env):
    label_env.affected = set()
    label_env.light = True
    assert label().get_json()['success'] is True
    assert [kw['scope'] for _, kw in label_env.queued] == ['people']


@pytest.mark.parametrize('name,person_id,status', [(None, 'person-1', 400), ('Alice', 'missing', 404)])
def test_rejected_label_does_not_queue_indexes(label_env, name, person_id, status):
    _, actual = label(name, person_id)
    assert actual == status
    assert label_env.queued == [] and label_env.dirty == []


@pytest.mark.parametrize('outcome', ['unavailable', RuntimeError('queue failure')])
def test_queue_failure_does_not_undo_saved_name_or_skip_other_scope(label_env, monkeypatch, outcome):
    label_env.light = True

    def enqueue(user, **kwargs):
        label_env.queued.append((user, kwargs))
        if kwargs['scope'] == 'people':
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return 'queued'

    monkeypatch.setattr(app, 'enqueue_index_build', enqueue)
    assert label().get_json()['success'] is True
    assert label_env.table.get_entity('u1', 'person-1')['name'] == 'Alice'
    assert [kw['scope'] for _, kw in label_env.queued] == ['people', 'light']


def test_burst_of_labels_coalesces_queue_messages_but_retains_every_person(label_env, monkeypatch):
    jobs, dirty = FakeTable(), FakeTable()
    sent = []
    monkeypatch.setattr(app, 'jobs_table_client', jobs)
    monkeypatch.setattr(app, 'people_dirty_set_table_client', dirty)
    monkeypatch.setattr(app, 'library_ops_queue_client', SimpleNamespace(
        send_message=lambda body, **kw: sent.append(json.loads(body))))
    monkeypatch.setattr(app, 'enqueue_index_build', label_env.enqueue)
    monkeypatch.setattr(app, 'mark_people_dirty', label_env.mark_dirty)
    for i in range(3):
        person_id = f'person-{i}'
        label_env.table.upsert_entity({'PartitionKey': 'u1', 'RowKey': person_id, 'faceIds': '[]'})
        assert label(f'Name {i}', person_id).get_json()['success'] is True
    assert len(sent) == 1 and sent[0]['scope'] == 'people'
    assert {row['RowKey'] for row in dirty.query_entities("PartitionKey eq 'u1'")} == {
        'person-0', 'person-1', 'person-2'}


def test_affected_photo_marks_reach_storage_before_worker_is_queued(label_env, monkeypatch):
    storage = app.storage_utils_module
    dirty = FakeTable()
    monkeypatch.setitem(storage._CTX, 'search_index_dirty_table_client', dirty)
    monkeypatch.setattr(storage, '_flush_dirty_filename_buffer', label_env.flush)
    monkeypatch.setattr(app, '_rebuild_metadata_faces_for_filenames',
                        lambda user, files: storage._mark_search_index_dirty_filenames(user, files))

    def enqueue(user, **kwargs):
        for kind in ('lexical', 'sort', 'access'):
            partition = storage._search_index_dirty_partition_key(user, kind)
            assert dirty.get_entity(partition, 'photo.jpg')['RowKey'] == 'photo.jpg'
        return 'queued'

    monkeypatch.setattr(app, 'enqueue_index_build', enqueue)
    assert label().get_json()['success'] is True


def test_label_refresh_does_not_depend_on_successful_identity_propagation(label_env, monkeypatch):
    label_env.propagate.return_value = {'status': 'unavailable'}
    monkeypatch.setattr(app, '_propagate_person_identity', Mock(side_effect=RuntimeError('propagation failed')))
    assert label().get_json()['success'] is True
    assert [kw['scope'] for _, kw in label_env.queued] == ['people']


def test_label_job_refreshes_people_and_incremental_search_without_full_build(label_env, monkeypatch):
    assert label().get_json()['success'] is True
    primed, searched = [], []
    monkeypatch.setattr(app, 'storage_utils_ensure_sort_current', lambda user: None)
    monkeypatch.setattr(app, '_upsert_job_status', lambda *a, **kw: None)
    monkeypatch.setattr(app, 'get_dirty_people', lambda user: {'person-1'})
    monkeypatch.setattr(app, 'get_dirty_albums', lambda user: set())
    monkeypatch.setattr(app, 'clear_dirty_people', Mock())
    monkeypatch.setattr(app, 'prime_all_user_indexes_sequentially',
                        lambda user, **kwargs: primed.append(kwargs) or [])
    monkeypatch.setattr(app.storage_utils_module, 'reconcile_search_db', lambda user: None)
    monkeypatch.setattr(app, 'refresh_user_search_db_incremental',
                        lambda user: searched.append(user) or {'status': 'delta', 'published': 1})
    for user, kwargs in label_env.queued:
        app._run_index_build_job(user, f'index-build-{user}-{kwargs["scope"]}', kwargs['scope'])
    assert primed == [{'wait': True, 'kinds': ('people', 'albums'),
                       'affected_person_ids': {'person-1'}, 'affected_album_ids': None}]
    assert searched == ['u1']
