"""Real Flask guards: live admission must fail closed and remain tenant scoped."""
from types import SimpleNamespace
import pytest
import app
import wave_policy
import upload_session_lease
_SEND_EXPIRY_CHECK=upload_session_lease.send_check
from upload_waves import ShadowWaves, Conflict
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
    import upload_session_lease
    monkeypatch.setattr(upload_session_lease,'send_check',lambda *args:None)
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


def test_stopped_selection_keeps_accepted_member_and_dispatches_vision(live_api,monkeypatch):
    client,store,metadata,library=live_api
    import wave_runtime
    sent=[]
    monkeypatch.setattr(wave_runtime,'send_stage',lambda *args:sent.append(args))
    root='/upload/waves/live/sessions'
    opened=client.post(root,json=dict(sessionId='stopped',declaredCount=3)).json
    assert client.post(root+'/stopped/items',json=dict(items=[
        dict(itemId=str(i),filename=f'{i}.jpg') for i in range(3)])).status_code==200
    metadata.create_entity(dict(PartitionKey='u',RowKey='0.jpg',anonymousImageId='p',processingRevision='r'))
    accepted=client.post(root+'/stopped/resolve',json=dict(itemId='0',outcome='accepted',filename='0.jpg'))
    assert accepted.status_code==200 and accepted.json['state']=='open'
    for item in ('1','2'):
        response=client.post(root+'/stopped/resolve',json=dict(itemId=item,outcome='cancelled'))
        assert response.status_code==200
    assert response.json['state']=='closed'
    wave=store.status(opened['waveId'])
    assert wave['state']=='processing_pending' and wave['memberCount']==1
    assert metadata.get_entity('u','0.jpg')['anonymousImageId']=='p'
    assert any(message[1].startswith('wave-vision:') for message in sent)


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
    response=client.post('/upload/finalize-batch',json=body)
    assert response.status_code==200
    receipts=[entry['waveResolution'] for entry in response.json['results']]
    assert [(receipt['sessionId'],receipt['itemId']) for receipt in receipts]==[('partial','a'),('partial','b')]
    assert receipts[-1]['state']=='closed'
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


def test_batched_duplicate_and_failed_outcomes_close_once_and_replay(live_api):
    client, store, metadata, library = live_api
    root = '/upload/waves/live/sessions'
    client.post(root, json=dict(sessionId='batch', declaredCount=3))
    client.post(root + '/batch/items', json=dict(items=[dict(itemId=k, filename=k+'.jpg') for k in 'abc']))
    metadata.create_entity(dict(PartitionKey='u', RowKey='a.jpg', anonymousImageId='p', processingRevision='r'))
    body = dict(items=[dict(itemId='a', outcome='duplicate', filename='a.jpg'),
                       dict(itemId='b', outcome='failed'), dict(itemId='c', outcome='cancelled')])
    first = client.post(root + '/batch/resolve-batch', json=body)
    assert first.status_code == 200 and first.json['state'] == 'closed'
    assert first.json['resolvedCount'] == 3
    replay = client.post(root + '/batch/resolve-batch', json=body)
    assert replay.status_code == 200 and replay.json['manifestRevision'] == first.json['manifestRevision']
    assert store.status(first.json['waveId'])['memberCount'] == 1
    library[0] = 'other'
    assert client.post(root + '/batch/resolve-batch', json=body).status_code == 409


