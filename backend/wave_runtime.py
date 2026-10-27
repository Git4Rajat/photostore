"""Queue adapters for the existing role-specific workers. Default disabled.

No worker executes another role's stage and no API request builds a catalog.
The release record remains authoritative; queue messages are advisory wakes.
"""
import json
import os
import threading
import uuid
import processing_metrics
import processing_diagnostics
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor

from upload_waves import ShadowWaves, digest, MutationContention
from execution_lease import ExecutionLeases
from wave_policy import source_change
from upload_wave_execution import (execution_enabled,stage_work_store,run_vision_release,
    run_cluster_release,run_index_release,release_and_relay,advance_wave)

TYPE='upload_wave_stage'
ROLES={'vision':'vision','cluster':'cluster','index':'index-worker'}
_SOURCE_POOL = None
_SOURCE_POOL_LOCK = threading.Lock()


def _source_executor(workers):
    """All wave deliveries share the configured process-wide source slots."""
    global _SOURCE_POOL
    with _SOURCE_POOL_LOCK:
        if _SOURCE_POOL is None:
            _SOURCE_POOL = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='wave-source')
        return _SOURCE_POOL


def send_stage(library,scope,revision,delay=0):
    if not execution_enabled(): return
    import app
    parts=scope.split(':')
    if len(parts)!=3 or parts[0] not in ('wave-vision','wave-cluster','wave-index'):
        raise ValueError('Invalid wave wakeup scope')
    stage=parts[0][5:]
    queue={'vision':app.ipwork_queue_client,'cluster':app.clustering_queue_client,
           'index':app.index_maintenance_queue_client}[stage]
    if queue is None: raise RuntimeError('Wave stage queue unavailable')
    queue.send_message(json.dumps(dict(type=TYPE,userId=library,waveId=parts[1],
        stage=stage,scope=scope,intentRevision=revision)),visibility_timeout=delay or None)


@contextmanager
def _source_heartbeat(storage,library,filename,lease,renew):
    stop,errors=threading.Event(),[]
    def check():
        renew()
        if errors: raise RuntimeError('Source lease renewal failed') from errors[0]
    def heartbeat():
        while not stop.wait(30):
            try:
                check()
                storage.heartbeat_processing_lease(library,filename,lease['ownerId'],
                    expected_processing_fence=lease['entity']['processingFence'])
            except Exception as error:
                errors.append(error); stop.set()
    thread=threading.Thread(target=processing_metrics.inherit(heartbeat),name='wave-source-lease',daemon=True); thread.start()
    try:
        yield check
        check()
    finally:
        stop.set();thread.join(timeout=1)


@source_change
def process_source(library,filename,revision,renew):
    import app
    import storage_utils as storage
    from processing_completion import TERMINAL,completion_capture_enabled,StaleProcessingResult
    from processing_generation import required_steps_for_media,enforcement_enabled
    from image_utils import is_video_file
    if not completion_capture_enabled() or not enforcement_enabled():
        raise RuntimeError('Wave source completion and generation enforcement must be enabled together')
    renew()
    steps=required_steps_for_media(is_video_file(filename))
    lease=storage.claim_processing_lease(library,filename,'wave-'+uuid.uuid4().hex,steps=list(steps),mark_running=False)
    try:
        row=lease['entity']
        if row.get('processingRevision')!=revision:
            raise StaleProcessingResult('Wave source was superseded')
        with _source_heartbeat(storage,library,filename,lease,renew) as check:
            runnable=[step for step in steps if row.get(step+'_status') not in TERMINAL]
            if runnable:
                app._register_ipwork_processors()
                output=app._run_ipwork_steps(library,filename,runnable,metadata_snapshot=dict(row))
                check()
                storage.apply_client_processing_results_for_file(library,filename,
                    client_processing=output,origin='ipworker',claimed_steps=runnable,
                    lease_owner=lease['ownerId'],expected_processing_revision=revision,
                    expected_processing_fence=row['processingFence'])
            else:
                storage.qualify_completed_processing(library,filename,lease)
    finally:
        storage.release_processing_lease(library,filename,lease['ownerId'],
            expected_processing_fence=lease['entity']['processingFence'])


def invalidate_wave_tags(storage,library):
    """Restore Vision-owned tag freshness suppressed by wave source writes."""
    storage._clear_manifest_dirty_flag(library,'tag_embedding')
    version=storage.touch_user_tag_embedding_index_state(library)
    if storage._tag_embedding_index_container_name():
        manifest=storage._load_tag_embedding_index_manifest(library)
        if not version or manifest.get('sourceVersion')!=version:
            raise RuntimeError('Wave tag invalidation was not durably published')


