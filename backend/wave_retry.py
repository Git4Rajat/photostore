"""Which stuck waves a single "Retry" should re-queue, and the stage re-queue itself.

Waves run in order: a later wave waits for the earlier one, so a blocked wave cannot be
helped by a retry and only a wave that is itself failed or idle can unblock the others.
`retry_stuck` re-queues every such wave, oldest first. The Tools repair action and the
processing bar share `retry_stage`, so both do exactly the same thing."""
from upload_waves import Conflict, digest

IDLE_RETRY_SECONDS = 600  # frontend StuckWavesCard.IDLE_RETRY_SECONDS
MAX_UNFINISHED = 100

STAGE_OF_STATE = {'processing_pending': 'vision', 'processing': 'vision', 'ready': 'cluster', 'indexing': 'index'}


def retryable(health):
    """True when a retry can unblock this wave: failed or never queued, or idle with no worker."""
    if health.get('state') == 'blocked' or not health.get('stage'):
        return False
    if health.get('state') == 'failed' or health.get('reason') == 'dispatch_missing':
        return True
    return (health.get('state') == 'pending' and health.get('ownerActive') is False
            and float(health.get('noProgressSeconds') or 0) >= IDLE_RETRY_SECONDS)


def skip_reason(health):
    if health.get('state') == 'blocked':
        return 'blocked'
    if health.get('state') in ('processing', 'pending'):
        return 'working'
    return 'waiting'


def retry_stage(store, wave_id, current):
    from upload_wave_execution import stage_work_store
    from execution_lease import ExecutionLeases
    stage = STAGE_OF_STATE.get(current['state'])
    if stage is None:
        raise Conflict('Wave has no retryable stage')
    lease = store._get(ExecutionLeases.key(store.library, wave_id, stage))
    if lease and lease.get('leaseUntil', 0) > store.clock():
        raise Conflict('Stage owner is still active')
    key = (current.get('visionPendingKey') if stage == 'vision' else
           'shadow-cluster!' + wave_id if stage == 'cluster' else current.get('indexPendingKey'))
    stage_work_store(store.table).request(store.library, f'wave-{stage}:{wave_id}:{digest(key)}', advance=True)
    return stage


def unfinished(store):
    """Unfinished waves oldest first. Collecting waves are an open upload, not a stuck one."""
    from wave_health import status
    waves, after = [], ''
    while len(waves) < MAX_UNFINISHED:
        rows = store._page('wave!', after)
        if not rows:
            break
        after = rows[-1]['RowKey']
        for row in rows:
            if row.get('state') in ('collecting', 'complete', 'cancelled'):
                continue
            waves.append(status(store, row['RowKey'][len('wave!'):]))
    return sorted(waves, key=lambda wave: float(wave.get('createdAt') or 0))


def retry_stuck(store, drive):
    retried, skipped = [], []
    for wave in unfinished(store):
        wave_id, health = wave['waveId'], wave['health']
        if not retryable(health):
            skipped.append(dict(waveId=wave_id, reason=skip_reason(health)))
            continue
        try:
            stage = retry_stage(store, wave_id, wave)
        except Conflict as error:  # a worker took it between the list and the click
            skipped.append(dict(waveId=wave_id, reason='working', detail=str(error)))
            continue
        drive(wave_id)
        retried.append(dict(waveId=wave_id, stage=stage))
    return dict(retried=retried, skipped=skipped)
