import pytest
from upload_waves import ShadowWaves
from wave_recovery import recover_library
from test_upload_waves import AtomicTable,close


class RuntimeTable(AtomicTable):
    def create_entity(self,row):return self.submit_transaction([('create',row)])
    def update_entity(self,row,**options):
        from azure.data.tables import UpdateMode
        if options.get('mode')==UpdateMode.MERGE:
            row=dict(self.get_entity(row['PartitionKey'],row['RowKey']),**row)
        return self.submit_transaction([('update',row,options)])
    def delete_entity(self,partition,row,**options):
        current=self.get_entity(partition,row)
        if options.get('etag'):assert current.metadata['etag']==options['etag']
        del self.rows[partition,row]
    def query_entities(self,query,**options):
        if 'RowKey ge' in query:
            return super().query_entities(query,**options)
        from test_index_work import EtagTable
        return EtagTable.query_entities(self,query,**options)


def test_recover_library_checkpoints_each_advanced_wave(monkeypatch):
    """recover_library's optional `checkpoint` callback must fire once per
    advanced wave (with that wave's own RowKey), not only once when the whole
    pass finishes -- that's what lets recover()'s directory cursor survive a
    later page crashing without losing durable progress for waves already
    advanced earlier in the pass."""
    monkeypatch.setenv('UPLOAD_WAVE_EXECUTION_ENABLED','true')
    table=RuntimeTable()
    store=ShadowWaves(table,'a')
    opened=close(store,'s',store.open_session('s',0))
    checkpoints=[]
    count,errors,after=recover_library(table,'a',lambda *a:None,checkpoint=checkpoints.append)
    assert count==1 and errors==0
    assert checkpoints==[store.wave_key(opened['waveId'])]


def test_recover_library_works_with_no_checkpoint_callback(monkeypatch):
    """recover_library must work with no checkpoint at all (the opportunistic
    per-event route path's usage) -- the parameter is optional, not required."""
    monkeypatch.setenv('UPLOAD_WAVE_EXECUTION_ENABLED','true')
    table=RuntimeTable()
    store=ShadowWaves(table,'a')
    close(store,'s',store.open_session('s',0))
    count,errors,after=recover_library(table,'a',lambda *a:None)
    assert count==1 and errors==0


def test_recover_library_is_the_same_mechanism_the_opportunistic_route_nudge_uses(monkeypatch):
    """The scheduled/manual cross-library sweep and the per-event inline nudge
    in routes/upload_wave_shadow.py both call recover_library directly (no
    DIRECTORY/cursor bookkeeping needed for a single already-known library),
    so a stage dispatch lost to a worker scale-down race still gets retried
    the next time this library's wave advances, without a standalone job."""
    monkeypatch.setenv('UPLOAD_WAVE_EXECUTION_ENABLED','true')
    table=RuntimeTable()
    store=ShadowWaves(table,'solo')
    opened=store.open_session('s',0);close(store,'s',opened)
    sent=[];events=[]
    count,errors,after=recover_library(table,'solo',lambda *args:sent.append(args),
        wave_pages=1,emit=lambda **fields:events.append(fields))
    assert count==1 and errors==0
    assert sent and sent[0][0]=='solo'
    assert any(record['event']=='wave_recovery' for record in events)


def test_stale_open_session_is_auto_abandoned_so_its_wave_can_advance(monkeypatch):
    """A session with no real activity (browser tab closed mid-upload) must
    not keep its wave stuck in 'collecting' forever -- recovery reaps it using
    the WAVE's lastProgressAt, not the session's own unchanging createdAt."""
    monkeypatch.setenv('UPLOAD_WAVE_EXECUTION_ENABLED','true')
    table=RuntimeTable()
    now=[1000.0]
    store=ShadowWaves(table,'a',clock=lambda:now[0])
    store.open_session('s',3)  # declares 3 items, registers/resolves none
    now[0]+=4000  # past STALE_SESSION_SECONDS
    recover_library(table,'a',lambda *a:None,clock=lambda:now[0])
    session=store._required(store.session_key('s'))
    assert session['state']=='abandoned'


def test_recent_open_session_is_left_alone(monkeypatch):
    """An upload still genuinely in progress (recent wave activity) must not
    be auto-abandoned just because it's been open for a while in wall time."""
    monkeypatch.setenv('UPLOAD_WAVE_EXECUTION_ENABLED','true')
    table=RuntimeTable()
    now=[1000.0]
    store=ShadowWaves(table,'a',clock=lambda:now[0])
    store.open_session('s',3)
    now[0]+=60  # well under STALE_SESSION_SECONDS
    recover_library(table,'a',lambda *a:None,clock=lambda:now[0])
    session=store._required(store.session_key('s'))
    assert session['state']=='open'


