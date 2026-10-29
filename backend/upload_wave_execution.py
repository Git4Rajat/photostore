"""Durable stage release/dispatch, and gated Vision/cluster/index consumer
entry points -- see docs/indexing-clustering-target-state.md step 3.

New libraries additionally require durable ownership and a qualified capability
certificate; configuration alone never converts an existing library. Stage intents must never enter the index-maintenance
recovery partition -- `stage_work_store` uses its own isolated partition,
confirmed by test_indexer_recovery_cannot_see_wave_stage_intents.

Three ownership concepts stay distinct throughout this module and must not
be conflated: `work_store` (index_work.WorkStore) is the DISPATCHER's
temporary claim that a queue send is in flight for one outbox -- a
successful send never means the stage's work completed. `leases`
(execution_lease.ExecutionLeases) is the WORKER's renewable per-(library,
wave, stage) ownership for the whole duration of that stage's work. Neither
is the per-photo source-processing lease (storage_utils.
claim_processing_lease), which belongs to an individual photo, not a wave
stage.
"""
import os
import json

import processing_diagnostics

from index_work import WorkStore

STAGE_PARTITION = 'upload-wave-stage-pending'
MAX_SWEEP_PAGES = 50


def stage_work_store(table, **kwargs):
    return WorkStore(table, partition=STAGE_PARTITION, **kwargs)


def live_consumers_available():
    # Configuration hint only: actual live admission/consumption also validates
    # durable ownership and the role's certified deployment identity.
    from wave_policy import configuration_ready
    return configuration_ready() and bool(os.getenv('UPLOAD_WAVE_CAPABILITY_ID'))


def execution_enabled():
    return os.getenv('UPLOAD_WAVE_EXECUTION_ENABLED', '').strip().lower() == 'true'


def dispatch_outbox(work_store, store, library, outbox_key, destination):
    """Prepare an isolated intent for an eligible outbox, without queue send.

    The historical dispatchable flag records preparation, not a live claim.
    Returns True for a new preparation; False when execution is disabled, the
    outbox row doesn't exist yet, or it was already dispatched (so a caller
    like sweep_library can distinguish a new preparation from a replay).
    """
    if not execution_enabled():
        return False
    if work_store.partition != STAGE_PARTITION:
        raise ValueError('Wave stage intents require an isolated work partition')
    if library != store.library:
        raise ValueError('Wave library identity mismatch')
    outbox = store._get(outbox_key)
    if outbox is None or outbox.get('dispatchable'):
        return False
    if destination not in ('vision', 'cluster', 'index') or outbox.get('destination') != destination:
        raise ValueError('Wave stage destination mismatch')
    wave = store._required(store.wave_key(outbox['waveId']))
    if not store.outbox_eligible(wave, outbox):
        return False
    # Closing a reopened intake creates a new revision-specific intent, not
    # an alias of a possibly leased, obsolete Vision release.
    from upload_waves import digest
    scope = f'wave-{destination}:{outbox["waveId"]}:{digest(outbox_key)}'
    work_store.request(library, scope, advance=False)
    store.set_dispatchable(outbox_key, dispatchable=True)
    return True


def advance_wave(work_store, store, library, wave_id):
    """Single-wave driver: promotes out of waiting_predecessor if eligible,
    then dispatches whichever outbox the wave's current state makes
    eligible (visionPendingKey while processing_pending, the deterministic
    shadow-cluster! outbox once ready). Safe to call on any wave in any
    state -- every branch is a no-op when not applicable.
    """
    wave = store.promote_successor(wave_id)
    from wave_policy import owned
    if owned(store.table,library):
        from wave_no_work import advance_empty,advance_zero_faces
        wave = advance_empty(store,wave_id)
        wave = advance_zero_faces(store,wave_id)
    dispatched = []
    if wave['state'] in ('processing_pending', 'processing') and wave.get('visionPendingKey'):
        if dispatch_outbox(work_store, store, library, wave['visionPendingKey'], 'vision'):
            dispatched.append('vision')
    if wave['state'] == 'ready':
        if dispatch_outbox(work_store, store, library, 'shadow-cluster!' + wave_id, 'cluster'):
            dispatched.append('cluster')
    if wave['state'] == 'indexing' and wave.get('indexPendingKey'):
        if dispatch_outbox(work_store, store, library, wave['indexPendingKey'], 'index'):
            dispatched.append('index')
    return wave, dispatched


def sweep_library(work_store, store, library, *, limit=MAX_SWEEP_PAGES):
    """Bounded scan over this library's wave rows, advancing each one.

    Serves both roles the target doc asks for: an opportunistic call right
    after a wave transition, and the independent recovery sweep for crash
    recovery (re-running this is always safe -- every step it takes is
    idempotent). Returns the waves it touched for diagnostics/tests.
    """
    touched = []
    after = ''
    for _ in range(limit):
        page = store._page('wave!', after)
        if not page:
            break
        for row in page:
            wave, dispatched = advance_wave(work_store, store, library, row['waveId'])
            if dispatched or wave['state'] != row['state']:
                touched.append({'waveId': row['waveId'], 'state': wave['state'], 'dispatched': dispatched})
        after = page[-1]['RowKey']
        if len(page) < 20:
            break
    return touched


