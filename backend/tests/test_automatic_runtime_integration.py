"""Actual worker replay/recovery plus renewable universal lease boundaries."""
from contextlib import contextmanager
import hashlib
import threading
from types import SimpleNamespace

import pytest

import app
import automatic_index_updates as automatic
import index_artifact_cleanup as gc
import index_publication as publication
import index_work
import storage_utils as su
from index_ownership import catalog_publisher, model_tag_publisher
from tests.test_automatic_index_updates import Table, runtime
from tests.test_clustering_lease import FakeService


@pytest.fixture
def leased(runtime, monkeypatch):
    service = FakeService()
    su._CTX.update(blob_service_client=service, blob_lexical_index_container='catalog')
    return runtime, service


def prove(user='A'):
    fence = automatic.operator_fence(user, operator='admin', reason='all processes stopped')['fenceId']
    with gc.artifact_catalog_serialization(user, storage=su) as guard:
        automatic.operator_prove(user, fence, guard=guard, operator='admin', reason='drained externally',
            all_writers_stopped=True, no_paused_requests=True, legacy_writers_stopped=True)
    return fence


def test_nonwave_real_lock_reused_by_nested_full_tag_and_hash_publishers(leased):
    runtime, service = leased
    observed = []
    @catalog_publisher
    def full(user):
        with gc.artifact_catalog_serialization(user, storage=su) as guard:
            observed.append(guard)
            tag(user)
    @model_tag_publisher
    def tag(user):
        with gc.artifact_catalog_serialization(user, storage=su) as guard:
            observed.append(guard)
    full('A')
    blob = service.blobs['catalog', 'catalog-publication-locks/' + hashlib.sha256(b'A').hexdigest() + '.json']
    assert len(blob.leases) == 1 and blob.leases[0].releases == 1
    assert observed[0] is observed[1]
    with pytest.raises(RuntimeError):
        observed[0].check()


def test_publisher_waits_before_any_base_capture(leased, monkeypatch):
    runtime, service = leased
    entered, contended, allow = threading.Event(), threading.Event(), threading.Event()
    observed, errors = [], []
    blob = service.get_blob_client(container='catalog', blob='catalog-publication-locks/' +
                                   hashlib.sha256(b'A').hexdigest() + '.json')
    acquire = blob.acquire_lease
    def acquire_observed(**kw):
        if blob.active is not None:
            contended.set()
        return acquire(**kw)
    monkeypatch.setattr(blob, 'acquire_lease', acquire_observed)
    def backoff(_):
        assert allow.wait(5)
    monkeypatch.setattr(publication.time, 'sleep', backoff)
    @catalog_publisher
    def builder(user):
        observed.append('base captured')
    def holder():
        with gc.artifact_catalog_serialization('A', storage=su):
            entered.set()
            assert allow.wait(5)
    def contender():
        try:
            builder('A')
        except Exception as exc:
            errors.append(exc)
    first = threading.Thread(target=holder)
    second = threading.Thread(target=contender)
    first.start()
    assert entered.wait(5)
    second.start()
    assert contended.wait(5)
    assert observed == []
    allow.set()
    first.join(5)
    second.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert errors == [] and observed == ['base captured']


def test_automatic_worker_lease_loss_after_publish_preserves_dirty(leased, monkeypatch):
    runtime, service = leased
    runtime.store.begin('A', 'vector', ['one'])
    runtime.store.finish('A', 'vector')
    original = su.refresh_user_vector_index
    def lose(user, **kw):
        receipt = original(user, **kw)
        active = publication._CATALOG.get()[1]
        active._lost = RuntimeError('renewal failed')
        return receipt
    monkeypatch.setattr(su, 'refresh_user_vector_index', lose)
    with pytest.raises(RuntimeError):
        app._run_index_build_job('A', 'job', 'vector')
    assert runtime.store.capture('A', 'vector').ids == ['one']
    assert runtime.store.control('A', 'vector')['base'] == ''


def test_real_catalog_lock_renews_and_fails_closed(leased):
    _, service = leased
    with pytest.raises(RuntimeError, match='lease lost'):
        with gc.artifact_catalog_serialization('A', storage=su) as guard:
            guard._renew()
            assert guard._lease.renewals == 1
            guard._lease.renew_error = OSError('transport failed')
            guard._renew()
            with pytest.raises(RuntimeError):
                guard.check()