def test_batch_resolution_partial_failure_is_retryable(live_api, monkeypatch):
    import upload_wave_shadow
    from azure.core.exceptions import AzureError
    client, store, _, _ = live_api
    root = '/upload/waves/live/sessions'
    client.post(root, json=dict(sessionId='retry', declaredCount=2))
    client.post(root + '/retry/items', json=dict(items=[dict(itemId=k, filename=k+'.jpg') for k in 'ab']))
    original = upload_wave_shadow.resolve_source
    failed = [False]
    def resolve(store, table, session, item, *args):
        if item == 'b' and not failed[0]:
            failed[0] = True
            raise AzureError('lost storage response')
        return original(store, table, session, item, *args)
    monkeypatch.setattr(upload_wave_shadow, 'resolve_source', resolve)
    body = dict(items=[dict(itemId=k, outcome='failed') for k in 'ab'])
    assert client.post(root + '/retry/resolve-batch', json=body).status_code == 503
    assert store._required('session!retry')['resolvedCount'] == 1
    response = client.post(root + '/retry/resolve-batch', json=body)
    assert response.status_code == 200 and response.json['state'] == 'closed'
    assert response.json['resolvedCount'] == 2


def test_registration_contention_is_a_distinct_retryable_conflict(live_api,monkeypatch):
    from upload_waves import MutationContention,ShadowWaves
    client,store,metadata,library=live_api
    root='/upload/waves/live/sessions'
    client.post(root,json=dict(sessionId='s',declaredCount=1))
    def busy(*args,**kwargs):raise MutationContention('Concurrent wave mutation; retry the request')
    monkeypatch.setattr(ShadowWaves,'register',busy)
    response=client.post(root+'/s/items',json={'items':[{'itemId':'i','filename':'a.jpg'}]})
    assert response.status_code==409
    assert response.json['code']=='wave_mutation_contention'
    assert response.headers['Retry-After']=='1'


def test_open_never_admits_a_lease_without_a_durable_expiry_wake(live_api,monkeypatch):
    from azure.core.exceptions import AzureError
    import upload_session_lease
    client,store,_,_=live_api
    def unavailable(*args):raise AzureError('queue unavailable')
    monkeypatch.setattr(upload_session_lease,'send_check',unavailable)
    result=client.post('/upload/waves/live/sessions',json=dict(sessionId='no-wake',declaredCount=10))
    assert result.status_code==503
    assert store._get(store.session_key('no-wake')) is None


def test_existing_heartbeat_renews_authenticated_parent_but_cannot_resurrect_it(live_api,monkeypatch):
    import upload_session_lease
    client,store,_,library=live_api
    monkeypatch.setattr(app,'_require_user_id',lambda **kw:('owner',None))
    monkeypatch.setattr(app,'_mark_upload_activity_heartbeat',lambda user:None)
    client.post('/upload/waves/live/sessions',json=dict(sessionId='lease',declaredCount=2))
    result=client.post('/upload/transfer-heartbeat',json=dict(waveSessionId='lease',libraryId='forged'))
    assert result.status_code==200 and result.json['leaseActive']
    parent=store._required(store.session_key('lease'))
    assert parent['leaseExpiresAt']-parent['heartbeatAt']==300
    store.table.submit_transaction([store._change(parent,dict(parent,leaseExpiresAt=1))])
    result=client.post('/upload/transfer-heartbeat',json=dict(waveSessionId='lease'))
    assert result.status_code==200 and result.json['leaseActive'] is False
    assert store._required(store.session_key('lease'))['leaseExpiresAt']==1
    library[0]='other'
    # A different library cannot renew this parent's lease.
    result=client.post('/upload/transfer-heartbeat',json=dict(waveSessionId='lease'))
    assert not result.json.get('leaseActive')
    assert store._required(store.session_key('lease'))['leaseExpiresAt']==1


def test_discard_absent_session_needs_no_admission_queue_or_resume_ancestor(live_api,monkeypatch):
    import upload_session_lease
    client,store,_,_=live_api
    monkeypatch.setattr(upload_session_lease,'send_check',lambda *args:pytest.fail('Discard must not enqueue expiry'))
    result=client.post('/upload/waves/live/sessions/never-admitted/abandon',json=dict(reason='User discarded'))
    assert result.status_code==200 and result.json['state']=='absent'
    assert store._required(store.session_key('never-admitted'))['discardAbsent']
    assert store._get('control') is None


