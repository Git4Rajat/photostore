"""Real scoped worker policy with transactional/ETag-bearing table fakes."""
import copy
import re
from types import SimpleNamespace

import pytest
from azure.core import MatchConditions
from azure.core.exceptions import ResourceExistsError, ResourceModifiedError

import app
import automatic_index_updates as automatic
import storage_utils as su
from index_work import WorkStore
from tests.fakes import FakeTable
from tests.test_vector_incremental import ctx as vector_context


class Table(FakeTable):
    def create_entity(self, entity, **kwargs):
        if (entity['PartitionKey'], entity['RowKey']) in self.rows:
            raise ResourceExistsError('exists')
        self.upsert_entity(entity)

    def submit_transaction(self, operations, **kwargs):
        operations = list(operations)
        assert len(operations) <= 100
        assert len({op[1]['PartitionKey'] for op in operations}) <= 1
        before = copy.deepcopy((self.rows, self.versions))
        self.submit_transaction_calls.append(operations)
        try:
            for op in operations:
                method, entity = op[:2]
                options = op[2] if len(op) > 2 else {}
                if method == 'update':
                    self.update_entity(entity, **options)
                elif method == 'create':
                    self.create_entity(entity)
                elif method == 'upsert':
                    self.upsert_entity(entity, **options)
                elif method == 'delete':
                    self.delete_entity(entity['PartitionKey'], entity['RowKey'], **options)
                else:
                    raise AssertionError(method)
        except Exception:
            self.rows, self.versions = before
            raise

    def query_entities(self, query, **kwargs):
        if 'leaseUntil le' in query:
            pk = query.split("'")[1]
            rows = [self.get_entity(*key) for key in self.rows if key[0] == pk]
            for field in ('leaseUntil', 'wakeUntil'):
                ceiling = float(re.search(rf'{field} le ([0-9.]+)', query).group(1))
                rows = [row for row in rows if float(row.get(field) or 0) <= ceiling]
            if 'failed eq false' in query:
                rows = [row for row in rows if not row.get('failed')]
            if 'libraryId eq' in query:
                library = query.split("libraryId eq '")[1].split("'")[0]
                rows = [row for row in rows if row['libraryId'] == library]
            return rows
        if "RowKey ne '!coverage'" in query:
            return [r for r in super().query_entities(query.split(' and RowKey ne ')[0])
                    if r['RowKey'] != automatic.CONTROL]
        if query.startswith("RowKey eq '!coverage'"):
            return [self.get_entity(*key) for key, row in self.rows.items()
                    if key[1] == automatic.CONTROL and
                    ('libraryId eq' not in query or row['libraryId'] == query.split("libraryId eq '")[1].split("'")[0])]
        return super().query_entities(query, **kwargs)


@pytest.fixture
def runtime(monkeypatch):
    dirty, jobs, source = Table(), Table(), Table()
    monkeypatch.setattr(su, '_CTX', {'search_index_dirty_table_client': dirty,
                                   'metadata_table_client': source, 'wave_owned': lambda _: False})
    monkeypatch.setattr(su, 'DURABLE_INDEX_UPDATES', True)
    monkeypatch.setattr(app, 'INDEX_MAINTENANCE_QUEUE_NAME', 'index')
    monkeypatch.setattr(app, 'jobs_table_client', jobs)
    monkeypatch.setattr(app, 'search_index_dirty_table_client', dirty)
    monkeypatch.setattr(app, '_library_cut_over_to_waves', lambda _: False)
    monkeypatch.setattr(app, '_upsert_job_status', lambda *a, **kw: None)
    monkeypatch.setattr(app, '_send_index_wakeup', lambda *a, **kw: None)
    monkeypatch.setattr(app, '_send_tag_index_wakeup', lambda *a, **kw: None)
    monkeypatch.setattr(app, 'ipwork_queue_client', object())
    monkeypatch.setattr(app, 'index_maintenance_queue_client', object())
    monkeypatch.setattr(app, 'index_maintenance_deferred_queue_client', object())
    monkeypatch.setattr(su, 'METADATA_INDEX_REQUEST_HOOK', lambda user, scope:
        app.enqueue_index_build(user, reason='metadata-change', scope=scope))
    store = automatic.CoverageStore(dirty)
    bases, calls = {}, []
    monkeypatch.setattr(automatic, 'artifact_base', lambda user, kind: bases.get(kind, ''))

    def builder(kind):
        def build(user, **options):
            calls.append((kind, options))
            bases[kind] = 'published-' + str(len(calls))
            return SimpleNamespace(source_version=bases[kind])
        return build
    for kind in automatic.KINDS[:-1]:
        name = 'lexical' if kind == 'lexical_artifact' else kind
        monkeypatch.setattr(su, f'refresh_user_{name}_index', builder(kind))
    return SimpleNamespace(dirty=dirty, jobs=jobs, source=source, store=store,
                           bases=bases, calls=calls)


def mark(runtime, kind, ids, **kwargs):
    runtime.store.begin('A', kind, ids, **kwargs)
    runtime.store.finish('A', kind)


def worker(kind):
    app._run_index_build_job('A', 'job', automatic.SCOPES.get(kind, kind))