def test_no_real_guard_cannot_recover_or_purge(runtime):
    fake = SimpleNamespace(check=lambda: None)
    with pytest.raises(RuntimeError, match='real catalog guard'):
        automatic.quiescence_check('A', guard=fake)
    with pytest.raises(RuntimeError, match='storage unavailable'):
        with gc.artifact_catalog_serialization('A', storage=su):
            pytest.fail('unsafe guard')


def test_quiesced_recovery_resets_orphan_without_losing_ids_and_persists_all_jobs(leased, monkeypatch):
    monkeypatch.setattr(automatic, 'VECTOR_INDEX_AUTOMATIC', True)
    runtime, _ = leased
    runtime.store.begin('A', 'vector', ['orphan'])
    fence = prove()
    with pytest.raises(automatic.SourceBusy):
        su.track_index_source_table(runtime.source, 'embeddings').upsert_entity(
            {'PartitionKey': 'A', 'RowKey': 'blocked'})
    with gc.artifact_catalog_serialization('A', storage=su) as guard:
        result = automatic.recover_quiesced_source_reservations('A', guard=guard, fence_id=fence)
    assert result['recovered'] == list(automatic.KINDS)
    assert result['orphanedReservations']['vector'][0]['idsCount'] == 1
    assert runtime.store.reservations('A', 'vector') == []
    assert runtime.store.capture('A', 'vector').ids == ['orphan']
    assert runtime.store.control('A', 'vector')['active'] == 0
    assert all(runtime.store.control('A', kind)['fullRequired'] for kind in automatic.KINDS)
    for kind in automatic.KINDS:
        store = index_work.WorkStore(runtime.jobs, partition=automatic.TAG_PARTITION
            if kind == 'tag_embedding' else index_work.PARTITION)
        assert store._get(store.key('A', automatic.SCOPES.get(kind, kind)))
    with gc.artifact_catalog_serialization('A', storage=su) as guard:
        automatic.resume_quiesced_sources('A', guard=guard, fence_id=fence)
    automatic.check_source_admission('A')
    assert app.recover_pending_index_work('A') >= 1


def test_recovery_job_failure_keeps_orphan_active_and_fenced(leased, monkeypatch):
    monkeypatch.setattr(automatic, 'VECTOR_INDEX_AUTOMATIC', True)
    runtime, _ = leased
    runtime.store.begin('A', 'vector', ['orphan'])
    fence = prove()
    monkeypatch.setattr(index_work.WorkStore, 'request', lambda *a, **kw:
                        (_ for _ in ()).throw(OSError('ledger unavailable')))
    with gc.artifact_catalog_serialization('A', storage=su) as guard:
        with pytest.raises(OSError):
            automatic.recover_quiesced_source_reservations('A', guard=guard, fence_id=fence)
    assert runtime.store.control('A', 'vector')['active'] == 1
    assert automatic._quiescence_row('A')['proven']


def test_active_claim_prevents_operator_proof(leased):
    runtime, _ = leased
    work = index_work.WorkStore(runtime.jobs)
    key = work.request('A', 'search')
    claim = work.claim(key)
    fence = automatic.operator_fence('A', operator='admin', reason='stop')['fenceId']
    with gc.artifact_catalog_serialization('A', storage=su) as guard:
        with pytest.raises(automatic.SourceBusy, match='maintenance claim'):
            automatic.operator_prove('A', fence, guard=guard, operator='admin', reason='stop',
                all_writers_stopped=True, no_paused_requests=True, legacy_writers_stopped=True)
    work.release(claim)


def test_purge_flow_defers_before_any_source_delete_without_proof(leased, monkeypatch):
    calls = []
    monkeypatch.setattr(app.processing_cleanup if hasattr(app, 'processing_cleanup') else
                        __import__('processing_cleanup'), 'begin_library_cleanup',
                        lambda *a: calls.append('begun'))
    with pytest.raises(automatic.SourceBusy, match='operator'):
        app._purge_library_data('A')
    with pytest.raises(automatic.SourceBusy, match='operator'):
        app._execute_library_clean('A')
    assert calls == []


