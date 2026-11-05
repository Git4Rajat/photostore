"""Correctness gates for isolated qualification/handoff experiments."""
import json
from pathlib import Path
import sys
import threading
import time

import pytest
from azure.core import MatchConditions
from azure.data.tables import UpdateMode
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import benchmark_vision_pipeline as bench
from test_face_by_filename_lookup import FilenameLookupTable
from test_upload_waves import AtomicTable


@pytest.fixture(autouse=True)
def restore_benchmark_context():
    saved=dict(bench.storage._CTX)
    touch=bench.storage.touch_user_search_indexes_state
    warm=bench.storage._start_face_crop_warming
    yield
    bench.storage._CTX.clear();bench.storage._CTX.update(saved)
    bench.storage.touch_user_search_indexes_state=touch
    bench.storage._start_face_crop_warming=warm


class SourceTable(FilenameLookupTable):
    def update_entity(self,entity,mode=None,*,etag=None,match_condition=None):
        if mode==UpdateMode.MERGE:
            previous=self.get_entity(entity['PartitionKey'],entity['RowKey'])
            entity=dict(previous,**entity)
        if etag is None:
            previous=self.get_entity(entity['PartitionKey'],entity['RowKey'])
            etag=previous.metadata['etag']
            match_condition=MatchConditions.IfNotModified
        super().update_entity(entity,mode=UpdateMode.REPLACE,etag=etag,match_condition=match_condition)
        return {'etag':self.get_entity(entity['PartitionKey'],entity['RowKey']).metadata['etag']}


def tables():
    result={n:SourceTable() for n in ('metadata','faces','embeddings','lookup','photoembeddings')}
    result['jobs']=AtomicTable()
    return result


@pytest.mark.parametrize('faces',[0,1,8])
def test_qualification_parity_and_batch_reads(faces):
    raw=tables();fixture=bench.seed_completed(raw,20,faces)
    results=[bench.measured_qualification(raw,images=20,faces=faces,background=0,mode=mode,fixture=fixture)
             for mode in ('serial','parallel','batch')]
    assert len({r['output_digest'] for r in results})==1
    assert results[2]['sdk_calls']<results[0]['sdk_calls']


@pytest.mark.parametrize('mode',['serial','parallel','batch'])
@pytest.mark.parametrize('failure',['replacement','active_lease','corrupt_manifest','missing_snapshot'])
def test_fresh_qualification_fails_closed(mode,failure):
    raw=tables();store,members=bench.seed_completed(raw,1,1)
    row=raw['metadata'].get_entity(store.library,members[0]['filename'])
    if failure=='replacement':
        raw['metadata'].upsert_entity(dict(row,anonymousImageId='new-source'))
    elif failure=='active_lease':
        raw['metadata'].upsert_entity(dict(row,processing_lease_owner='new-owner',processing_lease_expires_at='2099-01-01T00:00:00Z'))
    else:
        reference=json.loads(row['artifactReceipt']);receipt=bench.ArtifactReceipts(store.table,store.library)
        manifest_key=(receipt.partition,'manifest!'+reference['id'])
        if failure=='corrupt_manifest':
            raw['jobs'].rows[manifest_key]['payload']='{}'
        else:
            snapshot=json.loads(raw['jobs'].rows[manifest_key]['payload'])['faceSnapshots'][0]
            del raw['jobs'].rows[receipt.partition,'face!'+snapshot]
    if failure in ('replacement','active_lease'):
        assert bench.qualify_group(store,raw['metadata'],members,mode)==[False]
    else:
        with pytest.raises((ValueError,KeyError)):
            bench.qualify_group(store,raw['metadata'],members,mode)


def test_pre_source_absence_is_not_reused():
    raw=tables();store,members=bench.seed_completed(raw,1,1)
    key=next(k for k in raw['jobs'].rows if k[1].startswith('source-completion!'))
    saved=raw['jobs'].rows.pop(key)
    with bench.ReceiptBatch(store.table,store.library) as receipts:
        metadata=bench.MetadataBatch(raw['metadata'],store.library,members)
        stale=bench.InspectionBatch(store,members,metadata,receipts)
        assert stale.inboxes[key[1]] is None
        raw['jobs'].rows[key]=saved
        assert bench.qualify_group(store,raw['metadata'],members,'batch')==[True]


