"""Read-only, point-read wave health; never publishes, repairs or scans a library."""
import json

from execution_lease import ExecutionLeases
from upload_waves import digest
from upload_wave_execution import stage_work_store


def status(store, wave_id):
    wave = store.status(wave_id)
    state = wave['state']
    stage = {'processing_pending':'vision', 'processing':'vision', 'ready':'cluster',
             'indexing':'index'}.get(state)
    health = dict(stage=stage, state='waiting', visionComplete=state in ('ready','indexing','complete'),
        searchable=state == 'complete' and bool(wave.get('completionReceiptId')),
        hasFailures=bool(wave.get('failedMembers')), noProgressSeconds=wave['noProgressSeconds'])
    if state in ('complete', 'cancelled'):
        health['state'] = state
    elif state == 'collecting':
        health['reason'] = 'intake_open'
    predecessor_id = wave.get('predecessorWaveId')
    if predecessor_id and state in ('waiting_predecessor', 'ready'):
        predecessor = store._get(store.wave_key(predecessor_id))
        needs_vision = state == 'waiting_predecessor'
        satisfied = (bool(predecessor) and predecessor['state'] in ('ready','indexing','complete')
                     and bool(predecessor.get('frozenRevision'))) if needs_vision else store.predecessor_published(wave)
        if not satisfied:
            health.update(reason='predecessor_vision' if needs_vision else 'predecessor_publication',
                blockedByWaveId=predecessor_id, blockedByState=(predecessor or {}).get('state','missing'))
    if stage:
        key = {'vision':wave.get('visionPendingKey'), 'cluster':'shadow-cluster!' + wave_id,
               'index':wave.get('indexPendingKey')}[stage]
        work = stage_work_store(store.table)
        intent = work._get(work.key(store.library, f'wave-{stage}:{wave_id}:{digest(key)}')) if key else None
        lease = store._get(ExecutionLeases.key(store.library,wave_id,stage)) or {}
        health['noProgressSeconds'] = max(0, store.clock() - max(
            float(wave.get('lastProgressAt') or 0), float(lease.get('lastProgressAt') or 0)))
        health.update(dispatchAttempts=int((intent or {}).get('attempts') or 0),
            ownerActive=bool(lease.get('owner') and float(lease.get('leaseUntil') or 0)>store.clock()))
        if intent and intent.get('failed'):
            health.update(state='failed', reason='dispatch_exhausted')
        elif not health.get('reason'):
            health['state'] = 'processing' if health['ownerActive'] else 'pending'
            health['reason'] = 'stage_owned' if health['ownerActive'] else 'dispatch_pending' if intent else 'dispatch_missing'
        for field, output in [('lastFailureId','lastFailure'), ('lastProcessingFailureId','lastProcessingFailure')]:
            if not intent or not intent.get(field):
                continue
            failure = work._get(intent[field])
            if failure and failure.get('libraryId') == store.library:
                health[output] = dict(json.loads(failure['payload']),
                    recordedAt=failure['recordedAt'], dispatchAttempt=failure['dispatchAttempt'],
                    currentRevision=failure.get('revision') == intent['revision'])
    if health.get('blockedByWaveId'):
        health['state'] = 'blocked'
    return dict(wave, health=health)
