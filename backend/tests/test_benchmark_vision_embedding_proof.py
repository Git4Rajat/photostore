import sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import benchmark_vision_embedding_proof as b
from test_face_by_filename_lookup import FilenameLookupTable
from test_upload_waves import AtomicTable


def tables():
    t={n:FilenameLookupTable() for n in ('metadata','faces','embeddings','lookup')};t['jobs']=AtomicTable();return t


@pytest.mark.parametrize('faces,seeded',[(0,False),(1,False),(2,False),(8,False),(1,True)])
def test_outputs_and_curation_match_production(faces,seeded):
    a=b.run(tables(),mode='production',face_count=faces,seeded=seeded,images=8,group=8)
    c=b.run(tables(),mode='proof',face_count=faces,seeded=seeded,images=8,group=8)
    assert a['output_digest']==c['output_digest']
    assert c['embedding_proof_cache_hits']==(8 if faces==1 else 0)
    if faces==1 and not seeded:assert a['requests']-c['requests']==8


def test_overflow_reads_durable_immutable_proof():
    a=b.run(tables(),mode='production',face_count=1,images=4,group=4)
    c=b.run(tables(),mode='proof',face_count=1,images=4,group=4,cache_bytes=0)
    assert a['output_digest']==c['output_digest']
    assert c['embedding_proof_cache_hits']==0
    assert c['embedding_proof_fallbacks']==4


def test_failed_transaction_never_remembers_proof(monkeypatch):
    table=FilenameLookupTable();proofs=b.Proofs();proxy=b.Embeddings(table,proofs)
    monkeypatch.setattr(table,'submit_transaction',lambda *a,**k:(_ for _ in ()).throw(OSError('ambiguous write')))
    token=b._WRITING.set(True)
    try:
        with pytest.raises(OSError):proxy.create_entity(dict(PartitionKey='lib',RowKey='face',embedding='[1]'))
    finally:b._WRITING.reset(token)
    assert not proofs.rows and not proofs.current


def test_capture_sees_fresh_curation(monkeypatch):
    original=b.base.ArtifactReceipts.capture
    def edited(self,storage,metadata):
        table=storage._CTX['face_table_client']
        if isinstance(table,b.Faces):
            raw=table.table
            # Read identities from fresh complete lookup, then change curation
            # without changing that lookup (as real labeling routes can do).
            lookup=storage._CTX['face_by_filename_table_client'].get_entity(self.library,metadata['RowKey'])
            import json
            for fid in json.loads(lookup['faceIds']):
                row=dict(raw.get_entity(self.library,fid));row['personId']='fresh-label';raw.upsert_entity(row)
        return original(self,storage,metadata)
    monkeypatch.setattr(b.base.ArtifactReceipts,'capture',edited)
    t=tables();b.run(t,mode='proof',face_count=1,images=1,group=1)
    import json
    snapshots=[json.loads(row['payload']) for row in t['jobs'].rows.values() if row['RowKey'].startswith('face!')]
    assert snapshots and snapshots[0]['personId']=='fresh-label'


def test_cached_proof_checks_scope_and_payload():
    proofs=b.Proofs();identity,_=proofs.remember(dict(PartitionKey='other',RowKey='face',embedding='[1]'))
    proxy=b.Embeddings(FilenameLookupTable(),proofs);token=b._CAPTURING.set({('lib','face'):identity})
    try:
        with pytest.raises(ValueError,match='scope mismatch'):proxy.get_entity('lib','face')
        proofs.rows[identity]='{}'
        with pytest.raises(ValueError,match='Invalid cached'):proxy.get_entity('lib','face')
    finally:b._CAPTURING.reset(token)
