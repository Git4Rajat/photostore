"""Prototype batches preserve conditional writes and final lookup ordering."""
import importlib.util
from pathlib import Path
import sys

import pytest
from azure.core.exceptions import ResourceModifiedError
from processing_completion import StaleProcessingResult

spec=importlib.util.spec_from_file_location('face_persistence_benchmark',
    Path(__file__).resolve().parents[1]/'scripts'/'benchmark_face_persistence.py')
bench=importlib.util.module_from_spec(spec);sys.modules[spec.name]=bench;spec.loader.exec_module(bench)


@pytest.fixture(autouse=True)
def original_read_baseline(monkeypatch):
    # This historical experiment compares write batching before read consolidation.
    monkeypatch.setenv('IPWORK_FACE_APPLY_READS_BATCH_ENABLED', 'false')


@pytest.mark.parametrize('count',[0,1,8])
@pytest.mark.parametrize('seed',[False,True])
def test_output_and_curation_match_current(count,seed):
    current=bench.execute(count,'current',seed=seed)
    for mode in ('batch_only','batch_guard'):
        candidate=bench.execute(count,mode,seed=seed)
        assert candidate['output_digest']==current['output_digest']
        if mode=='batch_guard':
            assert candidate['requests']<=current['requests']
        elif count==1:
            # Control retains per-row checks and adds two pre-batch checks.
            assert candidate['requests']==current['requests']+2


def test_changed_source_blocks_batches_and_complete_lookup():
    observed={}
    def change(metadata,faces,embeddings,lookup):
        observed.update(faces=faces,embeddings=embeddings,lookup=lookup)
        metadata.rows['lib','photo.jpg']['processingRevision']='newer'
    with pytest.raises(StaleProcessingResult):bench.execute(8,'batch_guard',before_flush=change)
    assert not observed['faces'].rows and not observed['embeddings'].rows
    assert observed['lookup'].rows['lib','photo.jpg']['state']!='complete'


def test_concurrent_curation_is_not_overwritten():
    observed={}
    def change(metadata,faces,embeddings,lookup):
        observed.update(faces=faces,lookup=lookup)
        first=next(iter(faces.rows.values()))
        faces.upsert_entity(dict(first,personId='newer-curation'))
    with pytest.raises(ResourceModifiedError):bench.execute(8,'batch_guard',seed=True,before_flush=change)
    assert any(r.get('personId')=='newer-curation' for r in observed['faces'].rows.values())
    assert observed['lookup'].rows['lib','photo.jpg']['state']!='complete'


def test_failed_batch_does_not_partially_mutate_its_table():
    from collections import Counter
    from test_face_by_filename_lookup import FilenameLookupTable
    table=FilenameLookupTable();meter=bench.TableMeter(table,Counter(),'faces',0)
    table.upsert_entity(dict(PartitionKey='lib',RowKey='existing',personId='curated'))
    operations=[('create',dict(PartitionKey='lib',RowKey='new'),{}),
        ('update',dict(PartitionKey='lib',RowKey='existing',personId='stale'),
         dict(mode=bench.UpdateMode.REPLACE,etag='old',match_condition=bench.MatchConditions.IfNotModified))]
    with pytest.raises(ResourceModifiedError):meter.submit_transaction(operations)
    assert ('lib','new') not in table.rows
    assert table.rows['lib','existing']['personId']=='curated'
