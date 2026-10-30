"""Benchmark-only immutable embedding publication; no production imports.

Publish an immutable vector proof atomically with the compatibility embedding
row, reference it in the existing face write, and reuse the proof during capture.
Face/lookup/source reads remain fresh. This introduces a new data contract:
embedding-changing writers must update the face proof pointer or invalidate it;
legacy embedding rows alone cannot override a referenced immutable proof.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import argparse
import json
import os
from pathlib import Path
import benchmark_vision_persistence as base
from processing_receipt_cache import committed_receipt_scope
from azure.data.tables import TableTransactionError
from azure.core.exceptions import ResourceExistsError

_WRITING=ContextVar('proof_benchmark_writing',default=False)
_CAPTURING=ContextVar('proof_benchmark_capture',default=None)


def encoded(row):
    return json.dumps(dict(row),sort_keys=True,separators=(',',':'),allow_nan=False)


class Proofs:
    def __init__(self,limit=2*1024*1024):
        import threading
        self.rows={};self.current={};self.bytes=0;self.limit=limit;self.lock=threading.Lock()
        self.hits=0;self.fallbacks=0
    def remember(self,row):
        body=encoded(row);identity=base.hashlib.sha256(body.encode()).hexdigest()
        with self.lock:
            self.current[row['PartitionKey'],row['RowKey']]=identity
            if identity not in self.rows and self.bytes+len(body.encode())+256<=self.limit:
                self.rows[identity]=body;self.bytes+=len(body.encode())+256
        return identity,body
    def get(self,identity):
        with self.lock:
            body=self.rows.get(identity)
        if body is None:return None
        if base.hashlib.sha256(body.encode()).hexdigest()!=identity:
            raise ValueError('Invalid cached immutable embedding proof')
        return json.loads(body)


class Proxy:
    def __init__(self,table):self.table=table
    def __getattr__(self,name):return getattr(self.table,name)


class Embeddings(Proxy):
    def __init__(self,table,proofs):super().__init__(table);self.proofs=proofs
    def commit(self,kind,entity,**kwargs):
        if not _WRITING.get():return getattr(self.table,kind+'_entity')(entity,**kwargs)
        body=encoded(entity);identity=base.hashlib.sha256(body.encode()).hexdigest()
        proof=dict(PartitionKey=entity['PartitionKey'],RowKey='embedding-proof!'+identity,payload=body)
        operation=(kind,entity,kwargs) if kwargs else (kind,entity)
        try:self.table.submit_transaction([operation,('create',proof)])
        except (ResourceExistsError,TableTransactionError) as exc:
            if not isinstance(exc,ResourceExistsError) and getattr(exc,'status_code',None)!=409:raise
            # A duplicate immutable proof must be checked durably. Preserve the
            # original mutable artifact ETag; never upgrade it after conflict.
            previous=self.table.get_entity(proof['PartitionKey'],proof['RowKey'])
            if previous.get('payload')!=body:raise ValueError('Embedding proof collision')
            self.table.submit_transaction([operation])
        self.proofs.remember(entity) # only after durable transaction success
    def create_entity(self,entity,**kwargs):return self.commit('create',entity,**kwargs)
    def update_entity(self,entity,**kwargs):return self.commit('update',entity,**kwargs)
    def get_entity(self,partition_key,row_key):
        selected=_CAPTURING.get()
        identity=selected.get((partition_key,row_key)) if selected is not None else None
        if identity:
            row=self.proofs.get(identity)
            if row is not None:
                if (row.get('PartitionKey'),row.get('RowKey'))!=(partition_key,row_key):
                    raise ValueError('Embedding proof scope mismatch')
                self.proofs.hits+=1;return row
            # Eviction needs the immutable proof, not a possibly changed mutable
            # compatibility row. Hash/scope/source validation remain mandatory.
            proof=self.table.get_entity(partition_key,'embedding-proof!'+identity)
            body=proof['payload']
            if base.hashlib.sha256(body.encode()).hexdigest()!=identity:
                raise ValueError('Invalid durable embedding proof')
            row=json.loads(body)
            if (row.get('PartitionKey'),row.get('RowKey'))!=(partition_key,row_key):
                raise ValueError('Embedding proof scope mismatch')
            self.proofs.fallbacks+=1;return row
        return self.table.get_entity(partition_key,row_key)


class Faces(Proxy):
    def __init__(self,table,proofs):super().__init__(table);self.proofs=proofs
    def row(self,entity):
        if not _WRITING.get():return entity
        row=dict(entity);identity=self.proofs.current.get((row['PartitionKey'],row['RowKey']))
        if identity:row['embeddingProofId']=identity
        return row
    def create_entity(self,entity,**kwargs):return self.table.create_entity(self.row(entity),**kwargs)
    def update_entity(self,entity,**kwargs):return self.table.update_entity(self.row(entity),**kwargs)
    def get_entity(self,partition_key,row_key):
        row=self.table.get_entity(partition_key,row_key) # NEVER reuse mutable curation
        selected=_CAPTURING.get()
        if selected is not None and row.get('embeddingProofId'):
            selected[partition_key,row_key]=row['embeddingProofId']
        return row


@contextmanager
def prototype(enabled,proofs):
    configure=base.configure;store=base.storage._store_client_face_entities;capture=base.ArtifactReceipts.capture
    def configured(tables):
        configure(tables)
        if enabled:
            base.storage._CTX['face_embeddings_table_client']=Embeddings(tables['embeddings'],proofs)
            base.storage._CTX['face_table_client']=Faces(tables['faces'],proofs)
    def persist(user,filename,faces,**kwargs):
        token=_WRITING.set(enabled and len(faces)==1)
        try:return store(user,filename,faces,**kwargs)
        finally:_WRITING.reset(token)
    def captured(self,storage,metadata):
        token=_CAPTURING.set({} if enabled and metadata.get('faceCount',0)==1 else None)
        try:return capture(self,storage,metadata)
        finally:_CAPTURING.reset(token)
    # Multi-face batching bypasses proxy create/update via submit_transaction.
    # Scope this first experiment to the dominant one-face path; other counts
    # remain unchanged controls. Do not accidentally disable production batching.
    with base.patch(base,'configure',configured),base.patch(base.storage,'_store_client_face_entities',persist),base.patch(base.ArtifactReceipts,'capture',captured),base.patch(base,'receipt_scope',committed_receipt_scope):
        yield


def run(tables,*,mode,cache_bytes=2*1024*1024,**kwargs):
    proofs=Proofs(cache_bytes)
    with prototype(mode=='proof',proofs):
        row=base.execute(tables,mode='current',**kwargs)
    return row|dict(mode=mode,embedding_proof_cache_hits=proofs.hits,embedding_proof_fallbacks=proofs.fallbacks,embedding_proof_cache_bytes=proofs.bytes)


def main():
    from azure.data.tables import TableServiceClient
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True);parser.add_argument('--images',type=int,default=32);parser.add_argument('--rounds',type=int,default=4);args=parser.parse_args()
    service=TableServiceClient.from_connection_string(os.environ['BENCHMARK_STORAGE_CONNECTION']);tables={n:service.get_table_client('bench'+n) for n in ('metadata','faces','embeddings','lookup','jobs')}
    base.storage._LOGGER.setLevel('ERROR');reports=[];references={}
    for faces,seeded in [(0,False),(1,False),(1,True)]:
        for rnd in range(args.rounds):
            for mode in (['production','proof'] if rnd%2==0 else ['proof','production']):
                row=run(tables,mode=mode,face_count=faces,seeded=seeded,images=args.images,group=8)
                references.setdefault((faces,seeded),row['output_digest']);assert references[faces,seeded]==row['output_digest']
                row['round']=rnd;reports.append(row);args.output.write_text(json.dumps(dict(scope=__doc__,output_parity=True,reports=reports),indent=2))
                print('ROUND '+json.dumps({k:row[k] for k in ('mode','faces','seeded','round','wall_seconds','requests','embedding_proof_cache_hits')}),flush=True)

if __name__=='__main__':main()
