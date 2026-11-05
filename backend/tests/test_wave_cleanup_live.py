import json
from types import SimpleNamespace
from datetime import datetime,timedelta,timezone
import pytest
from azure.core.exceptions import ResourceNotFoundError
from test_wave_recovery import RuntimeTable
from test_wave_activation import active
from upload_waves import digest,Conflict
from processing_cleanup import (fence_photo_deletion,collect_library_artifacts,
    begin_library_cleanup,finish_library_cleanup,check_source_policy)


def world():
    table=RuntimeTable();metadata=RuntimeTable();store=active(table)
    storage=SimpleNamespace(_CTX={},_get_blob_client=lambda *a:None)
    return table,metadata,store,storage


def source(metadata,**fields):
    metadata.create_entity(dict(PartitionKey='u',RowKey='photo.jpg',anonymousImageId='p',processingRevision='r',**fields))


def manifest(table,name,*,snapshots=()):
    row=dict(PartitionKey='processing-artifacts-'+digest('u'),RowKey='manifest!'+name,
        payload=json.dumps(dict(library='u',filename='photo.jpg',photoId='p',revision='r',derivatives={},faceSnapshots=list(snapshots))))
    table.create_entity(row)
    for snapshot in snapshots:
        if (row['PartitionKey'],'face!'+snapshot) not in table.rows:
            table.create_entity(dict(PartitionKey=row['PartitionKey'],RowKey='face!'+snapshot,payload='{}'))
    return row


def test_delete_fences_source_before_physical_cascade_and_leaves_durable_witness():
    table,metadata,store,storage=world();source(metadata)
    fence_photo_deletion(table,metadata,'u',['photo.jpg'])
    assert metadata.get_entity('u','photo.jpg')['processing_state']=='deleted'
    assert store._page('deleted-source!')[0]['photoId']=='p'
    assert store._required('policy')['gcRequested']


def test_live_source_lease_prevents_deletion():
    table,metadata,store,storage=world()
    source(metadata,processing_lease_owner='worker',processing_lease_expires_at=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat())
    with pytest.raises(Conflict,match='leased'):fence_photo_deletion(table,metadata,'u',['photo.jpg'])
    assert metadata.get_entity('u','photo.jpg').get('processing_state')!='deleted'


def test_gc_protects_shared_snapshots_and_metadata_receipt_then_resumes():
    table,metadata,store,storage=world()
    retained=manifest(table,'retained',snapshots=['shared'])
    orphan=manifest(table,'orphan',snapshots=['shared'])
    source(metadata,artifactReceipt=json.dumps({'id':'retained'}))
    assert collect_library_artifacts(table,storage,metadata,'u')['deletedManifests']==1
    assert table.get_entity(retained['PartitionKey'],'face!shared')
    assert table.get_entity(retained['PartitionKey'],retained['RowKey'])
    fence_photo_deletion(table,metadata,'u',['photo.jpg'])
    metadata.delete_entity('u','photo.jpg')
    collect_library_artifacts(table,storage,metadata,'u')
    with pytest.raises(ResourceNotFoundError):table.get_entity(retained['PartitionKey'],'face!shared')
    assert not store._required('policy').get('gcUntil')


def test_gc_failure_releases_policy_fence_without_claiming_success():
    table,metadata,store,storage=world()
    manifest(table,'corrupt')
    row=table.get_entity('processing-artifacts-'+digest('u'),'manifest!corrupt')
    table.update_entity(dict(row,payload='not-json'),etag=row.metadata['etag'],match_condition='IfNotModified')
    with pytest.raises(ValueError):collect_library_artifacts(table,storage,metadata,'u')
    assert not store._required('policy').get('gcUntil')