@pytest.mark.parametrize('kind', ['vector', 'people_embedding', 'lexical_artifact'])
def test_actual_worker_seeds_then_invokes_strict_incremental(runtime, kind):
    mark(runtime, kind, ['one'])
    worker(kind)
    assert not runtime.calls[-1][1].get('incremental_only')
    mark(runtime, kind, ['two'])
    worker(kind)
    options = runtime.calls[-1][1]
    assert options['incremental_only'] is True
    assert options['previous_source_version'] == 'published-1'
    assert options.get('affected_person_ids', options.get('affected_filenames')) == ['two']
    assert not runtime.store.capture('A', kind).ids
    if kind == 'people_embedding':
        assert options['model_version'] == app.FACE_CLUSTER_EMBEDDING_VERSION


def test_more_than_capture_limit_falls_back_full_and_drains_complete_dirty_snapshot(runtime):
    mark(runtime, 'vector', ['seed'])
    worker('vector')
    mark(runtime, 'vector', [str(i) for i in range(automatic.LIMIT + 100)])
    assert runtime.store.capture('A', 'vector').dirty.truncated
    worker('vector')
    assert runtime.calls[-1][1] == {'force_full': True}
    assert not runtime.store.capture('A', 'vector').ids


def test_batch_above_512_stays_incremental_for_bulk_capable_kinds(runtime):
    mark(runtime, 'vector', ['seed'])
    worker('vector')
    mark(runtime, 'vector', [str(i) for i in range(600)])
    worker('vector')
    options = runtime.calls[-1][1]
    assert options['incremental_only'] is True
    assert len(options['affected_filenames']) == 600
    assert not runtime.store.capture('A', 'vector').ids


def test_batch_above_kind_cap_takes_full_path(runtime, monkeypatch):
    monkeypatch.setitem(automatic.KIND_ID_CAPS, 'vector', 512)
    mark(runtime, 'vector', ['seed'])
    worker('vector')
    mark(runtime, 'vector', [str(i) for i in range(600)])
    worker('vector')
    assert runtime.calls[-1][1] == {'force_full': True}
    assert not runtime.store.capture('A', 'vector').ids


def test_pointer_from_uncertified_external_publisher_requires_full(runtime):
    mark(runtime, 'vector', ['seed'])
    worker('vector')
    runtime.bases['vector'] = 'external-full'
    mark(runtime, 'vector', ['one'])
    worker('vector')
    assert runtime.calls[-1][1]['force_full']


def test_failed_incremental_retains_ids_and_full_retry(runtime, monkeypatch):
    mark(runtime, 'vector', ['seed'])
    worker('vector')
    mark(runtime, 'vector', ['one'])
    original = su.refresh_user_vector_index
    monkeypatch.setattr(su, 'refresh_user_vector_index', lambda *a, **kw: None)
    with pytest.raises(RuntimeError, match='full retry'):
        worker('vector')
    assert runtime.store.capture('A', 'vector').ids == ['one']
    monkeypatch.setattr(su, 'refresh_user_vector_index', original)
    worker('vector')
    assert runtime.calls[-1][1]['force_full']


def test_concurrent_source_remark_after_publication_cannot_certify_or_ack(runtime, monkeypatch):
    mark(runtime, 'vector', ['one'])
    original = su.refresh_user_vector_index
    def racing(*args, **kwargs):
        receipt = original(*args, **kwargs)
        mark(runtime, 'vector', ['one', 'new'])
        return receipt
    monkeypatch.setattr(su, 'refresh_user_vector_index', racing)
    with pytest.raises(automatic.SourceBusy):
        worker('vector')
    assert set(runtime.store.capture('A', 'vector').ids) == {'one', 'new'}
    assert not runtime.store.control('A', 'vector')['base']


def test_remark_between_certification_and_ack_retains_new_etag(runtime, monkeypatch):
    mark(runtime, 'vector', ['one'])
    original = automatic.acknowledge_dirty
    def racing(table, snapshot):
        mark(runtime, 'vector', ['one'])
        original(table, snapshot)
    monkeypatch.setattr(automatic, 'acknowledge_dirty', racing)
    worker('vector')
    assert runtime.store.capture('A', 'vector').ids == ['one']


def test_active_and_abandoned_prewrite_intents_never_expire(runtime):
    runtime.store.begin('A', 'vector', ['one'])
    with pytest.raises(automatic.SourceBusy):
        worker('vector')
    assert runtime.store.control('A', 'vector')['active'] == 1
    assert not runtime.calls


def test_source_proxy_records_before_write_and_schedules_after(runtime, monkeypatch):
    monkeypatch.setattr(automatic, 'VECTOR_INDEX_AUTOMATIC', True)
    table = su.track_index_source_table(runtime.source, 'metadata')
    observed = []
    original = runtime.source.upsert_entity
    def write(entity, **kwargs):
        observed.append(runtime.store.control('A', 'vector')['active'])
        assert runtime.store.control('A', 'vector')['active'] == 1
        original(entity, **kwargs)
    monkeypatch.setattr(runtime.source, 'upsert_entity', write)
    table.upsert_entity({'PartitionKey': 'A', 'RowKey': 'one', 'caption': 'cat'})
    assert observed == [1]
    assert runtime.store.capture('A', 'vector').ids == ['one']
    assert runtime.store.capture('A', 'lexical_artifact').ids == ['one']
    assert WorkStore(runtime.jobs)._get(WorkStore.key('A', 'vector'))