def relay_successor(work,store,wave_id,send):
    """Wake the direct successor; scheduled recovery also covers lost sends."""
    from itertools import islice
    query=(f"PartitionKey eq '{store.partition}' and RowKey ge 'wave!' and RowKey lt 'wave!~' "
           f"and predecessorWaveId eq '{wave_id}'")
    for successor in islice(store.table.query_entities(query,results_per_page=1),1):
        wave,_=advance_wave(work,store,store.library,successor['waveId'])
        relay_current(work,store,wave,send)


def relay_current(work,store,wave,send):
    state=wave['state'];wave_id=wave['waveId']
    stage=('vision' if state in ('processing_pending','processing') else
           'cluster' if state=='ready' else 'index' if state=='indexing' else None)
    key=(wave.get('visionPendingKey') if stage=='vision' else
         'shadow-cluster!'+wave_id if stage=='cluster' else wave.get('indexPendingKey'))
    if stage and key:release_and_relay(work,store,store.library,key,stage,send)


def consume(payload,*,deadline=None,progress=None):
    if not execution_enabled(): return 'noop'
    import app
    import storage_utils as storage
    from wave_publication import ObligationLedger
    from wave_catalog import CatalogPublisher
    from index_publication import publication
    library,stage,wave_id=payload['userId'],payload['stage'],payload['waveId']
    if ROLES.get(stage)!=os.getenv('APP_ROLE','core'):
        raise ValueError('Wrong app role for wave stage')
    store=ShadowWaves(app.jobs_table_client,library)
    from wave_policy import owned,require_capability
    if owned(store.table,library):require_capability(store,ROLES[stage])
    wave=store._required(store.wave_key(wave_id))
    key=(wave.get('visionPendingKey') if stage=='vision' else
         'shadow-cluster!'+wave_id if stage=='cluster' else wave.get('indexPendingKey'))
    outbox=store._get(key) if key else None
    expected=f'wave-{stage}:{wave_id}:{digest(key)}' if key else ''
    if payload.get('scope')!=expected or not outbox or not store.outbox_eligible(wave,outbox):
        return 'noop'
    work=stage_work_store(store.table)
    token=work.key(library,expected)
    claim=work.claim(token)
    if claim is None: return 'noop'
    if claim.revision!=payload.get('intentRevision'):
        work.release(claim,deferred=True);return 'noop'
    if claim.attempt>5:
        work.finish(claim,failed=True)
        raise RuntimeError('Wave dispatch attempts exhausted; operator retry required')
    leases=ExecutionLeases(store.table,partition=store.partition)
    healthy_continuation = True
    try:
        # Dispatcher bookkeeping is renewed separately from stage ownership.
        with publication(work,claim), processing_diagnostics.stage_context(wave_id, stage, claim):
            elapsed = max(0, work.clock() - claim.scheduled_at) if claim.scheduled_at and claim.schedule_id else None
            processing_diagnostics.emit('wave_stage_started', wave_id=wave_id, stage=stage,
                dispatch_attempt=claim.attempt, attempt_id=claim.owner, schedule_id=claim.schedule_id or None,
                reason=claim.schedule_reason or None, actual_delay_seconds=elapsed,
                requested_delay_seconds=claim.scheduled_delay if elapsed is not None else None,
                excess_delay_seconds=max(0, elapsed - claim.scheduled_delay) if elapsed is not None else None,
                resume_point='stage_start')
            if stage=='vision':
                collector = getattr(app._ipwork_metrics_context, 'collector', None)
                def prepare_sources(members, metadata, renew, expired):
                    from azure.core.exceptions import ResourceNotFoundError
                    from processing_completion import TERMINAL
                    names = []
                    for member in members:
                        if expired(): return
                        try:
                            row = metadata.get_entity(library, member['filename'])
                        except ResourceNotFoundError:
                            continue
                        if (row.get('anonymousImageId') == member['photoId']
                                and row.get('processingRevision') == member['processingRevision']
                                and row.get('processing_state') != 'deleted' and not row.get('processingIsolated')
                                and row.get('face_status') not in TERMINAL):
                            names.append(member['filename'])
                    if names:
                        renew()
                        app.reconcile_face_filename_indexes_batch(library, names, cancelled=expired)
                def process_with_metrics(*args):
                    previous = getattr(app._ipwork_metrics_context, 'collector', None)
                    app._ipwork_metrics_context.collector = collector
                    try:
                        return process_source(*args)
                    finally:
                        app._ipwork_metrics_context.collector = previous
                def invalidate_tags():
                    # Wave source writes bypass legacy invalidation. Vision's
                    # model-owned sweep must not reuse a clean, older tag set.
                    # Best-effort: stale tag-embedding search (missing a newly
                    # uploaded tag until the next sweep) is a minor, recoverable
                    # staleness issue. It must never be allowed to abort the
                    # entire wave-vision completion -- which is exactly what
                    # happened in production when this write's blob container
                    # didn't exist yet (see create_blob_containers): every
                    # upload's gallery visibility was blocked by a tag-search
                    # freshness side effect. A later run (another upload event,
                    # or the independent tag-embedding sweep) retries this.
                    try:
                        invalidate_wave_tags(storage,library)
                    except Exception:
                        try:app.app.logger.exception('Wave tag invalidation failed for %s; continuing without it',library)
                        except Exception:pass
                inspection_failures = [0]
                result=run_vision_release(leases,work,store,library,wave_id,key,
                    app.metadata_table_client,None,None,send_stage,process_source=process_with_metrics,
                    deadline=deadline,progress=progress,before_freeze=invalidate_tags,
                    source_workers=app.IPWORKER_CONCURRENCY,
                    source_executor=_source_executor(app.IPWORKER_CONCURRENCY),
                    prepare_sources=prepare_sources, inspection_failures=inspection_failures)
                healthy_continuation = not (inspection_failures[0] and result
                    and float(result.get('lastProgressAt') or 0) <= float(wave.get('lastProgressAt') or 0))
            elif stage=='cluster':
                # Bounded zero-face probes continue on Cluster wakes without
                # loading FAISS. The release remains eligible while probing.
                result,_=advance_wave(work,store,library,wave_id)
                if result['state']=='ready' and not result.get('zeroFaceProbePending'):
                    result=run_cluster_release(leases,store,library,wave_id,key,
                        assigner=app._get_live_faiss_assigner(),journal=None,obligations_table=store.table,
                        face_table=None,face_ids=None)
            else:
                ledger=ObligationLedger(store.table,wave_id,library=library,
                    frozen_revision=wave['frozenRevision'],assignment_revision=wave.get('assignmentReceiptId',''))
                def publish(scope,obligation,check):
                    return CatalogPublisher(store,wave_id,storage,check).publish(scope,obligation)
                @source_change
                def prepare(check):
                    after=''
                    while True:
                        members=store._page(f'e!{wave_id}!m!',after)
                        for member in members:
                            if member['outcome'] not in ('settled','failed_exhausted'):continue
                            check();app._live_faiss_metadata_update(library,member['filename']);check()
                        if len(members)<20:break
                        after=members[-1]['RowKey']
                from index_publication import catalog_serialization
                with catalog_serialization(store.table,library,app.blob_service_client,app.BLOB_LEXICAL_INDEX_CONTAINER):
                    result=run_index_release(leases,store,library,wave_id,key,ledger,publisher=publish,prepare=prepare)
        if result and result['state'] in ('processing_pending','processing','ready'):
            progressing = float(result.get('lastProgressAt') or 0) > float(wave.get('lastProgressAt') or 0)
            delay = 0 if stage == 'vision' and healthy_continuation and progressing else 5
            work.release(claim,deferred=healthy_continuation,retry_delay=delay,
                         retry_reason='progress' if progressing else 'barrier_wait')
        else:
            work.finish(claim)
        current=store._required(store.wave_key(wave_id))
        relay_current(work,store,current,send_stage)
        if current['state']=='complete':
            import index_events
            index_events.publish(library,'wave',current['completionReceiptId'])
        if current['state'] in ('ready','indexing','complete'):
            relay_successor(work,store,wave_id,send_stage)
        return 'done'
    except MutationContention:
        current = store._required(store.wave_key(wave_id))
        intent = work._get(claim.key)
        if (stage != 'vision' or current['state'] not in ('processing_pending', 'processing')
                or intent is None or intent.get('owner') != claim.owner):
            if intent is not None and intent.get('owner') == claim.owner:
                work.release(claim)
            raise
        # Source results precede coordination/checkpoint writes. Retry those
        # through the normal durable release, without waiting out the queue's
        # five-minute visibility timeout or admitting another model attempt.
        # Keep this failure charged to the dispatch budget: contention cannot
        # create an unbounded healthy-continuation loop.
        delay = min(30, 2 ** min(claim.attempt, 5))
        work.release(claim, retry_delay=delay, retry_reason='coordination_conflict')
        relay_current(work, store, current, send_stage)
        processing_diagnostics.emit('vision_coordination_retry', wave_id=wave_id,
            attempt=claim.attempt, delay_seconds=delay)
        return 'done'
    except Exception:
        try: work.release(claim)
        except Exception: pass
        raise
