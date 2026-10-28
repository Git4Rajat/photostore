"""Browser loss, multiple resumes and racing renewal against expiry."""
import pytest
from test_upload_waves import AtomicTable
from upload_waves import ShadowWaves, Conflict
from upload_session_lease import expire, renew, seal_expired, active


def world():
    now=[1000.0];table=AtomicTable()
    store=ShadowWaves(table,'library',clock=lambda:now[0])
    return store,table,now


def admit(store,session,previous=''):
    opened=store.open_session(session,2,resumed_from=previous,lease_seconds=300)
    store.register(session,[dict(itemId='a',filename='a.jpg'),dict(itemId='b',filename='b.jpg')])
    store.resolve(session,'a',outcome='accepted',filename='a.jpg',photo_id='p-'+session,
                  processing_revision='r',required_steps=['face'])
    return opened


def test_n_interruptions_release_admitted_portions_in_distinct_successors():
    store,table,now=world();previous='';waves=[];driven=[]
    for i in range(4):
        session='session-'+str(i)
        opened=admit(store,session,previous);waves.append(opened['waveId'])
        now[0]+=301
        expire(store,session,lambda *args:pytest.fail('No reschedule needed'),
               lambda current,wave:driven.append(wave))
        parent=store._required(store.session_key(session))
        assert parent['state']=='abandoned'
        assert store._required(store.item_key(session,'a'))['outcome']=='accepted'
        assert store._required(store.item_key(session,'b'))['outcome']=='cancelled'
        assert store._required(store.wave_key(opened['waveId']))['memberCount']==1
        # Test without a live-policy certificate: seal still detaches intake.
        assert not store._required('control')['collectingWaveId']
        previous=session
    assert len(set(waves))==4 and driven==waves
    assert store._required(store.wave_key(waves[0]))['state']=='processing_pending'
    for index,wave in enumerate(waves[1:]):
        assert store._required(store.wave_key(wave))['predecessorWaveId']==waves[index]


def test_heartbeat_only_renews_its_parent_and_check_follows_new_deadline():
    store,table,now=world();admit(store,'one');admit(store,'two')
    now[0]+=200;assert renew(store,'one')['leaseActive']
    now[0]+=101;wakes=[]
    expire(store,'one',lambda *args:wakes.append(args),lambda *args:None)
    expire(store,'two',lambda *args:None,lambda *args:None)
    assert wakes==[('library','one',199.0)]
    assert store._required(store.session_key('one'))['state']=='open'
    assert store._required(store.session_key('two'))['state']=='abandoned'


def test_renewal_winning_etag_race_prevents_expiry():
    store,table,now=world();admit(store,'one');now[0]=1299
    # Expiry's read can be stale relative to a just-committed renewal.
    old=store._required(store.session_key('one'));now[0]=1300
    def concurrent_renew():
        now[0]=1299;renew(store,'one');now[0]=1300
    table.before_commit=concurrent_renew
    result=seal_expired(store,'one')
    assert not result.get('abandoning') and result['leaseExpiresAt']==1599
    assert active(result,now[0])


def test_expiry_winning_cannot_be_renewed_or_reopened():
    store,table,now=world();opened=admit(store,'one');now[0]+=300
    assert not renew(store,'one')['leaseActive']
    seal_expired(store,'one')
    assert not renew(store,'one')['leaseActive']
    with pytest.raises(Conflict,match='closed'):
        store.register('one',[dict(itemId='c',filename='c.jpg')])
    resumed=store.open_session('two',1,resumed_from='one',lease_seconds=300)
    assert resumed['waveId']!=opened['waveId']


