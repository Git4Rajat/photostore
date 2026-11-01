"""Safety regressions for worker GC and destructive, scoped purge discovery."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import threading
from types import SimpleNamespace

from azure.core import MatchConditions
from azure.core.exceptions import ResourceModifiedError, ResourceNotFoundError
from azure.data.tables import TableEntity
import pytest

import automatic_index_updates as automatic
import index_artifact_cleanup as gc
import index_work
import storage_utils as su

NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=3)
USER = 'gc-library'


def entity(pk, rk, **values):
    row = TableEntity(PartitionKey=pk, RowKey=rk, **values)
    row._metadata = {'etag': 'row-' + rk}
    return row


class Table:
    def __init__(self):
        self.rows, self.queries, self.deleted = {}, [], []
        self.error = None

    def get_entity(self, partition_key, row_key):
        if self.error:
            raise self.error
        try:
            return self.rows[partition_key, row_key]
        except KeyError:
            raise ResourceNotFoundError('row')

    def query_entities(self, query, results_per_page):
        self.queries.append((query, results_per_page))
        assert query.startswith('PartitionKey eq ')
        pk = query.split("'")[1]
        for (partition, _), row in list(self.rows.items()):
            if partition == pk and ('libraryId eq' not in query or
                                   "libraryId eq '" + row.get('libraryId', '').replace("'", "''") + "'" in query):
                yield row

    def delete_entity(self, partition_key, row_key, *, etag, match_condition):
        row = self.get_entity(partition_key, row_key)
        assert match_condition == MatchConditions.IfNotModified
        if index_work.etag(row) != etag:
            raise ResourceModifiedError('table etag')
        self.deleted.append((partition_key, row_key))
        del self.rows[partition_key, row_key]


class Service:
    def __init__(self):
        self.data, self.queries, self.deleted = {}, [], []
        self.revision = 0
        self.lock = threading.RLock()
        self.download_hook = self.properties_hook = self.delete_hook = None
        self.guard_lost = False
        self.page_reads = 0

    def put(self, container, name, data=b'npz', modified=OLD):
        with self.lock:
            self.revision += 1
            self.data[container, name] = (data, str(self.revision), modified)

    def get_blob_client(self, container, blob):
        return Blob(self, container, blob)

    def get_container_client(self, container):
        service = self

        class Container:
            def list_blobs(self, *, name_starts_with, results_per_page):
                assert name_starts_with
                service.queries.append((container, name_starts_with, results_per_page))

                class Paged:
                    def by_page(self):
                        names = sorted(name for c, name in service.data
                                       if c == container and name.startswith(name_starts_with))
                        for offset in range(0, len(names), results_per_page):
                            service.page_reads += 1
                            yield [Blob(service, container, name).get_blob_properties()
                                   for name in names[offset:offset + results_per_page]
                                   if (container, name) in service.data]
                return Paged()
        return Container()

    @contextmanager
    def serialization(self, user):
        assert user == USER
        with self.lock:
            service = self

            class Guard:
                def check(self):
                    if service.guard_lost:
                        raise RuntimeError('catalog lease lost')
            yield Guard()


class Blob:
    def __init__(self, service, container, name):
        self.service, self.container, self.name = service, container, name

    def get_blob_properties(self):
        if self.service.properties_hook:
            self.service.properties_hook(self)
        try:
            data, version, modified = self.service.data[self.container, self.name]
        except KeyError:
            raise ResourceNotFoundError('blob')
        return SimpleNamespace(name=self.name, size=len(data), etag=version, last_modified=modified)

    def download_blob(self, *, offset, length, etag, match_condition):
        if self.service.download_hook:
            self.service.download_hook(self)
        props = self.get_blob_properties()
        assert match_condition == MatchConditions.IfNotModified
        if props.etag != etag:
            raise ResourceModifiedError('manifest download CAS')
        data = self.service.data[self.container, self.name][0][offset:offset + length]
        return SimpleNamespace(readall=lambda: data)

    def delete_blob(self, *, etag=None, match_condition=None):
        if self.service.delete_hook:
            self.service.delete_hook(self)
        props = self.get_blob_properties()
        if etag is not None:
            assert match_condition == MatchConditions.IfNotModified
            if props.etag != etag:
                raise ResourceModifiedError('delete CAS')
        self.service.deleted.append((self.container, self.name, etag, match_condition))
        del self.service.data[self.container, self.name]


@pytest.fixture
def env(monkeypatch):
    service, dirty, jobs, hashes = Service(), Table(), Table(), Table()
    monkeypatch.setenv('APP_ROLE', 'index-worker')
    monkeypatch.setattr(su, '_CTX', dict(blob_service_client=service,
        search_index_dirty_table_client=dirty, hash_index_table_client=hashes))
    for user in (USER, 'other'):
        for kind in automatic.KINDS:
            row = entity(automatic.partition(user, kind), automatic.CONTROL,
                         libraryId=user, kind=kind, coverageVersion=automatic.VERSION,
                         revision='revision', coveredRevision='revision', active=0,
                         fullRequired=False, base='base')
            dirty.rows[row['PartitionKey'], row['RowKey']] = row
    return SimpleNamespace(service=service, dirty=dirty, jobs=jobs, hashes=hashes)


def setup_scope(env, kind='vector', *, user=USER, current=90, field=None):
    scope = next(s for s in gc.scopes(su, user) if s.kind == kind)
    name = scope.prefix + f'{current:032x}' + scope.suffixes[0]
    field = field or ('npzBlobName' if kind in ('people_embedding', 'tag_embedding') else 'blobName')
    row = dict(userId=user, schemaVersion=scope.schema, sourceVersion='base', **{field: name})
    if kind == 'hash':
        row = entity(user, '!upload-hash-snapshot', schemaVersion=1, blobName=name, Timestamp=OLD)
        env.hashes.rows[user, row['RowKey']] = row
        env.dirty.rows[automatic.partition(user, kind), automatic.CONTROL]['base'] = name
    else:
        env.service.put(scope.container, scope.manifest, json.dumps(row).encode())
    env.service.put(scope.container, name)
    return scope, name, row


def add(env, scope, generation, *, suffix=None, modified=OLD):
    name = scope.prefix + f'{generation:032x}' + (suffix or scope.suffixes[0])
    env.service.put(scope.container, name, modified=modified)
    return name


def run(env, kind='vector', **kwargs):
    return gc.cleanup_user_index_artifacts(USER, storage=su, jobs=env.jobs,
        serialization=env.service.serialization, kinds=(kind,), now=NOW, **kwargs)


@pytest.mark.parametrize('kind', gc.KINDS)
def test_active_current_previous_and_old_orphan(env, kind):
    scope, current, _ = setup_scope(env, kind)
    orphan = add(env, scope, 1)
    previous = add(env, scope, 2, modified=OLD + timedelta(hours=1))
    result = run(env, kind)
    assert result['status'] == 'complete'
    assert result['deleted'] == 1
    assert (scope.container, orphan) not in env.service.data
    assert (scope.container, current) in env.service.data
    assert (scope.container, previous) in env.service.data
    assert all(prefix == scope.prefix for _, prefix, _ in env.service.queries)
    assert all(etag and condition == MatchConditions.IfNotModified
               for _, _, etag, condition in env.service.deleted)


@pytest.mark.parametrize('fields', [('npzBlobName', 'contributionBlobName'), ('blobName', 'stateBlobName')])
def test_atomic_tag_pointers_sidecars_and_lineage_are_protected(env, fields):
    scope, current, row = setup_scope(env, 'tag_embedding', field=fields[0])
    state = add(env, scope, 90, suffix='.sqlite')
    row[fields[1]] = state
    base = add(env, scope, 80)
    row['snapshotLineage'] = [{'baseBlobName': base}]
    env.service.put(scope.container, scope.manifest, json.dumps(row).encode())
    base_state = add(env, scope, 80, suffix='.sqlite')
    add(env, scope, 2, modified=OLD + timedelta(hours=1))
    orphan = add(env, scope, 1, suffix='.sqlite')
    assert run(env, 'tag_embedding')['deleted'] == 1
    assert all((scope.container, name) in env.service.data for name in (current, state, base, base_state))
    assert (scope.container, orphan) not in env.service.data


def test_lexical_snapshot_lineage_survives_shared_source_changes(env):
    scope, current, row = setup_scope(env, 'lexical_artifact')
    base = add(env, scope, 1)
    row.update(sourceVersion='shared-catalog', lexicalSnapshotSourceVersion='snapshot-only',
               baseBlobName=base)
    env.dirty.rows[automatic.partition(USER, 'lexical_artifact'), automatic.CONTROL]['base'] = 'snapshot-only'
    env.service.put(scope.container, scope.manifest, json.dumps(row).encode())
    add(env, scope, 2)
    assert run(env, 'lexical_artifact')['deleted'] == 0
    assert (scope.container, current) in env.service.data
    assert (scope.container, base) in env.service.data


def test_grace_for_young_orphans_and_freshly_retired_old_reader(env):
    scope, _, row = setup_scope(env)
    retired = add(env, scope, 1)
    add(env, scope, 2, modified=NOW - timedelta(hours=1))
    env.service.put(scope.container, scope.manifest, json.dumps(row).encode(), modified=NOW)
    result = run(env)
    assert result['deleted'] == 0 and 'reader grace' in result['reason']
    assert (scope.container, retired) in env.service.data
    env.service.put(scope.container, scope.manifest, json.dumps(row).encode(), modified=OLD)
    young = add(env, scope, 3, modified=NOW - timedelta(hours=2))
    result = run(env)
    assert result['deleted'] == 1
    assert (scope.container, young) in env.service.data


def test_keep_extra_previous_generations_and_cap(env):
    scope, current, _ = setup_scope(env)
    for i in range(12):
        add(env, scope, i, modified=OLD + timedelta(minutes=i))
    result = run(env, policy=gc.Policy(max_deletions=3, retain_generations=3))
    assert result['status'] == 'capped' and result['deleted'] == 3
    assert (scope.container, current) in env.service.data
    assert all((scope.container, scope.prefix + f'{i:032x}.npz') in env.service.data for i in (10, 11))


def test_page_cap_is_fail_closed_before_any_delete(env):
    scope, _, _ = setup_scope(env)
    for i in range(12):
        add(env, scope, i)
    result = run(env, policy=gc.Policy(page_size=2, max_pages=2))
    assert result['status'] == 'deferred' and result['deleted'] == 0
    assert env.service.page_reads == 3
    assert not env.service.deleted


@pytest.mark.parametrize('problem', ['missing', 'corrupt', 'unknown', 'crossuser', 'unknown-pointer', 'no-etag', 'timestamp'])
def test_manifest_evidence_fail_closed(env, problem):
    scope, _, row = setup_scope(env)
    add(env, scope, 1)
    add(env, scope, 2)
    if problem == 'missing':
        del env.service.data[scope.container, scope.manifest]
    else:
        if problem == 'unknown':
            row['schemaVersion'] = 999
        if problem == 'crossuser':
            row['blobName'] = su._vector_index_blob_key('other') + '.' + 'a' * 32 + '.npz'
        if problem == 'unknown-pointer':
            row['futureBlobName'] = row['blobName']
        raw = b'{broken' if problem == 'corrupt' else json.dumps(row).encode()
        env.service.put(scope.container, scope.manifest, raw)
        if problem == 'no-etag':
            data, _, modified = env.service.data[scope.container, scope.manifest]
            env.service.data[scope.container, scope.manifest] = (data, '*', modified)
        if problem == 'timestamp':
            data, etag, _ = env.service.data[scope.container, scope.manifest]
            env.service.data[scope.container, scope.manifest] = (data, etag, None)
    result = run(env)
    assert result['status'] == 'deferred' and result['deleted'] == 0


@pytest.mark.parametrize('problem', ['active', 'fullRequired', 'missing', 'revision', 'schema', 'error', 'lease', 'model-lease'])
def test_relevant_source_and_global_publication_reservations_protect_kind(env, problem):
    scope, _, _ = setup_scope(env)
    add(env, scope, 1)
    add(env, scope, 2)
    pk = automatic.partition(USER, 'vector')
    row = env.dirty.rows[pk, automatic.CONTROL]
    if problem == 'active':
        row['active'] = 1
    elif problem == 'fullRequired':
        row['fullRequired'] = True
    elif problem == 'missing':
        del env.dirty.rows[pk, automatic.CONTROL]
    elif problem == 'revision':
        row['revision'] = 'new-source'
    elif problem == 'schema':
        row['coverageVersion'] = 999
    elif problem == 'error':
        env.dirty.error = RuntimeError('coverage read failed')
    else:
        partition = automatic.TAG_PARTITION if problem == 'model-lease' else index_work.PARTITION
        key = index_work.WorkStore.key(USER, 'tag_embedding')
        env.jobs.rows[partition, key] = entity(partition, key, libraryId=USER,
                                               scope='tag_embedding', leaseUntil=NOW.timestamp() + 60)
    assert run(env)['deleted'] == 0
    assert not env.service.deleted


def test_manifest_etag_recheck_stops_new_current(env):
    scope, _, row = setup_scope(env)
    orphan = add(env, scope, 1)
    add(env, scope, 2)
    reads = 0

    def change(blob):
        nonlocal reads
        if blob.name == scope.manifest:
            reads += 1
            if reads == 2:
                row['blobName'] = orphan
                env.service.put(scope.container, scope.manifest, json.dumps(row).encode(), modified=NOW)
    env.service.download_hook = change
    result = run(env)
    assert result['status'] == 'deferred' and result['deleted'] == 0
    assert (scope.container, orphan) in env.service.data


def test_candidate_etag_conflict_is_retryable_and_preserves_bytes(env):
    scope, _, _ = setup_scope(env)
    orphan = add(env, scope, 1)
    add(env, scope, 2)

    def change(blob):
        env.service.put(blob.container, blob.name, b'replaced', modified=NOW)
    env.service.delete_hook = change
    result = run(env)
    assert result['status'] == 'deferred' and result['deleted'] == 0
    assert env.service.data[scope.container, orphan][0] == b'replaced'


def test_catalog_serialization_closes_reference_delete_window(env):
    scope, current, row = setup_scope(env)
    orphan = add(env, scope, 1)
    add(env, scope, 2)
    started, committed = threading.Event(), threading.Event()
    threads = []

    def on_delete(blob):
        def publisher():
            started.set()
            with env.service.serialization(USER):
                # Builders always create a NEW UUID; they never resurrect a
                # retired orphan after GC. A stale candidate cannot be current.
                fresh = add(env, scope, 100, modified=NOW)
                row['blobName'] = fresh
                env.service.put(scope.container, scope.manifest, json.dumps(row).encode(), modified=NOW)
                committed.set()
        thread = threading.Thread(target=publisher)
        threads.append(thread)
        thread.start()
        assert started.wait(1)
        assert not committed.is_set()
        assert (scope.container, current) in env.service.data
    env.service.delete_hook = on_delete
    result = run(env)
    for thread in threads:
        thread.join(2)
        assert not thread.is_alive()
    assert result['deleted'] == 1 and committed.is_set()
    assert (scope.container, orphan) not in env.service.data


def test_lease_loss_source_change_and_publication_check(env, monkeypatch):
    scope, _, _ = setup_scope(env)
    add(env, scope, 1)
    add(env, scope, 2)
    monkeypatch.setattr(gc.index_publication, 'check', lambda: (_ for _ in ()).throw(RuntimeError('lost')))
    assert run(env)['deleted'] == 0


def test_noop_serialization_and_wrong_roles_never_authorize_cleanup(env, monkeypatch):
    result = gc.cleanup_user_index_artifacts(USER, storage=su, jobs=env.jobs, now=NOW)
    assert result['status'] == 'deferred' and not env.service.queries
    @contextmanager
    def noop(user):
        yield
    result = gc.cleanup_user_index_artifacts(USER, storage=su, jobs=env.jobs, serialization=noop, now=NOW)
    assert result['deleted'] == 0 and not env.service.queries
    for role in ('core', 'vision', 'cluster', 'upload'):
        monkeypatch.setenv('APP_ROLE', role)
        with pytest.raises(Exception, match='index-worker'):
            run(env)


def test_crossuser_legacy_aliases_and_wave_projections_are_never_candidates(env):
    scope, current, _ = setup_scope(env)
    other, other_current, _ = setup_scope(env, user='other')
    add(env, scope, 1)
    add(env, scope, 2)
    names = [su._vector_index_npz_blob_name(USER), scope.prefix + 'bad.npz',
             scope.prefix + 'a' * 31 + '.npz', scope.prefix + 'A' * 32 + '.npz',
             'wave-projections/' + scope.prefix + 'a' * 32 + '.npz']
    for name in names:
        env.service.put(scope.container, name)
    assert run(env)['deleted'] == 1
    assert all((scope.container, name) in env.service.data for name in names)
    assert (other.container, other_current) in env.service.data
    assert (scope.container, current) in env.service.data


@pytest.mark.parametrize('kind,function', [
    ('vector', su.delete_user_vector_index_data),
    ('people_embedding', su.delete_user_people_embedding_index_data),
    ('tag_embedding', su.delete_user_tag_embedding_index_data),
    ('lexical_artifact', su.delete_user_lexical_index_data)])
def test_existing_purge_helpers_discover_orphans_sidecars_without_crossuser(env, kind, function, monkeypatch):
    scope, current, _ = setup_scope(env, kind)
    other, other_current, _ = setup_scope(env, kind, user='other')
    orphan = add(env, scope, 1)
    sidecar = add(env, scope, 1, suffix='.sqlite') if kind == 'tag_embedding' else None
    # Legacy manifest readers use a different API from strict GC; their fallback
    # must not govern discovery of orphaned immutable blobs during account purge.
    for loader in ('_load_vector_index_manifest', '_load_people_embedding_index_manifest',
                   '_load_tag_embedding_index_manifest'):
        monkeypatch.setattr(su, loader, lambda user: {})
    if kind == 'lexical_artifact':
        import search_db
        monkeypatch.setattr(search_db, 'delete_for_user', lambda user: None)
        monkeypatch.setattr(su, 'delete_user_explore_summary_data', lambda user: None)
        monkeypatch.setattr(su, '_timeline_summary_blob_client', lambda user: None)
        hash_scope, hash_current, _ = setup_scope(env, 'hash')
    function(USER)
    assert (scope.container, orphan) not in env.service.data
    assert (scope.container, current) not in env.service.data
    if sidecar:
        assert (scope.container, sidecar) not in env.service.data
    if kind == 'lexical_artifact':
        assert (hash_scope.container, hash_current) not in env.service.data
    assert (other.container, other_current) in env.service.data


def test_purge_discovery_optin_does_not_add_scans_to_warming(env):
    scope, current, _ = setup_scope(env)
    assert isinstance(su._user_index_blob_locations(USER), list)
    assert not env.service.queries
    locations = su._user_index_blob_locations(USER, include_immutable=True)
    assert not isinstance(locations, list)
    assert (scope.container, current) in list(locations)
    assert all(prefix.startswith(su._vector_index_blob_key(USER)) for _, prefix, _ in env.service.queries)


def test_quiesced_automatic_purge_deletes_controls_dirty_and_model_pending_only_this_library(env):
    for user in (USER, 'other'):
        for kind in automatic.KINDS:
            pk = automatic.partition(user, kind)
            env.dirty.rows[pk, 'id-1'] = entity(pk, 'id-1', sourceId='photo')
        for pk in (index_work.PARTITION, automatic.TAG_PARTITION):
            key = index_work.WorkStore.key(user, 'tag_embedding')
            env.jobs.rows[pk, key] = entity(pk, key, libraryId=user, scope='tag_embedding')
    checks = []
    assert gc.purge_automatic_state(su, env.jobs, USER, quiesced_check=lambda: checks.append(1)) == 12
    assert len(checks) == 13
    assert all(row.get('libraryId', 'other') != USER for row in env.jobs.rows.values())
    assert all(pk == automatic.partition('other', kind) for pk, _ in env.dirty.rows
               for kind in automatic.KINDS if pk == automatic.partition('other', kind))
    assert len(env.dirty.rows) == 10
    assert all('libraryId eq' in query for query, _ in env.jobs.queries)


def test_nonquiesced_and_conflicting_purge_fails_without_broad_delete(env):
    def fail():
        raise RuntimeError('not quiesced')
    with pytest.raises(RuntimeError, match='not quiesced'):
        gc.purge_automatic_state(su, env.jobs, USER, quiesced_check=fail)
    assert not env.dirty.deleted and not env.dirty.queries
    scope, _, _ = setup_scope(env)
    def conflict(blob):
        env.service.put(blob.container, blob.name, b'new')
    env.service.delete_hook = conflict
    with pytest.raises(ResourceModifiedError):
        gc.purge_immutable_artifacts(su, USER, kinds=('vector',))


def test_source_etag_change_during_candidate_survey_aborts_deletion(env, monkeypatch):
    scope, _, _ = setup_scope(env)
    add(env, scope, 1)
    add(env, scope, 2)
    original = gc._idle_snapshot
    calls = 0

    def changed(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            row = env.dirty.rows[automatic.partition(USER, 'vector'), automatic.CONTROL]
            # Even identical content at a new ETag invalidates the initial
            # source survey; no stale reads get turned into a cleanup proof.
            row._metadata = {'etag': 'new-source-etag'}
        return original(*args)
    monkeypatch.setattr(gc, '_idle_snapshot', changed)
    result = run(env)
    assert result['deleted'] == 0 and 'Source coverage changed' in result['reason']


def test_missing_selected_manifest_isolated_after_independent_family_survey(env):
    scope, _, _ = setup_scope(env)
    add(env, scope, 1)
    add(env, scope, 2)
    result = gc.cleanup_user_index_artifacts(USER, storage=su, jobs=env.jobs,
        serialization=env.service.serialization, now=NOW, kinds=('vector', 'tag_embedding'))
    assert result['deleted'] == 1 and result['status'] == 'partial'
    assert result['kinds']['vector']['status'] == 'complete'
    assert result['kinds']['tag_embedding']['status'] == 'deferred'


def test_partial_failure_preserves_deleted_count_and_stops_pass(env):
    scope, _, _ = setup_scope(env)
    for i in range(5):
        add(env, scope, i, modified=OLD + timedelta(minutes=i))
    def fail_second(blob):
        if env.service.deleted:
            raise RuntimeError('storage unavailable')
    env.service.delete_hook = fail_second
    result = run(env)
    assert result['deleted'] == 1 and result['status'] == 'partial'
    assert result['kinds']['vector']['status'] == 'partial'
    assert len(env.service.deleted) == 1


def test_lost_catalog_guard_protects_candidates(env):
    setup_scope(env)
    env.service.guard_lost = True
    result = run(env)
    assert result['deleted'] == 0 and 'catalog lease lost' in result['reason']
    assert not env.service.queries


def test_universal_catalog_primitive_never_noops_nonwave_library(env, monkeypatch):
    import clustering_lease
    calls = []
    class Factory:
        def __init__(self, service, container, **options):
            calls.append((service, container, options))
        @contextmanager
        def __call__(self, user):
            calls.append(user)
            yield SimpleNamespace(check=lambda: calls.append('check'))
    monkeypatch.setattr(clustering_lease, 'BlobLibraryLeaseFactory', Factory)
    # Exercise the real-capability branch; factory injection is this test's
    # intentional lease implementation, not permission for a no-op GC guard.
    original = env.service.get_blob_client
    def capable(container, blob):
        client = original(container, blob)
        client.acquire_lease = lambda **kw: None
        return client
    monkeypatch.setattr(env.service, 'get_blob_client', capable)
    monkeypatch.setitem(su._CTX, 'wave_owned', lambda user: False)
    with gc.artifact_catalog_serialization(USER, storage=su) as guard:
        guard.check()
    assert calls[0][2] == {'namespace': 'catalog-publication-locks'}
    assert calls[1] == USER and calls.count('check') >= 3


def test_quiesced_combined_purge_all_scopes_and_sidecars(env):
    for kind in gc.KINDS:
        scope, _, _ = setup_scope(env, kind)
        add(env, scope, 1)
        if kind == 'tag_embedding':
            add(env, scope, 1, suffix='.sqlite')
    result = gc.purge_quiesced_library_artifacts(su, env.jobs, USER, quiesced_check=lambda: None)
    assert result == {'deletedBlobs': 11, 'deletedAutomaticRows': 5}
    assert len(env.dirty.rows) == 5  # Another library's controls remain intact.
    assert all(name.endswith('.json') for _, name in env.service.data)


def test_quiesced_purge_stops_when_fence_is_lost(env):
    scope, _, _ = setup_scope(env)
    add(env, scope, 1)
    def fence():
        if env.service.deleted:
            raise RuntimeError('quiescence lost')
    with pytest.raises(RuntimeError, match='quiescence lost'):
        gc.purge_quiesced_library_artifacts(su, env.jobs, USER, quiesced_check=fence)
    assert len(env.service.deleted) == 1
    assert not env.dirty.deleted


def test_hash_pointer_retirement_grace_and_missing_snapshot_are_fail_closed(env):
    scope, _, row = setup_scope(env, 'hash')
    add(env, scope, 1)
    add(env, scope, 2)
    row['Timestamp'] = NOW
    assert run(env, 'hash')['deleted'] == 0
    del env.hashes.rows[USER, row['RowKey']]
    assert run(env, 'hash')['status'] == 'deferred'
    assert not env.service.deleted


@pytest.mark.parametrize('kind', gc.KINDS)
def test_uncertified_new_manifest_never_collects_ledger_base(env, kind):
    scope, current, row = setup_scope(env, kind)
    add(env, scope, 1)
    add(env, scope, 2)
    if kind == 'hash':
        row['blobName'] = add(env, scope, 100)
    else:
        row['sourceVersion'] = 'uncertified'
        env.service.put(scope.container, scope.manifest, json.dumps(row).encode())
    result = run(env, kind)
    assert result['deleted'] == 0 and 'certified snapshot lineage' in result['reason']
    assert (scope.container, current) in env.service.data


def test_dirty_manifest_certifies_base_not_shared_dirty_revision(env):
    scope, _, row = setup_scope(env)
    row.update(dirty=True, sourceVersion='pending-source', baseSourceVersion='base')
    env.service.put(scope.container, scope.manifest, json.dumps(row).encode())
    add(env, scope, 1)
    add(env, scope, 2)
    assert run(env)['deleted'] == 1


@pytest.mark.parametrize('kwargs', [{'grace_seconds': 0}, {'grace_seconds': float('nan')},
                                    {'retain_generations': 1}, {'page_size': 0}, {'max_deletions': -1}])
def test_configuration_rejects_unsafe_values(kwargs):
    with pytest.raises(ValueError):
        gc.Policy(**kwargs)


def run_kinds(env, kinds, **kwargs):
    return gc.cleanup_user_index_artifacts(USER, storage=su, jobs=env.jobs,
        serialization=env.service.serialization, kinds=kinds, now=NOW, **kwargs)


@pytest.mark.parametrize('problem', ['missing', 'active', 'revision', 'fullRequired', 'schema'])
def test_unused_tag_source_never_blocks_vector(env, problem, monkeypatch):
    scope, _, _ = setup_scope(env)
    orphan = add(env, scope, 1)
    add(env, scope, 2)
    key = automatic.partition(USER, 'tag_embedding'), automatic.CONTROL
    row = env.dirty.rows[key]
    if problem == 'missing':
        del env.dirty.rows[key]
    elif problem == 'active':
        row['active'] = 1
    elif problem == 'revision':
        row['revision'] = 'uncovered'
    elif problem == 'fullRequired':
        row['fullRequired'] = True
    else:
        row['coverageVersion'] = 999
    get = env.dirty.get_entity
    reads = []

    def selected_only(pk, rk):
        reads.append((pk, rk))
        assert pk == automatic.partition(USER, 'vector')
        return get(pk, rk)

    monkeypatch.setattr(env.dirty, 'get_entity', selected_only)
    result = run(env)
    assert result['status'] == 'complete' and result['deleted'] == 1
    assert set(result['kinds']) == {'vector'}
    assert len(reads) == 2
    assert (scope.container, orphan) not in env.service.data


@pytest.mark.parametrize('problem', ['source', 'manifest', 'read'])
def test_early_vector_failure_preserves_vector_and_lexical_continues(env, problem):
    vector, current, _ = setup_scope(env)
    old_vector = add(env, vector, 1)
    add(env, vector, 2)
    lexical, lexical_current, _ = setup_scope(env, 'lexical_artifact')
    old_lexical = add(env, lexical, 1)
    add(env, lexical, 2)
    if problem == 'source':
        env.dirty.rows[automatic.partition(USER, 'vector'), automatic.CONTROL]['revision'] = 'stale'
    elif problem == 'manifest':
        env.service.put(vector.container, vector.manifest, b'{invalid')
    else:
        def fail(blob):
            if (blob.container, blob.name) == (vector.container, vector.manifest):
                raise OSError('manifest read unavailable')
        env.service.download_hook = fail
    result = run_kinds(env, ('vector', 'lexical_artifact'))
    assert result['status'] == 'partial' and result['deleted'] == 1
    assert result['kinds']['vector']['status'] == 'deferred'
    assert result['kinds']['lexical_artifact']['status'] == 'complete'
    assert (vector.container, old_vector) in env.service.data
    assert (vector.container, current) in env.service.data
    assert (lexical.container, old_lexical) not in env.service.data
    assert (lexical.container, lexical_current) in env.service.data
    assert result['examined'] == sum(row['examined'] for row in result['kinds'].values())


@pytest.mark.parametrize('scope_name', ['full', 'reconcile', 'compact', 'lexical_artifact'])
@pytest.mark.parametrize('partition', [index_work.PARTITION, automatic.TAG_PARTITION])
def test_cross_dependency_builder_claim_blocks_all_selected_outputs(env, scope_name, partition):
    for kind in gc.KINDS:
        scope, _, _ = setup_scope(env, kind)
        add(env, scope, 1)
        add(env, scope, 2)
    key = index_work.WorkStore.key(USER, scope_name)
    env.jobs.rows[partition, key] = entity(partition, key, libraryId=USER,
        scope=scope_name, leaseUntil=NOW.timestamp() + 30)
    result = run_kinds(env, gc.KINDS)
    assert result['status'] == 'deferred' and result['deleted'] == 0
    assert all(row['status'] == 'deferred' and 'Active publication lease' in row['reason']
               for row in result['kinds'].values())
    assert not env.service.queries


@pytest.mark.parametrize('change', ['unrelated-source', 'unrelated-inactive-job', 'relevant-job', 'new-full-claim'])
def test_per_family_cas_ignores_unrelated_changes_but_fences_builders(env, monkeypatch, change):
    scope, _, _ = setup_scope(env)
    orphan = add(env, scope, 1)
    add(env, scope, 2)
    key = index_work.WorkStore.key(USER, 'tag_embedding' if change == 'unrelated-inactive-job' else 'full')
    if change in ('unrelated-inactive-job', 'relevant-job'):
        env.jobs.rows[index_work.PARTITION, key] = entity(index_work.PARTITION, key,
            libraryId=USER, scope='tag_embedding' if change == 'unrelated-inactive-job' else 'full', leaseUntil=0)
    original = gc._idle_snapshot
    calls = 0

    def changed(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            if change == 'unrelated-source':
                row = env.dirty.rows[automatic.partition(USER, 'tag_embedding'), automatic.CONTROL]
                row.update(active=1, revision='uncovered', base='changed-common-source')
                row._metadata = {'etag': 'changed'}
            elif change == 'new-full-claim':
                env.jobs.rows[index_work.PARTITION, key] = entity(index_work.PARTITION, key,
                    libraryId=USER, scope='full', leaseUntil=NOW.timestamp() + 10)
            else:
                env.jobs.rows[index_work.PARTITION, key]._metadata = {'etag': 'changed'}
        return original(*args)

    monkeypatch.setattr(gc, '_idle_snapshot', changed)
    result = run(env)
    protected = change in ('relevant-job', 'new-full-claim')
    assert result['status'] == ('deferred' if protected else 'complete')
    assert result['deleted'] == (0 if protected else 1)
    assert ((scope.container, orphan) in env.service.data) == protected


def test_each_family_finishes_retention_survey_before_its_deletions(env):
    vector, _, _ = setup_scope(env)
    for generation in range(12):
        add(env, vector, generation)
    lexical, _, _ = setup_scope(env, 'lexical_artifact')
    for generation in range(3):
        add(env, lexical, generation)
    result = run_kinds(env, ('vector', 'lexical_artifact'), policy=gc.Policy(page_size=2, max_pages=3))
    assert result['status'] == 'partial' and result['deleted'] == 2
    assert result['kinds']['vector']['status'] == 'deferred'
    assert 'page cap' in result['kinds']['vector']['reason']
    assert all(container == lexical.container and lexical.generation(name)
               for container, name, _, _ in env.service.deleted)


@pytest.mark.parametrize('budget', [3, 100, 1000])
def test_deletion_budget_is_global_and_never_exceeds_100(env, budget):
    for kind in ('vector', 'lexical_artifact'):
        scope, _, _ = setup_scope(env, kind, current=999)
        for generation in range(71):
            add(env, scope, generation, modified=OLD + timedelta(minutes=generation))
    result = run_kinds(env, ('vector', 'lexical_artifact'), policy=gc.Policy(max_deletions=budget))
    assert result['status'] == 'capped'
    assert result['deleted'] == min(100, budget) == len(env.service.deleted)
    assert result['deleted'] == sum(row['deleted'] for row in result['kinds'].values())
    assert result['kinds']['lexical_artifact']['status'] == 'capped'


def test_exactly_exhausted_budget_is_complete_if_no_more_eligible_bytes(env):
    scope, _, _ = setup_scope(env)
    add(env, scope, 1)
    add(env, scope, 2)
    result = run(env, policy=gc.Policy(max_deletions=1))
    assert result['status'] == 'complete' and result['deleted'] == 1


def test_hash_is_independent_and_preserves_pointer_etag_grace_and_other_user(env):
    scope, current, row = setup_scope(env, 'hash')
    orphan = add(env, scope, 1)
    previous = add(env, scope, 2)
    other, other_current, _ = setup_scope(env, 'hash', user='other')
    del env.dirty.rows[automatic.partition(USER, 'tag_embedding'), automatic.CONTROL]
    result = run(env, 'hash')
    assert result['status'] == 'complete' and result['deleted'] == 1
    assert (scope.container, orphan) not in env.service.data
    assert all((scope.container, name) in env.service.data for name in (current, previous))
    assert (other.container, other_current) in env.service.data
    add(env, scope, 0)
    row['Timestamp'] = NOW - timedelta(hours=23)
    assert gc.Policy().grace_seconds == 86400
    assert run(env, 'hash')['status'] == 'deferred'


def test_default_role_is_not_gc_owner(env, monkeypatch):
    monkeypatch.delenv('APP_ROLE')
    with pytest.raises(Exception, match='index-worker'):
        run(env)
    assert not env.service.queries


def test_deleted_family_failure_does_not_stop_later_independent_family(env):
    for kind in ('vector', 'lexical_artifact'):
        scope, _, _ = setup_scope(env, kind)
        for generation in range(4):
            add(env, scope, generation)
    vector = gc.scopes(su, USER)[0]

    def fail(blob):
        if vector.generation(blob.name) and env.service.deleted:
            raise OSError('vector delete unavailable')

    env.service.delete_hook = fail
    result = run_kinds(env, ('vector', 'lexical_artifact'))
    assert result['status'] == 'partial' and result['deleted'] == 4
    assert result['kinds']['vector']['status'] == 'partial'
    assert result['kinds']['lexical_artifact']['status'] == 'complete'


def test_zero_budget_and_missing_serialization_report_every_selected_kind(env):
    result = run_kinds(env, ('vector', 'hash'), policy=gc.Policy(max_deletions=0))
    assert result['status'] == 'capped'
    assert all(row['status'] == 'capped' for row in result['kinds'].values())
    result = gc.cleanup_user_index_artifacts(USER, storage=su, jobs=env.jobs,
        kinds=('vector', 'hash'), now=NOW)
    assert result['status'] == 'deferred' and set(result['kinds']) == {'vector', 'hash'}
    assert not env.service.queries


def test_retention_uses_newest_tag_sidecar_and_never_collects_half_retained_generation(env):
    scope, _, _ = setup_scope(env, 'tag_embedding')
    previous_npz = add(env, scope, 1, modified=OLD)
    previous_state = add(env, scope, 1, suffix='.sqlite', modified=OLD + timedelta(hours=3))
    old_npz = add(env, scope, 2, modified=OLD + timedelta(hours=1))
    old_state = add(env, scope, 2, suffix='.sqlite', modified=OLD + timedelta(hours=2))
    result = run(env, 'tag_embedding')
    assert result['status'] == 'complete' and result['deleted'] == 2
    assert all((scope.container, name) in env.service.data for name in (previous_npz, previous_state))
    assert all((scope.container, name) not in env.service.data for name in (old_npz, old_state))


def test_retention_memory_stays_policy_bounded_across_many_generations(env, monkeypatch):
    scope, _, _ = setup_scope(env, 'tag_embedding', current=999)
    for generation in range(300):
        add(env, scope, generation)
        add(env, scope, generation, suffix='.sqlite')
    push = gc.heapq.heappush
    seen = []

    def bounded(heap, entry):
        assert len(heap) <= 2
        push(heap, entry)
        assert len(heap) <= 3
        seen.append(len(heap))

    monkeypatch.setattr(gc.heapq, 'heappush', bounded)
    result = run(env, 'tag_embedding', policy=gc.Policy(retain_generations=3, page_size=7))
    assert result['status'] == 'capped' and result['deleted'] == 100
    assert seen and max(seen) == 3


def test_shared_guard_loss_stops_later_families_and_reports_actual_deletion(env):
    for kind in ('vector', 'lexical_artifact'):
        scope, _, _ = setup_scope(env, kind)
        add(env, scope, 1)
        add(env, scope, 2)
    vector = gc.scopes(su, USER)[0]

    def lost(blob):
        if vector.generation(blob.name):
            env.service.guard_lost = True

    env.service.delete_hook = lost
    result = run_kinds(env, ('vector', 'lexical_artifact'))
    assert result['status'] == 'partial' and result['deleted'] == 1
    assert result['kinds']['vector']['status'] == 'partial'
    assert result['kinds']['lexical_artifact']['status'] == 'deferred'
    lexical = next(scope for scope in gc.scopes(su, USER) if scope.kind == 'lexical_artifact')
    assert not any(container == lexical.container for container, _, _ in env.service.queries)


@pytest.mark.parametrize('young_suffix', ['.npz', '.sqlite'])
def test_reader_grace_protects_both_tag_sidecars_even_outside_retention_heap(env, young_suffix):
    scope, _, _ = setup_scope(env, 'tag_embedding')
    old_suffix = '.sqlite' if young_suffix == '.npz' else '.npz'
    young = add(env, scope, 1, suffix=young_suffix, modified=NOW - timedelta(hours=1))
    old = add(env, scope, 1, suffix=old_suffix)
    # A still-newer unrelated generation occupies the bounded retention slot.
    add(env, scope, 2, modified=NOW)
    result = run(env, 'tag_embedding')
    assert result['status'] == 'complete' and result['deleted'] == 0
    assert all((scope.container, name) in env.service.data for name in (young, old))