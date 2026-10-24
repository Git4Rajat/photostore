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
from upload_wave_execution import (execution_enabled, stage_work_store, advance_wave, release_and_relay,
    recover_releases, revive_stale_failed_dispatches)

DIRECTORY='upload-wave-directory'
# A session with no real activity for this long (a browser tab closed mid
# upload, a dropped connection) is abandoned, not just slow. Measured against
# the WAVE's own lastProgressAt (bumped by every register()/resolve() call
# across every session feeding that wave), not the session row's createdAt --
# createdAt never changes after open_session, so using it would misfire on a
# large, genuinely still-active upload that simply takes a long time.
STALE_SESSION_SECONDS=3600


def _reap_stale_sessions(store,library,clock,emit,*,deadline,pages=2,work=None,send=None):
    """Resume both the session survey and paged abandonment durably."""
    from wave_settlement import abandon
    cursor_key='session-recovery-cursor'
    saved=store._get(cursor_key)
    after=(saved or {}).get('after','')
    def checkpoint(value):
        def commit():
            old=store._get(cursor_key)
            store.table.submit_transaction([store._change(old,store._row(cursor_key,after=value)),
                *store._policy_guard()])
        store._retry(commit)
    reaped=0
    for _ in range(pages):
        if clock()>=deadline:break
        rows=store._page('session!',after)
        if not rows:
            checkpoint('');break
        for row in rows:
            if clock()>=deadline:return reaped
            previous=after
            if row.get('state')=='open':
                wave=store._get(store.wave_key(row['waveId']))
                last=wave.get('lastProgressAt',wave.get('createdAt',0)) if wave else clock()
                if row.get('abandoning') or clock()-last>=STALE_SESSION_SECONDS:
                    try:
                        result=abandon(store,row['sessionId'],row['manifestRevision'],
                            reason=row.get('abandonReason') or f'Auto-abandoned: no activity for over {STALE_SESSION_SECONDS}s')
                        if result.get('abandonmentNeedsMore'):
                            checkpoint(previous)  # Resume this parent's next page.
                            return reaped
                        reaped+=1
                        emit(event='wave_session_abandoned',library_id=digest(library),
                            session_id=digest(row['sessionId']),wave_id=row['waveId'])
                        if work is not None and send is not None:
                            from wave_runtime import relay_current,relay_successor
                            wave,_=advance_wave(work,store,library,row['waveId'])
                            relay_current(work,store,wave,send)
                            if wave['state']=='complete':relay_successor(work,store,wave['waveId'],send)
                    except Exception as error:
                        emit(event='wave_session_reap_skipped',library_id=digest(library),
                            session_id=digest(row['sessionId']),reason=type(error).__name__)
            after=row['RowKey']
            checkpoint(after)
        if len(rows)<20:
            checkpoint('');break
    return reaped


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
                     deadline=None,checkpoint=None):
    """Full recovery pass for one library: supersession settlement, every wave
    in this page range advanced, any lost release notify retried, and orphaned
    processing artifacts relayed for cleanup.

    This is the single implementation shared by recover()'s cross-library
    directory sweep (scheduled/manual job) and the opportunistic per-event
    nudge in routes/upload_wave_shadow.py's _drive() -- so the two call sites
    can never silently drift into different recovery behavior. Returns
    (count, errors, after) where `after` is the final wave-page cursor.

    `checkpoint`, if given, is called with the cursor after every wave (not
    just once at the end) so a caller that needs durable per-wave resume
    (recover()'s cross-library directory sweep) doesn't lose already-advanced
    progress if a later page in the same pass raises -- re-advancing an
    already-advanced wave is safe/idempotent, but there is no reason to repeat
    work a crash didn't actually undo. The per-event nudge in
    routes/upload_wave_shadow.py omits it: it always starts a fresh small pass
    and discards the final cursor, so per-wave durability doesn't apply there.
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
                             'cluster' if state=='ready' else 'index' if state=='indexing' else None)
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
            if checkpoint is not None:checkpoint(after)
        if clock()>=deadline:break
        if len(rows)<20: after=''; break
    try:
        policy=store._get('policy')
        if clock()<deadline and not (policy and policy.get('cleanupState')):
            revived=revive_stale_failed_dispatches(work,store,clock=clock)
            if revived:emit(event='wave_dispatch_revived',library_id=digest(library),count=revived)
            recover_releases(work,store,send,limit=release_limit)
            _reap_stale_sessions(store,library,clock,emit,deadline=deadline,work=work,send=send)
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
        # Checkpoint after every wave, not just once at the end: if a later
        # page in this same pass raises (e.g. a transient store._page error),
        # already-advanced waves in earlier pages stay checkpointed instead of
        # being re-processed (safe either way, but wasteful) on the next run.
        waves,library_errors,after=recover_library(table,library,send,after=entry.get('waveCursor') or '',
            wave_pages=wave_pages,clock=clock,emit=emit,metadata=metadata,cleanup_send=cleanup_send,deadline=deadline,
            checkpoint=lambda cursor:_save(table,table.get_entity(DIRECTORY,entry['RowKey']),waveCursor=cursor))
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