def test_discard_uses_current_revision_without_reopening_intake(live_api,monkeypatch):
    import upload_session_lease
    client,store,_,_=live_api
    parent=store.open_session('discard',2)
    store.register('discard',[dict(itemId='one',filename='one.jpg')])
    store.resolve('discard','one',outcome='accepted',filename='one.jpg',photo_id='photo',processing_revision='revision',required_steps=['face'])
    monkeypatch.setattr(upload_session_lease,'send_check',lambda *args:pytest.fail('Discard must not enqueue expiry'))
    result=client.post('/upload/waves/live/sessions/discard/abandon',json=dict(reason='User discarded'))
    assert result.status_code==200 and result.json['state']=='abandoned'
    assert store._required(store.item_key('discard','one'))['outcome']=='accepted'
    assert store._required(store.wave_key(parent['waveId']))['memberCount']==1
    assert client.post('/upload/waves/live/sessions/discard/abandon',json=dict(reason='Retry discard')).status_code==200


def test_missing_expiry_queue_is_service_failure_not_missing_photo(live_api,monkeypatch):
    from azure.core.exceptions import ResourceNotFoundError
    import upload_session_lease
    client,store,_,_=live_api
    error=ResourceNotFoundError('The specified queue does not exist.')
    error.error_code='QueueNotFound'
    def send(*args,**kwargs):raise error
    monkeypatch.setattr(app,'queue_service_client',SimpleNamespace(get_queue_client=lambda name:SimpleNamespace(send_message=send)))
    monkeypatch.setattr(upload_session_lease,'send_check',_SEND_EXPIRY_CHECK)
    result=client.post('/upload/waves/live/sessions',json=dict(sessionId='missing-queue',declaredCount=1))
    assert result.status_code==503 and result.json['code']=='upload_expiry_unavailable'
    assert 'photo' not in result.json['error'].lower()
    assert store._get(store.session_key('missing-queue')) is None


def test_missing_source_still_reports_missing_photo_only_during_resolution(live_api):
    client,store,_,_=live_api
    store.open_session('missing-source',1)
    store.register('missing-source',[dict(itemId='one',filename='one.jpg')])
    result=client.post('/upload/waves/live/sessions/missing-source/resolve',json=dict(
        itemId='one',outcome='accepted',filename='one.jpg'))
    assert result.status_code==409 and result.json['error']=='Finalized photo not found'


def test_unleased_heartbeat_upgrade_reports_missing_queue_as_service_failure(live_api,monkeypatch):
    from upload_session_lease import ExpiryUnavailable
    client,store,_,_=live_api
    store.open_session('old-unleased',1)
    monkeypatch.setattr(app,'_require_user_id',lambda **kw:('owner',None))
    monkeypatch.setattr(app,'_mark_upload_activity_heartbeat',lambda user:None)
    def unavailable(*args):raise ExpiryUnavailable('Upload expiry queue is not provisioned')
    monkeypatch.setattr(upload_session_lease,'send_check',unavailable)
    result=client.post('/upload/transfer-heartbeat',json=dict(waveSessionId='old-unleased'))
    assert result.status_code==503 and result.json['code']=='upload_expiry_unavailable'
    assert not store._required(store.session_key('old-unleased')).get('leaseExpiresAt')



def test_discard_tombstone_prevents_late_open_and_ignores_its_expiry_wake(live_api):
    from upload_session_lease import expire
    client,store,_,_=live_api
    result=client.post('/upload/waves/live/sessions/late-open/discard',json=dict(reason='Discard'))
    assert result.status_code==200 and result.json['state']=='absent'
    late=store.open_session('late-open',100)
    assert late['state']=='abandoned' and late['discardAbsent']
    with pytest.raises(Conflict,match='closed'):
        store.register('late-open',[dict(itemId='one',filename='one.jpg')])
    assert store._get('control') is None
    expire(store,'late-open',lambda *args:pytest.fail('No reschedule'),lambda *args:pytest.fail('No wave to drive'))


