import json
from collections import Counter

import pytest
from azure.core.exceptions import ServiceResponseError, ResourceModifiedError
import storage_utils as storage
from processing_artifacts import publish, promote_prepared, verify_receipt
from processing_fence import result_fence, ResultFence
from processing_completion import StaleProcessingResult
from test_processing_artifacts import blobs, jpeg
from test_processing_fence import world
from test_face_by_filename_lookup import _face
from test_face_persistence_benchmark import bench


def test_upload_ack_receipt_default_and_verification_reuse(blobs, monkeypatch):
    (metadata,*_,fence),objects=blobs
    monkeypatch.delenv('IPWORK_ARTIFACT_UPLOAD_RESPONSE_ENABLED')
    row=metadata.get_entity('lib','photo.jpg')
    with result_fence(fence):
        receipt=publish(storage,row,'preview',data=jpeg())
        assert objects[receipt['blob']].gets==0
        assert verify_receipt(storage,row,'preview',reuse_published=True)==receipt
        assert objects[receipt['blob']].gets==0
    assert objects[receipt['blob']].data==jpeg()


def test_upload_ack_conflict_and_missing_etag_fall_back(blobs,monkeypatch):
    (metadata,*_,fence),objects=blobs
    monkeypatch.setenv('IPWORK_ARTIFACT_UPLOAD_RESPONSE_ENABLED','true')
    row=metadata.get_entity('lib','photo.jpg')
    with result_fence(fence):
        receipt=publish(storage,row,'preview',data=jpeg())
        assert publish(storage,row,'preview',data=jpeg())==receipt
    assert objects[receipt['blob']].gets==1
    from test_processing_artifacts import Blob
    original=Blob.upload_blob
    def no_etag(self,*args,**kwargs):original(self,*args,**kwargs);return {}
    monkeypatch.setattr(Blob,'upload_blob',no_etag)
    with result_fence(fence):receipt=publish(storage,row,'thumbnail',data=jpeg())
    assert objects[receipt['blob']].gets==1


def test_ambiguous_upload_never_returns_receipt(blobs,monkeypatch):
    (metadata,*_,fence),objects=blobs
    monkeypatch.setenv('IPWORK_ARTIFACT_UPLOAD_RESPONSE_ENABLED','true')
    from test_processing_artifacts import Blob
    original=Blob.upload_blob
    def lose(self,*args,**kwargs):original(self,*args,**kwargs);raise ServiceResponseError('lost response')
    monkeypatch.setattr(Blob,'upload_blob',lose)
    row=metadata.get_entity('lib','photo.jpg')
    with result_fence(fence),pytest.raises(ServiceResponseError):publish(storage,row,'preview',data=jpeg())
    assert 'previewArtifact' not in row
    monkeypatch.setattr(Blob,'upload_blob',original)
    with result_fence(fence):receipt=publish(storage,row,'preview',data=jpeg())
    assert objects[receipt['blob']].gets==1


@pytest.mark.parametrize('revision',['rev','changed'])
def test_preview_promotion_reuses_only_matching_revision(blobs,monkeypatch,revision):
    from ipwork_image import crop_input_job,remember_crop_preview
    from test_processing_artifacts import Blob
    (metadata,*_,fence),objects=blobs
    row=metadata.get_entity('lib','photo.jpg');row.update(preview_status='done')
    source=Blob();source.data=jpeg();source.version=1;objects['preview/photo.jpg']=source
    with crop_input_job('lib','photo.jpg'),result_fence(fence):
        remember_crop_preview('lib','photo.jpg',dict(row,processingRevision=revision),jpeg(),40,40)
        promote_prepared(storage,row)
    assert source.gets==int(revision!='rev')


def batch_world(world,monkeypatch):
    metadata,faces,embeddings,lookup,fence=world
    counts=Counter()
    for name,table,key in [('face',faces,'face_table_client'),('embedding',embeddings,'face_embeddings_table_client')]:
        monkeypatch.setitem(storage._CTX,key,bench.TableMeter(table,counts,name,0))
    return metadata,faces,embeddings,lookup,fence,counts


