import io
import json
from types import SimpleNamespace

import pytest
from azure.core.exceptions import ResourceExistsError,ResourceModifiedError,ResourceNotFoundError

import storage_utils as su
from execution_lease import ExecutionLeases,execution_scope
from upload_waves import ShadowWaves
from test_upload_waves import AtomicTable,close,finish_scan
from fakes import FakeTable
from wave_catalog import CatalogPublisher,BuildGuard
from wave_publication import ObligationLedger,compute_obligations


class Blobs:
    def __init__(self):self.rows={};self.counter=0;self.fail=None
    def get_blob_client(self,container,blob):
        service=self
        class Blob:
            container_name=container
            blob_name=blob
            def get_blob_properties(self):
                try:data,version,settings,metadata=service.rows[container,blob]
                except KeyError:raise ResourceNotFoundError('missing')
                return SimpleNamespace(size=len(data),etag=version,content_settings=settings,metadata=metadata)
            def upload_blob(self,data,overwrite=False,etag=None,**kw):
                key=(container,blob)
                if service.fail and service.fail in blob:raise OSError('upload unavailable')
                if etag and (key not in service.rows or service.rows[key][1]!=etag):raise ResourceModifiedError('changed')
                if key in service.rows and not overwrite and etag is None:raise ResourceExistsError('exists')
                service.counter+=1
                service.rows[key]=(data.read() if hasattr(data,'read') else data,str(service.counter),kw.get('content_settings'),kw.get('metadata',{}))
                return {'etag': str(service.counter)}
            def download_blob(self,**kw):
                props=self.get_blob_properties()
                if kw.get('etag') and kw['etag']!=props.etag:raise ResourceModifiedError('changed')
                data=service.rows[container,blob][0]
                return SimpleNamespace(readall=lambda:data,chunks=lambda:iter([data]))
        return Blob()


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setenv('APP_ROLE','index-worker')
    monkeypatch.setattr(su,'_ROLE_MAY_BUILD_INDEXES',True)
    monkeypatch.setattr(su,'INDEX_DISK_CACHE_DIR','')
    persons,faces=FakeTable(),FakeTable()
    blobs=Blobs()
    for name,value in [('person_table_client',persons),('face_table_client',faces),
        ('blob_service_client',blobs),('blob_lexical_index_container','lexical-index')]:
        monkeypatch.setitem(su._CTX,name,value)
    persons.upsert_entity(dict(PartitionKey='u',RowKey='person',name='Alice',faceIds='["f"]'))
    faces.upsert_entity(dict(PartitionKey='u',RowKey='f',filename='photo.jpg',personId='person',bbox='{}'))
    store=ShadowWaves(AtomicTable(),'u'); opened=store.open_session('s',0);close(store,'s',opened)
    wave=finish_scan(store,opened['waveId'])
    store.commit_assignment(wave['waveId'],'frontier',manifest_digest=wave['manifestDigest'])
    leases=ExecutionLeases(store.table,partition=store.partition)
    ownership=leases.claim('u',wave['waveId'],'index','frontier')
    def check():leases.check(ownership)
    return store,wave,leases,ownership,blobs,check


def test_real_people_publisher_produces_verified_generation_and_replays_without_build(world,monkeypatch):
    store,wave,leases,owner,blobs,check=world
    publisher=CatalogPublisher(store,wave['waveId'],su,check)
    with execution_scope(leases,owner,store,check):
        generation=publisher.publish('people','a'*64)
        monkeypatch.setattr(su,'refresh_user_people_index',lambda *a,**k:pytest.fail('replayed builder'))
        assert publisher.publish('people','a'*64)==generation
    immutable=[key for key in blobs.rows if key[1].startswith('wave-projections/')]
    assert len(immutable)==2
    manifest=su._load_people_index_manifest('u')
    assert manifest['sourceVersion']=='a'*64


def test_swallowed_blob_failure_cannot_create_receipt(world):
    store,wave,leases,owner,blobs,check=world
    blobs.fail='people'
    with execution_scope(leases,owner,store,check):
        with pytest.raises((RuntimeError,OSError)):
            CatalogPublisher(store,wave['waveId'],su,check).publish('people','a'*64)
    assert not store._get(f"publication!{wave['waveId']}!"+'a'*64)


def test_missing_immutable_generation_is_not_accepted_on_replay(world):
    store,wave,leases,owner,blobs,check=world
    publisher=CatalogPublisher(store,wave['waveId'],su,check)
    with execution_scope(leases,owner,store,check):
        publisher.publish('people','a'*64)
        del blobs.rows[next(k for k in blobs.rows if k[1].startswith('wave-projections/'))]
        with pytest.raises(ResourceNotFoundError):publisher.publish('people','a'*64)