def test_live_finalization_defers_cancellation_and_can_finish():
    store,table,now=world();admit(store,'one')
    row=store._required(store.item_key('one','b'))
    table.submit_transaction([store._change(row,dict(row,finalizeLeaseUntil=1600))])
    now[0]=1301;wakes=[];driven=[]
    expire(store,'one',lambda *args:wakes.append(args),lambda *args:driven.append(args))
    assert wakes==[('library','one',30)] and not driven
    assert store._required(store.item_key('one','b'))['outcome']=='reserved'
    store.resolve('one','b',outcome='accepted',filename='b.jpg',photo_id='p2',processing_revision='r',required_steps=['face'])
    expire(store,'one',lambda *args:None,lambda *args:driven.append(args))
    assert store._required(store.session_key('one'))['state']=='abandoned' and driven
    assert store._required(store.item_key('one','b'))['outcome']=='accepted'


def test_closure_redelivery_repairs_handoff_after_failure():
    store,table,now=world();admit(store,'one');now[0]+=301
    def fail(*args):raise RuntimeError('queue outage')
    with pytest.raises(RuntimeError):expire(store,'one',lambda *args:None,fail)
    driven=[];expire(store,'one',lambda *args:None,lambda *args:driven.append(args))
    assert len(driven)==1


def test_partial_manifest_pages_need_no_browser_to_finish():
    store,table,now=world();store.open_session('one',10000,lease_seconds=300)
    for offset in range(0,45,20):
        store.register('one',[dict(itemId=str(i),filename=f'{i}.jpg') for i in range(offset,min(offset+20,45))])
    now[0]+=301;wakes=[];driven=[]
    expire(store,'one',lambda *args:wakes.append(args),lambda *args:driven.append(args))
    parent=store._required(store.session_key('one'))
    assert parent['state']=='abandoned' and parent['resolvedCount']==45
    assert not wakes and len(driven)==1


def test_new_source_binding_cannot_cross_expiry_fence():
    from wave_policy import validate_admission
    store,table,now=world();admit(store,'one')
    data=dict(waveSessionId='one',waveItemId='b',filename='b.jpg',sha256='a'*64)
    def expired_first():
        now[0]=1301
        seal_expired(store,'one')
    table.before_commit=expired_first
    with pytest.raises(Conflict,match='closed'):
        validate_admission(store,data,finalizing=True)
    assert not store._required(store.item_key('one','b')).get('sourceHash')


def test_cancelled_reservation_cannot_restart_upload_under_old_parent():
    from wave_policy import validate_admission
    store,table,now=world();admit(store,'one');now[0]+=301
    expire(store,'one',lambda *args:None,lambda *args:None)
    with pytest.raises(Conflict,match='closed'):
        validate_admission(store,dict(waveSessionId='one',waveItemId='b',filename='b.jpg',sha256='a'*64),finalizing=True)


def test_large_expiry_checkpoints_and_requeues_at_bounded_page_budget():
    store,table,now=world();store.open_session('one',10000,lease_seconds=300)
    for offset in range(0,401,20):
        store.register('one',[dict(itemId=f'{i:04}',filename=f'{i}.jpg') for i in range(offset,min(offset+20,401))])
    now[0]+=301;wakes=[];driven=[]
    expire(store,'one',lambda *args:wakes.append(args),lambda *args:driven.append(args))
    assert wakes==[('library','one',0)] and not driven
    assert store._required(store.session_key('one'))['resolvedCount']==400
    expire(store,'one',lambda *args:wakes.append(args),lambda *args:driven.append(args))
    assert len(driven)==1 and store._required(store.session_key('one'))['state']=='abandoned'


def test_expiry_polls_while_main_clustering_thread_is_busy():
    import threading
    from types import SimpleNamespace
    from upload_session_lease import start_worker, QUEUE_NAME
    ready=threading.Event();shutdown=threading.Event();calls=[]
    queue=object()
    def poll(client,name,max_retries,deadletter):
        assert client is queue and name==QUEUE_NAME and max_retries==float('inf')
        calls.append(name);ready.set()
        return False
    service=SimpleNamespace(get_queue_client=lambda name:queue)
    thread=start_worker(service,shutdown,.01,poll,SimpleNamespace(exception=lambda *args:None))
    try:
        # No call to the main clustering queue/poll loop is needed for expiry.
        assert ready.wait(1)
    finally:
        shutdown.set();thread.join(1)
    assert calls and not thread.is_alive()
