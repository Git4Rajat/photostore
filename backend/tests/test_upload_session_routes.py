"""Core request boundary: tenant isolation, adoption, durable acknowledgment."""
import pytest
from azure.core.exceptions import AzureError
from test_wave_live_routes import live_api
from upload_session_registry import Sessions
import app


def test_core_adopts_old_session_closes_it_and_returns_durable_notice(live_api,monkeypatch):
    client,store,_,library=live_api
    store.open_session('pre-upgrade',2,lease_seconds=300)
    parent=store._required(store.session_key('pre-upgrade'))
    store.table.submit_transaction([store._change(parent,dict(parent,leaseExpiresAt=1))])
    import routes.upload_sessions as routes
    monkeypatch.setattr('routes.upload_wave_shadow._drive',lambda *a:None)
    result=client.post('/api/upload-sessions/reconcile',json={'libraryId':'forged'})
    assert result.status_code==200
    assert result.json['notifications'][0]['sessionId']=='pre-upgrade'
    assert result.json['notifications'][0]['affectedCount']==2
    assert store._required(store.session_key('pre-upgrade'))['state']=='abandoned'
    library[0]='other'
    assert client.post('/api/upload-sessions/reconcile',json={}).json['notifications']==[]
    assert client.post('/api/upload-sessions/notifications/pre-upgrade/ack',json={}).status_code==200
    library[0]='u'
    assert len(client.post('/api/upload-sessions/reconcile',json={}).json['notifications'])==1
    assert client.post('/api/upload-sessions/notifications/pre-upgrade/ack',json={}).status_code==200
    assert client.post('/api/upload-sessions/reconcile',json={}).json['notifications']==[]


def test_failed_notice_commit_remains_retryable(live_api,monkeypatch):
    client,store,_,_=live_api
    monkeypatch.setattr('routes.upload_wave_shadow._drive',lambda *a:None)
    store.open_session('interrupted',1,lease_seconds=300)
    parent=store._required(store.session_key('interrupted'))
    store.table.submit_transaction([store._change(parent,dict(parent,leaseExpiresAt=1))])
    table=app.upload_sessions_table_client
    original=table.submit_transaction
    def unavailable(operations):
        if any(op[1]['RowKey'].startswith('notice!') for op in operations):
            raise AzureError('table unavailable')
        return original(operations)
    monkeypatch.setattr(table,'submit_transaction',unavailable)
    assert client.post('/api/upload-sessions/reconcile',json={}).status_code==503
    assert Sessions(table,'u').get('session!interrupted')
    monkeypatch.setattr(table,'submit_transaction',original)
    result=client.post('/api/upload-sessions/reconcile',json={})
    assert result.status_code==200 and len(result.json['notifications'])==1


def test_core_user_traffic_reconciles_once_and_probes_do_not(live_api,monkeypatch):
    from flask import g
    from routes.upload_sessions import reconcile_on_core_access, _last
    import routes.upload_sessions as routes
    monkeypatch.setenv('APP_ROLE','core')
    calls=[]
    _last.clear()
    monkeypatch.setattr(routes,'reconcile',lambda library:calls.append(library))
    with app.app.test_request_context('/health'):
        reconcile_on_core_access(app.jsonify(status='healthy'))
    assert not calls
    for _ in range(2):
        with app.app.test_request_context('/api/photos'):
            g.upload_session_library='u'
            reconcile_on_core_access(app.jsonify(photos=[]))
    assert calls==['u']
    monkeypatch.setenv('APP_ROLE','upload')
    with app.app.test_request_context('/upload/init'):
        g.upload_session_library='another'
        reconcile_on_core_access(app.jsonify(complete=True))
    assert calls==['u']


def test_finalize_replay_retries_handoff_before_removing_directory(live_api,monkeypatch):
    from routes.upload_wave_shadow import close_resolved_intake
    client,store,_,_=live_api
    assert client.post('/upload/waves/live/sessions',json=dict(sessionId='close-retry',declaredCount=0)).status_code==200
    parent=store._required(store.session_key('close-retry'))
    def unavailable(*args):
        raise AzureError('handoff unavailable')
    monkeypatch.setattr('routes.upload_wave_shadow._drive',unavailable)
    with pytest.raises(AzureError):
        close_resolved_intake(store,parent)
    directory=Sessions(app.upload_sessions_table_client,'u')
    assert directory.get('session!close-retry')
    assert store._required(store.session_key('close-retry'))['state']=='closed'
    driven=[]
    monkeypatch.setattr('routes.upload_wave_shadow._drive',lambda *args:driven.append(args))
    close_resolved_intake(store,store._required(store.session_key('close-retry')))
    assert len(driven)==1 and directory.get('session!close-retry') is None


