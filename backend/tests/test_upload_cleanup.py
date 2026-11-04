"""Manual upload cleanup preserves committed photos and retryable failures."""
import json
import re
import time
from types import SimpleNamespace
from azure.core.exceptions import ResourceNotFoundError
import pytest
import app
import storage_utils
from upload_cleanup import cleanup
from upload_waves import Conflict
from test_wave_live_routes import live_api
from upload_session_fakes import RegistryTable


class Metadata(RegistryTable):
    def create_entity(self, row):
        self.submit_transaction([('create', row)])
    def update_entity(self, row, **options):
        self.submit_transaction([('update', row, options)])
    def delete_entity(self, partition_key, row_key, **options):
        self.submit_transaction([('delete', dict(PartitionKey=partition_key, RowKey=row_key), options)])
    def query_entities(self, query, **options):
        pk = re.search(r"PartitionKey eq '((?:[^']|'')*)'", query)[1].replace("''", "'")
        rows = [self.get_entity(p,k) for p,k in sorted(self.rows) if p==pk]
        blob = re.search(r"anonymousImageId eq '((?:[^']|'')*)'", query)
        if blob:
            rows = [row for row in rows if row.get('anonymousImageId')==blob[1].replace("''", "'")]
        else:
            rows = [row for row in rows if any(field in row for field in app.UPLOAD_TRACKING_FIELDS)]
        after = re.search(r"RowKey gt '((?:[^']|'')*)'", query)
        if after:
            rows = [row for row in rows if row['RowKey']>after[1].replace("''", "'")]
        return iter(rows[:options.get('results_per_page', 1000)])


@pytest.fixture
def env(live_api, monkeypatch):
    client,store,_,library=live_api
    metadata=Metadata()
    monkeypatch.setattr(app,'metadata_table_client',metadata)
    monkeypatch.setattr(app,'blob_service_client',object())
    monkeypatch.setattr(app,'image_names_table_client',None)
    original=store.table.query_entities
    def query(q,**options):
        rows=list(original(q,**options))
        if "state eq 'open'" in q:
            rows=[row for row in rows if row.get('state')=='open']
        return iter(rows)
    monkeypatch.setattr(store.table,'query_entities',query)
    deleted=[]
    monkeypatch.setattr(app,'_delete_blob_if_present',lambda container,name:deleted.append((container,name)))
    return client,store,metadata,deleted,library


def seed(metadata,name='unfinished.jpg',**fields):
    metadata.create_entity(dict(PartitionKey='u',RowKey=name,pendingAnonymousBlob='pending-'+name,**fields))


def test_cleanup_survives_missing_directory_and_preserves_completed_photos(env):
    client,store,metadata,deleted,_=env
    seed(metadata)
    seed(metadata,'completed.jpg',anonymousImageId='saved',fileHash='abc',exif='retained')
    metadata.create_entity(dict(PartitionKey='other',RowKey='other.jpg',pendingAnonymousBlob='private'))
    response=client.post('/upload/cleanup',json={})
    assert response.status_code==200 and response.json['deleted']==1
    assert response.json['trackingCleared']==1
    assert ('u','unfinished.jpg') not in metadata.rows
    saved=metadata.get_entity('u','completed.jpg')
    assert saved['anonymousImageId']=='saved' and saved['exif']=='retained'
    assert 'pendingAnonymousBlob' not in saved
    assert ('other','other.jpg') in metadata.rows
    assert (app.BLOB_THUMBNAIL_CONTAINER,'preview/pending-unfinished.jpg.jpg') in deleted
    assert not any(name=='saved' for _,name in deleted)
    assert client.post('/upload/cleanup',json={}).json['deleted']==0
    assert not store._required('policy').get('gcOwner')


def test_blob_failure_retains_identity_then_retry_cleans_it(env,monkeypatch):
    client,store,metadata,deleted,_=env
    seed(metadata)
    monkeypatch.setattr(app,'_delete_blob_if_present',lambda container,name:'delete failed' if name.startswith('preview/') else None)
    response=client.post('/upload/cleanup',json={})
    assert response.status_code==207 and not response.json['success']
    row=metadata.get_entity('u','unfinished.jpg')
    assert json.loads(row['uploadCleanupBlobs'])==['pending-unfinished.jpg']
    assert row['pendingAnonymousBlob']=='pending-unfinished.jpg'
    monkeypatch.setattr(app,'_delete_blob_if_present',lambda *args:None)
    response=client.post('/upload/cleanup',json={})
    assert response.status_code==200 and response.json['deleted']==1


def test_new_upload_allocates_new_blob_after_interrupted_cleanup(env,monkeypatch):
    client,store,metadata,deleted,_=env
    seed(metadata,uploadCleanupBlobs=json.dumps(['pending-unfinished.jpg']))
    monkeypatch.setitem(storage_utils._CTX,'metadata_table_client',metadata)
    monkeypatch.setattr(storage_utils,'_require_context',lambda:None)
    # Legacy reservation helper also must not reuse a cleanup-owned blob.
    metadata.upsert_entity=lambda row:metadata.create_entity(row) if (row['PartitionKey'],row['RowKey']) not in metadata.rows else metadata.update_entity(row,etag=metadata.get_entity(row['PartitionKey'],row['RowKey']).metadata['etag'],match_condition=object())
    reserved=storage_utils.reserve_pending_anonymous_blob('u','unfinished.jpg')
    assert reserved!='pending-unfinished.jpg'
    assert json.loads(metadata.get_entity('u','unfinished.jpg')['uploadCleanupBlobs'])==['pending-unfinished.jpg']