def test_open_committing_first_is_fenced_and_closed_by_discard_retry(live_api):
    client,store,_,_=live_api
    store.table.before_commit=lambda:store.open_session('racing',1)
    result=client.post('/upload/waves/live/sessions/racing/discard',json=dict(reason='Discard'))
    assert result.status_code==200 and result.json['state']=='abandoned'
    parent=store._required(store.session_key('racing'))
    assert parent['state']=='abandoned' and parent.get('abandoning')
    assert store._required(store.wave_key(parent['waveId']))['openSessions']==0


def test_discard_committing_first_prevents_inflight_open_transaction(live_api):
    client,store,_,_=live_api
    def discard_first():
        result=client.post('/upload/waves/live/sessions/racing/discard',json=dict(reason='Discard'))
        assert result.status_code==200 and result.json['state']=='absent'
    store.table.before_commit=discard_first
    result=store.open_session('racing',1)
    assert result.get('discardAbsent') and result['state']=='abandoned'
    assert store._get('control') is None


def test_discard_unadmitted_retry_exposes_link_and_closes_unleased_parent(live_api):
    client,store,_,_=live_api
    old=store.open_session('old',2)
    store.register('old',[dict(itemId='accepted',filename='a.jpg'),dict(itemId='pending',filename='b.jpg')])
    store.resolve('old','accepted',outcome='accepted',filename='a.jpg',photo_id='photo',processing_revision='rev',required_steps=['face'])
    result=client.post('/upload/waves/live/sessions/unadmitted/discard',json=dict(reason='Discard',resumedFromSessionId='old'))
    assert result.status_code==200 and result.json['resumedFromSessionId']=='old'
    result=client.post('/upload/waves/live/sessions/old/discard',json=dict(reason='Discard'))
    assert result.status_code==200 and result.json['state']=='abandoned'
    assert store._required(store.item_key('old','accepted'))['outcome']=='accepted'
    assert store._required(store.wave_key(old['waveId']))['memberCount']==1
    assert store._required(store.wave_key(old['waveId']))['openSessions']==0


def test_discard_uses_stored_lineage_and_refuses_rebinding_it(live_api):
    client,store,_,_=live_api
    store.open_session('old',1)
    store.open_session('retry',1,resumed_from='old')
    result=client.post('/upload/waves/live/sessions/retry/discard',json=dict(reason='Discard',resumedFromSessionId='wrong'))
    assert result.status_code==409
    assert store._required(store.session_key('retry'))['state']=='open'
    result=client.post('/upload/waves/live/sessions/retry/discard',json=dict(reason='Discard'))
    assert result.status_code==200 and result.json['resumedFromSessionId']=='old'


def test_modern_discard_preserves_live_finalization_then_closes_after_admission(live_api):
    client,store,_,_=live_api
    opened=store.open_session('finalizing',1)
    store.register('finalizing',[dict(itemId='one',filename='one.jpg')])
    item=store._required(store.item_key('finalizing','one'))
    store.table.submit_transaction([store._change(item,dict(item,finalizeLeaseUntil=store.clock()+600))])
    result=client.post('/upload/waves/live/sessions/finalizing/discard',json=dict(reason='Discard'))
    assert result.status_code==409 and 'still in flight' in result.json['error']
    assert store._required(store.session_key('finalizing'))['abandoning']
    assert store._required(store.item_key('finalizing','one'))['outcome']=='reserved'
    store.resolve('finalizing','one',outcome='accepted',filename='one.jpg',photo_id='photo',processing_revision='rev',required_steps=['face'])
    result=client.post('/upload/waves/live/sessions/finalizing/discard',json=dict(reason='Discard'))
    assert result.status_code==200 and result.json['state']=='abandoned'
    assert store._required(store.item_key('finalizing','one'))['outcome']=='accepted'
    assert store._required(store.wave_key(opened['waveId']))['memberCount']==1
