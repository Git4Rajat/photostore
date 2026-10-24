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


def test_lifecycle_events_claim_the_opportunistic_recovery_nudge_at_most_once_per_window(live_api,monkeypatch):
    """_drive() piggybacks an opportunistic recover_library() sweep on every
    real wave-lifecycle event (close/resolve-to-completion/repair/abandon)
    instead of a standalone polling job. The claim must be durable (a Table
    Storage row other replicas/processes also see), not an in-process dict,
    and bounded to one sweep per library per window even across several
    separate lifecycle events in quick succession."""
    client,store,metadata,library=live_api
    root='/upload/waves/live/sessions'
    calls=[]
    import wave_recovery
    monkeypatch.setattr(wave_recovery,'recover_library',
        lambda *a,**k:(calls.append((a,k)) or (0,0,'')))
    for session in ('s1','s2'):
        opened=client.post(root,json=dict(sessionId=session,declaredCount=3)).json
        manifest=store._required('session!'+session)['manifestRevision']
        result=client.post(root+'/'+session+'/abandon',
            json=dict(manifestRevision=manifest,reason='Desktop lost'))
        assert result.status_code==200
    assert len(calls)==1
    assert calls[0][0][1]=='u'  # library id passed through unchanged
    claim=store._get('recovery-nudge')
    assert claim is not None and claim['nextEligibleAt']>store.clock()


def test_opportunistic_nudge_claim_is_durable_not_per_process(live_api):
    """A second ShadowWaves instance over the same table (standing in for a
    different replica/worker process) must see and respect the same claim --
    an in-process-only cooldown would not."""
    from routes.upload_wave_shadow import _claim_recovery_nudge
    client,store,metadata,library=live_api
    assert _claim_recovery_nudge(store) is True
    other_process_view=ShadowWaves(store.table,store.library)
    assert _claim_recovery_nudge(other_process_view) is False


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


def test_finalize_contention_returns_retryable_response_not_tuple_500(live_api, monkeypatch):
    from flask import Blueprint, Flask, jsonify
    from routes.upload_wave_live import install
    from upload_waves import Conflict
    import upload_wave_shadow
    _, store, metadata, _ = live_api
    store.open_session('batch', 1)
    store.register('batch', [dict(itemId='i', filename='a.jpg')])
    bp = Blueprint('contention_upload', __name__)
    install(bp)

    @bp.route('/upload/finalize-batch', methods=['POST'])
    def finalize():
        return jsonify(results=[dict(complete=True, filename='a.jpg', duplicates=[])])

    api = Flask('contention_test')
    api.register_blueprint(bp)
    def conflict(*args, **kwargs):
        raise Conflict('Concurrent wave mutation; retry the request')
    monkeypatch.setattr(upload_wave_shadow, 'resolve_source', conflict)
    response = api.test_client().post('/upload/finalize-batch', json=dict(files=[dict(
        filename='a.jpg', sha256='a'*64, waveSessionId='batch', waveItemId='i')]))
    assert response.status_code == 503
    assert response.json['code'] == 'wave_admission_retry'
    assert not wave_policy.source_change_active()
    assert wave_policy.finalization_check() is False


def test_successful_replay_keeps_committed_outcome_and_source_fence(live_api):
    from upload_waves import Conflict
    from upload_wave_shadow import resolve_source
    _, store, metadata, _ = live_api
    store.open_session('batch', 2)
    store.register('batch', [dict(itemId='a',filename='a.jpg'),dict(itemId='b',filename='b.jpg')])
    for item in ('a','b'):
        metadata.create_entity(dict(PartitionKey='u',RowKey=item+'.jpg',anonymousImageId=item,
                                    processingRevision='r-'+item))
    # First batch committed A, then lost the response before B was admitted.
    first = resolve_source(store,metadata,'batch','a','accepted','a.jpg')
    # Re-running dedup on a finalize retry can now report A as a duplicate.
    replay = resolve_source(store,metadata,'batch','a','duplicate','a.jpg')
    assert replay['resolvedCount'] == first['resolvedCount'] == 1
    assert store._required(store.item_key('batch','a'))['outcome'] == 'accepted'
    resolve_source(store,metadata,'batch','b','accepted','b.jpg')
    assert store._required('session!batch')['resolvedCount'] == 2
    assert store.status(first['waveId'])['memberCount'] == 2
    metadata.rows['u','a.jpg']['processingRevision'] = 'changed'
    with pytest.raises(Conflict, match='cannot be rebound'):
        resolve_source(store,metadata,'batch','a','duplicate','a.jpg')


def test_partial_finalize_batch_replays_and_keeps_unadmitted_sources_publishable(live_api, monkeypatch):
    from flask import Blueprint, Flask, jsonify
    from routes.upload_wave_live import install
    from upload_waves import Conflict
    import upload_wave_shadow
    _, store, metadata, _ = live_api
    store.open_session('partial', 2)
    store.register('partial',[dict(itemId=k,filename=k+'.jpg') for k in 'ab'])
    for k in 'ab':
        metadata.create_entity(dict(PartitionKey='u',RowKey=k+'.jpg',anonymousImageId=k,processingRevision='r-'+k))
    bp=Blueprint('partial_upload',__name__);install(bp)
    @bp.route('/upload/finalize-batch',methods=['POST'])
    def finalized():
        # Sources already persisted; their hash index can report themselves.
        return jsonify(results=[dict(complete=True,filename=k+'.jpg',duplicates=[dict(filename=k+'.jpg')]) for k in 'ab'])
    api=Flask('partial_test');api.register_blueprint(bp)
    original=upload_wave_shadow.resolve_source;fail=[True]
    def resolve(store,table,session,item,*args):
        if item=='b' and fail[0]:
            fail[0]=False
            raise Conflict('Concurrent wave mutation; retry the request')
        return original(store,table,session,item,*args)
    monkeypatch.setattr(upload_wave_shadow,'resolve_source',resolve)
    body=dict(files=[dict(filename=k+'.jpg',sha256=k*64,waveSessionId='partial',waveItemId=k) for k in 'ab'])
    client=api.test_client()
    assert client.post('/upload/finalize-batch',json=body).status_code==503
    assert store._required('session!partial')['resolvedCount']==1
    assert client.post('/upload/finalize-batch',json=body).status_code==200
    parent=store._required('session!partial')
    assert parent['resolvedCount']==2 and parent['state']=='closed'
    assert all(store._required(store.item_key('partial',k))['outcome']=='accepted' for k in 'ab')
    assert all(member['needsProjection'] for member in store._page('e!'+parent['waveId']+'!m!'))


@pytest.mark.parametrize('path',['/upload/waves/protocol','/upload/init','/upload/finalize-batch'])
def test_fresh_preparation_blocks_intake_instead_of_legacy_fallback(live_api,monkeypatch,path):
    client,store,metadata,library=live_api
    monkeypatch.setenv('UPLOAD_WAVE_BOOTSTRAP_PENDING','true')
    response=client.get(path) if path.endswith('/protocol') else client.post(path,json={})
    assert response.status_code==503
    assert not metadata.rows
