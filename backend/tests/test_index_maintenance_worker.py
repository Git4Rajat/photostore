import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import app
import index_work
import storage_utils as su
from routes import tools
from tests.fakes import FakeTable
from test_index_work import EtagTable


@pytest.fixture
def worker(monkeypatch):
    sent, statuses = [], []
    table = EtagTable()
    monkeypatch.setattr(app, 'INDEX_MAINTENANCE_QUEUE_NAME', 'index-work')
    monkeypatch.setattr(app, 'jobs_table_client', table)
    monkeypatch.setattr(app, 'index_maintenance_queue_client', SimpleNamespace(
        send_message=lambda body, **kw: sent.append(json.loads(body))))
    monkeypatch.setattr(app, 'library_ops_queue_client', SimpleNamespace(
        send_message=Mock(side_effect=AssertionError('legacy queue'))))
    monkeypatch.setattr(app, '_upsert_job_status', lambda *a, **kw: statuses.append((a, kw)))
    return SimpleNamespace(table=table, sent=sent, statuses=statuses)


def test_new_index_jobs_use_the_indexer_queue_and_durable_intent(worker):
    assert app.enqueue_index_build('A', reason='person-label', scope='people') == 'queued'
    assert len(worker.sent) == 1
    payload = worker.sent[0]
    assert payload['maintenanceKey'] == index_work.WorkStore.key('A', 'people')
    assert worker.table.get_entity(index_work.PARTITION, payload['maintenanceKey'])['revision'] == payload['intentRevision']


def test_queue_status_is_written_before_a_fast_consumer(worker, monkeypatch):
    def send(*args, **kwargs):
        assert worker.statuses[-1][0][3] == 'queued'
        app._upsert_job_status('job', 'A', app.INDEX_BUILD_JOB_TYPE, 'done')
    monkeypatch.setattr(app, 'index_maintenance_queue_client', SimpleNamespace(send_message=send))
    app.enqueue_index_build('A', scope='people')
    assert worker.statuses[-1][0][3] == 'done'


def test_auto_build_queues_only_dirty_kinds_not_a_full_build(worker, monkeypatch):
    monkeypatch.setattr(app, '_require_user_id', lambda: ('A', None))
    monkeypatch.setattr(app, 'index_build_needed', lambda user: False)
    monkeypatch.setattr(app, 'get_user_index_build_state', lambda user: {'ready': True, 'indexes': {}})
    monkeypatch.setattr(su, 'get_user_index_status_detailed', lambda user: {'indexes': {
        'people': {'ready': True, 'dirty': True}, 'lexical': {'ready': True, 'dirty': True},
        'sort': {'ready': True, 'dirty': False}, 'albums': {'ready': True, 'dirty': False},
    }})
    with app.app.test_request_context('/api/tools/indexes/build', method='POST', json={'scope': 'auto'}):
        tools.tools_build_indexes()
    assert {row['scope'] for row in worker.sent} == {'people', 'search'}


def test_tools_status_detects_a_scoped_job_without_a_full_job(worker, monkeypatch):
    monkeypatch.setattr(app, '_require_user_id', lambda: ('A', None))
    monkeypatch.setattr(su, 'get_user_index_status_detailed', lambda user: {
        'ready': True, 'needs_rebuild': True, 'indexes': {},
    })
    monkeypatch.setattr(app, '_get_job_row', lambda user, job: (
        {'status': 'queued', 'indexScope': 'people'} if job.endswith('-people') else None))
    with app.app.test_request_context('/api/tools/indexes/status'):
        payload = tools.tools_indexes_status().get_json()
    assert payload['building'] is True and payload['building_job']['scope'] == 'people'


def test_read_poll_during_build_does_not_create_a_followup_loop(worker, monkeypatch):
    app.enqueue_index_build('A', reason='person-label', scope='people')
    def run(*args):
        assert app.enqueue_index_build('A', reason='serving-process', scope='people') == 'already_active'
    monkeypatch.setattr(app, '_run_index_build_job', run)
    app._run_index_maintenance_payload(worker.sent[0])
    assert len(worker.sent) == 1
    assert worker.table.rows == {}


def test_read_poll_does_not_report_poison_revision_as_active(worker):
    app.enqueue_index_build('A', reason='person-label', scope='people')
    payload = worker.sent[0]
    store = index_work.WorkStore(worker.table)
    store.fail_revision(payload['maintenanceKey'], payload['intentRevision'])
    assert app.enqueue_index_build('A', reason='serving-process', scope='people') == 'unavailable'
    assert len(worker.sent) == 1
    assert store._get(payload['maintenanceKey'])['failed']


def test_edit_during_build_gets_one_durable_followup(worker, monkeypatch):
    app.enqueue_index_build('A', reason='person-label', scope='people')
    runs = []
    def run(*args):
        runs.append(args)
        if len(runs) == 1:
            assert app.enqueue_index_build('A', reason='person-label', scope='people') == 'already_active'
            assert app.enqueue_index_build('A', reason='person-label', scope='people') == 'already_active'
    monkeypatch.setattr(app, '_run_index_build_job', run)
    app._run_index_maintenance_payload(worker.sent[0])
    assert len(worker.sent) == 2
    app._run_index_maintenance_payload(worker.sent[1])
    assert len(runs) == 2 and worker.table.rows == {}