def release_and_relay(work_store, store, library, outbox_key, destination, send):
    """Combines dispatch_outbox's durable WorkStore request with the actual
    relay send, covering all three handoffs via one generic path (intake
    closed -> Vision, frozen wave -> cluster, assignment committed ->
    index). Crash before send: the WorkStore request already landed
    durably, so a later work_store.recover() sweep retries the notify.
    Lost ack: notify() reserves a wakeUntil window before calling send(),
    so a concurrent/duplicate relay call during that window is a no-op
    (returns False) rather than sending twice -- only recover(), after that
    reservation itself expires, retries a genuinely lost send. Returns
    False when execution is disabled, the outbox doesn't exist, or nothing
    new needed sending (already relayed and still within its reservation).
    """
    if not execution_enabled():
        return False
    if library != store.library or work_store.partition != STAGE_PARTITION:
        raise ValueError('Invalid stage dispatch scope')
    dispatch_outbox(work_store, store, library, outbox_key, destination)
    outbox = store._get(outbox_key)
    if (outbox is None or not outbox.get('dispatchable')
            or outbox.get('destination') != destination
            or not store.outbox_eligible(store._required(store.wave_key(outbox['waveId'])), outbox)):
        return False
    from upload_waves import digest
    scope = f'wave-{destination}:{outbox["waveId"]}:{digest(outbox_key)}'
    key = work_store.key(library, scope)
    return work_store.notify(key, send)


def recover_releases(work_store, store, send, *, limit=100):
    """Recovery uses the same flag and eligibility checks as first delivery."""
    if not execution_enabled():
        return 0
    if work_store.partition != STAGE_PARTITION:
        raise ValueError('Invalid stage recovery partition')
    from upload_waves import digest
    def guarded_send(library, scope, revision, delay):
        if not execution_enabled() or library != store.library:
            return
        parts = scope.split(':')
        if len(parts) != 3 or parts[0] not in ('wave-vision', 'wave-cluster', 'wave-index'):
            return
        destination = parts[0][5:]
        wave = store._get(store.wave_key(parts[1]))
        if wave is None:
            return
        key = (wave.get('visionPendingKey') if destination == 'vision' else
               wave.get('indexPendingKey') if destination == 'index' else 'shadow-cluster!' + parts[1])
        outbox = store._get(key) if key else None
        if (outbox and digest(key) == parts[2] and outbox.get('destination') == destination
                and store.outbox_eligible(wave, outbox)):
            send(library, scope, revision, delay)
    return work_store.recover(guarded_send, limit=limit, library=store.library)


# A stage dispatch gives up after 5 delivery attempts (see wave_runtime.consume's
# `claim.attempt>5` check) and is marked failed=True -- excluded from every
# future recover_releases call, by design: a wave stuck mid-dispatch shouldn't
# spin retrying something genuinely broken forever with no one ever finding
# out. But "forever, with nothing short of an operator running the manual
# /repair endpoint" is also too strong a failure mode for something that could
# just as easily be a transient infra blip (a worker crash loop during a
# deploy, a brief storage outage) that happened to coincide with all 5
# attempts. This gives it exactly one more try after enough time has passed
# for a human to plausibly have noticed and fixed the underlying cause, while
# still emitting a distinct, visible event every time it happens so chronic
# flapping isn't silent either.
STALE_FAILED_DISPATCH_SECONDS = 3600


def revive_stale_failed_dispatches(work_store, store, *, limit=20, clock=None):
    """Give each stage dispatch that's been failed=True for a long time one
    more chance: resets it to a fresh revision (failed=False, no lease/wake),
    making it eligible for the very next recover_releases call in the same
    pass. recover_releases' own outbox_eligible re-check still protects
    against resending for a wave that's since moved past that stage.
    """
    if not execution_enabled():
        return 0
    if work_store.partition != STAGE_PARTITION:
        raise ValueError('Invalid stage recovery partition')
    now = (clock or work_store.clock)()
    query = (f"PartitionKey eq '{work_store.partition}' and failed eq true"
             f" and libraryId eq '{store.library.replace(chr(39), chr(39)*2)}'"
             f" and requestedAt le {float(now - STALE_FAILED_DISPATCH_SECONDS)}")
    import itertools
    revived = 0
    for row in itertools.islice(work_store.table.query_entities(query), limit):
        if now - float(row.get('failedAt') or row.get('requestedAt') or 0) < STALE_FAILED_DISPATCH_SECONDS: continue
        work_store.request(store.library, row['scope'], advance=True)
        revived += 1
    return revived


def _release_input_revision(outbox, wave):
    if outbox['destination'] == 'vision':
        return f"{wave['waveId']}:{outbox.get('intakeEpoch')}"
    return outbox.get('inputRevision') or ''


