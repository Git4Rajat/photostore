"""Bounded independently scheduled wave recovery; no API traffic is needed.

Durable directory and per-library cursors prevent repeatedly scanning the first
page. Repairs only existing releases; no missing source is called successful,
no poison work is silently reset, and no catalog is built by recovery.
"""
import os
import time
from itertools import islice

from azure.core import MatchConditions
from azure.core.exceptions import ResourceNotFoundError
from azure.data.tables import UpdateMode

from upload_waves import ShadowWaves, digest
from upload_wave_execution import execution_enabled, stage_work_store, advance_wave, release_and_relay, recover_releases

DIRECTORY='upload-wave-directory'


def register_library(table,library):
    key='library!'+digest(library)
    try:
        return table.get_entity(DIRECTORY,key)
    except ResourceNotFoundError:
        pass
    from azure.core.exceptions import ResourceExistsError
    from azure.data.tables import TableTransactionError
    row=dict(PartitionKey=DIRECTORY,RowKey=key,libraryId=library,waveCursor='')
    try:
        table.submit_transaction([('create',row)])
    except (ResourceExistsError,TableTransactionError) as error:
        if isinstance(error,TableTransactionError) and error.status_code != 409: raise
    return table.get_entity(DIRECTORY,key)


def _save(table,current,**fields):
    row=dict(current,**fields)
    table.submit_transaction([('update',row,dict(mode=UpdateMode.REPLACE,
        etag=current.metadata['etag'],match_condition=MatchConditions.IfNotModified))])


def metrics(store,wave,*,clock=time.time):
    now=clock(); state=wave['state']
    result=dict(state=state,wave_age_seconds=max(0,now-wave['createdAt']),
        state_age_seconds=max(0,now-wave['stateEnteredAt']),
        no_progress_seconds=max(0,now-wave['lastProgressAt']),
        barrier_wait_seconds=max(0,now-wave.get('barrierStartedAt',now)),
        outstanding_leases=wave.get('outstandingLeases',0),
        outstanding_members=wave.get('outstandingMembers',0),wait_reason=wave.get('waitReason',''),
        predecessor_wave_id=wave.get('predecessorWaveId',''),
        handoff_pending_count=0)
    key=(wave.get('visionPendingKey') if state in ('processing_pending','processing') else
         'shadow-cluster!'+wave['waveId'] if state=='ready' else wave.get('indexPendingKey') if state=='indexing' else None)
    if key:
        outbox=store._get(key)
        if outbox:
            from execution_lease import ExecutionLeases
            stage=outbox['destination']
            lease=store._get(ExecutionLeases.key(store.library,wave['waveId'],stage))
            running=bool(lease and lease.get('status')=='running' and lease.get('leaseUntil',0)>now)
            result.update(handoff=stage,handoff_pending_count=int(not running),
                          handoff_age_seconds=max(0,now-outbox['createdAt']))
            scope=f'wave-{stage}:{wave["waveId"]}:{digest(key)}'
            work=stage_work_store(store.table)
            pending=work._get(work.key(store.library,scope))
            result['dispatch_failed_count']=int(bool(pending and pending.get('failed')))
            if lease:
                result.update(stage_no_progress_seconds=max(0,now-lease['lastProgressAt']),
                              stage_lease_active=running)
    return result


