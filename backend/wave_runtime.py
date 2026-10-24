"""Queue adapters for the existing role-specific workers. Default disabled.

No worker executes another role's stage and no API request builds a catalog.
The release record remains authoritative; queue messages are advisory wakes.
"""
import json
import os
import threading
import uuid
from contextlib import contextmanager

from upload_waves import ShadowWaves, digest
from execution_lease import ExecutionLeases
from wave_policy import source_change
from upload_wave_execution import (execution_enabled,stage_work_store,run_vision_release,
    run_cluster_release,run_index_release,release_and_relay)

TYPE='upload_wave_stage'
ROLES={'vision':'vision','cluster':'cluster','index':'index-worker'}


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
    thread=threading.Thread(target=heartbeat,name='wave-source-lease',daemon=True); thread.start()
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


def consume(payload):
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
    try:
        # Dispatcher bookkeeping is renewed separately from stage ownership.
        with publication(work,claim):
            if stage=='vision':
                result=run_vision_release(leases,work,store,library,wave_id,key,
                    app.metadata_table_client,None,None,send_stage,process_source=process_source)
            elif stage=='cluster':
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
        if result and result['state'] in ('processing_pending','processing'):
            work.release(claim,deferred=True)
        else:
            work.finish(claim)
        current=store._required(store.wave_key(wave_id))
        if current['state']=='indexing':
            release_and_relay(work,store,library,current['indexPendingKey'],'index',send_stage)
        if current['state']=='complete':
            import index_events
            index_events.publish(library,'wave',current['completionReceiptId'])
        return 'done'
    except Exception:
        try: work.release(claim)
        except Exception: pass
        raise