def claim_release(leases, store, library, wave_id, stage, outbox_key, destination):
    """Validates a queue wakeup is still the exact eligible release (not an
    obsolete/duplicate one -- e.g. intake reopened under it, or a later
    revision already superseded it) before claiming renewable stage
    ownership. Returns (None, wave) for an obsolete release: there is
    nothing to do, and that is not an error -- a duplicate or late-arriving
    message for a release that no longer matches current wave state must
    be tolerated silently, never retried into a wrong action.
    """
    if not execution_enabled():
        return None, None
    if library != store.library or stage != destination:
        raise ValueError('Invalid stage execution scope')
    if leases.table is not store.table or leases.partition != store.partition:
        raise ValueError('Stage execution must share the wave table and partition')
    wave = store._required(store.wave_key(wave_id))
    outbox = store._get(outbox_key)
    if outbox is None or outbox.get('waveId') != wave_id or outbox.get('destination') != destination or not store.outbox_eligible(wave, outbox):
        return None, wave
    revision = _release_input_revision(outbox, wave)
    return leases.claim(library, wave_id, stage, revision), wave


@processing_diagnostics.wave_phase('eligibility_check')
def _execution_check(leases, ownership, store, outbox_key):
    from execution_lease import OwnershipLost
    if not execution_enabled():
        raise OwnershipLost('Wave execution is disabled')
    with processing_diagnostics.wave_phase_scope('guard_policy_read'):
        policy=store._get('policy')
    if policy and policy.get('cleanupState'):
        raise OwnershipLost('Library cleanup has fenced wave execution')
    with processing_diagnostics.wave_phase_scope('guard_wave_read'):
        wave = store._required(store.wave_key(ownership.waveId))
    with processing_diagnostics.wave_phase_scope('guard_dispatch_read'):
        outbox = store._required(outbox_key)
    if (not store.outbox_eligible(wave, outbox)
            or _release_input_revision(outbox, wave) != ownership.inputRevision):
        raise OwnershipLost('Stage release is no longer current')
    leases.check(ownership)


def _completion(store, member, identity, receipts=None, inboxes=None):
    from processing_completion import validate_intent
    from processing_receipts import ArtifactReceipts
    from upload_waves import digest
    key = 'source-completion!' + digest([member['photoId'], member['processingRevision']]) + '!' + identity
    inbox = store._get(key) if inboxes is None else inboxes.get(key)
    if inbox is None or not inbox.get('artifactReceiptQualified'):
        return None
    intent = dict(json.loads(inbox['payload']), id=inbox['completionId'])
    validate_intent(intent, store.library)
    if any(intent.get(key) != member.get(other) for key, other in
           [('filename', 'filename'), ('photoId', 'photoId'), ('revision', 'processingRevision')]):
        raise ValueError('Completion does not match the frozen member')
    if set(intent['requiredSteps']) != set(json.loads(member['requiredSteps'])):
        raise ValueError('Completion required steps differ from admission')
    receipts = receipts if receipts is not None else ArtifactReceipts(store.table, store.library)
    manifest = receipts.verify_reference(intent['artifactReceipt'], intent)
    return intent, manifest


def _observe_qualified(store, member, metadata_table, receipts=None, inboxes=None):
    from azure.core.exceptions import ResourceNotFoundError
    from processing_generation import is_superseded
    from image_utils import is_video_file
    from upload_waves import digest
    try:
        row = metadata_table.get_entity(store.library, member['filename'])
    except ResourceNotFoundError:
        return None, None
    if (row.get('anonymousImageId') != member['photoId']
            or row.get('processingRevision') != member['processingRevision']
            or row.get('processing_state') == 'deleted' or row.get('processingIsolated')
            or is_superseded(row, is_video=is_video_file(member['filename']))
            ):
        # Never ask the adapter to process a replacement, deleted source or
        # somebody else's active source lease for this frozen obligation.
        return None, None
    if any(row.get(k) for k in ('processing_lease_owner','processing_lease','processing_lease_expires_at')):
        return None, row
    identity = row.get('processingCompletionId')
    if not identity:
        return None, row
    completion = _completion(store, member, identity, receipts, inboxes)
    backlog = json.loads(row.get('processingCompletionOutbox') or '[]') if completion is None else []
    if not isinstance(backlog, list):
        raise ValueError('Processing completion outbox must be a list')
    if completion is None and backlog:
        from processing_completion import relay_completion
        from processing_receipts import ArtifactReceipts
        head = backlog[0]
        if (all(head.get(key) == value for key, value in dict(library=store.library,
                filename=member['filename'], photoId=member['photoId'],
                revision=member['processingRevision'], generation=row.get('processingGeneration')).items())
                and ArtifactReceipts(store.table, store.library).is_video_status_mismatch(head)):
            return None, row  # Requalify under a source lease before replacing this invalid intent.
        relay_completion(metadata_table, store.library, member['filename'], store.accept_source_completion)
        completion = _completion(store, member, identity, receipts)
    if completion is None or completion[0]['generation'] != row.get('processingGeneration'):
        return None, row
    from processing_generation import contract_id
    contract = json.loads(row.get('processingContract') or '{}')
    if contract_id(contract) != row['processingGeneration']:
        raise ValueError('Processing contract identity mismatch')
    from processing_receipts import ArtifactReceipts
    receipts = receipts if receipts is not None else ArtifactReceipts(store.table, store.library)
    for snapshot in completion[1]['faceSnapshots']:
        face = json.loads(receipts._get('face!' + snapshot)['payload'])
        if face.get('embeddingVersion') not in contract.get('faceEmbeddingVersions', []):
            raise ValueError('Face embedding does not satisfy the frozen model contract')
    return completion, row


