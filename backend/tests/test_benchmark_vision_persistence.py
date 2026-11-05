"""Correctness gates for isolated benchmark prototypes, not production wiring."""
import sys
import json
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import benchmark_vision_persistence as bench
from test_face_by_filename_lookup import FilenameLookupTable
from test_processing_fence import world
from test_upload_waves import AtomicTable
from processing_completion import StaleProcessingResult
from azure.core.exceptions import ResourceModifiedError


@pytest.fixture(autouse=True)
def restore_benchmark_context():
    # bench.configure() raw-assigns storage.touch_user_search_indexes_state/
    # _start_face_crop_warming (not via monkeypatch) with no restore of its
    # own -- every test here leaked the real functions permanently for the
    # rest of the pytest process otherwise, breaking any later, unrelated
    # test that depends on touch_user_search_indexes_state actually marking
    # anything dirty. Same pattern test_benchmark_vision_pipeline.py already
    # guards against for the same reason.
    saved = dict(bench.storage._CTX)
    touch = bench.storage.touch_user_search_indexes_state
    warm = bench.storage._start_face_crop_warming
    yield
    bench.storage._CTX.clear()
    bench.storage._CTX.update(saved)
    bench.storage.touch_user_search_indexes_state = touch
    bench.storage._start_face_crop_warming = warm


@pytest.mark.parametrize('faces,seeded', [(0, False), (1, False), (8, False), (1, True)])
def test_pipeline_equivalence(faces, seeded):
    reports = []
    for mode in ('current', 'batch', 'receipts', 'combined', 'batch_parallel', 'combined_parallel'):
        tables = {n: FilenameLookupTable() for n in ('metadata', 'faces', 'embeddings', 'lookup')}
        tables['jobs'] = AtomicTable()
        reports.append(bench.execute(tables, mode=mode, face_count=faces, images=8, group=8, seeded=seeded))
    assert len({r['output_digest'] for r in reports}) == 1
    assert reports[2]['calls'].get('jobs.get_entity', 0) < reports[0]['calls'].get('jobs.get_entity', 0)
    if faces:
        assert reports[1]['calls'].get('faces.submit_transaction', 0) == 1


@pytest.mark.parametrize('failure', ['source', 'lookup', 'curation', 'embedding', 'partial'])
@pytest.mark.parametrize('guard_workers', [1, 3])
def test_batch_cannot_publish_completion_after_failure(world, failure, guard_workers, monkeypatch):
    metadata, faces, embeddings, lookup, fence = world
    # Use a complete initial generation to avoid the legacy filename scan.
    lookup.create_entity(dict(PartitionKey='lib', RowKey='photo.jpg', schemaVersion=bench.storage._FACE_FILENAME_SCHEMA_VERSION,
        generation='a'*32, state='complete', faceIds='[]', leaseExpiresAt=''))
    with bench.fencing.result_fence(fence):
        bench.storage._store_client_face_entities('lib', 'photo.jpg', bench.inputs(1))
    with bench.CrossImageBatch(guard_workers).installed() as batch, bench.fencing.result_fence(fence):
        bench.storage._store_client_face_entities('lib', 'photo.jpg', bench.inputs(1))
        fid = next(iter(faces.rows))[1]
        if failure == 'source':
            metadata.upsert_entity(dict(metadata.get_entity('lib', 'photo.jpg'), processingFence='new'))
        elif failure == 'lookup':
            lookup.upsert_entity(dict(lookup.get_entity('lib', 'photo.jpg'), generation='c'*32))
        elif failure in ('curation', 'embedding'):
            table = faces if failure == 'curation' else embeddings
            table.upsert_entity(dict(table.get_entity('lib', fid), personId='concurrent-edit'))
        else:
            monkeypatch.setattr(faces, 'submit_transaction', lambda *a: (_ for _ in ()).throw(OSError('outage')))
        with pytest.raises((StaleProcessingResult, ResourceModifiedError, bench.storage.FaceFilenameLookupRetryableError, OSError)):
            batch.flush()
    assert lookup.get_entity('lib', 'photo.jpg')['state'] != 'complete'