def test_alias_changed_after_build_started_is_not_overwritten():
    blobs=Blobs();client=blobs.get_blob_client('c','alias')
    client.upload_blob(b'old')
    guard=BuildGuard(blobs.get_blob_client,[('c','alias')],lambda:None)
    client.upload_blob(b'newer',overwrite=True)
    from index_publication import guarded_publication,fenced
    with pytest.raises(ResourceModifiedError):
        with guarded_publication(guard):fenced(client).upload_blob(b'stale',overwrite=True)
    assert client.download_blob().readall()==b'newer'


def test_assignment_receipt_changes_obligation_identity_and_coverage():
    a=compute_obligations('u','wave','frozen',['people'],assignment_revision='a'*64)
    b=compute_obligations('u','wave','frozen',['people'],assignment_revision='b'*64)
    assert a!=b


def test_new_alias_created_during_build_is_not_overwritten():
    blobs=Blobs();client=blobs.get_blob_client('c','alias')
    guard=BuildGuard(blobs.get_blob_client,[('c','alias')],lambda:None)
    client.upload_blob(b'concurrent')
    from index_publication import guarded_publication,fenced
    with pytest.raises(ResourceExistsError):
        with guarded_publication(guard):fenced(client).upload_blob(b'stale',overwrite=True)
    assert client.download_blob().readall()==b'concurrent'


def test_alias_replaced_after_upload_cannot_be_recorded_as_our_output():
    blobs=Blobs();client=blobs.get_blob_client('c','alias')
    guard=BuildGuard(blobs.get_blob_client,[('c','alias')],lambda:None)
    result=client.upload_blob(b'ours')
    client.upload_blob(b'other',overwrite=True)
    with pytest.raises(RuntimeError,match='changed after upload'):guard.after_upload(client,result)
    assert not guard.outputs


@pytest.mark.parametrize('scope',['sort','access','albums','vector','people_embedding','dedup'])
def test_real_remaining_publishers_commit_empty_library_generations(world,monkeypatch,scope):
    store,wave,leases,owner,blobs,check=world
    from test_wave_recovery import RuntimeTable
    monkeypatch.setattr(su.vision_utils,'get_text_embedding_version',lambda:'test')
    monkeypatch.setattr(su.vision_utils,'get_text_embedding_dimension',lambda:su.PHOTO_EMBEDDING_DIMENSION)
    for name in ('metadata_table_client','albums_table_client','embeddings_table_client','hash_index_table_client'):
        monkeypatch.setitem(su._CTX,name,RuntimeTable())
    monkeypatch.setitem(su._CTX,'blob_vector_index_container','vectors')
    monkeypatch.setitem(su._CTX,'blob_people_embedding_index_container','people-embeddings')
    with execution_scope(leases,owner,store,check):
        assert CatalogPublisher(store,wave['waveId'],su,check).publish(scope,'a'*64)


@pytest.mark.parametrize('scope', ['sort', 'access', 'people', 'albums', 'search', 'vector', 'people_embedding', 'dedup'])
def test_real_catalog_publishers_include_browser_owned_video_without_inference(world, monkeypatch, scope):
    import gzip
    import app
    from test_wave_recovery import RuntimeTable
    store, wave, leases, owner, blobs, check = world
    metadata, hashes = RuntimeTable(), RuntimeTable()
    metadata.create_entity(dict(PartitionKey='u', RowKey='clip.mov', anonymousImageId='video',
        mimeType='video/quicktime', videoBrowserOnly=True, processing_state='active',
        faceCount=0, face_status='skipped', preview_status='skipped',
        thumbnail_status='failed', exif_status='failed', fileHash='d'*64,
        uploadDate='2026-10-25T18:00:00+00:00'))
    hashes.create_entity(dict(PartitionKey='u', RowKey='d'*64, filename='clip.mov'))
    monkeypatch.setitem(su._CTX, 'metadata_table_client', metadata)
    monkeypatch.setitem(su._CTX, 'hash_index_table_client', hashes)
    for name in ('albums_table_client', 'embeddings_table_client'):
        monkeypatch.setitem(su._CTX, name, RuntimeTable())
    monkeypatch.setitem(su._CTX, 'blob_vector_index_container', 'vectors')
    monkeypatch.setitem(su._CTX, 'blob_people_embedding_index_container', 'people-embeddings')
    monkeypatch.setattr(su.vision_utils, 'get_text_embedding_version', lambda: 'test')
    monkeypatch.setattr(su.vision_utils, 'get_text_embedding_dimension', lambda: su.PHOTO_EMBEDDING_DIMENSION)
    monkeypatch.setattr(su, 'LEXICAL_BUILD_HOOK', app._streaming_lexical_build)
    monkeypatch.setattr(su, 'download_media_bytes', lambda *a, **k: pytest.fail('Index downloaded video original'))
    with execution_scope(leases, owner, store, check):
        assert CatalogPublisher(store, wave['waveId'], su, check).publish(scope, 'd'*64)
    proof = json.loads(store._get(f"publication!{wave['waveId']}!"+'d'*64)['payload'])
    assert proof['scope'] == scope and proof['outputs']
    contents = []
    for output in proof['outputs']:
        data = blobs.rows[output['container'], output['blob']][0]
        contents.append(gzip.decompress(data) if data.startswith(b'\x1f\x8b') else data)
    if scope in ('sort', 'access', 'search', 'dedup'):
        assert any(b'clip.mov' in content for content in contents)