def _inspect_vision_member(store, wave_id, member, metadata_table, receipts):
    from wave_settlement import source_state, verified
    from storage_utils import _processing_is_expired
    inspection = getattr(receipts, 'inspection', None)
    proofs = inspection.proofs if inspection is not None else None
    if member['outcome'] not in ('pending', 'settled') and verified(store, member, proofs=proofs):
        return None
    if source_state(store, wave_id, member, metadata_table, proofs=proofs):
        return None
    completion, row = _observe_qualified(store, member, metadata_table, receipts,
        inspection.inboxes if inspection is not None else None)
    if completion is None and row is None and member['outcome'] == 'pending':
        from wave_settlement import blocked_source
        blocked_source(store, wave_id, member, metadata_table)
        return None
    leased = bool(row and any(row.get(k) for k in
        ('processing_lease_owner', 'processing_lease', 'processing_lease_expires_at')))
    runnable = completion is None and row is not None and (not leased or _processing_is_expired(row))
    return dict(member=member, completion=completion, row=row, runnable=runnable)


@processing_diagnostics.vision_group_summary
def _process_vision_group(store, wave_id, members, metadata, process_source, renew, progressed, executor, receipts, inspection_failures=None, *, source_workers=1, expired=lambda: False, processed_members=None):
    """Parallel source effects; the coordinator alone updates wave evidence."""
    from contextvars import copy_context
    from concurrent.futures import wait, FIRST_COMPLETED
    import time
    import processing_metrics as metrics
    from execution_lease import OwnershipLost
    from wave_settlement import exhausted, inspection_failed
    from wave_source_batch import reserve_attempts
    from storage_utils import _processing_is_expired
    import logging
    logger = logging.getLogger(__name__)
    planned, counts, errors, futures = [], {}, {}, {}
    remaining = iter(members)
    exhausted_members = False
    capacity = source_workers if executor is not None else 1
    pool_started = time.monotonic()

    def process(item, submitted):
        member = item['member']
        started = time.monotonic()
        with processing_diagnostics.image_scope(member['RowKey'], 'processing'):
            metrics.add(vision_source_queue_ms=(started - submitted) * 1000)
            try:
                renew()
                progressed()
                process_source(store.library, member['filename'], member['processingRevision'], renew)
            finally:
                metrics.add(vision_source_busy_ms=(time.monotonic() - started) * 1000)

    try:
        while True:
            for error in errors.values():
                if isinstance(error, (OwnershipLost, NotImplementedError)):
                    raise error
            # Reserve only slots we can submit now; never consume the attempt
            # budget of the unstarted page tail. Only this coordinator inspects
            # members and later qualifies results; source workers own effects.
            ready = []
            admission_started = time.monotonic()
            while not exhausted_members and len(futures) + len(ready) < capacity and not expired():
                member = next(remaining, None)
                if member is None:
                    exhausted_members = True
                    break
                if processed_members is not None:
                    processed_members.append(member)
                with processing_diagnostics.image_scope(member['RowKey'], 'inspection'):
                    try:
                        item = _inspect_vision_member(store, wave_id, member, metadata, receipts)
                    except (OwnershipLost, NotImplementedError):
                        raise
                    except Exception as error:
                        settled = inspection_failed(store, wave_id, member, metadata, error)
                        if not settled and inspection_failures is not None:
                            inspection_failures[0] += 1
                        logger.exception('Vision member inspection failed for %s/%s; continuing wave',
                                         store.library, member['filename'])
                        continue
                if item is not None:
                    planned.append(item)
                    if item['runnable']:
                        ready.append(item)
            if ready:
                if process_source is None:
                    raise NotImplementedError('A fenced source-processing adapter is required')
                admitted = reserve_attempts(store, [item['member'] for item in ready])
                counts.update(admitted)
                for item in ready:
                    key = item['member']['RowKey']
                    count, allowed = admitted[key]
                    if not allowed:
                        exhausted(store, wave_id, item['member'], metadata, count)
                    elif executor is not None:
                        futures[executor.submit(copy_context().run, process, item, time.monotonic())] = key
                    else:
                        try:
                            process(item, time.monotonic())
                        except Exception as error:
                            errors[key] = error
                metrics.add(vision_admission_batches=1)
            metrics.add(vision_admission_ms=(time.monotonic() - admission_started) * 1000)
            for error in errors.values():
                if isinstance(error, (OwnershipLost, NotImplementedError)):
                    raise error
            if not futures:
                if exhausted_members or expired():
                    break
                continue
            wait_started = time.monotonic()
            done, _ = wait(futures, return_when=FIRST_COMPLETED)
            metrics.add(vision_coordinator_wait_ms=(time.monotonic() - wait_started) * 1000)
            for future in done:
                key = futures.pop(future)
                try:
                    future.result()
                except Exception as error:
                    errors[key] = error
            # The next loop refills free slots immediately, even if an earlier
            # member is still running. Checkpointing stays at a drained page.
    finally:
        # Shared executors outlive this stage. Drain even if inspection,
        # admission, submission or a source raises before releasing its fence.
        if futures:
            wait(futures)
        elapsed_ms = (time.monotonic() - pool_started) * 1000
        metrics.add(vision_pool_ms=elapsed_ms, vision_pool_capacity_ms=elapsed_ms * capacity)
    for error in errors.values():
        if isinstance(error, (OwnershipLost, NotImplementedError)):
            raise error
    # Qualifying/settling drained source results is a durable batch boundary.
    # Detect an intake reopen or lost release before interpreting those results.
    settlement_started = time.monotonic()
    receipts.inspection = None  # Post-source qualification must see newly relayed inboxes.
    getattr(renew, 'boundary', renew)()
    observations = []
    for item in planned:
        member = item['member']
        with processing_diagnostics.image_scope(member['RowKey'], 'qualification'):
            if item['runnable']:
                renew()
                metadata.invalidate(member['filename'])
                from wave_settlement import source_state
                if source_state(store, wave_id, member, metadata):
                    continue
                count, _ = counts[member['RowKey']]
                error = errors.get(member['RowKey'])
                if error is not None and not exhausted(store, wave_id, member, metadata, count):
                    logger.error('Vision source failed for %s/%s; continuing wave', store.library,
                        member['filename'], exc_info=(type(error), error, error.__traceback__))
                    continue
                try:
                    item['completion'], item['row'] = _observe_qualified(store, member, metadata, receipts)
                except OwnershipLost:
                    raise
                except Exception:
                    logger.exception('Vision receipt qualification failed for %s/%s; continuing wave',
                                     store.library, member['filename'])
                    exhausted(store, wave_id, member, metadata, count)
                    continue
                if item['completion'] is None:
                    if exhausted(store, wave_id, member, metadata, count):
                        continue
            row, completion = item['row'], item['completion']
            leased = bool(row and any(row.get(k) for k in
                ('processing_lease_owner', 'processing_lease', 'processing_lease_expires_at'))
                and not _processing_is_expired(row))
            observations.append((member, completion, leased))
    metrics.add(vision_settlement_ms=(time.monotonic() - settlement_started) * 1000)
    return observations


