"""Scoped maintenance cannot silently expand into unrelated library work."""
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from azure.core.exceptions import ResourceNotFoundError

import app
import index_updates
import storage_utils as su
from routes import tools
from tests.fakes import FakeTable


@pytest.mark.parametrize('kind,scope', [
    ('people', 'people'), ('albums', 'albums'), ('sort', 'sort'),
    ('access', 'access'), ('lexical', 'search'), ('vector', 'vector'),
    ('people_embedding', 'people_embedding'), ('tag_embedding', 'tag_embedding'),
])
def test_serving_request_preserves_index_kind(monkeypatch, kind, scope):
    queued = Mock(return_value='queued')
    monkeypatch.setattr(app, 'enqueue_index_build', queued)
    monkeypatch.setattr(su, 'INDEX_BUILD_REQUEST_HOOK', app._request_scoped_index_build)
    su.request_index_build('library-A', kind=kind)
    queued.assert_called_once_with('library-A', reason='serving-process', scope=scope)


@pytest.mark.parametrize('kind', ['people', 'albums', 'sort', 'access', 'lexical', 'people_embedding'])
def test_stale_read_kicker_does_not_request_full(monkeypatch, kind):
    queued = Mock(return_value='queued')
    monkeypatch.setattr(app, 'enqueue_index_build', queued)
    monkeypatch.setattr(su, 'INDEX_BUILD_REQUEST_HOOK', app._request_scoped_index_build)
    monkeypatch.setattr(su, '_ROLE_MAY_BUILD_INDEXES', False)
    getattr(su, f'_rebuild_{kind}_index_in_background')('library-A', {})
    assert queued.call_args.kwargs['scope'] == index_updates.scope_for_index(kind)


@pytest.fixture
def worker(monkeypatch):
    env = SimpleNamespace(
        prime=Mock(return_value=[]), search=Mock(return_value={'status': 'noop'}),
        sort_upgrade=Mock(), enqueue=Mock(), reconcile=Mock(), statuses=[],
    )
    monkeypatch.setattr(app, 'prime_all_user_indexes_sequentially', env.prime)
    monkeypatch.setattr(app, 'refresh_user_search_db_incremental', env.search)
    monkeypatch.setattr(app, 'storage_utils_ensure_sort_current', env.sort_upgrade)
    monkeypatch.setattr(app, 'enqueue_index_build', env.enqueue)
    monkeypatch.setattr(su, 'reconcile_search_db', env.reconcile)
    monkeypatch.setattr(app, '_upsert_job_status', lambda *a, **kw: env.statuses.append(a[3]))
    monkeypatch.setattr(app, 'get_dirty_people', lambda uid: set())
    monkeypatch.setattr(app, 'get_dirty_albums', lambda uid: set())
    # Configured hashes must not cause an unrelated follow-up job.
    monkeypatch.setitem(su._CTX, 'hash_index_table_client', object())
    return env


@pytest.mark.parametrize('scope,kinds,search,upgrade', [
    ('people', ('people',), True, False),
    ('albums', ('albums',), False, False),
    ('search', (), True, False),
    ('sort', ('sort',), False, True),
    ('access', ('access',), False, False),
    ('light', ('sort', 'access'), True, True),
])
def test_worker_only_runs_declared_dependencies(worker, scope, kinds, search, upgrade):
    app._run_index_build_job('library-A', f'index-build-library-A-{scope}', scope)
    assert worker.prime.call_args.kwargs['kinds'] == kinds
    assert worker.search.called is search
    assert worker.sort_upgrade.called is upgrade
    worker.reconcile.assert_not_called()
    worker.enqueue.assert_not_called()
    assert worker.statuses == ['running', 'done']


def test_full_build_no_longer_refreshes_unchanged_hashes(worker, monkeypatch):
    import library_build
    monkeypatch.setattr(library_build, 'bootstrap_needed', lambda uid: False)
    app._run_index_build_job('library-A', 'index-build-library-A', 'full')
    worker.enqueue.assert_not_called()


def test_index_failures_have_their_own_retry_ceiling(monkeypatch):
    writes, deadletters, deleted = [], [], []
    monkeypatch.setattr(app, 'INDEX_BUILD_MAX_RETRIES', 5)
    monkeypatch.setattr(app, '_upsert_job_status', lambda *a, **kw: writes.append((a, kw)))
    dispatch = Mock()
    message = SimpleNamespace(
        content=json.dumps({'type': 'index_build', 'userId': 'library-A', 'jobId': 'job'}),
        dequeue_count=6, id='m1', pop_receipt='r1',
    )
    queue = SimpleNamespace(delete_message=lambda msg: deleted.append(msg))
    deadletter = SimpleNamespace(send_message=lambda body: deadletters.append(json.loads(body)))
    app._process_clustering_queue_message(
        message, queue, 'library-ops', 30, deadletter, dispatch=dispatch)
    dispatch.assert_not_called()
    assert deleted == [message]
    assert deadletters[0]['maxRetries'] == 5
    assert writes[0][0][2:4] == ('index_build', 'failed')