def test_cleanup_blocks_new_intake_and_cancels_unfinished_work_then_allows_new_library_content():
    from test_upload_waves import close
    table,metadata,store,storage=world()
    opened=store.open_session('s',0)
    with pytest.raises(Conflict,match='intake'):begin_library_cleanup(table,metadata,'u')
    close(store,'s',opened)
    source(metadata)
    begin_library_cleanup(table,metadata,'u')
    assert store.status(opened['waveId'])['state']=='cancelled'
    with pytest.raises(Conflict):store.open_session('blocked',0)
    with pytest.raises(RuntimeError):check_source_policy(table,'u')
    assert metadata.get_entity('u','photo.jpg')['processing_state']=='deleted'
    finish_library_cleanup(table,'u')
    assert store.open_session('new',0)['waveId']!=opened['waveId']


def test_frozen_members_prevent_destructive_deletion_until_publication():
    from test_upload_waves import close,finish_scan
    table,metadata,store,storage=world();source(metadata)
    session=store.open_session('s',1);store.register('s',[dict(itemId='i',filename='photo.jpg')])
    session=store.resolve('s','i',outcome='accepted',filename='photo.jpg',photo_id='p',processing_revision='r',required_steps=['face'])
    close(store,'s',session)
    store.observe(session['waveId'],'p','r',outcome='settled',evidence_id='test')
    finish_scan(store,session['waveId'])
    with pytest.raises(Conflict,match='frozen'):fence_photo_deletion(table,metadata,'u',['photo.jpg'])
    assert metadata.get_entity('u','photo.jpg').get('processing_state')!='deleted'


def test_deletion_cannot_race_readiness_freeze(monkeypatch):
    from test_upload_waves import close,finish_scan
    table,metadata,store,storage=world();source(metadata)
    session=store.open_session('s',1);store.register('s',[dict(itemId='i',filename='photo.jpg')])
    session=store.resolve('s','i',outcome='accepted',filename='photo.jpg',photo_id='p',processing_revision='r',required_steps=['face'])
    close(store,'s',session)
    store.observe(session['waveId'],'p','r',outcome='settled',evidence_id='old')
    original=metadata.update_entity
    def race(*args,**kw):
        with pytest.raises(Conflict):finish_scan(store,session['waveId'])
        return original(*args,**kw)
    monkeypatch.setattr(metadata,'update_entity',race)
    fence_photo_deletion(table,metadata,'u',['photo.jpg'])
    member=store._page(f'e!{session["waveId"]}!m!')[0]
    from wave_settlement import verified
    assert member['outcome']=='deleted' and verified(store,member)
    assert finish_scan(store,session['waveId'])['state']=='ready'


def test_gc_retries_exact_deleted_attempt_staging_without_touching_new_generation():
    table,metadata,store,storage=world()
    deleted=[]
    storage._CTX={'blob_thumbnail_container':'thumbs'}
    storage._get_blob_client=lambda container,name:SimpleNamespace(delete_blob=lambda:deleted.append(name))
    source(metadata,processingFence='attempt-old')
    fence_photo_deletion(table,metadata,'u',['photo.jpg'])
    metadata.delete_entity('u','photo.jpg')
    # A new source at the same filename owns a different generation.
    metadata.create_entity(dict(PartitionKey='u',RowKey='photo.jpg',anonymousImageId='p',processingRevision='new'))
    collect_library_artifacts(table,storage,metadata,'u')
    from processing_artifacts import staging_name
    assert set(deleted)=={staging_name('u','p','r','attempt-old',kind) for kind in ('thumbnail','preview')}


def test_gc_reference_pause_and_receipt_admission_are_one_conditional_write(monkeypatch):
    import processing_completion
    table,metadata,store,storage=world()
    monkeypatch.setattr(processing_completion,'validate_intent',lambda intent,library:intent)
    intent=dict(id='receipt',photoId='p',revision='r')
    def acquire_between_check_and_commit():
        policy=store._required('policy')
        table.update_entity(dict(policy,gcUntil=store.clock()+600),etag=policy.metadata['etag'],match_condition='IfNotModified')
    table.before_commit=acquire_between_check_and_commit
    with pytest.raises(Conflict):store.accept_source_completion(intent)
    assert not store._page('source-completion!')