def test_failed_source_write_conservatively_requires_full(runtime, monkeypatch):
    table = su.track_index_source_table(runtime.source, 'embeddings')
    monkeypatch.setattr(runtime.source, 'upsert_entity', lambda *a, **kw:
                        (_ for _ in ()).throw(OSError('write failed')))
    with pytest.raises(OSError):
        table.upsert_entity({'PartitionKey': 'A', 'RowKey': 'one'})
    row = runtime.store.control('A', 'vector')
    assert row['fullRequired'] and not row['active']
    assert runtime.store.capture('A', 'vector').ids == ['one']


def test_irrelevant_metadata_edit_does_not_bootstrap_vector(runtime):
    table = su.track_index_source_table(runtime.source, 'metadata')
    table.upsert_entity({'PartitionKey': 'A', 'RowKey': 'one', 'rating': 4})
    assert runtime.store.control('A', 'vector') is None
    assert runtime.store.capture('A', 'lexical_artifact').ids == ['one']


def test_faces_capture_old_new_owners_and_transaction_delete(runtime):
    runtime.source.upsert_entity({'PartitionKey': 'A', 'RowKey': 'face', 'personId': 'old'})
    previous = runtime.source.get_entity('A', 'face')
    table = su.track_index_source_table(runtime.source, 'face')
    table.submit_transaction([('update', {'PartitionKey': 'A', 'RowKey': 'face', 'personId': 'new'},
                               {'etag': previous.metadata['etag'], 'match_condition': MatchConditions.IfNotModified})])
    assert set(runtime.store.capture('A', 'people_embedding').ids) == {'old', 'new'}
    latest = runtime.source.get_entity('A', 'face')
    table.submit_transaction([('delete', latest, {'etag': latest.metadata['etag'],
                                                 'match_condition': MatchConditions.IfNotModified})])
    assert any(row['tombstone'] for row in runtime.store.capture('A', 'people_embedding').dirty.rows.values())


def test_unconditional_face_upsert_with_inherited_etag_is_not_trustworthy(runtime):
    mark(runtime, 'people_embedding', ['old'])
    worker('people_embedding')
    runtime.source.upsert_entity({'PartitionKey': 'A', 'RowKey': 'face', 'personId': 'old'})
    row = runtime.source.get_entity('A', 'face')
    row['personId'] = 'new'
    su.track_index_source_table(runtime.source, 'face').upsert_entity(row)
    assert runtime.store.control('A', 'people_embedding')['fullRequired']


def test_hash_deletion_persists_tombstone_before_source_transaction(runtime):
    import upload_hash_snapshot
    table = su.track_index_source_table(runtime.source, 'hash')
    file_hash = 'a' * 64
    table.upsert_entity({'PartitionKey': 'A', 'RowKey': file_hash, 'filename': 'one'})
    upload_hash_snapshot.delete_hash(table, 'A', file_hash)
    capture = runtime.store.capture('A', 'hash')
    assert capture.ids == [file_hash]
    assert next(iter(capture.dirty.rows.values()))['tombstone']


def test_tag_requests_are_durable_but_never_sent_to_index_queue(runtime, monkeypatch):
    monkeypatch.setattr(app, '_send_index_wakeup', lambda *a, **kw: pytest.fail('tag sent to indexer'))
    assert app.enqueue_index_build('A', reason='metadata-change', scope='tag_embedding') == 'queued'
    store = WorkStore(runtime.jobs, partition=automatic.TAG_PARTITION)
    assert store._get(store.key('A', 'tag_embedding'))
    assert not WorkStore(runtime.jobs)._get(store.key('A', 'tag_embedding'))


def test_vision_sweep_uses_tag_bootstrap_with_real_model_version(runtime, monkeypatch):
    monkeypatch.setenv('APP_ROLE', 'vision')
    monkeypatch.setattr(app.vision_utils, 'image_encoder_available', lambda: True)
    monkeypatch.setattr(app.vision_utils, 'get_text_embedding_version', lambda: 'clip-real')
    mark(runtime, 'tag_embedding', ['one'])
    app.enqueue_index_build('A', reason='metadata-change', scope='tag_embedding', coalesce=False)
    assert app._sweep_tag_embedding_indexes()['indexesAvailable'] == 1
    assert runtime.calls[-1] == ('tag_embedding', {'model_version': 'clip-real', 'bootstrap_incremental': True})


def test_failed_postwrite_schedule_recovered_from_coverage(runtime, monkeypatch):
    monkeypatch.setattr(automatic, 'VECTOR_INDEX_AUTOMATIC', True)
    mark(runtime, 'vector', ['one'])
    assert not WorkStore(runtime.jobs)._get(WorkStore.key('A', 'vector'))
    sent = []
    monkeypatch.setattr(app, '_send_index_wakeup', lambda *a, **kw: sent.append(a))
    assert app.recover_pending_index_work('A') == 1
    assert sent[0][0:2] == ('A', 'vector')


def test_recovery_does_not_revive_a_poisoned_revision(runtime):
    mark(runtime, 'vector', ['one'])
    store = WorkStore(runtime.jobs)
    key = store.request('A', 'vector')
    claim = store.claim(key)
    store.finish(claim, failed=True)
    assert app.recover_pending_index_work('A') == 0
    assert store._get(key)['failed']


