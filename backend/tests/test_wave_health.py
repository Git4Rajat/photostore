"""Health explains terminal failures and publication waits using only point reads."""
from azure.core import MatchConditions

from wave_health import status
from upload_wave_execution import stage_work_store
from upload_waves import digest
from test_wave_activation import active, RuntimeTable
from test_upload_waves import register_accepted, close


def test_health_reports_dispatch_failure_without_mutation():
    store=active(RuntimeTable());wid,opened=register_accepted(store)
    close(store,'parent',opened)
    wave=store.status(wid);work=stage_work_store(store.table)
    scope=f"wave-vision:{wid}:{digest(wave['visionPendingKey'])}"
    key=work.request(store.library,scope);claim=work.claim(key)
    work.record_failure(claim,dict(errorClass='TimeoutError'))
    work.finish(claim,failed=True)
    before=repr(store.table.rows)
    health=status(store,wid)['health']
    assert health['stage']=='vision' and health['state']=='failed'
    assert health['reason']=='dispatch_exhausted'
    assert health['lastFailure']['errorClass']=='TimeoutError'
    assert repr(store.table.rows)==before


def test_health_explains_predecessor_publication_and_vision_readiness():
    store=active(RuntimeTable());wid,opened=register_accepted(store)
    close(store,'parent',opened)
    current=store._required(store.wave_key(wid))
    pred=store._row('wave!predecessor',waveId='predecessor',state='indexing',frozenRevision='frozen')
    store.table.create_entity(pred)
    store.table.update_entity(dict(current,state='ready',frozenRevision='frozen',predecessorWaveId='predecessor'),
        etag=current.metadata['etag'], match_condition=MatchConditions.IfNotModified)
    health=status(store,wid)['health']
    assert health['state']=='blocked'
    assert health['reason']=='predecessor_publication'
    assert health['blockedByWaveId']=='predecessor'
    assert health['visionComplete'] and not health['searchable']


def test_stage_checkpoint_progress_takes_precedence_over_old_wave_timestamp():
    from execution_lease import ExecutionLeases
    store=active(RuntimeTable());wid,opened=register_accepted(store)
    close(store,'parent',opened)
    now=[store.clock()];store.clock=lambda:now[0]
    leases=ExecutionLeases(store.table,partition=store.partition,clock=store.clock)
    owner=leases.claim(store.library,wid,'vision','input')
    now[0]+=10
    leases.checkpoint(owner,'vision:page-1')
    now[0]+=5
    health=status(store,wid)['health']
    assert health['noProgressSeconds']==5
    assert store.status(wid)['noProgressSeconds']>=15


def test_exhaustion_preserves_underlying_attempt_error_in_health():
    store=active(RuntimeTable());wid,opened=register_accepted(store)
    close(store,'parent',opened)
    wave=store.status(wid);work=stage_work_store(store.table)
    key=work.request(store.library,f"wave-vision:{wid}:{digest(wave['visionPendingKey'])}")
    claim=work.claim(key)
    root=work.record_failure(claim,dict(errorClass='TimeoutError',httpStatus=503))
    work.release(claim)
    final=work.claim(key)
    work.record_failure(final,dict(errorClass='DispatchAttemptsExhausted'))
    work.finish(final,failed=True)
    health=status(store,wid)['health']
    assert health['lastFailure']['errorClass']=='DispatchAttemptsExhausted'
    assert health['lastProcessingFailure']['errorClass']=='TimeoutError'
    assert health['lastProcessingFailure']['httpStatus']==503
    assert work._get(root) is not None