def test_open_session_blocks_cleanup_and_no_blobs_deleted(env):
    client,store,metadata,deleted,_=env
    store.open_session('active',1,lease_seconds=300)
    seed(metadata)
    response=client.post('/upload/cleanup',json={})
    assert response.status_code==409 and not deleted
    assert ('u','unfinished.jpg') in metadata.rows


def test_recent_and_processing_rows_are_skipped(env):
    client,store,metadata,deleted,_=env
    seed(metadata,'recent.jpg',upload_started_at=app.datetime.now(app.timezone.utc).isoformat())
    seed(metadata,'processing.jpg',processing_lease_owner='worker',processing_lease_expires_at=app.datetime.fromtimestamp(time.time()+600,app.timezone.utc).isoformat())
    response=client.post('/upload/cleanup',json={})
    assert response.status_code==200 and response.json['skipped']==2 and not deleted


def test_finalized_renamed_blob_is_protected(env):
    client,store,metadata,deleted,_=env
    seed(metadata)
    metadata.create_entity(dict(PartitionKey='u',RowKey='renamed.jpg',anonymousImageId='pending-unfinished.jpg',fileHash='real'))
    response=client.post('/upload/cleanup',json={})
    assert response.status_code==200 and response.json['deleted']==1
    assert not deleted and ('u','renamed.jpg') in metadata.rows


def test_cleanup_pages_do_not_require_browser_checkpoint(env):
    client,store,metadata,deleted,_=env
    for i in range(28):seed(metadata,f'{i:03}.jpg')
    first=client.post('/upload/cleanup',json={})
    assert first.status_code==200 and first.json['deleted']==25 and first.json['more']
    second=client.post('/upload/cleanup',json={'after':first.json['nextCursor']})
    assert second.status_code==200 and second.json['deleted']==3 and not second.json['more']
    assert not metadata.rows


def test_only_owner_can_clean_library(env,monkeypatch):
    client,store,metadata,deleted,_=env
    monkeypatch.setattr(app.library_store,'is_owner',lambda *args:False)
    response=client.post('/upload/cleanup',json={})
    assert response.status_code==403 and not deleted


def test_active_processing_stage_blocks_cleanup(env):
    from execution_lease import ExecutionLeases
    client,store,metadata,deleted,_=env
    seed(metadata)
    leases=ExecutionLeases(store.table,partition=store.partition)
    leases.claim('u','wave','vision','input')
    response=client.post('/upload/cleanup',json={})
    assert response.status_code==409 and not deleted


def test_new_intake_racing_cleanup_claim_is_preserved(env):
    client,store,metadata,deleted,_=env
    seed(metadata)
    store.table.before_commit=lambda:store.open_session('racing',1,lease_seconds=300)
    response=client.post('/upload/cleanup',json={})
    assert response.status_code==409 and not deleted
    assert ('u','unfinished.jpg') in metadata.rows
    assert not store._required('policy').get('gcOwner')


def test_browser_cancel_cannot_erase_manual_cleanup_retry_record(env,monkeypatch):
    client,store,metadata,deleted,_=env
    seed(metadata,uploadCleanupBlobs=json.dumps(['pending-unfinished.jpg']))
    monkeypatch.setattr(app,'_delete_upload_temp_files_for_filename',lambda *args:([],[]))
    result=app._cleanup_failed_upload('u','unfinished.jpg')
    assert result['errors'] and result['metadataAction']=='none'
    assert ('u','unfinished.jpg') in metadata.rows and not deleted


def test_finalized_source_racing_cleanup_marker_is_never_deleted(env,monkeypatch):
    client,store,metadata,deleted,_=env
    seed(metadata)
    original=metadata.update_entity
    finalized=[False]
    def mark_then_finalize(row,**options):
        original(row,**options)
        if row.get('uploadCleanupBlobs') and not finalized[0]:
            finalized[0]=True
            latest=metadata.get_entity('u','unfinished.jpg')
            original(dict(latest,anonymousImageId='pending-unfinished.jpg',fileHash='committed'),
                etag=latest.metadata['etag'],match_condition=object())
    monkeypatch.setattr(metadata,'update_entity',mark_then_finalize)
    response=client.post('/upload/cleanup',json={})
    assert response.status_code==200 and response.json['trackingCleared']==1
    assert metadata.get_entity('u','unfinished.jpg')['fileHash']=='committed'
    assert not deleted


def test_metadata_delete_failure_keeps_blob_cleanup_identity(env,monkeypatch):
    from azure.core.exceptions import AzureError
    client,store,metadata,deleted,_=env
    seed(metadata)
    original=metadata.delete_entity
    monkeypatch.setattr(metadata,'delete_entity',lambda *args,**kw:(_ for _ in ()).throw(AzureError('delete failed')))
    response=client.post('/upload/cleanup',json={})
    assert response.status_code==207 and deleted
    assert metadata.get_entity('u','unfinished.jpg')['uploadCleanupBlobs']
    monkeypatch.setattr(metadata,'delete_entity',original)
    assert client.post('/upload/cleanup',json={}).json['deleted']==1