def test_ambiguous_prior_send_does_not_drop_stale_directory(live_api,monkeypatch):
    from upload_wave_execution import advance_wave,stage_work_store
    client,store,_,_=live_api
    parent=store.open_session('relay-retry',2,lease_seconds=300)
    store.register('relay-retry',[dict(itemId='a',filename='a.jpg')])
    store.resolve('relay-retry','a',outcome='accepted',filename='a.jpg',photo_id='photo',processing_revision='r',required_steps=['face'])
    current=store._required(store.session_key('relay-retry'))
    store.table.submit_transaction([store._change(current,dict(current,leaseExpiresAt=1))])
    # Existing relay reservation could have been written just before a replica
    # died, without its queue send landing. A retained directory must repair it.
    from upload_session_lease import seal_expired
    from wave_settlement import abandon
    current=seal_expired(store,'relay-retry')
    current=abandon(store,'relay-retry',current['manifestRevision'],reason='inactive')
    work=stage_work_store(store.table)
    wave,_=advance_wave(work,store,store.library,parent['waveId'])
    from upload_waves import digest
    key=work.key(store.library,f"wave-vision:{wave['waveId']}:{digest(wave['visionPendingKey'])}")
    pending=work._get(key)
    work._replace(pending,dict(pending,wakeUntil=store.clock()+3600))
    Sessions(app.upload_sessions_table_client,'u').track('relay-retry')
    def lost_send(*args):
        raise AzureError('queue acceptance unavailable')
    monkeypatch.setattr('wave_runtime.send_stage',lost_send)
    assert client.post('/api/upload-sessions/reconcile',json={}).status_code==503
    assert Sessions(app.upload_sessions_table_client,'u').get('session!relay-retry')
    sent=[]
    monkeypatch.setattr('wave_runtime.send_stage',lambda *args:sent.append(args))
    result=client.post('/api/upload-sessions/reconcile',json={})
    assert result.status_code==200 and len(result.json['notifications'])==1
    assert sent


def _finish_wave(store, session, *, closed_at, completed_at, members=(), failed=0):
    """Close the session and complete its wave directly, as the stages would."""
    row = store._required(store.session_key(session))
    store.table.submit_transaction([store._change(row, dict(row, state='closed', closedAt=closed_at))])
    wave = store._required(store.wave_key(row['waveId']))
    store.table.submit_transaction([store._change(wave, dict(wave, state='complete', stateEnteredAt=completed_at,
                                                            failedMembers=failed))])
    for index, filename in enumerate(members):
        store.table.create_entity(store._row(f"e!{row['waveId']}!m!{index:04d}", kind='member', filename=filename))
    return row['waveId']


def test_processing_summary_waits_for_the_wave_then_counts_time_and_people(live_api, monkeypatch):
    client, store, _, library = live_api
    import routes.upload_sessions as routes
    routes._summaries.clear()
    store.open_session('summary-session', 2)
    assert client.get('/api/upload-sessions/summary-session/processing').json == dict(done=False, state='collecting')
    _finish_wave(store, 'summary-session', closed_at=1000.0, completed_at=1250.0,
                 members=['a.jpg', 'b.jpg', 'c.jpg'], failed=1)
    reads = []

    def metadata(partition, names):
        reads.append((partition, list(names)))
        people = {'a.jpg': '["p1","p2"]', 'b.jpg': '["p2"]', 'c.jpg': ''}
        return {name: {'peopleIds': people[name]} for name in names}
    monkeypatch.setattr(app, '_get_metadata_entities', metadata)
    body = client.get('/api/upload-sessions/summary-session/processing').json
    assert body == dict(done=True, processedSeconds=250, failed=1, people=2)
    assert reads == [('u', ['a.jpg', 'b.jpg', 'c.jpg'])]
    # A complete wave never changes, so the answer is kept rather than recounted.
    assert client.get('/api/upload-sessions/summary-session/processing').json == body
    assert len(reads) == 1


def test_processing_summary_leaves_people_out_of_big_uploads(live_api, monkeypatch):
    client, store, _, library = live_api
    import routes.upload_sessions as routes
    routes._summaries.clear()
    monkeypatch.setattr(routes, 'PEOPLE_MAX_PHOTOS', 2)
    store.open_session('big-session', 3)
    _finish_wave(store, 'big-session', closed_at=10.0, completed_at=70.0, members=['a.jpg', 'b.jpg', 'c.jpg'])
    monkeypatch.setattr(app, '_get_metadata_entities', lambda *a: pytest.fail('no metadata reads past the cap'))
    assert client.get('/api/upload-sessions/big-session/processing').json == dict(
        done=True, processedSeconds=60, failed=0, people=None)


def test_processing_summary_rejects_unknown_or_invalid_sessions(live_api):
    client, *_ = live_api
    assert client.get('/api/upload-sessions/nobody/processing').status_code == 404
    assert client.get('/api/upload-sessions/bad%20id/processing').status_code == 400