def test_receipt_cache_only_after_success_and_does_not_escape(monkeypatch):
    table = AtomicTable(); receipt = bench.ArtifactReceipts(table, 'lib')
    with bench.receipt_prototype(True), bench.receipt_scope():
        identity = receipt._put('face', {'value': 1})
        monkeypatch.setattr(table, 'get_entity', lambda *a: (_ for _ in ()).throw(AssertionError('redundant read')))
        assert bench.ArtifactReceipts(table, 'lib')._get('face!'+identity)
    with pytest.raises(AssertionError):
        receipt._get('face!'+identity)
    table = AtomicTable(); receipt = bench.ArtifactReceipts(table, 'lib')
    monkeypatch.setattr(table, 'submit_transaction', lambda *a: (_ for _ in ()).throw(OSError('outage')))
    with bench.receipt_prototype(True), bench.receipt_scope():
        with pytest.raises(OSError):
            receipt._put('face', {'value': 1})
        assert bench._receipts.get() == {}


def test_cached_manifest_still_validates_scope_digest_and_snapshot():
    table = AtomicTable(); receipt = bench.ArtifactReceipts(table, 'lib')
    with bench.receipt_prototype(True), bench.receipt_scope():
        snapshot = receipt._put('face', {'embedding': '[1,2,3]'})
        payload = dict(library='lib', filename='photo.jpg', photoId='photo', revision='rev',
            statuses={}, derivatives={}, faceCount=1, faceSnapshots=[snapshot])
        identity = receipt._put('manifest', payload)
        reference = {k: payload[k] for k in ('library', 'filename', 'photoId', 'revision')} | dict(id=identity)
        assert receipt.verify_reference(reference, payload) == payload
        with pytest.raises(ValueError, match='scope'):
            receipt.verify_reference(reference, dict(payload, photoId='other'))
        with pytest.raises(ValueError, match='cover'):
            receipt.verify_reference(reference, dict(payload, faceCount=0))
        key = (id(table), receipt.partition, 'face!'+snapshot)
        bench._receipts.get()[key]['payload'] = '{}'
        with pytest.raises(ValueError, match='invalid face'):
            receipt.verify_reference(reference, payload)


def test_partial_snapshot_failure_never_caches_uncommitted_rows(monkeypatch):
    table = AtomicTable(); receipt = bench.ArtifactReceipts(table, 'lib')
    original = table.submit_transaction
    calls = []
    def submit(operations):
        calls.append(1)
        if len(calls) == 2:
            raise OSError('second batch failed')
        return original(operations)
    monkeypatch.setattr(table, 'submit_transaction', submit)
    with bench.receipt_prototype(True), bench.receipt_scope():
        with pytest.raises(OSError):
            receipt._put_many('face', [{'value': n} for n in range(21)])
        assert len(table.rows) == 20
        assert bench._receipts.get() == {}


@pytest.mark.parametrize('failure', ['cleanup', 'lookup_changed'])
def test_receipt_reuse_retains_fresh_mutable_guards(world, monkeypatch, failure):
    metadata, _, _, lookup, fence = world
    table = AtomicTable(); receipt = bench.ArtifactReceipts(table, 'lib')
    with bench.fencing.result_fence(fence):
        bench.storage._store_client_face_entities('lib', 'photo.jpg', bench.inputs(1))
    row = dict(metadata.get_entity('lib', 'photo.jpg'), faceCount=1,
        **{step+'_status': 'no_data' for step in bench.STEPS})
    row['face_status'] = 'done'
    with bench.receipt_prototype(True), bench.receipt_scope(), bench.fencing.result_fence(fence):
        if failure == 'lookup_changed':
            original = lookup.get_entity; calls = []
            def read(*a, **k):
                calls.append(1)
                result = original(*a, **k)
                if len(calls) == 2:
                    result['generation'] = 'c'*32
                return result
            monkeypatch.setattr(lookup, 'get_entity', read)
            with pytest.raises(ValueError, match='lookup changed'):
                receipt.capture(bench.storage, row)
            assert not any(key[1].startswith('manifest!') for key in table.rows)
        else:
            reference = receipt.capture(bench.storage, row)
            row.update(artifactReceipt=json.dumps(reference), processing_lease_owner='', processing_lease_expires_at='')
            bench.prepare_completion(row)
            store = bench.ShadowWaves(table, 'lib')
            table.submit_transaction([('create', dict(PartitionKey=store.partition, RowKey='policy', cleanupState='cleaning'))])
            from upload_waves import Conflict
            with pytest.raises(Conflict, match='cleanup'):
                store.accept_source_completion(json.loads(row['processingCompletionOutbox'])[0])
            assert not any(key[1].startswith('source-completion!') for key in table.rows)