def test_index_dispatch_failure_retains_its_job_type(monkeypatch):
    writes = []
    monkeypatch.setattr(app, '_upsert_job_status', lambda *a, **kw: writes.append((a, kw)))
    message = SimpleNamespace(
        content=json.dumps({'type': 'index_build', 'userId': 'library-A', 'jobId': 'job'}),
        dequeue_count=1, id='m1', pop_receipt='r1',
    )
    deleted = Mock()
    queue = SimpleNamespace(delete_message=deleted)
    app._process_clustering_queue_message(
        message, queue, 'library-ops', 30,
        dispatch=Mock(side_effect=RuntimeError('publication failed')))
    deleted.assert_not_called()
    assert writes[-1][0][2:4] == ('index_build', 'failed')


def test_people_job_includes_albums_only_with_explicit_album_changes(worker, monkeypatch):
    monkeypatch.setattr(app, 'get_dirty_albums', lambda uid: {'album-1'})
    clear = Mock()
    monkeypatch.setattr(app, 'clear_dirty_albums', clear)
    app._run_index_build_job('library-A', 'job', 'people')
    assert worker.prime.call_args.kwargs['kinds'] == ('people', 'albums')
    clear.assert_called_once_with('library-A', {'album-1'})


@pytest.mark.parametrize('scope', ['vector', 'tag_embedding', 'people_embedding'])
def test_embedding_repair_does_not_build_gallery_or_hashes(worker, monkeypatch, scope):
    refresh = Mock(return_value=object())
    monkeypatch.setattr(su, f'refresh_user_{scope}_index', refresh)
    app._run_index_build_job('library-A', 'job', scope)
    refresh.assert_called_once_with('library-A')
    for fn in (worker.prime, worker.search, worker.sort_upgrade, worker.enqueue):
        fn.assert_not_called()


def test_invalid_scope_never_becomes_full(worker):
    with pytest.raises(ValueError, match='scope'):
        app._run_index_build_job('library-A', 'job', 'typo')
    worker.prime.assert_not_called()
    assert worker.statuses == []


def test_invalid_producer_scope_does_not_write_a_job_or_message(monkeypatch):
    send, status = Mock(), Mock()
    monkeypatch.setattr(app, 'library_ops_queue_client', SimpleNamespace(send_message=send))
    monkeypatch.setattr(app, '_upsert_job_status', status)
    with pytest.raises(ValueError, match='scope'):
        app.enqueue_index_build('library-A', scope='typo')
    send.assert_not_called()
    status.assert_not_called()


def test_coalescing_is_isolated_by_library_and_scope(monkeypatch):
    rows, sent = {}, []
    monkeypatch.setattr(app, 'jobs_table_client', object())
    monkeypatch.setattr(app, '_get_job_row', lambda library, job: rows.get((library, job)))

    def status(job, library, job_type, state, **kwargs):
        rows[(library, job)] = {
            'status': state, 'updatedAt': app.datetime.now(app.timezone.utc).isoformat(), **kwargs,
        }

    monkeypatch.setattr(app, '_upsert_job_status', status)
    monkeypatch.setattr(app, 'library_ops_queue_client', SimpleNamespace(
        send_message=lambda body, **kw: sent.append(json.loads(body))))
    assert app.enqueue_index_build('library-A', scope='people') == 'queued'
    assert app.enqueue_index_build('library-A', scope='people') == 'already_active'
    assert app.enqueue_index_build('library-B', scope='people') == 'queued'
    assert app.enqueue_index_build('library-A', scope='search') == 'queued'
    assert [(row['userId'], row['scope']) for row in sent] == [
        ('library-A', 'people'), ('library-B', 'people'), ('library-A', 'search'),
    ]


@pytest.mark.parametrize('kind', ['sort', 'access', 'albums', 'people'])
def test_missing_publication_is_not_reported_as_success(monkeypatch, kind):
    monkeypatch.setattr(su, f'refresh_user_{kind}_index', lambda *a, **kw: None)
    assert su.prime_all_user_indexes_sequentially(
        f'missing-{kind}', wait=True, kinds=(kind,)) == [kind]


@pytest.mark.parametrize('scope', ['people', 'albums', 'search', 'sort', 'access'])
def test_http_fallback_preserves_explicit_scope_even_when_other_indexes_missing(monkeypatch, scope):
    monkeypatch.setattr(app, '_require_user_id', lambda: ('library-A', None))
    monkeypatch.setattr(app, 'index_build_needed', Mock(side_effect=AssertionError('unrelated readiness')))
    enqueue = Mock(return_value='queued')
    monkeypatch.setattr(app, 'enqueue_index_build', enqueue)
    monkeypatch.setattr(app, 'get_user_index_build_state', lambda uid: {'ready': False, 'indexes': {}})
    with app.app.test_request_context('/api/tools/indexes/build', method='POST', json={'scope': scope}):
        response = tools.tools_build_indexes()
    assert response.get_json()['queued'] == 'queued'
    assert enqueue.call_args.kwargs['scope'] == scope