def test_source_barrier_wait_does_not_charge_worker_failure_budget(runtime):
    runtime.store.begin('A', 'vector', ['one'])
    store = WorkStore(runtime.jobs)
    key = store.request('A', 'vector')
    app._run_index_maintenance_payload({'userId': 'A', 'scope': 'vector', 'jobId': 'job', 'maintenanceKey': key})
    row = store._get(key)
    assert row['attempts'] == 0 and row['retryReason'] == 'barrier_wait'


def test_wave_mutations_only_invalidate_coverage_without_legacy_ids_or_wakeups(runtime, monkeypatch):
    import wave_policy
    monkeypatch.setattr(wave_policy, 'source_change_active', lambda: True)
    su._CTX['wave_owned'] = lambda _: True
    table = su.track_index_source_table(runtime.source, 'embeddings')
    table.upsert_entity({'PartitionKey': 'A', 'RowKey': 'one'})
    assert runtime.store.control('A', 'vector')['fullRequired']
    assert not runtime.store.capture('A', 'vector').ids
    assert not runtime.jobs.rows


def test_actual_worker_publishes_real_vector_delta_after_source_write(vector_context, monkeypatch):
    import io
    import json
    import numpy as np
    metadata, embeddings, blobs = vector_context
    dirty = Table()
    monkeypatch.setitem(su._CTX, 'search_index_dirty_table_client', dirty)
    monkeypatch.setitem(su._CTX, 'wave_owned', lambda _: False)
    monkeypatch.setattr(su, 'DURABLE_INDEX_UPDATES', True)
    monkeypatch.setattr(su, 'METADATA_INDEX_REQUEST_HOOK', lambda *a: 'queued')
    monkeypatch.setattr(app, 'INDEX_MAINTENANCE_QUEUE_NAME', 'index')
    monkeypatch.setattr(app, '_upsert_job_status', lambda *a, **kw: None)
    metadata.upsert_entity({'PartitionKey': 'u1', 'RowKey': 'one.jpg', 'caption': 'cat'})
    tracked = su.track_index_source_table(embeddings, 'embeddings')
    tracked.upsert_entity({'PartitionKey': 'u1', 'RowKey': 'one.jpg',
        'photoEmbedding': '[1,0,0]', 'photoEmbeddingVersion': su.PHOTO_EMBEDDING_MODEL_VERSION})
    monkeypatch.setitem(su._CTX, 'embeddings_table_client', tracked)
    app._run_index_build_job('u1', 'job', 'vector')
    base = automatic.configured_store().control('u1', 'vector')['base']
    tracked.upsert_entity({'PartitionKey': 'u1', 'RowKey': 'one.jpg',
        'photoEmbedding': '[0,1,0]', 'photoEmbeddingVersion': su.PHOTO_EMBEDDING_MODEL_VERSION})
    monkeypatch.setattr(su, '_build_user_vector_index_file', lambda *a, **kw: pytest.fail('unexpected full scan'))
    monkeypatch.setattr(su, '_load_vector_index_npz', lambda *a, **kw: pytest.fail('whole-index hydration'))
    app._run_index_build_job('u1', 'job', 'vector')
    manifest = su._load_vector_index_manifest('u1')
    assert manifest['sourceVersion'] != base
    with np.load(io.BytesIO(blobs.data['vector-index/' + manifest['blobName']]), allow_pickle=False) as data:
        assert json.loads(str(data['row_keys'][0])) == ['one.jpg']
        np.testing.assert_array_equal(data['embeddings'], [[0, 1, 0]])
    assert not automatic.configured_store().capture('u1', 'vector').ids


def test_actual_hash_worker_applies_tombstone_and_expected_epoch(monkeypatch):
    import gzip
    import json
    import upload_hash_snapshot as hashes
    from tests.test_vector_incremental import Service
    dirty, raw, blobs = Table(), Table(), Service()
    monkeypatch.setattr(su, '_CTX', {'search_index_dirty_table_client': dirty,
        'hash_index_table_client': raw, 'blob_service_client': blobs,
        'blob_lexical_index_container': 'lexical', 'wave_owned': lambda _: False})
    monkeypatch.setattr(su, 'DURABLE_INDEX_UPDATES', True)
    monkeypatch.setattr(su, 'METADATA_INDEX_REQUEST_HOOK', lambda *a: 'queued')
    monkeypatch.setattr(app, 'INDEX_MAINTENANCE_QUEUE_NAME', 'index')
    monkeypatch.setattr(app, '_upsert_job_status', lambda *a, **kw: None)
    table = su.track_index_source_table(raw, 'hash')
    su._CTX['hash_index_table_client'] = table
    one, two = 'a' * 64, 'b' * 64
    table.upsert_entity({'PartitionKey': 'u1', 'RowKey': one, 'filename': 'one'})
    app._run_index_build_job('u1', 'job', 'dedup')
    hashes.delete_hash(table, 'u1', one)
    table.upsert_entity({'PartitionKey': 'u1', 'RowKey': two, 'filename': 'two'})
    monkeypatch.setattr(hashes, 'rebuild_snapshot', lambda *a, **kw: pytest.fail('unexpected full hash scan'))
    app._run_index_build_job('u1', 'job', 'dedup')
    manifest = hashes._row(table, 'u1', hashes.SNAPSHOT_ROW)
    assert manifest['deleteEpoch'] == hashes._epoch(table, 'u1')
    payload = json.loads(gzip.decompress(blobs.data['lexical/' + manifest['blobName']]))
    assert payload['hashes'] == {two: 'two'}
    assert not automatic.configured_store().capture('u1', 'hash').ids