def test_people_embedding_scan_failure_cannot_publish_an_empty_success(world,monkeypatch):
    store,wave,leases,owner,blobs,check=world
    def failed(*args,**kwargs):raise OSError('scan failed')
    monkeypatch.setattr(su._CTX['face_table_client'],'query_entities',failed)
    with execution_scope(leases,owner,store,check):
        with pytest.raises(RuntimeError,match='no artifact'):
            CatalogPublisher(store,wave['waveId'],su,check).publish('people_embedding','a'*64)
    assert not store._get(f"publication!{wave['waveId']}!"+'a'*64)


def test_immutable_copy_preserves_stored_gzip_bytes(world,monkeypatch):
    import gzip
    store,wave,leases,owner,blobs,check=world
    source=blobs.get_blob_client('lexical-index','compressed')
    payload=gzip.compress(b'{"photos":[]}')
    source.upload_blob(payload)
    original=blobs.get_blob_client
    def client(container,blob):
        result=original(container,blob)
        download=result.download_blob
        def read(**options):
            assert options.get('decompress') is False
            return download(**options)
        if blob=='compressed':result.download_blob=read
        return result
    monkeypatch.setattr(blobs,'get_blob_client',client)
    publisher=CatalogPublisher(store,wave['waveId'],su,check)
    with execution_scope(leases,owner,store,check):
        output=publisher._copy('lexical-index','compressed',source.get_blob_properties().etag,'a'*64)
    assert blobs.rows[output['container'],output['blob']][0]==payload


def test_real_streaming_search_publishes_database_listing_and_summaries(world,monkeypatch):
    import app
    from test_wave_recovery import RuntimeTable
    store,wave,leases,owner,blobs,check=world
    monkeypatch.setitem(su._CTX,'metadata_table_client',RuntimeTable())
    monkeypatch.setattr(su,'LEXICAL_BUILD_HOOK',app._streaming_lexical_build)
    with execution_scope(leases,owner,store,check):
        assert CatalogPublisher(store,wave['waveId'],su,check).publish('search','b'*64)
    proof=json.loads(store._get(f"publication!{wave['waveId']}!"+'b'*64)['payload'])
    assert len(proof['outputs'])==7


def test_dedup_delete_epoch_race_cannot_receive_a_wave_receipt(world,monkeypatch):
    from test_wave_recovery import RuntimeTable
    import upload_hash_snapshot
    store,wave,leases,owner,blobs,check=world
    hashes=RuntimeTable()
    hashes.create_entity(dict(PartitionKey='u',RowKey=upload_hash_snapshot.DELETE_EPOCH_ROW,epoch='old'))
    monkeypatch.setitem(su._CTX,'hash_index_table_client',hashes)
    submit=hashes.submit_transaction
    def commit(operations):
        if len(operations)==2:
            marker=hashes.get_entity('u',upload_hash_snapshot.DELETE_EPOCH_ROW)
            submit([('update',dict(marker,epoch='deleted'),dict(etag=marker.metadata['etag'],match_condition='IfNotModified'))])
        return submit(operations)
    monkeypatch.setattr(hashes,'submit_transaction',commit)
    with execution_scope(leases,owner,store,check):
        with pytest.raises(ResourceModifiedError):
            CatalogPublisher(store,wave['waveId'],su,check).publish('dedup','c'*64)
    assert not store._get(f"publication!{wave['waveId']}!"+'c'*64)