@pytest.mark.parametrize('scope', ['search', 'people'])
def test_actual_worker_summary_failure_replays_noop_and_keeps_claim(runtime, monkeypatch, scope):
    statuses, publications = [], []
    monkeypatch.setattr(app, '_upsert_job_status', lambda *a, **kw: statuses.append((a, kw)))
    monkeypatch.setattr(app, 'prime_all_user_indexes_sequentially', lambda *a, **kw: [])
    people_dirty, albums_dirty = Table(), Table()
    monkeypatch.setattr(app, 'people_dirty_set_table_client', people_dirty)
    monkeypatch.setattr(app, 'albums_dirty_set_table_client', albums_dirty)
    if scope == 'people':
        app.mark_people_dirty('A', ['p1'])
        app.mark_albums_dirty('A', ['album1'])
    outcomes = iter([{'status': 'delta'}, {'status': 'noop'}])
    monkeypatch.setattr(app, 'refresh_user_search_db_incremental', lambda _: next(outcomes))
    def summaries(user):
        publications.append(user)
        if len(publications) == 1:
            raise RuntimeError('summary publication not confirmed')
    monkeypatch.setattr(app, '_refresh_library_summaries', summaries)
    work = index_work.WorkStore(runtime.jobs)
    key = work.request('A', scope)
    payload = dict(userId='A', scope=scope, jobId='job', maintenanceKey=key)
    with pytest.raises(RuntimeError, match='summary publication'):
        app._run_index_maintenance_payload(payload)
    row = work._get(key)
    assert row and not row['owner'] and not row['failed']
    if scope == 'people':
        assert app.get_dirty_people('A') == {'p1'}
        assert app.get_dirty_albums('A') == {'album1'}
    app._run_index_maintenance_payload(payload)
    assert work._get(key) is None and publications == ['A', 'A']
    assert statuses[-1][1]['result'] == {'status': 'noop'}
    assert not app.get_dirty_people('A') and not app.get_dirty_albums('A')


@pytest.mark.parametrize('outcomes', [
    ['noop'], ['delta'], ['more', 'delta'],
])
def test_actual_worker_publishes_sqlite_summaries_without_compaction(leased, monkeypatch, tmp_path, outcomes):
    import search_db
    runtime, _ = leased
    path = str(tmp_path / 'search.sqlite')
    search_db.build_database(iter([{'RowKey': 'seed', 'uploadDate': '2020-01-01T00:00:00Z'}]), path)
    db = search_db.SearchDatabase(path)
    monkeypatch.setattr(search_db, 'open_database', lambda _: db)
    def forbidden(*args, **kwargs):
        pytest.fail('successful bounded drain must not compact or scan summary rows')
    monkeypatch.setattr(search_db, 'compact_database', forbidden)
    monkeypatch.setattr(db, 'iter_smart_rows', forbidden)
    monkeypatch.setattr(app, 'prime_all_user_indexes_sequentially', lambda *a, **kw: [])
    passes = iter(outcomes)
    def drain(_):
        status = next(passes)
        if status == 'delta':
            db.apply_delta({'seq': 1, 'upserts': [search_db.row_record(
                {'RowKey': 'new', 'uploadDate': '2020-01-02T00:00:00Z'})]})
        return {'status': status}
    monkeypatch.setattr(app, 'refresh_user_search_db_incremental', drain)
    published = []
    for kind in ('explore', 'timeline'):
        monkeypatch.setattr(app, 'store_' + kind + '_summary',
            lambda user, value, kind=kind: published.append((kind, user, value)))
        monkeypatch.setattr(app, 'load_' + kind + '_summary',
            lambda user, kind=kind: next(value for stored, owner, value in reversed(published)
                                       if stored == kind and owner == user))
    work = index_work.WorkStore(runtime.jobs)
    key = work.request('A', 'search')
    app._run_index_maintenance_payload(dict(userId='A', scope='search', jobId='job', maintenanceKey=key))
    assert work._get(key) is None
    assert [(kind, user) for kind, user, _ in published] == [('explore', 'A'), ('timeline', 'A')]
    explore, timeline = [value for _, _, value in published]
    assert explore['sourceVersion'] == timeline['sourceVersion']
    assert timeline['totalCount'] == (2 if 'delta' in outcomes else 1)
    assert db.applied_seq() == (1 if 'delta' in outcomes else 0)