@pytest.mark.parametrize('source_fails', [False, True])
@pytest.mark.parametrize('failure_phase', ['schedule', 'finish', 'both'])
def test_multikind_finalization_attempts_all_passes_and_preserves_source_error(
        runtime, monkeypatch, source_fails, failure_phase):
    kinds = ['lexical_artifact', 'tag_embedding', 'vector']
    events, schedule_counts, failures = [], {}, []
    source_error = OSError('original source failure')
    original_write = runtime.source.upsert_entity
    original_transaction = runtime.dirty.submit_transaction

    def write(entity, **kwargs):
        events.append(('source', None))
        if source_fails:
            raise source_error
        return original_write(entity, **kwargs)

    def transaction(operations, **kwargs):
        operations = list(operations)
        row = operations[0][1]
        if row.get('RowKey') == automatic.CONTROL and row.get('active') == 0:
            events.append(('finish', row['kind']))
            if row['kind'] == kinds[0] and failure_phase in {'finish', 'both'}:
                error = OSError('finish storage failure')
                failures.append(error)
                raise error
        return original_transaction(operations, **kwargs)

    def schedule(user, kind):
        schedule_counts[kind] = schedule_counts.get(kind, 0) + 1
        phase = 'pre' if schedule_counts[kind] == 1 else 'post'
        events.append((phase, kind))
        if phase == 'post' and kind == kinds[0] and failure_phase in {'schedule', 'both'}:
            error = RuntimeError('post-write scheduling failure')
            failures.append(error)
            raise error

    monkeypatch.setattr(runtime.source, 'upsert_entity', write)
    monkeypatch.setattr(runtime.dirty, 'submit_transaction', transaction)
    monkeypatch.setattr(automatic, 'schedule', schedule)
    table = su.track_index_source_table(runtime.source, 'metadata')
    with pytest.raises(OSError if source_fails else ExceptionGroup) as caught:
        table.upsert_entity({'PartitionKey': 'A', 'RowKey': 'odd name', 'tags': '["cat"]'})
    if source_fails:
        assert caught.value is source_error
        finalization = caught.value.__cause__
    else:
        finalization = caught.value
        assert ('A', 'odd name') in runtime.source.rows
    assert isinstance(finalization, ExceptionGroup)
    assert list(finalization.exceptions) == failures
    assert all(error.__notes__ for error in failures)
    assert 'retry required' in str(finalization)
    assert events == ([('pre', kind) for kind in kinds] + [('source', None)] +
                      [('finish', kind) for kind in kinds] + [('post', kind) for kind in kinds])
    for kind in kinds:
        row = runtime.store.control('A', kind)
        blocked = kind == kinds[0] and failure_phase in {'finish', 'both'}
        assert row['active'] == int(blocked)
        assert row['fullRequired']
        if not blocked:
            capture = runtime.store.capture('A', kind)
            assert capture.ids == ['odd name']
        else:
            with pytest.raises(automatic.SourceBusy):
                runtime.store.capture('A', kind)


def test_partial_failed_begin_remains_failclosed_but_prior_begin_finishes(runtime, monkeypatch):
    events = []
    original_begin = runtime.store.begin
    original_finish = runtime.store.finish
    begin_error = OSError('begin committed then response failed')
    schedule_error = RuntimeError('post schedule failed')
    counts = {}

    def begin(user, kind, ids, **kwargs):
        original_begin(user, kind, ids, **kwargs)
        if kind == 'tag_embedding':
            raise begin_error

    def finish(user, kind, **kwargs):
        events.append(kind)
        original_finish(user, kind, **kwargs)

    def schedule(user, kind):
        counts[kind] = counts.get(kind, 0) + 1
        if counts[kind] == 2:
            raise schedule_error

    monkeypatch.setattr(automatic, 'configured_store', lambda: runtime.store)
    monkeypatch.setattr(runtime.store, 'begin', begin)
    monkeypatch.setattr(runtime.store, 'finish', finish)
    monkeypatch.setattr(automatic, 'schedule', schedule)
    table = su.track_index_source_table(runtime.source, 'metadata')
    with pytest.raises(OSError) as caught:
        table.upsert_entity({'PartitionKey': 'A', 'RowKey': 'one', 'tags': '[]'})
    assert caught.value is begin_error
    assert caught.value.__cause__.exceptions == (schedule_error,)
    assert events == ['lexical_artifact']
    assert runtime.store.control('A', 'lexical_artifact')['active'] == 0
    assert runtime.store.control('A', 'tag_embedding')['active'] == 1
    assert runtime.store.control('A', 'vector') is None
    assert not runtime.source.rows


