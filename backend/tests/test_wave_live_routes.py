"""Real Flask guards: live admission must fail closed and remain tenant scoped."""
from types import SimpleNamespace
import pytest
import app
import wave_policy
from upload_waves import ShadowWaves
from test_wave_recovery import RuntimeTable


@pytest.fixture
def live_api(monkeypatch):
    table=RuntimeTable();metadata=RuntimeTable();library=['u']
    for name in ('UPLOAD_WAVE_SOURCE_COMPLETION_ENABLED','PROCESSING_GENERATION_ENFORCEMENT_ENABLED'):
        monkeypatch.setenv(name,'true')
    monkeypatch.setenv('PROCESSING_MODE','backend')
    monkeypatch.setenv('UPLOAD_WAVE_EXECUTION_ENABLED','true')
    builds={role:'qualified-build' for role in wave_policy.ROLES}
    certificate=wave_policy.certify(table,builds)
    monkeypatch.setenv('UPLOAD_WAVE_CAPABILITY_ID',certificate)
    monkeypatch.setenv('UPLOAD_WAVE_DEPLOYMENT_ID','qualified-build')
    store=ShadowWaves(table,'u')
    table.create_entity(store._row('policy',protocol=wave_policy.PROTOCOL,capabilityId=certificate,cleanupState=''))
    import wave_runtime
    monkeypatch.setattr(wave_runtime,'send_stage',lambda *args:None)
    monkeypatch.setattr(app,'jobs_table_client',table)
    monkeypatch.setattr(app,'metadata_table_client',metadata)
    monkeypatch.setattr(app,'_require_library_context',lambda **kw:('owner',library[0],None))
    monkeypatch.setattr(app,'library_store',SimpleNamespace(is_owner=lambda account,lib:account=='owner' and lib=='u'))
    return app.app.test_client(),store,metadata,library


def test_live_route_enforces_capability_and_tenant(live_api,monkeypatch):
    client,store,metadata,library=live_api
    root='/upload/waves/live/sessions'
    assert client.post(root,json=dict(sessionId='s',declaredCount=1)).status_code==200
    monkeypatch.setenv('UPLOAD_WAVE_DEPLOYMENT_ID','unqualified')
    assert client.post(root,json=dict(sessionId='bad',declaredCount=0)).status_code==409
    monkeypatch.setenv('UPLOAD_WAVE_DEPLOYMENT_ID','qualified-build')
    library[0]='other'
    assert client.get('/upload/waves/live/'+store._required('session!s')['waveId']).status_code==409


@pytest.mark.parametrize('path',['/upload/init','/upload/init-batch','/upload/finalize','/upload/finalize-batch'])
def test_old_upload_client_cannot_bypass_live_parent(live_api,path):
    client,store,metadata,library=live_api
    response=client.post(path,json=dict(filename='a.jpg',sha256='a'*64))
    assert response.status_code==409
    assert response.json['code']=='wave_admission_required'
    assert not metadata.rows


def test_live_guard_resets_request_context_and_rejects_browser_processing(live_api):
    client,store,metadata,library=live_api
    assert client.post('/upload/client-processing',json={}).status_code==409
    assert not wave_policy.source_change_active()
    assert wave_policy.finalization_check() is False


def test_owner_abandon_is_explicit_and_closes_cancelled_wave_without_worker(live_api):
    client,store,metadata,library=live_api
    root='/upload/waves/live/sessions'
    opened=client.post(root,json=dict(sessionId='s',declaredCount=3)).json
    manifest=store._required('session!s')['manifestRevision']
    result=client.post(root+'/s/abandon',json=dict(manifestRevision=manifest,reason='Desktop lost'))
    assert result.status_code==200
    assert store.status(opened['waveId'])['completionOutcome']=='no_work'


def test_resolve_autocloses_and_replay_is_idempotent(live_api):
    client,store,metadata,library=live_api
    root='/upload/waves/live/sessions'
    opened=client.post(root,json=dict(sessionId='s',declaredCount=1)).json
    client.post(root+'/s/items',json=dict(items=[dict(itemId='i',filename='a.jpg')]))
    metadata.create_entity(dict(PartitionKey='u',RowKey='a.jpg',anonymousImageId='p',processingRevision='r'))
    body=dict(itemId='i',outcome='accepted',filename='a.jpg')
    first=client.post(root+'/s/resolve',json=body)
    assert first.status_code==200 and first.json['state']=='closed'
    second=client.post(root+'/s/resolve',json=body)
    assert second.status_code==200
    assert store.status(opened['waveId'])['memberCount']==1


def test_protocol_discovery_is_authenticated_library_scoped(live_api):
    client,store,metadata,library=live_api
    live=client.get('/upload/waves/protocol')
    assert live.json=={'protocol':'live'} and live.headers['Cache-Control']=='no-store'
    library[0]='old'
    assert client.get('/upload/waves/protocol').json=={'protocol':'legacy'}