@processing_diagnostics.vision_wave_summary
def run_vision_release(leases, work_store, store, library, wave_id, outbox_key,
                       metadata_table, face_table, face_ids, cluster_send, *, process_source=None,
                       deadline=None, progress=None, before_freeze=None, source_workers=1, source_executor=None,
                       prepare_sources=None, inspection_failures=None, preview_loader=None,
                       member_fetch_size=1000):
    """Resume bounded source/evaluation passes using qualified receipts.

    A checkpoint is a scan position, never proof of source completion. Each
    complete pass evaluates all evidence, then revisits unresolved sources on
    the next pass. Healthy yields release ownership and keep their dispatch
    revision; the runtime queues the continuation before acknowledging its wake.
    """
    import time
    import processing_metrics as metrics
    import vision_utils
    from semantic_batching import text_batches
    from contextlib import nullcontext, ExitStack
    from concurrent.futures import ThreadPoolExecutor
    from execution_lease import execution_scope, OwnershipLost
    from upload_waves import MAX_PAGE
    from wave_source_batch import MetadataBatch, ReceiptBatch, InspectionBatch, commit_observations
    from processing_checks import CoalescedCheck
    from processing_cleanup import source_policy_scope
    from vision_buffer import MemberWindow
    from vision_prefetch import PreviewPrefetch
    if type(source_workers) is not int or not 1 <= source_workers <= MAX_PAGE:
        raise ValueError('Vision source concurrency must be between 1 and the member page size')
    ownership, wave = claim_release(leases, store, library, wave_id, 'vision', outbox_key, 'vision')
    if ownership is None:
        return None
    if ownership.status == 'succeeded':
        return wave
    stage_check = None
    def renew(*, force=False):
        # Source threads share stage observations; durable wave mutations still
        # use execution_scope's authoritative transaction guard below.
        stage_check(force=force)
    renew.boundary = lambda: renew(force=True)
    def checkpoint(phase, after=''):
        with metrics.timer('vision_checkpoint', count_name='vision_checkpoint_calls'):
            leases.checkpoint(ownership, 'vision:' + json.dumps(dict(phase=phase, after=after)))
    def expired():
        return deadline is not None and time.monotonic() >= deadline
    def progressed():
        if progress is not None: progress()
    saved = leases.row(ownership).get('progressCheckpoint', '')
    position = json.loads(saved[7:]) if saved.startswith('vision:') else {}
    phase, cursor = position.get('phase', 'sources'), position.get('after', '')
    pool = (nullcontext(source_executor) if source_executor is not None or source_workers == 1
            else ThreadPoolExecutor(max_workers=source_workers, thread_name_prefix='wave-source'))
    try:
        with execution_scope(leases, ownership, store, lambda: _execution_check(leases, ownership, store, outbox_key)), leases.keepalive(ownership, local_checks=True) as owned, source_policy_scope(store.table, library, wave_id=wave_id), text_batches(
                vision_utils.encode_text_embeddings_batch, vision_utils._encode_text_embedding_single), vision_utils.image_embedding_batches(source_workers=source_workers), ExitStack() as resources, pool as executor:
            stage_check = CoalescedCheck(
                lambda: _execution_check(leases, ownership, store, outbox_key), local=owned)
            receipts = resources.enter_context(ReceiptBatch(store.table, store.library))
            window = resources.enter_context(MemberWindow(store, wave_id, fetch_size=member_fetch_size))
            previews = resources.enter_context(PreviewPrefetch(preview_loader))
            from processing_artifacts import upload_response_enabled
            from face_write_batch import enabled as face_batch_enabled
            from face_write_batch import read_consolidation_enabled
            from semantic_batching import runtime_config as semantic_runtime_config
            semantic_config = semantic_runtime_config()
            processing_diagnostics.emit('vision_runtime_config', role='vision', wave_id=wave_id,
                artifact_upload_response_enabled=upload_response_enabled(),
                artifact_preview_reuse_enabled=os.getenv('IPWORK_ARTIFACT_PREVIEW_REUSE_ENABLED', 'true').lower() in ('1','true','yes'),
                face_persist_batch_enabled=face_batch_enabled(),
                face_apply_reads_batch_enabled=read_consolidation_enabled(),
                semantic_text_mode=semantic_config['mode'],
                semantic_text_batch_size=semantic_config['batch_size'],
                semantic_text_window_ms=semantic_config['window']*1000,
                member_fetch_size=member_fetch_size, processing_group_size=MAX_PAGE,
                source_workers=source_workers, inspection_read_workers=4,
                member_memory_limit_bytes=window.rows.memory_limit, member_disk_limit_bytes=window.rows.disk_limit,
                receipt_memory_limit_bytes=receipts.rows.memory_limit, receipt_disk_limit_bytes=receipts.rows.disk_limit,
                preview_prefetch_enabled=preview_loader is not None, preview_storage=previews.storage,
                preview_reserved_limit_bytes=previews.limit, preview_workers=previews.workers,
                model_schedule='source_serial', clip_image_batch_size=int(os.getenv('IPWORK_CLIP_IMAGE_BATCH_SIZE', '1')))
            while phase == 'sources':
                if expired(): return store._required(store.wave_key(wave_id))
                renew(force=True)
                with metrics.timer('vision_page_load', count_name='vision_page_load_calls'):
                    members = window.page(cursor)
                    with metrics.timer('vision_metadata_fetch', count_name='vision_metadata_fetch_calls'):
                        metadata = MetadataBatch(metadata_table, store.library, members)
                if members and prepare_sources is not None:
                    progressed()
                    with metrics.timer('vision_source_prepare', count_name='vision_source_prepare_calls'):
                        from storage_utils import FaceReconciliationCancelled
                        try:
                            prepare_sources(members, metadata, renew, expired)
                        except FaceReconciliationCancelled as error:
                            if error.reason != 'caller_cancelled' or not expired():
                                raise
                            # No sources from this page have been admitted yet.
                            checkpoint('sources', cursor)
                            processing_diagnostics.emit('vision_deadline_yield', wave_id=wave_id,
                                phase='source_prepare', reason='stage_deadline')
                            return store._required(store.wave_key(wave_id))
                    renew()
                    progressed()
                with metrics.timer('vision_inspection_prefetch', count_name='vision_inspection_prefetches'):
                    receipts.inspection = InspectionBatch(store, members, metadata, receipts)
                previews.prepare(members, metadata)
                processed = []
                observations = _process_vision_group(store, wave_id, members,
                    metadata, process_source, renew, progressed, executor, receipts, inspection_failures,
                    source_workers=source_workers, expired=expired, processed_members=processed)
                if processed:
                    cursor = processed[-1]['RowKey']
                    progressed()
                # A checkpoint covers only drained groups. Source results are
                # durable first; interruption before this commit is replayable
                # through receipt qualification without another model attempt.
                with metrics.timer('vision_page_commit', count_name='vision_page_commit_calls'):
                    commit_observations(store, wave_id, observations)
                checkpoint('sources', cursor)
                if expired(): return store._required(store.wave_key(wave_id))
                if window.done:
                    phase = 'evaluate'
                    checkpoint(phase)
            if before_freeze is not None:
                if expired(): return store._required(store.wave_key(wave_id))
                renew(force=True)
                before_freeze()
            while True:
                if expired(): return store._required(store.wave_key(wave_id))
                renew()
                with metrics.timer('vision_evaluation', count_name='vision_evaluation_calls'):
                    wave = store.evaluate(wave_id)
                progressed()
                if wave['state'] == 'ready' or not wave.get('evaluation'):
                    break
            if wave['state'] != 'ready':
                checkpoint('sources')  # Revisit earlier unresolved members.
        if wave['state'] != 'ready':
            return wave
        leases.complete(ownership, wave['frozenRevision'])
        wave, _ = advance_wave(work_store, store, library, wave_id)
        if wave['state'] == 'indexing':
            release_and_relay(work_store, store, library, wave['indexPendingKey'], 'index', cluster_send)
        elif wave['state'] == 'ready':
            release_and_relay(work_store, store, library, 'shadow-cluster!' + wave_id, 'cluster', cluster_send)
        return wave
    finally:
        if stage_check is not None:
            processing_diagnostics.emit('vision_stage_checks', role='vision', wave_id=wave_id,
                library_id=processing_diagnostics.file_id(library, ''), **stage_check.metrics())
        # keepalive has stopped before releasing, so it cannot renew a yielded
        # lease. A replacement owner is never cleared by this attempt's cleanup.
        current = leases._get(ownership.key)
        if current and current.get('owner') == ownership.owner:
            try: leases.release(ownership)
            except OwnershipLost: pass