@pytest.mark.parametrize('user,key', [
    ('clustering_maintenance', 'lib-A'),
    ('__system__', 'media_delegation_key_20261101'),
    ('__system__', 'ipwork_sweep_lock'),
    ('jobs', 'legacy_job_status'),
    ('performance', 'legacy_upload_metric'),
])
@pytest.mark.parametrize('operation', ['create', 'upsert', 'update', 'delete', 'transaction'])
def test_internal_metadata_bypasses_reads_admission_and_coverage(runtime, monkeypatch, user, key, operation):
    entity = {'PartitionKey': user, 'RowKey': key, 'lease_owner': 'worker'}
    if operation in {'update', 'delete'}:
        runtime.source.upsert_entity(entity)
    monkeypatch.setattr(runtime.source, 'get_entity', lambda *a, **kw: pytest.fail('internal old read'))
    monkeypatch.setattr(automatic, 'check_source_admission', lambda *a: pytest.fail('bogus library admission'))
    monkeypatch.setattr(automatic, 'configured_store', lambda: pytest.fail('internal coverage'))
    monkeypatch.setattr(automatic, 'schedule', lambda *a: pytest.fail('internal wakeup'))
    assert automatic.mutation_targets('metadata', runtime.source, 'delete', entity, {}) == (user, {}, False, True)
    table = su.track_index_source_table(runtime.source, 'metadata')
    if operation == 'transaction':
        table.submit_transaction(iter([('upsert', entity), ('delete', entity)]))
    elif operation == 'delete':
        table.delete_entity(partition_key=user, row_key=key)
    else:
        getattr(table, operation + '_entity')(entity)
    assert not runtime.dirty.rows and not runtime.jobs.rows
    assert ((user, key) in runtime.source.rows) == (operation not in {'delete', 'transaction'})


def test_hash_controls_bypass_old_reads_and_admission(runtime, monkeypatch):
    from upload_hash_snapshot import CONTROL_ROWS
    monkeypatch.setattr(runtime.source, 'get_entity', lambda *a: pytest.fail('hash control old read'))
    monkeypatch.setattr(automatic, 'check_source_admission', lambda *a: pytest.fail('hash control admission'))
    table = su.track_index_source_table(runtime.source, 'hash')
    for key in CONTROL_ROWS:
        entity = {'PartitionKey': 'A', 'RowKey': key}
        assert automatic.mutation_targets('hash', runtime.source, 'upsert', entity, {}) == ('A', {}, False, False)
        table.upsert_entity(entity)
        table.delete_entity('A', key)
    assert not runtime.dirty.rows and not runtime.jobs.rows


@pytest.mark.parametrize('key', ['extensionless', 'ODD.MediaSuffix', '__init__',
                                'ipwork_sweep_lock', 'media_delegation_key_20261101',
                                'clustering_maintenance', 'jobs', 'performance', '!coverage'])
@pytest.mark.parametrize('fields', [
    {'processing_state': 'deleted'},
    {'pendingAnonymousBlob': 'reservation', 'upload_started_at': '2026-11-01'},
    {'thumbnail_status': 'running'},
    {'tags': '["cat"]'},
])
def test_true_media_rows_without_finalization_fields_remain_tracked(runtime, key, fields):
    table = su.track_index_source_table(runtime.source, 'metadata')
    table.upsert_entity({'PartitionKey': 'A', 'RowKey': key, **fields})
    assert runtime.store.capture('A', 'lexical_artifact').ids == [key]
    # Identity-only hard deletes must cover all kinds, even if the old row is
    # absent (redelivery) or contains no photo/finalization fields.
    table.delete_entity('A', key)
    table.delete_entity(partition_key='A', row_key=key)
    for kind in ('lexical_artifact', 'tag_embedding', 'vector'):
        capture = runtime.store.capture('A', kind)
        assert capture.ids == [key]
        assert all(row['tombstone'] for row in capture.dirty.rows.values())


def test_media_admission_fence_still_applies_to_internal_looking_filename(runtime, monkeypatch):
    monkeypatch.setattr(automatic, 'check_source_admission', lambda user:
                        (_ for _ in ()).throw(automatic.SourceBusy('fenced ' + user)))
    table = su.track_index_source_table(runtime.source, 'metadata')
    with pytest.raises(automatic.SourceBusy, match='fenced A'):
        table.upsert_entity({'PartitionKey': 'A', 'RowKey': 'ipwork_sweep_lock', 'caption': 'cat'})
    assert not runtime.source.rows and not runtime.dirty.rows


def test_source_reservation_attribution_lifecycle(runtime):
    res_id = runtime.store.begin('A', 'vector', ['photo1', 'photo2'], owner='worker-pod-1', operation='metadata.upsert')
    assert res_id is not None
    reservations = runtime.store.reservations('A', 'vector')
    assert len(reservations) == 1
    assert reservations[0]['reservationId'] == res_id
    assert reservations[0]['owner'] == 'worker-pod-1'
    assert reservations[0]['operation'] == 'metadata.upsert'
    assert reservations[0]['idsCount'] == 2
    assert reservations[0]['startedAt'] > 0

    with pytest.raises(automatic.SourceBusy):
        runtime.store.capture('A', 'vector')

    runtime.store.finish('A', 'vector', reservation_id=res_id)
    assert runtime.store.reservations('A', 'vector') == []

    captured = runtime.store.capture('A', 'vector')
    assert set(captured.ids) == {'photo1', 'photo2'}