def test_stale_failed_dispatch_gets_one_more_attempt(monkeypatch):
    """A stage dispatch that gave up after 5 attempts (failed=True) must not
    be abandoned forever -- after enough time for an operator to plausibly
    have noticed and fixed the underlying cause, recovery gives it exactly
    one more try instead of requiring a manual /repair call indefinitely."""
    from upload_wave_execution import stage_work_store
    monkeypatch.setenv('UPLOAD_WAVE_EXECUTION_ENABLED','true')
    table=RuntimeTable()
    now=[1000.0]
    work=stage_work_store(table,clock=lambda:now[0])
    key=work.request('a','wave-vision:w1:deadbeef')
    claim=work.claim(key)
    work.finish(claim,failed=True)
    assert table.get_entity(work.partition,key)['failed']
    now[0]+=4000  # past STALE_FAILED_DISPATCH_SECONDS
    store=ShadowWaves(table,'a',clock=lambda:now[0])
    recover_library(table,'a',lambda *a:None,clock=lambda:now[0])
    assert table.get_entity(work.partition,key)['failed'] is False


def test_recently_failed_dispatch_is_not_retried_yet(monkeypatch):
    from upload_wave_execution import stage_work_store
    monkeypatch.setenv('UPLOAD_WAVE_EXECUTION_ENABLED','true')
    table=RuntimeTable()
    now=[1000.0]
    work=stage_work_store(table,clock=lambda:now[0])
    key=work.request('a','wave-vision:w1:deadbeef')
    claim=work.claim(key)
    work.finish(claim,failed=True)
    now[0]+=60  # well under STALE_FAILED_DISPATCH_SECONDS
    recover_library(table,'a',lambda *a:None,clock=lambda:now[0])
    assert table.get_entity(work.partition,key)['failed']


def test_live_stage_lease_is_not_reported_as_an_unsent_handoff():
    from execution_lease import ExecutionLeases
    from wave_recovery import metrics
    table=RuntimeTable();store=ShadowWaves(table,'a')
    opened=store.open_session('s',0);close(store,'s',opened)
    wave=store._required(store.wave_key(opened['waveId']))
    before=metrics(store,wave)
    assert before['handoff_pending_count']==1
    leases=ExecutionLeases(table,partition=store.partition)
    leases.claim('a',wave['waveId'],'vision','revision')
    after=metrics(store,wave)
    assert after['handoff_pending_count']==0 and after['stage_lease_active']


def test_abandonment_continues_past_twenty_items_and_dispatches(monkeypatch):
    monkeypatch.setenv('UPLOAD_WAVE_EXECUTION_ENABLED','true')
    table=RuntimeTable();now=[1000.0];store=ShadowWaves(table,'a',clock=lambda:now[0])
    opened=store.open_session('s',21)
    for start,end in ((0,20),(20,21)):
        store.register('s',[dict(itemId=f'i{i:02}',filename=f'{i}.jpg') for i in range(start,end)])
    now[0]+=4000;sent=[]
    recover_library(table,'a',lambda *a:sent.append(a),clock=lambda:now[0])
    assert store._required(store.session_key('s'))['resolvedCount']==20
    recover_library(table,'a',lambda *a:sent.append(a),clock=lambda:now[0])
    assert store._required(store.session_key('s'))['state']=='abandoned'
    assert store.status(opened['waveId'])['openSessions']==0 and sent


def test_session_survey_resumes_after_closed_history(monkeypatch):
    from wave_recovery import _reap_stale_sessions
    monkeypatch.setenv('UPLOAD_WAVE_EXECUTION_ENABLED','true')
    table=RuntimeTable();now=[1000.0];store=ShadowWaves(table,'a',clock=lambda:now[0])
    for i in range(40):
        opened=store.open_session(f'a{i:02}',0);close(store,f'a{i:02}',opened)
    store.open_session('z',1);now[0]+=4000
    for _ in range(2):
        _reap_stale_sessions(store,'a',lambda:now[0],lambda **kw:None,deadline=now[0]+100)
    assert store._required(store.session_key('z'))['state']=='abandoned'


def test_dispatch_cooldown_starts_when_it_failed_not_when_requested(monkeypatch):
    from upload_wave_execution import stage_work_store,revive_stale_failed_dispatches
    monkeypatch.setenv('UPLOAD_WAVE_EXECUTION_ENABLED','true')
    table=RuntimeTable();now=[1000.0];work=stage_work_store(table,clock=lambda:now[0])
    key=work.request('a','wave-vision:w:key');now[0]+=4000
    work.finish(work.claim(key),failed=True)
    assert revive_stale_failed_dispatches(work,ShadowWaves(table,'a'),clock=lambda:now[0])==0
    now[0]+=3601
    assert revive_stale_failed_dispatches(work,ShadowWaves(table,'a'),clock=lambda:now[0])==1