def _iter_wave_faces(store, wave_id, *, renew=lambda: None):
    """Read one bounded member page and one immutable face snapshot at a time."""
    from wave_source_batch import ReceiptBatch
    with ReceiptBatch(store.table, store.library) as receipts:
        yield from _iter_wave_faces_cached(store, wave_id, receipts, renew=renew)


def _iter_wave_faces_cached(store, wave_id, receipts, *, renew):
    from upload_waves import MAX_PAGE, digest
    from table_reads import read_entities
    after = ''
    while True:
        renew()
        page = store._page(f'e!{wave_id}!m!', after, size=MAX_PAGE)
        inboxes = read_entities(store.table, store.partition, [
            'source-completion!' + digest([member['photoId'], member['processingRevision']]) + '!' + member['evidenceId']
            for member in page if member['outcome'] == 'settled'], workers=4, group_namespaces=True)
        # Prefetch only immutable rows. verify_reference still checks every
        # digest and source binding before any row becomes a clustering input.
        references = [json.loads(row['payload']).get('artifactReceipt', {})
                      for row in inboxes.values() if row is not None]
        receipts.prefetch('manifest!' + ref['id'] for ref in references
                          if isinstance(ref, dict) and isinstance(ref.get('id'), str))
        def face_keys():
            for ref in references:
                if not isinstance(ref, dict) or not isinstance(ref.get('id'), str): continue
                manifest = receipts._get('manifest!' + ref['id'])
                if manifest is not None:
                    for identity in json.loads(manifest['payload'])['faceSnapshots']:
                        yield 'face!' + identity
        receipts.prefetch(face_keys())
        for member in page:
            renew()
            if member['outcome']!='settled':
                from wave_settlement import verified
                if verified(store,member):continue
            if member['outcome'] != 'settled' or not member.get('evidenceQualified'):
                raise ValueError('Frozen member lacks qualified completion evidence')
            completion = _completion(store, member, member['evidenceId'], receipts, inboxes)
            if completion is None:
                raise ValueError('Frozen completion receipt is missing')
            intent, manifest = completion
            count = 0
            for snapshot_id in manifest['faceSnapshots']:
                renew()
                row = json.loads(receipts._get('face!' + snapshot_id)['payload'])
                if (row.get('filename') != member['filename']
                        or row.get('sourcePhotoId') != member['photoId']
                        or row.get('processingRevision') != member['processingRevision']):
                    raise ValueError('Frozen face identity mismatch')
                count += 1
                yield row
            if count != manifest['faceCount']:
                raise ValueError('Frozen face coverage mismatch')
        if len(page) < MAX_PAGE:
            return
        after = page[-1]['RowKey']