def test_source_write_checks_fence_and_ownership_once_per_library(runtime, monkeypatch):
    table = su.track_index_source_table(runtime.source, 'metadata')
    fences, owners = [], []
    monkeypatch.setattr(automatic, 'check_source_admission', lambda user: fences.append(user))
    monkeypatch.setitem(su._CTX, 'wave_owned', lambda user: owners.append(user) or False)
    # Multi-kind metadata write: vector + tag_embedding + lexical_artifact.
    from wave_policy import begin_source_change, end_source_change
    token = begin_source_change()
    try:
        table.upsert_entity({'PartitionKey': 'A', 'RowKey': 'one', 'caption': 'cat', 'photoEmbedding': 'x'})
    finally:
        end_source_change(token)
    # One fence check before reservations and one immediately before the write,
    # independent of how many kinds were reserved.
    assert fences == ['A', 'A']
    assert owners == ['A']


def _wave_scope(monkeypatch, owned=True):
    from wave_policy import begin_source_change, end_source_change
    monkeypatch.setitem(su._CTX, 'wave_owned', lambda user: owned)
    token = begin_source_change()
    return lambda: end_source_change(token)


def test_coalesced_wave_writes_share_one_reservation_per_kind(runtime, monkeypatch):
    metadata = su.track_index_source_table(runtime.source, 'metadata')
    embeddings = su.track_index_source_table(runtime.source, 'embeddings')
    begins, finishes = [], []
    begin, finish = automatic.CoverageStore.begin, automatic.CoverageStore.finish
    monkeypatch.setattr(automatic.CoverageStore, 'begin',
                        lambda self, *a, **kw: begins.append(a[:2]) or begin(self, *a, **kw))
    monkeypatch.setattr(automatic.CoverageStore, 'finish',
                        lambda self, *a, **kw: finishes.append(a[:2]) or finish(self, *a, **kw))
    end = _wave_scope(monkeypatch)
    try:
        with automatic.coalesced_source_reservations():
            for index in range(5):
                metadata.upsert_entity({'PartitionKey': 'A', 'RowKey': f'p{index}', 'caption': 'cat'})
                embeddings.upsert_entity({'PartitionKey': 'A', 'RowKey': f'p{index}'})
            assert runtime.store.control('A', 'vector')['active'] == 1
            assert len(begins) == len(set(begins)) >= 2  # one begin per kind, not per write
            assert finishes == []
    finally:
        end()
    assert sorted(begins) == sorted(set(begins)) and len(begins) == len(finishes) >= 2
    for _, kind in begins:
        row = runtime.store.control('A', kind)
        assert row['active'] == 0 and row['fullRequired']
    assert runtime.store.reservations('A', 'vector') == []
    assert len(runtime.source.rows) == 5


def test_threaded_page_writes_share_one_reservation_per_kind(runtime, monkeypatch):
    import wave_no_work
    metadata = su.track_index_source_table(runtime.source, 'metadata')
    begins = []
    begin = automatic.CoverageStore.begin
    monkeypatch.setattr(automatic.CoverageStore, 'begin',
                        lambda self, *a, **kw: begins.append(a[:2]) or begin(self, *a, **kw))
    end = _wave_scope(monkeypatch)
    try:
        with automatic.coalesced_source_reservations():
            wave_no_work.map_members(
                range(12), lambda i: metadata.upsert_entity({'PartitionKey': 'A', 'RowKey': f'p{i}', 'caption': 'cat'}), 4)
            assert len(begins) == len(set(begins)) >= 1
    finally:
        end()
    assert len(runtime.source.rows) == 12


def test_coalesced_failure_marks_full_required_and_still_finishes(runtime, monkeypatch):
    table = su.track_index_source_table(runtime.source, 'embeddings')
    end = _wave_scope(monkeypatch)
    try:
        with automatic.coalesced_source_reservations():
            table.upsert_entity({'PartitionKey': 'A', 'RowKey': 'one'})
            monkeypatch.setattr(runtime.source, 'upsert_entity',
                                lambda *a, **kw: (_ for _ in ()).throw(OSError('write failed')))
            with pytest.raises(OSError):
                table.upsert_entity({'PartitionKey': 'A', 'RowKey': 'two'})
    finally:
        end()
    row = runtime.store.control('A', 'vector')
    assert row['active'] == 0 and row['fullRequired']


def test_coalesced_body_exception_finishes_reservations(runtime, monkeypatch):
    table = su.track_index_source_table(runtime.source, 'embeddings')
    end = _wave_scope(monkeypatch)
    try:
        with pytest.raises(KeyError):
            with automatic.coalesced_source_reservations():
                table.upsert_entity({'PartitionKey': 'A', 'RowKey': 'one'})
                raise KeyError('boom')
    finally:
        end()
    assert runtime.store.control('A', 'vector')['active'] == 0


def test_abandoned_coalesced_reservation_stays_active_and_blocks_builders(runtime, monkeypatch):
    table = su.track_index_source_table(runtime.source, 'embeddings')
    end = _wave_scope(monkeypatch)
    scope = automatic.coalesced_source_reservations()
    scope.__enter__()
    try:
        table.upsert_entity({'PartitionKey': 'A', 'RowKey': 'one'})
        # A killed process never reaches __exit__: the reservation must still be
        # durable, active and fail closed for builders.
        assert runtime.store.control('A', 'vector')['active'] == 1
        with pytest.raises(automatic.SourceBusy):
            runtime.store.capture('A', 'vector')
    finally:
        scope.__exit__(None, None, None)
        end()


