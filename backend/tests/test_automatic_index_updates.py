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


def test_more_than_512_falls_back_full_and_drains_complete_dirty_snapshot(runtime):
    mark(runtime, 'vector', ['seed'])
    worker('vector')
    mark(runtime, 'vector', [str(i) for i in range(600)])
    assert runtime.store.capture('A', 'vector').dirty.truncated
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
    app.enqueue_index_build('A', reason='metadata-change', scope='tag_embedding')
    assert app._sweep_tag_embedding_indexes()['indexesAvailable'] == 1
    assert runtime.calls[-1] == ('tag_embedding', {'model_version': 'clip-real', 'bootstrap_incremental': True})


def test_failed_postwrite_schedule_recovered_from_coverage(runtime, monkeypatch):
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