def test_duplicate_wakeup_after_completion_is_a_noop(worker, monkeypatch):
    app.enqueue_index_build('A', scope='people')
    run = Mock()
    monkeypatch.setattr(app, '_run_index_build_job', run)
    payload = worker.sent[0]
    app._run_index_maintenance_payload(payload)
    app._run_index_maintenance_payload(payload)
    assert run.call_count == 1


def test_exhausted_old_wakeup_cannot_fail_the_new_revision_or_notification(worker):
    app.enqueue_index_build('A', reason='person-label', scope='people')
    old = worker.sent[0]
    app.enqueue_index_build('A', reason='person-label', scope='people')
    before = len(worker.statuses)
    deleted, deadletters = [], []
    message = SimpleNamespace(content=json.dumps(old), dequeue_count=app.INDEX_BUILD_MAX_RETRIES + 1)
    app._process_clustering_queue_message(message,
        SimpleNamespace(delete_message=lambda msg: deleted.append(msg)), 'index-work', app.INDEX_BUILD_MAX_RETRIES,
        SimpleNamespace(send_message=lambda body: deadletters.append(body)))
    assert deleted == [message] and len(deadletters) == 1
    assert len(worker.statuses) == before
    assert not next(iter(worker.table.rows.values()))['failed']


def test_queue_failure_keeps_work_recoverable(worker, monkeypatch):
    monkeypatch.setattr(app, 'index_maintenance_queue_client', SimpleNamespace(
        send_message=Mock(side_effect=OSError('queue unavailable'))))
    assert app.enqueue_index_build('A', reason='person-label', scope='people') == 'unavailable'
    row = next(iter(worker.table.rows.values()))
    assert row['revision'] and not row['failed']


def test_dispatch_failure_keeps_intent_for_retry(worker, monkeypatch):
    app.enqueue_index_build('A', scope='people')
    monkeypatch.setattr(app, '_run_index_build_job', Mock(side_effect=RuntimeError('storage down')))
    with pytest.raises(RuntimeError, match='storage down'):
        app._run_index_maintenance_payload(worker.sent[0])
    row = next(iter(worker.table.rows.values()))
    assert not row.get('owner') and not row['failed']


def test_old_cluster_queue_message_is_forwarded_not_built(worker, monkeypatch):
    monkeypatch.setenv('APP_ROLE', 'cluster')
    run = Mock(side_effect=AssertionError('cluster built index'))
    monkeypatch.setattr(app, '_run_index_build_job', run)
    app._handle_clustering_queue_payload({'scope': 'people'}, 'job', 'A', 'index_build')
    run.assert_not_called()
    assert worker.sent[0]['scope'] == 'people'


def test_compaction_is_deferred_to_its_own_job(worker, monkeypatch):
    import search_db
    monkeypatch.setattr(app, 'refresh_user_search_db_incremental', lambda user: {'status': 'needs_full'})
    compact = Mock(side_effect=AssertionError('inline compaction'))
    monkeypatch.setattr(search_db, 'compact_database', compact)
    assert app._drain_incremental_search_db('A', 'search')['status'] == 'deferred'
    compact.assert_not_called()
    assert worker.sent[0]['scope'] == 'compact'


@pytest.mark.parametrize('outcome', ['unavailable', 'conflict'])
def test_transient_search_outcome_is_retried_not_reported_done(worker, monkeypatch, outcome):
    monkeypatch.setattr(app, 'refresh_user_search_db_incremental', lambda user: {'status': outcome})
    with pytest.raises(RuntimeError, match='must retry'):
        app._drain_incremental_search_db('A', 'search')


def test_missing_base_handoff_must_be_durably_queued(worker, monkeypatch):
    monkeypatch.setattr(app, 'refresh_user_search_db_incremental', lambda user: {'status': 'no_base'})
    monkeypatch.setattr(app, 'enqueue_index_build', lambda *a, **kw: 'unavailable')
    with pytest.raises(RuntimeError, match='bootstrap'):
        app._drain_incremental_search_db('A', 'search')


def test_missing_base_queues_full_bootstrap_only_when_needed(worker, monkeypatch):
    monkeypatch.setattr(app, 'refresh_user_search_db_incremental', lambda user: {'status': 'no_base'})
    assert app._drain_incremental_search_db('A', 'search')['status'] == 'no_base'
    assert [payload['scope'] for payload in worker.sent] == ['full']


def test_recovery_only_wakes_the_authorized_library(worker, monkeypatch):
    store = index_work.WorkStore(worker.table)
    store.request('A', 'search')
    store.request('B', 'people')
    monkeypatch.setattr(app, '_require_user_id', lambda: ('A', None))
    with app.app.test_request_context('/api/tools/indexes/recover-pending', method='POST', json={}):
        response, status = tools.recover_pending_indexes()
    assert status == 202 and response.get_json()['queued'] == 1
    assert [row['userId'] for row in worker.sent] == ['A']