def test_non_wave_writes_in_scope_keep_per_write_reservations(runtime, monkeypatch):
    table = su.track_index_source_table(runtime.source, 'embeddings')
    end = _wave_scope(monkeypatch, owned=False)
    try:
        with automatic.coalesced_source_reservations():
            table.upsert_entity({'PartitionKey': 'A', 'RowKey': 'one'})
            assert runtime.store.control('A', 'vector')['active'] == 0
    finally:
        end()
    assert runtime.store.capture('A', 'vector').ids == ['one']


def test_vector_is_not_scheduled_automatically_by_default(runtime, monkeypatch):
    monkeypatch.setattr(automatic, 'VECTOR_INDEX_AUTOMATIC', False)
    automatic.schedule('A', 'vector')  # must not call the scheduling hook or raise
    assert not runtime.calls


def test_wave_scopes_exclude_vector_but_still_accept_it_for_in_flight_waves():
    import wave_catalog
    assert 'vector' not in wave_catalog.SCOPES
    assert 'vector' in wave_catalog.ACCEPTED_SCOPES


def test_stale_reservation_is_reaped_and_forces_full_build(runtime, monkeypatch):
    stale = runtime.store.begin('A', 'lexical_artifact', ['photo1'], owner='dead-pod')
    fresh = runtime.store.begin('A', 'lexical_artifact', ['photo2'], owner='live-pod')
    row = runtime.store.reservations('A', 'lexical_artifact')
    old = next(r for r in row if r['reservationId'] == stale)
    runtime.store.table.upsert_entity(dict(old, startedAt=old['startedAt'] - 3 * 3600))

    # The live writer still blocks the build, but the dead one is gone.
    with pytest.raises(automatic.SourceBusy):
        runtime.store.capture('A', 'lexical_artifact')
    assert [r['reservationId'] for r in runtime.store.reservations('A', 'lexical_artifact')] == [fresh]
    control = runtime.store.control('A', 'lexical_artifact')
    assert control['active'] == 1 and control['fullRequired'] is True

    runtime.store.finish('A', 'lexical_artifact', reservation_id=fresh)
    captured = runtime.store.capture('A', 'lexical_artifact')
    assert set(captured.ids) == {'photo1', 'photo2'}


def test_fresh_reservation_is_not_reaped_and_reaping_can_be_disabled(runtime, monkeypatch):
    res = runtime.store.begin('A', 'vector', ['photo1'])
    assert runtime.store.reap_stale_reservations('A', 'vector') == []
    old = runtime.store.reservations('A', 'vector')[0]
    runtime.store.table.upsert_entity(dict(old, startedAt=old['startedAt'] - 10 * 3600))
    monkeypatch.setattr(automatic, 'STALE_RESERVATION_SECONDS', 0)
    with pytest.raises(automatic.SourceBusy):
        runtime.store.capture('A', 'vector')
    assert runtime.store.control('A', 'vector')['active'] == 1
    assert [r['reservationId'] for r in runtime.store.reservations('A', 'vector')] == [res]


def _age_reservation(runtime, kind, reservation_id, seconds):
    row = next(r for r in runtime.store.reservations('A', kind) if r['reservationId'] == reservation_id)
    runtime.store.table.upsert_entity(dict(row, startedAt=row['startedAt'] - seconds))


def test_late_finish_of_reaped_writer_does_not_decrement_again(runtime):
    dead = runtime.store.begin('A', 'lexical_artifact', ['photo1'], owner='slow-pod')
    other = runtime.store.begin('A', 'lexical_artifact', ['photo2'], owner='live-pod')
    _age_reservation(runtime, 'lexical_artifact', dead, 3 * 3600)
    assert len(runtime.store.reap_stale_reservations('A', 'lexical_artifact')) == 1
    before = runtime.store.control('A', 'lexical_artifact')
    assert before['active'] == 1

    runtime.store.finish('A', 'lexical_artifact', reservation_id=dead)  # must not raise or decrement

    after = runtime.store.control('A', 'lexical_artifact')
    assert after['active'] == 1 and after['revision'] != before['revision']
    assert [r['reservationId'] for r in runtime.store.reservations('A', 'lexical_artifact')] == [other]
    with pytest.raises(automatic.SourceBusy):
        runtime.store.capture('A', 'lexical_artifact')
    runtime.store.finish('A', 'lexical_artifact', reservation_id=other)
    assert runtime.store.control('A', 'lexical_artifact')['active'] == 0


def test_late_finish_of_sole_reaped_writer_does_not_raise_unbalanced(runtime):
    dead = runtime.store.begin('A', 'vector', ['photo1'])
    _age_reservation(runtime, 'vector', dead, 3 * 3600)
    runtime.store.reap_stale_reservations('A', 'vector')
    runtime.store.finish('A', 'vector', reservation_id=dead)
    assert runtime.store.control('A', 'vector')['active'] == 0


def test_reaped_markers_are_collected_after_grace(runtime):
    dead = runtime.store.begin('A', 'vector', ['photo1'])
    _age_reservation(runtime, 'vector', dead, 3 * 3600)
    import time
    reaped_at = time.time()
    runtime.store.reap_stale_reservations('A', 'vector', now=reaped_at)
    pk = automatic.partition('A', 'vector')
    assert runtime.store.table.get_entity(pk, f'res-{dead}').get('reapedAt')
    runtime.store.reap_stale_reservations('A', 'vector', now=reaped_at + 2 * 86400 + 10)
    with pytest.raises(Exception):
        runtime.store.table.get_entity(pk, f'res-{dead}')
