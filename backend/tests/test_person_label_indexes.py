"""Names are source records, not instructions to rediscover/rewrite faces."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import app
from routes import people
from tests.fakes import FakeTable


@pytest.fixture
def env(monkeypatch):
    table, dirty = FakeTable(), FakeTable()
    table.upsert_entity({'PartitionKey': 'u1', 'RowKey': 'p1', 'name': 'Alice', 'faceIds': '["f1"]'})
    state = SimpleNamespace(table=table, dirty=dirty, queued=[], outcome='queued')
    monkeypatch.setattr(app, '_require_user_id', lambda: ('u1', None))
    monkeypatch.setattr(app, '_people_features_available', lambda: True)
    monkeypatch.setattr(app, 'person_table_client', table)
    monkeypatch.setattr(app, 'people_dirty_set_table_client', dirty)
    monkeypatch.setattr(app, 'albums_dirty_set_table_client', FakeTable())
    monkeypatch.setattr(app, 'blob_service_client', None)
    monkeypatch.setattr(app, '_invalidate_people_scan_cache', Mock())
    for name in ('_update_person_rep_embedding', '_rebuild_metadata_faces_for_filenames',
                 '_enqueue_propagate_job', '_propagate_person_identity', 'light_index_build_needed'):
        monkeypatch.setattr(app, name, Mock(side_effect=AssertionError('unexpected ' + name)))
    monkeypatch.setattr(people, '_confirm_faces_for_person', Mock(side_effect=AssertionError('face confirmation')))
    monkeypatch.setattr(app, 'enqueue_index_build',
                        lambda user, **kw: state.queued.append((user, kw)) or state.outcome)
    monkeypatch.setattr(app, 'storage_utils_ensure_sort_current', Mock(side_effect=AssertionError('sort upgrade')))
    monkeypatch.setattr(app, '_upsert_job_status', lambda *a, **kw: None)
    monkeypatch.setattr(app, 'prime_all_user_indexes_sequentially', Mock(return_value=[]))
    monkeypatch.setattr(app, 'refresh_user_search_db_incremental', lambda user: {'status': 'noop'})
    return state


def label(name='Bob', person_id='p1'):
    with app.app.test_request_context('/api/persons/test/label', method='POST', json={'name': name}):
        return people.label_person(person_id)


def test_rename_only_saves_name_and_queues_people(env):
    result = label().get_json()
    assert result['success'] and result['propagateJobId'] is None and result['autoAssignedFaces'] == 0
    row = env.table.get_entity('u1', 'p1')
    assert row['name'] == 'Bob' and row['faceIds'] == '["f1"]'
    assert row['labelPending'] and not row['labelMembershipPending']
    assert env.queued == [('u1', {'reason': 'person-label', 'scope': 'people'})]


def test_repeated_identical_name_is_a_noop(env):
    assert label('Alice').get_json()['success']
    assert not env.queued
    assert env.table.versions[('u1', 'p1')] == 1


@pytest.mark.parametrize('name', ['', 'Unnamed'])
def test_removing_name_defers_legacy_membership_sync_without_discovery(env, name):
    assert label(name).get_json()['success']
    assert env.table.get_entity('u1', 'p1')['labelMembershipPending']
    assert [kwargs['scope'] for _, kwargs in env.queued] == ['people']


def test_first_name_requires_membership_sync_but_not_request_path_work(env):
    env.table.upsert_entity({'PartitionKey': 'u1', 'RowKey': 'p1', 'name': '', 'faceIds': '["f1"]'})
    label()
    assert env.table.get_entity('u1', 'p1')['labelMembershipPending']


@pytest.mark.parametrize('name,person_id,status', [(None, 'p1', 400), ('Bob', 'missing', 404)])
def test_invalid_or_missing_person_does_not_schedule(env, name, person_id, status):
    _, actual = label(name, person_id)
    assert actual == status and not env.queued


def test_queue_failure_retains_source_outbox(env):
    env.outcome = 'unavailable'
    result = label().get_json()
    assert result['success'] and result['indexRefreshPending']
    assert env.table.get_entity('u1', 'p1')['labelPending']
    assert app._pending_person_labels('u1') == {'p1'}


def test_rename_worker_clears_only_its_source_intent_after_publication(env):
    label()
    app._run_index_build_job('u1', 'job', 'people')
    row = env.table.get_entity('u1', 'p1')
    assert row['name'] == 'Bob' and not row['labelPending']
    assert row['faceIds'] == '["f1"]'
    assert app.get_dirty_people('u1') == set()
    assert app._pending_person_labels('u1') == set()
    assert app.prime_all_user_indexes_sequentially.call_args.kwargs['kinds'] == ('people',)


def test_rename_arriving_during_publication_survives_old_ack(env):
    label()
    app.prime_all_user_indexes_sequentially.side_effect = lambda *a, **kw: label('Carol') and []
    app._run_index_build_job('u1', 'job', 'people')
    assert env.table.get_entity('u1', 'p1')['name'] == 'Carol'
    assert env.table.get_entity('u1', 'p1')['labelPending']
    assert app.get_dirty_people('u1') == {'p1'}
    assert app._pending_person_labels('u1') == {'p1'}
    app.prime_all_user_indexes_sequentially.side_effect = None
    app._run_index_build_job('u1', 'job', 'people')
    assert not env.table.get_entity('u1', 'p1')['labelPending']


def test_membership_sync_is_background_and_scoped_to_this_person(env, monkeypatch):
    env.table.upsert_entity({'PartitionKey': 'u1', 'RowKey': 'p1', 'name': '', 'faceIds': '["f1"]'})
    label()
    rebuild = Mock()
    monkeypatch.setattr(app, '_load_face_rows_by_ids', lambda user, ids: {'f1': {'filename': 'one.jpg'}})
    monkeypatch.setattr(app, '_rebuild_metadata_faces_for_filenames', rebuild)
    app._run_index_build_job('u1', 'job', 'people')
    rebuild.assert_called_once_with('u1', {'one.jpg'}, strict=True)


def test_membership_read_failure_does_not_ack_source_intent(env, monkeypatch):
    env.table.upsert_entity({'PartitionKey': 'u1', 'RowKey': 'p1', 'name': '', 'faceIds': '["f1"]'})
    label()
    monkeypatch.setattr(app, '_load_face_rows_by_ids', Mock(side_effect=RuntimeError('storage down')))
    with pytest.raises(RuntimeError, match='storage down'):
        app._run_index_build_job('u1', 'job', 'people')
    assert env.table.get_entity('u1', 'p1')['labelPending']
    assert app._pending_person_labels('u1') == {'p1'}


def test_search_retry_preserves_people_ids_for_targeted_rebuild(env, monkeypatch):
    label()
    monkeypatch.setattr(app, 'INDEX_MAINTENANCE_QUEUE_NAME', 'index-work')
    monkeypatch.setattr(app, 'refresh_user_search_db_incremental', lambda user: {'status': 'unavailable'})
    with pytest.raises(RuntimeError, match='must retry'):
        app._run_index_build_job('u1', 'job', 'people')
    assert app.get_dirty_people('u1') == {'p1'}
    assert app._pending_person_labels('u1') == {'p1'}
    monkeypatch.setattr(app, 'refresh_user_search_db_incremental', lambda user: {'status': 'noop'})
    app._run_index_build_job('u1', 'job', 'people')
    assert app.prime_all_user_indexes_sequentially.call_args.kwargs['affected_person_ids'] == {'p1'}