def recover_library(table,library,send,*,after='',wave_pages=2,release_limit=100,
                     clock=time.time,emit=lambda **fields:None,metadata=None,cleanup_send=None,
                     deadline=None):
    """Full recovery pass for one library: supersession settlement, every wave
    in this page range advanced, any lost release notify retried, and orphaned
    processing artifacts relayed for cleanup.

    This is the single implementation shared by recover()'s cross-library
    directory sweep (scheduled/manual job) and the opportunistic per-event
    nudge in routes/upload_wave_shadow.py's _drive() -- so the two call sites
    can never silently drift into different recovery behavior. Returns
    (count, errors, after) where `after` is the wave-page cursor to resume
    from on a later call (the directory sweep persists it; the per-event
    caller discards it, since it always starts a fresh small pass).
    """
    if deadline is None:deadline=clock()+1e9
    store=ShadowWaves(table,library,clock=clock);work=stage_work_store(table)
    count=0;errors=0
    for _ in range(wave_pages):
        if clock()>=deadline:break
        rows=store._page('wave!',after)
        if not rows:
            after=''; break
        for wave in rows:
            if clock()>=deadline:break
            try:
                from wave_policy import owned,require_capability
                paused=False
                if owned(table,library):
                    policy=store._required('policy')
                    paused=bool(policy.get('cleanupState'))
                    if not paused:require_capability(store,'wave-recovery')
                    if not paused and metadata is not None:
                        from wave_settlement import recover_supersessions
                        recover_supersessions(store,wave,metadata)
                if not paused:wave,_=advance_wave(work,store,library,wave['waveId'])
                state=wave['state']
                destination=('vision' if state in ('processing_pending','processing') else
                             'cluster' if state=='ready' and not wave.get('zeroFaceProbePending') else 'index' if state=='indexing' else None)
                if paused:destination=None
                key=(wave.get('visionPendingKey') if destination=='vision' else
                     'shadow-cluster!'+wave['waveId'] if destination=='cluster' else wave.get('indexPendingKey'))
                if destination and key:
                    release_and_relay(work,store,library,key,destination,send)
                if state!='complete':
                    emit(event='wave_recovery',library_id=digest(library),wave_id=wave['waveId'],**metrics(store,wave,clock=clock))
                count+=1
            except Exception as error:
                errors+=1
                emit(event='wave_recovery_error',library_id=digest(library),wave_id=wave['waveId'],
                     reason=type(error).__name__)
            after=wave['RowKey']
        if clock()>=deadline:break
        if len(rows)<20: after=''; break
    try:
        policy=store._get('policy')
        if clock()<deadline and not (policy and policy.get('cleanupState')):
            recover_releases(work,store,send,limit=release_limit)
        if cleanup_send is not None and clock()<deadline:
            from processing_cleanup import relay_artifact_cleanup
            relay_artifact_cleanup(table,library,cleanup_send)
    except Exception as error:
        errors+=1
        emit(event='wave_recovery_error',library_id=digest(library),reason=type(error).__name__)
    return count,errors,after


def recover(table,send,*,max_libraries=20,wave_pages=2,clock=time.time,emit=lambda **fields:None,max_seconds=210,
            metadata=None,cleanup_send=None):
    if not execution_enabled() or os.getenv('UPLOAD_WAVE_RECOVERY_ENABLED','').lower()!='true':
        return dict(libraries=0,waves=0,disabled=True)
    if not 1<=max_libraries<=100 or not 1<=wave_pages<=10:
        raise ValueError('Recovery bounds exceeded')
    try:
        cursor=table.get_entity(DIRECTORY,'scan-cursor')
    except ResourceNotFoundError:
        table.submit_transaction([('create',dict(PartitionKey=DIRECTORY,RowKey='scan-cursor',after=''))])
        cursor=table.get_entity(DIRECTORY,'scan-cursor')
    query=f"PartitionKey eq '{DIRECTORY}' and RowKey ge 'library!' and RowKey lt 'library!~'"
    if cursor['after']: query+=f" and RowKey gt '{cursor['after']}'"
    libraries=list(islice(table.query_entities(query,results_per_page=max_libraries),max_libraries))
    count=0;errors=0;processed=[];deadline=clock()+max_seconds
    for entry in libraries:
        if clock()>=deadline:break
        processed.append(entry)
        library=entry['libraryId']
        waves,library_errors,after=recover_library(table,library,send,after=entry.get('waveCursor') or '',
            wave_pages=wave_pages,clock=clock,emit=emit,metadata=metadata,cleanup_send=cleanup_send,deadline=deadline)
        count+=waves;errors+=library_errors
        # A missing response may repeat repairs, but cannot manufacture readiness.
        _save(table,table.get_entity(DIRECTORY,entry['RowKey']),waveCursor=after)
    _save(table,table.get_entity(DIRECTORY,'scan-cursor'),after=processed[-1]['RowKey'] if processed and (len(libraries)==max_libraries or len(processed)<len(libraries)) else '')
    return dict(libraries=len(processed),waves=count,errors=errors,disabled=False)


def main():
    # Dedicated scheduled-job host; importing app configures storage only.
    import app
    from wave_runtime import send_stage
    import processing_diagnostics
    result=recover(app.jobs_table_client,send_stage,
        metadata=app.metadata_table_client,
        cleanup_send=__import__('processing_cleanup').send_artifact_cleanup,
        emit=lambda **fields:processing_diagnostics.emit(fields.pop('event'),**fields))
    processing_diagnostics.emit('wave_recovery_run',**result)


if __name__=='__main__': main()
