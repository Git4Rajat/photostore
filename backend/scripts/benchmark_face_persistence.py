"""Local production face persistence versus isolated per-table CAS batches.

Uses existing ETag-aware test tables. Delays are simulated, not Azure timing.
Prototype interception never changes production source files.
"""
import argparse
from collections import Counter
from contextlib import contextmanager
import copy
import hashlib
import json
from pathlib import Path
import statistics
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
import pytest
from azure.core import MatchConditions
from azure.core.exceptions import ResourceExistsError, ResourceModifiedError, ResourceNotFoundError
from azure.data.tables import UpdateMode
import storage_utils as storage
import processing_fence as fencing
from test_processing_fence import world
from test_face_by_filename_lookup import _face


class TableMeter:
    def __init__(self, table, counts, name, delay):
        self.table,self.counts,self.name,self.delay=table,counts,name,delay
    def call(self,operation):
        self.counts[self.name+'.'+operation]+=1
        if self.delay:time.sleep(self.delay/1000)
    def get_entity(self,*args,**kwargs):
        self.call('get');return self.table.get_entity(*args,**kwargs)
    def query_entities(self,*args,**kwargs):
        self.call('query');return self.table.query_entities(*args,**kwargs)
    def create_entity(self,*args,**kwargs):
        self.call('create');return self.table.create_entity(*args,**kwargs)
    def update_entity(self,*args,**kwargs):
        self.call('update');return self.table.update_entity(*args,**kwargs)
    def upsert_entity(self,*args,**kwargs):
        self.call('upsert');return self.table.upsert_entity(*args,**kwargs)
    def submit_transaction(self,operations):
        operations=[(op[0],op[1],op[2] if len(op)>2 else {}) for op in operations]
        # Validate the entire partition transaction before applying any row.
        assert 0<len(operations)<=100
        assert len({op[1]['PartitionKey'] for op in operations})==1
        assert len({op[1]['RowKey'] for op in operations})==len(operations)
        self.call('transaction')
        with self.table.lock:
            for operation,entity,options in operations:
                key=entity['PartitionKey'],entity['RowKey']
                if operation=='create':
                    if key in self.table.rows:raise ResourceExistsError('Concurrent create')
                else:
                    assert options['match_condition']==MatchConditions.IfNotModified
                    if options['etag']!=str(self.table.versions.get(key)):raise ResourceModifiedError('Concurrent curation')
            for operation,entity,options in operations:
                if operation=='create':self.table.create_entity(entity)
                else:self.table.update_entity(entity,**options)


@contextmanager
def batched_writes(monkeypatch, mode, *, before_flush=None):
    if mode=='current':
        yield
        return
    pending={}
    original_finish=storage._finish_face_filename_write
    def enqueue(table,entity,*,previous=None,previous_known=False):
        fence=fencing.current_fence(entity['PartitionKey'],entity.get('filename'))
        if not previous_known:
            try:previous=table.get_entity(entity['PartitionKey'],entity['RowKey'])
            except ResourceNotFoundError:previous=None
        if mode=='batch_only':fence.check()
        row=dict(entity,**fence.headers())
        operation=('create',row,{}) if previous is None else ('update',row,dict(
            mode=UpdateMode.REPLACE,etag=previous.metadata['etag'],match_condition=MatchConditions.IfNotModified))
        pending.setdefault(table,[]).append(operation)
    def finish(*args,**kwargs):
        if args[3] is not None:
            if before_flush:before_flush()
            for table,operations in pending.items():
                for offset in range(0,len(operations),100):
                    fencing.current_fence().check()
                    table.submit_transaction(operations[offset:offset+100])
            pending.clear()
        return original_finish(*args,**kwargs)
    monkeypatch.setattr(fencing,'write_artifact',enqueue)
    monkeypatch.setattr(storage,'_finish_face_filename_write',finish)
    try:yield
    finally:pending.clear()


def execute(face_count,mode,delay=0,*,seed=False,before_flush=None):
    with pytest.MonkeyPatch.context() as mp:
        metadata,faces,embeddings,lookup,fence=world.__wrapped__(mp)
        # Realistic 512-dimensional embeddings; deterministic input/canonical output.
        inputs=[dict(_face(i*20),embedding=[((j+i)%31-15)/100 for j in range(512)]) for i in range(face_count)]
        if seed:
            with fencing.result_fence(fence):storage._store_client_face_entities('lib','photo.jpg',inputs)
            for row in faces.rows.values():row.update(personId='curated-person',confirmedByUser=True,reviewStatus='confirmed')
        counts=Counter()
        for name,table in [('metadata',metadata),('face',faces),('embedding',embeddings),('lookup',lookup)]:
            wrapped=TableMeter(table,counts,name,delay)
            mp.setitem(storage._CTX,{'metadata':'metadata_table_client','face':'face_table_client',
                'embedding':'face_embeddings_table_client','lookup':'face_by_filename_table_client'}[name],wrapped)
            if name=='metadata':fence=fencing.ResultFence(wrapped,fence.library,fence.filename,
                fence.revision,fence.attempt,fence.owner,fence.photo,generation=fence.generation)
        callback=(lambda:before_flush(metadata,faces,embeddings,lookup)) if before_flush else None
        started,cpu=time.perf_counter(),time.process_time()
        with batched_writes(mp,mode,before_flush=callback),fencing.result_fence(fence):
            ids=storage._store_client_face_entities('lib','photo.jpg',inputs)
        elapsed,cpu_used=time.perf_counter()-started,time.process_time()-cpu
        canonical=dict(faces=sorted([dict(r) for r in faces.rows.values()],key=lambda r:r['RowKey']),
            embeddings=sorted([dict(r) for r in embeddings.rows.values()],key=lambda r:r['RowKey']),
            lookup_ids=json.loads(lookup.rows['lib','photo.jpg']['faceIds']))
        # Timestamps/generation are expected to change, output identity/curation is not.
        for rows in (canonical['faces'],canonical['embeddings']):
            for row in rows:
                for key in ('updatedAt','createdAt','Timestamp'):row.pop(key,None)
        fingerprint=hashlib.sha256(json.dumps(canonical,sort_keys=True,default=str).encode()).hexdigest()
        assert len(ids)==face_count and lookup.rows['lib','photo.jpg']['state']=='complete'
        return dict(faces=face_count,mode=mode,seeded_curated=seed,simulated_call_delay_ms=delay,
            wall_seconds=elapsed,cpu_seconds=cpu_used,requests=sum(counts.values()),calls=dict(counts),
            output_digest=fingerprint)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--rounds',type=int,default=5)
    args=parser.parse_args();reports=[];reference={}
    for delay in (0,10):
        for seeded in (False,True):
            for faces in (0,1,8):
                for round_number in range(args.rounds):
                    modes=['current','batch_only','batch_guard']
                    for mode in modes if round_number%2==0 else reversed(modes):
                        row=execute(faces,mode,delay,seed=seeded);row['round']=round_number
                        key=(seeded,faces)
                        reference.setdefault(key,row['output_digest'])
                        if reference[key]!=row['output_digest']:raise RuntimeError('Face output mismatch')
                        reports.append(row)
                print(f'Completed delay={delay}, seeded={seeded}, faces={faces}',flush=True)
    args.output.write_text(json.dumps(dict(scope='Local ETag-aware table doubles; per-call delays simulated; no Azure IO',
        output_parity=True,reports=reports),indent=2)+'\n')


if __name__=='__main__':main()