def _load_wave_faces(store, wave_id, face_table=None, face_ids=None, *, renew=lambda: None):
    # Compatibility for diagnostics/tests. The live runner uses the iterator.
    members, faces = {}, {}
    for row in _iter_wave_faces(store, wave_id, renew=renew):
        face_id = row['RowKey']
        if face_id in faces:
            raise ValueError('Duplicate frozen face')
        members.setdefault(row['filename'], []).append(face_id)
        faces[face_id] = row
    return members, faces


@processing_diagnostics.cluster_wave_summary
def run_cluster_release(leases, store, library, wave_id, outbox_key, *,
                        assigner, journal, obligations_table, face_table, face_ids,
                        required_scopes=None, chunk_size=None):
    """One journaled canonical pass over immutable inputs; no index builds."""
    from execution_lease import execution_scope
    from wave_publication import ObligationLedger, compute_obligations
    ownership, wave = claim_release(leases, store, library, wave_id, 'cluster', outbox_key, 'cluster')
    if ownership is None:
        return None
    if ownership.status == 'succeeded':
        return wave
    with execution_scope(leases, ownership, store, lambda: _execution_check(leases, ownership, store, outbox_key)):
        _execution_check(leases, ownership, store, outbox_key)
        outbox = store._required(outbox_key)
        if wave.get('manifestDigest') != outbox.get('manifestDigest'):
            raise ValueError('Cluster release input receipts no longer match the frozen wave')
        def renew():
            _execution_check(leases, ownership, store, outbox_key)
            leases.renew(ownership)
        from wave_assignment import run_frozen_assignment
        import time
        next_check = [0.0]
        with leases.keepalive(ownership, local_checks=True) as owned:
            @processing_diagnostics.wave_phase('guard_check')
            def check(force=False):
                with processing_diagnostics.wave_phase_scope('guard_keepalive_check'):
                    owned()
                if force or time.monotonic() >= next_check[0]:
                    _execution_check(leases, ownership, store, outbox_key)
                    next_check[0] = time.monotonic() + 1.0
            check.boundary = lambda: check(force=True)
            assignment_receipt = run_frozen_assignment(store, wave, assigner,
                lambda: _iter_wave_faces(store, wave_id, renew=check), check,
                page_size=128 if chunk_size is None else chunk_size,
                progress=lambda count: leases.checkpoint(ownership, 'faces-applied:' + str(count)),
                local_journal=True)
        with processing_diagnostics.wave_phase_scope('publication_handoff'):
            renew()
            from wave_catalog import SCOPES, PREREQUISITES
            scopes=tuple(SCOPES if required_scopes is None else required_scopes)
            graph={scope:deps for scope,deps in PREREQUISITES.items() if scope in scopes}
            ledger = ObligationLedger(obligations_table, wave_id, library=library,
                frozen_revision=wave['frozenRevision'], assignment_revision=assignment_receipt)
            obligations=compute_obligations(library,wave_id,wave['frozenRevision'],scopes,
                prerequisite_scopes=graph,assignment_revision=assignment_receipt)
            frontier=ledger.record_required(obligations,prerequisite_scopes=graph)
            next_wave = store.commit_assignment(wave_id, frontier, manifest_digest=wave['manifestDigest'],
                assignment_receipt=assignment_receipt)
    with processing_diagnostics.wave_phase_scope('execution_complete'):
        leases.complete(ownership, frontier)
    return next_wave