def test_actual_worker_missing_summary_schema_schedules_compaction_and_retains_retry(runtime, monkeypatch):
    from summary_incremental import SummarySchemaMissing
    monkeypatch.setattr(app, 'prime_all_user_indexes_sequentially', lambda *a, **kw: [])
    monkeypatch.setattr(app, 'refresh_user_search_db_incremental', lambda _: {'status': 'noop'})
    def missing(_):
        raise SummarySchemaMissing('legacy schema needs explicit compaction')
    monkeypatch.setattr(app, '_refresh_library_summaries', missing)
    work = index_work.WorkStore(runtime.jobs)
    key = work.request('A', 'search')
    with pytest.raises(SummarySchemaMissing):
        app._run_index_maintenance_payload(dict(userId='A', scope='search', jobId='job', maintenanceKey=key))
    row = work._get(key)
    assert row and not row['owner'] and not row['failed']
    assert work._get(work.key('A', 'compact'))


def test_quiesced_purge_hash_failure_cannot_finish_and_remains_retryable(leased, monkeypatch):
    import processing_cleanup
    runtime, _ = leased
    hashes = Table()
    hashes.upsert_entity({'PartitionKey': 'A', 'RowKey': 'hash'})
    hashes.upsert_entity({'PartitionKey': 'other', 'RowKey': 'hash'})
    for name in vars(app):
        if name.endswith('_table_client') and name != 'jobs_table_client':
            monkeypatch.setattr(app, name, None)
    monkeypatch.setattr(app, 'hash_index_table_client', hashes)
    events = []
    monkeypatch.setattr(processing_cleanup, 'begin_library_cleanup', lambda *a: events.append('begin'))
    monkeypatch.setattr(processing_cleanup, 'collect_library_artifacts', lambda *a, **kw: events.append('collect'))
    monkeypatch.setattr(processing_cleanup, 'finish_library_cleanup', lambda *a, **kw: events.append('finish'))
    def artifact_purge(user):
        check = automatic.current_cleanup_check(user)
        assert check is not None
        check()
        assert ('A', 'hash') not in hashes.rows
        events.append('artifacts')
    monkeypatch.setattr(app, 'quiesced_user_index_artifact_purge', artifact_purge)
    prove()
    delete = hashes.delete_entity
    def fail(**kwargs):
        raise OSError('hash table unavailable')
    monkeypatch.setattr(hashes, 'delete_entity', fail)
    with pytest.raises(OSError, match='hash table unavailable'):
        app._purge_library_data('A')
    assert events == ['begin']
    assert automatic._quiescence_row('A')['proven']
    monkeypatch.setattr(hashes, 'delete_entity', delete)
    app._purge_library_data('A')
    assert events == ['begin', 'begin', 'collect', 'artifacts', 'finish']
    assert set(hashes.rows) == {('other', 'hash')}


def test_worker_gc_runs_after_claim_release_with_shared_live_lock(leased, monkeypatch):
    runtime, _ = leased
    monkeypatch.setenv('APP_ROLE', 'index-worker')
    work = index_work.WorkStore(runtime.jobs)
    key = work.request('A', 'vector')
    monkeypatch.setattr(app, '_run_index_build_job', lambda *a: None)
    observed = []
    def collect(user, *, storage, jobs, serialization):
        assert work._get(key) is None
        assert publication._ACTIVE.get() is None
        with serialization(user) as guard:
            publication.require_catalog_guard(user, guard)
            observed.append(user)
        return {'status': 'complete', 'deleted': 0}
    monkeypatch.setattr(gc, 'cleanup_user_index_artifacts', collect)
    app._run_index_maintenance_payload(dict(userId='A', scope='vector', jobId='job', maintenanceKey=key))
    assert observed == ['A']


def test_admin_recovery_requires_authenticated_allowlisted_operator(monkeypatch):
    from routes.admin import recover_source_reservations
    monkeypatch.setattr(app, '_require_admin', lambda **kw: (None, ('denied', 403)))
    with app.app.test_request_context('/api/admin/indexes/source-reservations', method='POST',
                                      json={'action': 'recover'}):
        assert recover_source_reservations() == ('denied', 403)