@pytest.mark.parametrize('change',[{'anonymousImageId':'replacement'},
    {'processing_lease_owner':'new-owner','processing_lease_expires_at':'2099-01-01T00:00:00Z'}])
def test_post_prefetch_change_cannot_qualify_old_snapshot(monkeypatch,change):
    raw=tables();store,members=bench.seed_completed(raw,1,1)
    original=bench.InspectionBatch
    def prefetch(*args):
        result=original(*args)
        row=raw['metadata'].get_entity(store.library,members[0]['filename'])
        raw['metadata'].upsert_entity(dict(row,**change))
        return result
    monkeypatch.setattr(bench,'InspectionBatch',prefetch)
    assert bench.qualify_group(store,raw['metadata'],members,'batch')==[False]


@pytest.mark.parametrize('io_workers',[1,2,3])
def test_handoff_is_bounded_associated_and_drained(io_workers):
    active=0;peak=0;lock=threading.Lock();admitted=set()
    def admit(items):admitted.update(items)
    def compute(i):
        assert i in admitted
        return {'id':i,'value':str(i)*100}
    def apply(value):
        nonlocal active,peak
        with lock:active+=1;peak=max(peak,active)
        time.sleep(.001)
        with lock:active-=1
        return value['id']
    result,stats=bench.bounded_handoff(range(20),compute,apply,apply_workers=io_workers,
        limit=2+io_workers,admit=admit)
    assert sorted(result)==list(range(20)) and active==0 and peak<=io_workers
    assert stats['peak_inflight']<=2+io_workers and stats['pending_at_finish']==0


def test_handoff_drains_active_work_on_apply_failure():
    active=0;lock=threading.Lock()
    def apply(value):
        nonlocal active
        with lock:active+=1
        try:
            time.sleep(.002)
            if value==1:raise OSError('storage outage')
            return value
        finally:
            with lock:active-=1
    with pytest.raises(OSError):
        bench.bounded_handoff(range(20),lambda i:i,apply)
    assert active==0


def test_handoff_rejects_oversized_output():
    with pytest.raises(ValueError,match='byte budget'):
        bench.bounded_handoff([0],lambda i:'x'*1000,lambda value:value,byte_limit=100,limit=2)


@pytest.mark.parametrize('mode',['current3','handoff2x1','handoff2x2','handoff2x3'])
@pytest.mark.parametrize('face_count',[0,1])
def test_pipeline_real_claim_apply_capture_ack_qualification(monkeypatch,mode,face_count):
    monkeypatch.setenv('UPLOAD_WAVE_SOURCE_COMPLETION_ENABLED','true')
    monkeypatch.setenv('PROCESSING_GENERATION_ENFORCEMENT_ENABLED','true')
    version=bench.current_contract()['photoEmbeddingVersion']
    monkeypatch.setattr(bench.storage.vision_utils,'get_text_embedding_version',lambda:version)
    monkeypatch.setattr(bench.storage.vision_utils,'get_text_embedding_dimension',lambda:512)
    monkeypatch.setattr(bench.storage.vision_utils,'encode_text_embedding',lambda text:[.1]*512)
    def models(i):
        faces=bench.inputs(face_count)
        for face in faces:face['embeddingVersion']=bench.current_contract()['faceEmbeddingVersions'][0]
        return dict(ocr=dict(hasData=False,text=''),face=dict(hasData=bool(faces),faces=faces,rawFaceCount=len(faces)),
            ai_vision=dict(hasData=False,modelAvailability='available',modelVersion='test',
                modelTaxonomyVersion='test',runtime='test',model='CLIP'))
    result=bench.pipeline_run(tables(),[],list(range(4)),models,{},mode=mode)
    assert result['images']==4 and result['output_digest']
