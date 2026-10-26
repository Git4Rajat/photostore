"""Wave recovery: repairs one library's releases, settlement and sessions.

No standing scheduled host. Called from routes/upload_wave_shadow.py's
opportunistic per-event nudge (piggybacked on real wave-lifecycle events) and,
for a one-off operator sweep, a throwaway Manual-trigger job provisioned and
torn down on the fly by deploy/fresh_wave.py or deploy/upgrade_wave.py. Repairs
only existing releases; no missing source is called successful, no poison work
is silently reset, and no catalog is built by recovery.
"""
import time

from upload_waves import ShadowWaves, digest
from upload_wave_execution import (stage_work_store, advance_wave, release_and_relay,
    recover_releases, revive_stale_failed_dispatches)

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

    Called from the opportunistic per-event nudge in routes/upload_wave_shadow.py's
    _drive() (small pass, discards the final cursor -- see `checkpoint` below).
    A library with no further wave-lifecycle events (idle, nobody visiting)
    has no automatic path back here; there is no standing scheduled sweep and
    no manual-trigger UI/endpoint today. Returns (count, errors, after) where
    `after` is the final wave-page cursor.

    `checkpoint`, if given, is called with the cursor after every wave (not
    just once at the end) so a caller that needs durable per-wave resume
    doesn't lose already-advanced progress if a later page in the same pass
    raises -- re-advancing an already-advanced wave is safe/idempotent, but
    there is no reason to repeat work a crash didn't actually undo. The
    per-event nudge always starts a fresh small pass and has no durable
    resume state of its own, so it omits this.
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
                    if not paused:require_capability(store,'upload')
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