def run_index_release(leases, store, library, wave_id, outbox_key, ledger, *, publisher=None, prepare=None):
    from execution_lease import execution_scope
    ownership, wave = claim_release(leases, store, library, wave_id, 'index', outbox_key, 'index')
    if ownership is None:
        return None
    if ownership.status == 'succeeded':
        return wave
    with execution_scope(leases, ownership, store, lambda: _execution_check(leases, ownership, store, outbox_key)):
        if ledger.assignment_revision != (wave.get('assignmentReceiptId') or ''):
            raise ValueError('Index obligations do not match the committed assignment receipt')
        with leases.keepalive(ownership) as owned:
            def check():
                owned()
                _execution_check(leases,ownership,store,outbox_key)
            if publisher is not None:
                if prepare is not None:
                    prepare(check)
                manifest,specs=ledger._specs()
                if manifest is None or manifest['requiredSetId'] != wave.get('requiredPublicationId'):
                    raise ValueError('Index obligation set is not the committed wave frontier')
                remaining=dict(specs)
                while remaining:
                    advanced=False
                    for scope,spec in list(remaining.items()):
                        check()
                        if any(not ledger._valid_receipt(ledger._get(parent),manifest) for parent in spec['prerequisites']):
                            continue
                        generation=publisher(scope,spec['id'],check)
                        check()
                        ledger.record_receipt(spec['id'],output_generation=generation)
                        leases.checkpoint(ownership,'published:'+scope)
                        del remaining[scope]; advanced=True
                    if not advanced:
                        raise RuntimeError('Publication prerequisites cannot advance')
            incomplete=not ledger.is_complete()
            if not incomplete:
                from wave_no_work import record_publication_members
                record_publication_members(store,wave,check)
                result = store.complete_indexing(wave_id, ledger)
        if incomplete:
            leases.fail(ownership,'publication_incomplete')
            return wave
    leases.complete(ownership, result.get('completionReceiptId', ''))
    return result