@pytest.fixture
def source(monkeypatch):
    table, dirty = FakeTable(), FakeTable()
    table.upsert_entity({'PartitionKey': 'A', 'RowKey': 'one.jpg', 'rating': 0, 'tags': '[]', 'processing_complete': False})
    monkeypatch.setattr(su, 'DURABLE_INDEX_UPDATES', True)
    monkeypatch.setitem(su._CTX, 'metadata_table_client', table)
    monkeypatch.setitem(su._CTX, 'search_index_dirty_table_client', dirty)
    monkeypatch.setattr(app, 'metadata_table_client', table)
    monkeypatch.setattr(app, '_invalidate_metadata_scan_cache', lambda user: None)
    monkeypatch.setattr(su, 'sort_index_skipped', lambda user: False)
    for name in ('touch_user_lexical_index_state', 'touch_user_sort_index_state', 'touch_user_access_index_state',
                 'touch_user_vector_index_state', 'touch_user_tag_embedding_index_state', 'touch_user_albums_index_state'):
        monkeypatch.setattr(su, name, Mock())
    queued = []
    monkeypatch.setattr(su, 'METADATA_INDEX_REQUEST_HOOK', lambda user, scope: queued.append((user, scope)) or 'queued')
    return SimpleNamespace(table=table, dirty=dirty, queued=queued)


def test_rating_queues_only_search_sort_and_album_covers(source):
    app._update_metadata_entity_fields('A', 'one.jpg', {'rating': 5})
    assert set(source.queued) == {('A', kind) for kind in ('search', 'sort', 'albums')}
    row = source.table.get_entity('A', 'one.jpg')
    assert row['rating'] == 5 and row['tags'] == '[]' and not row['indexPending']
    su.touch_user_access_index_state.assert_not_called()
    su.touch_user_vector_index_state.assert_not_called()
    su.touch_user_tag_embedding_index_state.assert_not_called()


def test_location_does_not_dirty_media_access_or_gallery_sort(source):
    app._update_metadata_entity_fields('A', 'one.jpg', {'locationCity': 'Paris'})
    assert source.queued == [('A', 'search')]
    su.touch_user_sort_index_state.assert_not_called()
    su.touch_user_access_index_state.assert_not_called()


def test_unchanged_rating_neither_writes_metadata_nor_queues_work(source):
    previous = source.table.get_entity('A', 'one.jpg')
    app._update_metadata_entity_fields('A', 'one.jpg', {'rating': 0})
    assert source.queued == []
    assert source.table.get_entity('A', 'one.jpg').metadata == previous.metadata


def test_noop_edit_still_repairs_a_stale_derived_completion_flag(source):
    row = source.table.get_entity('A', 'one.jpg')
    source.table.upsert_entity(dict(row, processing_complete=True))
    app._update_metadata_entity_fields('A', 'one.jpg', {'rating': 0})
    assert source.table.get_entity('A', 'one.jpg')['processing_complete'] is False
    assert source.queued == [('A', 'search')]


def test_unchanged_fields_in_a_real_edit_do_not_expand_dependencies(source):
    app._update_metadata_entity_fields('A', 'one.jpg', {'locationCity': 'Paris', 'rating': 0, 'tags': '[]'})
    assert source.queued == [('A', 'search')]
    su.touch_user_sort_index_state.assert_not_called()
    su.touch_user_tag_embedding_index_state.assert_not_called()


def test_unchanged_edit_can_relay_a_previous_pending_intent(source):
    row = source.table.get_entity('A', 'one.jpg')
    su.prepare_metadata_index_intent(row, {'rating': 0})
    source.table.upsert_entity(row)
    app._update_metadata_entity_fields('A', 'one.jpg', {'rating': 0})
    assert {scope for user, scope in source.queued} == {'search', 'sort', 'albums'}
    assert not source.table.get_entity('A', 'one.jpg')['indexPending']


def test_source_write_retains_outbox_when_scheduler_fails(source, monkeypatch):
    monkeypatch.setattr(su, 'METADATA_INDEX_REQUEST_HOOK', lambda *a: 'unavailable')
    app._update_metadata_entity_fields('A', 'one.jpg', {'rating': 4})
    row = source.table.get_entity('A', 'one.jpg')
    assert row['rating'] == 4
    assert set(json.loads(row['indexPending'])['kinds']) == {'lexical', 'sort', 'albums'}


def test_old_source_ack_cannot_clear_a_newer_source_edit(source, monkeypatch):
    def schedule(user, scope):
        if scope == 'sort':
            row = source.table.get_entity(user, 'one.jpg')
            row['indexPending'] = json.dumps({'revision': 'newer', 'kinds': ['access']})
            source.table.upsert_entity(row)
        return 'queued'
    monkeypatch.setattr(su, 'METADATA_INDEX_REQUEST_HOOK', schedule)
    app._update_metadata_entity_fields('A', 'one.jpg', {'rating': 3})
    assert json.loads(source.table.get_entity('A', 'one.jpg')['indexPending'])['revision'] == 'newer'