@pytest.mark.parametrize('kind,scope', [
    ('people', 'people'), ('albums', 'albums'), ('lexical', 'search'), ('sort', 'sort'), ('all', 'full'),
])
def test_manual_refresh_uses_supported_api_and_exact_scope(monkeypatch, kind, scope):
    monkeypatch.setattr(app, '_require_user_id', lambda: ('library-A', None))
    sent = []
    monkeypatch.setattr(app, 'library_ops_queue_client', SimpleNamespace(
        send_message=lambda body, **kw: sent.append(json.loads(body))))
    monkeypatch.setattr(app, 'jobs_table_client', None)
    monkeypatch.setattr(app, '_upsert_job_status', lambda *a, **kw: None)
    monkeypatch.setattr(su, 'get_user_index_status_detailed', lambda uid: {'indexes': {}})
    with app.app.test_request_context('/api/tools/indexes/rebuild/test', method='POST'):
        _, status = tools.tools_rebuild_index(kind)
    assert status == 202
    assert sent[0]['scope'] == scope


def test_incremental_people_uses_snapshot_rows_without_scanning(monkeypatch):
    people = FakeTable()
    people.upsert_entity({'PartitionKey': 'library-A', 'RowKey': 'p2', 'name': 'Bob', 'faceIds': '[]'})
    monkeypatch.setitem(su._CTX, 'person_table_client', people)
    monkeypatch.setitem(su._CTX, 'face_table_client', FakeTable())
    monkeypatch.setattr(people, 'query_entities', Mock(side_effect=AssertionError('person scan')))
    previous = [
        {'personId': 'p1', 'name': 'Unnamed 77', 'autoName': True, 'isNamed': False},
        {'personId': 'p2', 'name': 'Old', 'autoName': False, 'isNamed': True},
    ]
    snapshot = su._incremental_people_index_snapshot_by_ids('library-A', 'v2', {'p2'}, previous)
    assert [r['name'] for r in snapshot.rows] == ['Bob', 'Unnamed 1']
    assert previous[0]['name'] == 'Unnamed 77'


def test_incremental_people_preserves_removed_people_deletion(monkeypatch):
    class Gone:
        def get_entity(self, **kwargs):
            raise ResourceNotFoundError('deleted')
    monkeypatch.setitem(su._CTX, 'person_table_client', Gone())
    monkeypatch.setitem(su._CTX, 'face_table_client', FakeTable())
    snapshot = su._incremental_people_index_snapshot_by_ids(
        'library-A', 'v2', {'gone'}, [{'personId': 'gone'}])
    assert snapshot.rows == []


def test_transient_person_read_is_not_a_deletion(monkeypatch):
    class Unavailable:
        def get_entity(self, **kwargs):
            raise RuntimeError('storage unavailable')
    monkeypatch.setitem(su._CTX, 'person_table_client', Unavailable())
    monkeypatch.setitem(su._CTX, 'face_table_client', FakeTable())
    assert su._incremental_people_index_snapshot_by_ids(
        'library-A', 'v2', {'p1'}, [{'personId': 'p1', 'name': 'Alice'}]) is None


def test_waiting_scope_runs_its_own_work_and_does_not_block_another_library(monkeypatch):
    entered, release, people_done = threading.Event(), threading.Event(), threading.Event()
    calls = []
    errors = []

    def albums(library, **kwargs):
        entered.set()
        if not release.wait(timeout=5):
            raise RuntimeError('test release timed out')
        calls.append((library, 'albums'))
        return object()

    def people(library, **kwargs):
        calls.append((library, 'people'))
        return object()

    monkeypatch.setattr(su, 'refresh_user_albums_index', albums)
    monkeypatch.setattr(su, 'refresh_user_people_index', people)

    def run(library, kinds, done=None):
        try:
            assert su.prime_all_user_indexes_sequentially(library, wait=True, kinds=kinds) == []
        except Exception as error:
            errors.append(error)
        finally:
            if done is not None:
                done.set()

    first = threading.Thread(target=run, args=('scope-test-A', ('albums',)))
    second = threading.Thread(target=run, args=('scope-test-A', ('people',), people_done))
    first.start()
    try:
        assert entered.wait(timeout=5)
        second.start()
        run('scope-test-B', ('people',))
        assert ('scope-test-B', 'people') in calls
        assert not people_done.wait(timeout=0.05)
    finally:
        release.set()
        first.join(timeout=5)
        if second.ident is not None:
            second.join(timeout=5)
    assert not first.is_alive() and not second.is_alive()
    assert not errors
    assert calls[-2:] == [('scope-test-A', 'albums'), ('scope-test-A', 'people')]
