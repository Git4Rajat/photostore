"""Benchmark-only correctness gates; no production wiring."""
import sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import benchmark_vision_apply_followup as bench
from test_face_by_filename_lookup import FilenameLookupTable
from test_upload_waves import AtomicTable


@pytest.fixture(autouse=True)
def restore_benchmark_context():
    # bench.execute() -> benchmark_vision_persistence.configure() raw-assigns
    # storage.touch_user_search_indexes_state/_start_face_crop_warming (not
    # via monkeypatch) with no restore of its own -- leaks the real
    # functions permanently for the rest of the pytest process otherwise.
    # Same pattern test_benchmark_vision_pipeline.py/
    # test_benchmark_vision_persistence.py/
    # test_benchmark_vision_embedding_proof.py already guard against.
    storage = bench.base.storage
    saved = dict(storage._CTX)
    touch = storage.touch_user_search_indexes_state
    warm = storage._start_face_crop_warming
    yield
    storage._CTX.clear()
    storage._CTX.update(saved)
    storage.touch_user_search_indexes_state = touch
    storage._start_face_crop_warming = warm


@pytest.mark.parametrize('faces,seeded',[(0,False),(1,False),(2,False),(8,False),(1,True)])
def test_production_cache_baseline_and_candidates_have_durable_parity(faces,seeded):
    reports=[]
    for mode in ('production','capture_parallel','batch_parallel','batch_capture_parallel'):
        tables={n:FilenameLookupTable() for n in ('metadata','faces','embeddings','lookup')}
        tables['jobs']=AtomicTable()
        reports.append(bench.run(tables,mode=mode,face_count=faces,images=8,group=8,seeded=seeded))
    assert len({r['output_digest'] for r in reports})==1
    # Parallel capture changes scheduling, not the number of durable SDK reads.
    assert reports[0]['requests']==reports[1]['requests']


def test_capture_snapshot_is_consumed_once_then_reads_fresh():
    table=FilenameLookupTable()
    table.create_entity(dict(PartitionKey='lib',RowKey='photo',value='new'))
    view=bench.SnapshotTable(table,{('lib','photo'):{'value':'old'}})
    assert view.get_entity('lib','photo')['value']=='old'
    assert view.get_entity('lib','photo')['value']=='new'


@pytest.mark.parametrize('failure', ['lookup_changed','embedding_attempt'])
def test_parallel_capture_retains_lookup_and_source_validation(monkeypatch,failure):
    original=bench.base.ArtifactReceipts.capture
    def altered_capture(self,storage,metadata):
        if isinstance(storage,bench.StorageView):
            if failure=='lookup_changed':
                table=storage._CTX['face_by_filename_table_client'].table
                row=dict(table.get_entity(self.library,metadata['RowKey']))
                row['generation']='changed-during-capture'
                table.upsert_entity(row)
            else:
                view=storage._CTX['face_embeddings_table_client']
                for row in view.rows.values():
                    row['processingFence']='other-attempt'
        return original(self,storage,metadata)
    monkeypatch.setattr(bench.base.ArtifactReceipts,'capture',altered_capture)
    tables={n:FilenameLookupTable() for n in ('metadata','faces','embeddings','lookup')}
    tables['jobs']=AtomicTable()
    expected='lookup changed' if failure=='lookup_changed' else 'attempts differ'
    with pytest.raises(ValueError,match=expected):
        bench.run(tables,mode='capture_parallel',face_count=1,images=1,group=1)
    assert not any('processingCompletionOutbox' in row for row in tables['metadata'].rows.values())
