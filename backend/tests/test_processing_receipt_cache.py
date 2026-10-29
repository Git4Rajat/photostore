"""Only durable immutable writes cross source/cache boundaries."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from processing_receipt_cache import CommittedReceipts, committed_receipt_scope
from processing_receipts import ArtifactReceipts
from wave_source_batch import ReceiptBatch
from test_upload_waves import AtomicTable
from test_table_reads import QueryRows
from test_processing_artifacts import blobs
from test_processing_fence import world


def test_committed_writes_reused_across_instances_then_scope_clears(monkeypatch):
    table=AtomicTable();receipts=ArtifactReceipts(table,'lib');reads=[]
    original=table.get_entity
    monkeypatch.setattr(table,'get_entity',lambda *a:(reads.append(a),original(*a))[1])
    with committed_receipt_scope() as cache:
        identity=receipts._put('face',dict(value=1))
        row=ArtifactReceipts(table,'lib')._get('face!'+identity)
        row['payload']='local caller mutation'
        assert receipts._get('face!'+identity)['payload']=='{"value":1}'
        assert not reads
        snapshot=cache.snapshot()
    assert cache.bytes==0 and not cache.rows
    assert receipts._get('face!'+identity)['payload']=='{"value":1}'
    assert len(reads)==1
    with ReceiptBatch(table,'lib') as batch:
        batch.accept_committed(snapshot)
        assert batch._get('face!'+identity)['payload']=='{"value":1}'
    assert len(reads)==1


@pytest.mark.parametrize('ambiguous',[False,True])
def test_failed_or_ambiguous_creates_do_not_populate_cache(monkeypatch,ambiguous):
    table=AtomicTable();receipts=ArtifactReceipts(table,'lib');submit=table.submit_transaction
    def fail(operations):
        if ambiguous:submit(operations)
        raise OSError('Lost response')
    monkeypatch.setattr(table,'submit_transaction',fail)
    with committed_receipt_scope() as cache:
        with pytest.raises(OSError):receipts._put('face',dict(value=1))
        assert cache.snapshot()==()


def test_conflict_equality_uses_storage_despite_an_earlier_cached_create():
    table=AtomicTable();receipts=ArtifactReceipts(table,'lib')
    with committed_receipt_scope():
        identity=receipts._put('face',dict(value=1))
        table.rows[receipts.partition,'face!'+identity]['payload']='{"value":2}'
        with pytest.raises(ValueError,match='rebound'):receipts._put('face',dict(value=1))


def test_identical_conflicts_and_batch_writes_are_reusable():
    table=AtomicTable();receipts=ArtifactReceipts(table,'lib')
    existing=receipts._put('face',dict(value=1))
    with committed_receipt_scope() as cache:
        ids=receipts._put_many('face',[dict(value=1),dict(value=2)])
        assert ids[0]==existing and len(cache.snapshot())==2
        assert all(receipts._get('face!'+identity) for identity in ids)


def test_failed_batch_does_not_publish_rows_to_cache(monkeypatch):
    receipts=ArtifactReceipts(AtomicTable(),'lib')
    monkeypatch.setattr(receipts.table,'submit_transaction',lambda *a:(_ for _ in ()).throw(OSError('outage')))
    with committed_receipt_scope() as cache:
        with pytest.raises(OSError):receipts._put_many('face',[dict(value=1),dict(value=2)])
        assert cache.snapshot()==()


def test_absence_other_tables_and_other_libraries_are_not_reused():
    table=AtomicTable();receipts=ArtifactReceipts(table,'lib')
    with committed_receipt_scope() as cache:
        assert receipts._get('face!missing') is None
        identity=receipts._put('face',dict(value=1))
        assert ArtifactReceipts(table,'other')._get('face!'+identity) is None
        assert ArtifactReceipts(AtomicTable(),'lib')._get('face!'+identity) is None
        with ReceiptBatch(table,'other') as batch:
            batch.accept_committed(cache.snapshot())
            assert not len(batch.rows)
        assert all(entry[2]!='face!missing' for entry in cache.snapshot())


def test_nested_scopes_restore_parent_and_clear_on_failure():
    table=AtomicTable();receipts=ArtifactReceipts(table,'lib')
    with committed_receipt_scope() as outer:
        identity=receipts._put('face',dict(value=1))
        with pytest.raises(RuntimeError):
            with committed_receipt_scope() as inner:
                receipts._put('face',dict(value=2))
                raise RuntimeError('source failed')
        assert not inner.rows
        assert len(outer.rows)==1 and receipts._get('face!'+identity)
    assert not outer.rows


def test_parallel_sources_have_distinct_bounded_caches():
    table=AtomicTable();barrier=threading.Barrier(2)
    def source(index):
        with committed_receipt_scope() as cache:
            identity=ArtifactReceipts(table,'lib')._put('face',dict(value=index))
            barrier.wait(timeout=5)
            assert len(cache.rows)==1
            return identity,cache.snapshot()
    with ThreadPoolExecutor(2) as pool:results=list(pool.map(source,range(2)))
    assert results[0][0]!=results[1][0]
    with ReceiptBatch(table,'lib') as batch:
        for _,entries in results:batch.accept_committed(entries)
        assert len(batch.rows)==2


def test_cache_entry_and_byte_limits_evict_or_fall_back():
    table=AtomicTable();cache=CommittedReceipts(max_bytes=800,max_rows=1)
    row=lambda k,p:dict(PartitionKey='lib',RowKey='face!'+k,payload=p)
    cache.remember(table,row('a','{}'));cache.remember(table,row('b','{}'))
    assert cache.get(table,'lib','face!a') is None
    assert cache.get(table,'lib','face!b')
    cache.remember(table,row('oversize','x'*900))
    assert len(cache.rows)==1 and cache.bytes<=800
    cache.remember(table,dict(PartitionKey='lib',RowKey='source-completion!mutable',payload='{}'))
    assert len(cache.rows)==1


def test_slow_receipt_query_switches_to_points_with_bounded_initial_probe(monkeypatch):
    import time
    import processing_metrics as metrics
    table=QueryRows();clock=[0.]
    original=table.query_entities
    def query(filter_text):
        clock[0]+=.2
        return original(filter_text)
    monkeypatch.setattr(table,'query_entities',query)
    monkeypatch.setattr(time,'monotonic',lambda:clock[0])
    with metrics.accounting() as counters, ReceiptBatch(table,'lib') as receipts:
        receipts.prefetch('face!'+str(i) for i in range(40))
        assert len(table.queries)==1 and table.reads==26
        receipts.prefetch(['manifest!later'])
        assert len(table.queries)==1 and table.reads==27
    assert counters.snapshot()['receipt_prefetch_strategy_switches']==1
    assert counters.snapshot()['receipt_prefetch_point_groups']==2


def test_fast_receipt_queries_remain_queries_for_small_partitions():
    table=QueryRows()
    with ReceiptBatch(table,'lib') as receipts:
        receipts.prefetch('face!'+str(i) for i in range(20))
        receipts.prefetch(['manifest!later'])
        assert len(table.queries)==3 and table.reads==0


def test_singleton_lookup_does_not_replace_the_first_scan_probe(monkeypatch):
    import time
    table=QueryRows();clock=[0.];query=table.query_entities
    def slow(filter_text):
        clock[0]+=.2
        return query(filter_text)
    monkeypatch.setattr(table,'query_entities',slow)
    monkeypatch.setattr(time,'monotonic',lambda:clock[0])
    with ReceiptBatch(table,'lib') as receipts:
        receipts.prefetch(['manifest!single'])
        receipts.prefetch('face!'+str(i) for i in range(40))
        assert len(table.queries)==2 and table.reads==26


def test_large_fast_fetch_is_not_mistaken_for_a_slow_scan(monkeypatch):
    import time
    table=QueryRows();clock=[0.];query=table.query_entities
    def fast(filter_text):
        clock[0]+=.01
        return query(filter_text)
    monkeypatch.setattr(table,'query_entities',fast)
    monkeypatch.setattr(time,'monotonic',lambda:clock[0])
    with ReceiptBatch(table,'lib') as receipts:
        receipts.prefetch('face!'+str(i) for i in range(1000))
        assert not receipts._point_prefetch and table.reads==0


def test_capture_acceptance_and_coordinator_reuse_committed_receipts(blobs,monkeypatch):
    import json
    import storage_utils as storage
    from processing_completion import STEPS,prepare_completion
    from processing_fence import result_fence
    from upload_waves import ShadowWaves
    from test_face_by_filename_lookup import _face
    (metadata,_,_,_,fence),_=blobs
    table=AtomicTable();receipts=ArtifactReceipts(table,'lib');reads=[]
    original=table.get_entity
    monkeypatch.setattr(table,'get_entity',lambda *a:(reads.append(a),original(*a))[1])
    row=dict(metadata.get_entity('lib','photo.jpg'))
    row.update({step+'_status':'no_data' for step in STEPS})
    row.update(face_status='done',faceCount=1)
    with committed_receipt_scope() as cache:
        with result_fence(fence):
            storage._store_client_face_entities('lib','photo.jpg',[_face(0)|{'embeddingVersion':'v1'}])
            reference=receipts.capture(storage,row)
        row.update(artifactReceipt=json.dumps(reference),processing_lease_owner='',processing_lease_expires_at='')
        prepare_completion(row);intent=json.loads(row['processingCompletionOutbox'])[0]
        assert ShadowWaves(table,'lib').accept_source_completion(intent)['artifactReceiptQualified']
        snapshot=cache.snapshot()
    with ReceiptBatch(table,'lib') as batch:
        batch.accept_committed(snapshot)
        batch.prefetch('manifest!'+reference['id'] for _ in range(2))
        assert batch.verify_reference(reference,intent)['faceCount']==1
    assert not [key for _,key in reads if key.startswith(('face!','manifest!'))]