def test_multiface_transactions_publish_lookup_last(world,monkeypatch):
    metadata,faces,embeddings,lookup,fence,counts=batch_world(world,monkeypatch)
    checks=[];original=ResultFence.check
    def check(self):checks.append(1);original(self)
    monkeypatch.setattr(ResultFence,'check',check)
    with result_fence(fence):ids=storage._store_client_face_entities('lib','photo.jpg',[_face(0),_face(40)])
    assert len(ids)==2 and counts['face.transaction']==counts['embedding.transaction']==1
    assert len(checks)==4
    assert lookup.rows['lib','photo.jpg']['state']=='complete'
    assert set(json.loads(lookup.rows['lib','photo.jpg']['faceIds']))==set(ids)


def test_partial_cross_table_failure_does_not_publish_complete_lookup(world,monkeypatch):
    metadata,faces,embeddings,lookup,fence,counts=batch_world(world,monkeypatch)
    table=storage._CTX['face_embeddings_table_client'];original=table.submit_transaction
    def replace(ops):
        original(ops)
        metadata.rows['lib','photo.jpg']['processingRevision']='successor'
    monkeypatch.setattr(table,'submit_transaction',replace)
    with result_fence(fence),pytest.raises(StaleProcessingResult):
        storage._store_client_face_entities('lib','photo.jpg',[_face(0),_face(40)])
    assert embeddings.rows and not faces.rows
    assert lookup.rows['lib','photo.jpg']['state']!='complete'


def test_batch_toggle_retains_row_writes(world,monkeypatch):
    *_,fence,counts=batch_world(world,monkeypatch)
    monkeypatch.setenv('IPWORK_FACE_PERSIST_BATCH_ENABLED','false')
    with result_fence(fence):storage._store_client_face_entities('lib','photo.jpg',[_face(0),_face(40)])
    assert not counts['face.transaction'] and counts['face.create']==2


def test_upload_ack_checks_source_before_receipt_publication(blobs,monkeypatch):
    (metadata,*_,fence),objects=blobs
    monkeypatch.setenv('IPWORK_ARTIFACT_UPLOAD_RESPONSE_ENABLED','true')
    from test_processing_artifacts import Blob
    original=Blob.upload_blob
    def replace(self,*args,**kwargs):
        response=original(self,*args,**kwargs)
        metadata.rows['lib','photo.jpg']['processingRevision']='successor'
        return response
    monkeypatch.setattr(Blob,'upload_blob',replace)
    row=metadata.get_entity('lib','photo.jpg')
    with result_fence(fence),pytest.raises(StaleProcessingResult):publish(storage,row,'preview',data=jpeg())
    assert 'previewArtifact' not in row


def test_production_batches_preserve_concurrent_curation(world,monkeypatch):
    metadata,faces,embeddings,lookup,fence,counts=batch_world(world,monkeypatch)
    with result_fence(fence):storage._store_client_face_entities('lib','photo.jpg',[_face(0),_face(40)])
    table=storage._CTX['face_embeddings_table_client'];original=table.submit_transaction
    def curate(ops):
        original(ops)
        row=next(iter(faces.rows.values()))
        faces.upsert_entity(dict(row,personId='new-curation',confirmedByUser=True))
    monkeypatch.setattr(table,'submit_transaction',curate)
    with result_fence(fence),pytest.raises(ResourceModifiedError):
        storage._store_client_face_entities('lib','photo.jpg',[_face(0),_face(40)])
    assert any(row.get('personId')=='new-curation' for row in faces.rows.values())
    assert lookup.rows['lib','photo.jpg']['state']!='complete'


def test_face_transactions_split_at_100_and_clear_scope(world):
    from face_write_batch import face_write_batch,flush
    from processing_fence import write_artifact
    metadata,faces,embeddings,lookup,fence=world
    counts=Counter();table=bench.TableMeter(faces,counts,'face',0)
    guards=[]
    with result_fence(fence),face_write_batch(True,lambda:guards.append(1)):
        for index in range(101):
            write_artifact(table,dict(PartitionKey='lib',RowKey=str(index),filename='photo.jpg'),previous_known=True)
        assert not faces.rows
        flush()
    assert len(faces.rows)==101 and counts['face.transaction']==2 and len(guards)==2
    with result_fence(fence):
        write_artifact(table,dict(PartitionKey='lib',RowKey='direct',filename='photo.jpg'),previous_known=True)
    assert counts['face.create']==1
