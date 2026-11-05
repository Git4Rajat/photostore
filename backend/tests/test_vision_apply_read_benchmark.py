"""Isolation and race checks for apply-read consolidation experiment."""
import importlib.util
from pathlib import Path
import sys

import pytest
from azure.core.exceptions import ResourceModifiedError
import storage_utils as storage
from processing_completion import StaleProcessingResult
from processing_fence import result_fence
from test_processing_fence import world
from test_face_by_filename_lookup import _face
from test_vision_apply_optimizations import batch_world

spec=importlib.util.spec_from_file_location('apply_read_benchmark',
    Path(__file__).resolve().parents[1]/'scripts'/'benchmark_vision_apply_reads.py')
bench=importlib.util.module_from_spec(spec);sys.modules[spec.name]=bench;spec.loader.exec_module(bench)


@pytest.mark.parametrize('count',[0,1,8,32])
@pytest.mark.parametrize('seed',[False,True])
def test_candidate_preserves_output_and_curation(count,seed):
    current=bench.execute(count,'current',seed=seed)
    for mode in ('defer_guards','batched_versions'):
        with bench.candidate(mode):candidate=bench.execute(count,'current',seed=seed)
        assert candidate['output_digest']==current['output_digest']
        assert candidate['requests']<=current['requests']
        if count in (0,1):assert candidate['requests']==current['requests']


def test_source_replacement_before_flush_blocks_all_writes(world,monkeypatch):
    metadata,faces,embeddings,lookup,fence,_=batch_world(world,monkeypatch)
    def replace():metadata.rows['lib','photo.jpg']['processingRevision']='successor'
    with bench.candidate('batched_versions',after_versions=replace),result_fence(fence):
        with pytest.raises(StaleProcessingResult):
            storage._store_client_face_entities('lib','photo.jpg',[_face(0),_face(40)])
    assert not faces.rows and not embeddings.rows
    assert lookup.rows['lib','photo.jpg']['state']!='complete'


def test_concurrent_face_curation_still_conflicts(world,monkeypatch):
    metadata,faces,embeddings,lookup,fence,_=batch_world(world,monkeypatch)
    with result_fence(fence):storage._store_client_face_entities('lib','photo.jpg',[_face(0),_face(40)])
    def curate():faces.upsert_entity(dict(next(iter(faces.rows.values())),personId='new-curation'))
    with bench.candidate('batched_versions',after_versions=curate),result_fence(fence):
        with pytest.raises(ResourceModifiedError):
            storage._store_client_face_entities('lib','photo.jpg',[_face(0),_face(40)])
    assert any(row.get('personId')=='new-curation' for row in faces.rows.values())
    assert lookup.rows['lib','photo.jpg']['state']!='complete'


def test_embedding_etag_change_after_batch_read_is_rejected(world,monkeypatch):
    metadata,faces,embeddings,lookup,fence,_=batch_world(world,monkeypatch)
    with result_fence(fence):storage._store_client_face_entities('lib','photo.jpg',[_face(0),_face(40)])
    def curate():embeddings.upsert_entity(dict(next(iter(embeddings.rows.values())),embedding='[9,9,9]'))
    with bench.candidate('batched_versions',after_versions=curate),result_fence(fence):
        with pytest.raises(ResourceModifiedError):
            storage._store_client_face_entities('lib','photo.jpg',[_face(0),_face(40)])
    assert any(row.get('embedding')=='[9,9,9]' for row in embeddings.rows.values())
    assert lookup.rows['lib','photo.jpg']['state']!='complete'


def test_lost_filename_generation_blocks_writes(world,monkeypatch):
    metadata,faces,embeddings,lookup,fence,_=batch_world(world,monkeypatch)
    def replace():lookup.upsert_entity(dict(lookup.rows['lib','photo.jpg'],generation='successor'))
    with bench.candidate('batched_versions',after_versions=replace),result_fence(fence):
        with pytest.raises(storage.FaceFilenameLookupRetryableError):
            storage._store_client_face_entities('lib','photo.jpg',[_face(0),_face(40)])
    assert not faces.rows and not embeddings.rows
    assert lookup.rows['lib','photo.jpg']['state']!='complete'


def test_experiment_restores_production_hooks_after_error():
    import face_write_batch
    original=(face_write_batch.enqueue,face_write_batch.flush,storage._renew_face_filename_write)
    with pytest.raises(RuntimeError),bench.candidate('batched_versions'):raise RuntimeError('test')
    assert original==(face_write_batch.enqueue,face_write_batch.flush,storage._renew_face_filename_write)


@pytest.mark.parametrize('seed',[False,True])
def test_production_consolidation_matches_baseline_and_reduces_requests(monkeypatch,seed):
    monkeypatch.setenv('IPWORK_FACE_APPLY_READS_BATCH_ENABLED','false')
    old=bench._execute(8,'current',seed=seed)
    monkeypatch.setenv('IPWORK_FACE_APPLY_READS_BATCH_ENABLED','true')
    new=bench._execute(8,'current',seed=seed)
    assert old['output_digest']==new['output_digest']
    assert new['requests']==old['requests']-23
    assert new['calls'].get('embedding.get',0)==0
    assert new['calls']['embedding.query']==1


def test_production_version_read_failure_prevents_all_writes(world,monkeypatch):
    metadata,faces,embeddings,lookup,fence,_=batch_world(world,monkeypatch)
    def fail(*a,**k):raise OSError('version query unavailable')
    monkeypatch.setattr(storage._CTX['face_embeddings_table_client'],'query_entities',fail)
    with result_fence(fence),pytest.raises(OSError,match='version query'):
        storage._store_client_face_entities('lib','photo.jpg',[_face(0),_face(40)])
    assert not faces.rows and not embeddings.rows
    assert lookup.rows['lib','photo.jpg']['state']!='complete'


def test_production_checks_source_after_version_query(world,monkeypatch):
    metadata,faces,embeddings,lookup,fence,_=batch_world(world,monkeypatch)
    table=storage._CTX['face_embeddings_table_client'];original=table.query_entities
    def replace(*a,**k):
        rows=original(*a,**k)
        metadata.rows['lib','photo.jpg']['processingRevision']='successor'
        return rows
    monkeypatch.setattr(table,'query_entities',replace)
    with result_fence(fence),pytest.raises(StaleProcessingResult):
        storage._store_client_face_entities('lib','photo.jpg',[_face(0),_face(40)])
    assert not faces.rows and not embeddings.rows
    assert lookup.rows['lib','photo.jpg']['state']!='complete'
