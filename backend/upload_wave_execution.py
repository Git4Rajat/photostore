"""Durable stage release/dispatch, and gated Vision/cluster/index consumer
entry points -- see docs/indexing-clustering-target-state.md step 3.

Enabling dispatch or the consumer entry points below does not permit live
cutover: `live_consumers_available()` stays False until the remaining
assignment/publication/recovery gates (step 5/6 publisher integration) are
also satisfied. Stage intents must never enter the index-maintenance
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

from index_work import WorkStore

STAGE_PARTITION = 'upload-wave-stage-pending'
MAX_SWEEP_PAGES = 50


def stage_work_store(table, **kwargs):
    return WorkStore(table, partition=STAGE_PARTITION, **kwargs)


def live_consumers_available():
    # Replace with a validated durable cutover/capability protocol when the
    # real Vision/cluster/index consumers and source fencing are implemented.
    return False


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


def _execution_check(leases, ownership, store, outbox_key):
    from execution_lease import OwnershipLost
    if not execution_enabled():
        raise OwnershipLost('Wave execution is disabled')
    wave = store._required(store.wave_key(ownership.waveId))
    outbox = store._required(outbox_key)
    if (not store.outbox_eligible(wave, outbox)
            or _release_input_revision(outbox, wave) != ownership.inputRevision):
        raise OwnershipLost('Stage release is no longer current')
    leases.check(ownership)


def _completion(store, member, identity):
    from processing_completion import validate_intent
    from processing_receipts import ArtifactReceipts
    from upload_waves import digest
    key = 'source-completion!' + digest([member['photoId'], member['processingRevision']]) + '!' + identity
    inbox = store._get(key)
    if inbox is None or not inbox.get('artifactReceiptQualified'):
        return None
    intent = dict(json.loads(inbox['payload']), id=inbox['completionId'])
    validate_intent(intent, store.library)
    if any(intent.get(key) != member.get(other) for key, other in
           [('filename', 'filename'), ('photoId', 'photoId'), ('revision', 'processingRevision')]):
        raise ValueError('Completion does not match the frozen member')
    if set(intent['requiredSteps']) != set(json.loads(member['requiredSteps'])):
        raise ValueError('Completion required steps differ from admission')
    manifest = ArtifactReceipts(store.table, store.library).verify_reference(intent['artifactReceipt'], intent)
    return intent, manifest


def _observe_qualified(store, member, metadata_table):
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
            or any(row.get(k) for k in ('processing_lease_owner', 'processing_lease', 'processing_lease_expires_at'))):
        # Never ask the adapter to process a replacement, deleted source or
        # somebody else's active source lease for this frozen obligation.
        return None, None
    identity = row.get('processingCompletionId')
    if not identity:
        return None, row
    completion = _completion(store, member, identity)
    if completion is None and row.get('processingCompletionOutbox'):
        from processing_completion import relay_completion
        relay_completion(metadata_table, store.library, member['filename'], store.accept_source_completion)
        completion = _completion(store, member, identity)
    if completion is None or completion[0]['generation'] != row.get('processingGeneration'):
        return None, row
    from processing_generation import contract_id
    contract = json.loads(row.get('processingContract') or '{}')
    if contract_id(contract) != row['processingGeneration']:
        raise ValueError('Processing contract identity mismatch')
    from processing_receipts import ArtifactReceipts
    receipts = ArtifactReceipts(store.table, store.library)
    for snapshot in completion[1]['faceSnapshots']:
        face = json.loads(receipts._get('face!' + snapshot)['payload'])
        if face.get('embeddingVersion') not in contract.get('faceEmbeddingVersions', []):
            raise ValueError('Face embedding does not satisfy the frozen model contract')
    return completion, row


def run_vision_release(leases, work_store, store, library, wave_id, outbox_key,
                       metadata_table, face_table, face_ids, cluster_send, *, process_source=None):
    """Process unresolved sources through an explicit source-worker adapter.

    process_source(library, filename, revision, renew) must use the existing
    source-lease/result-fence path. No adapter means unfinished source work
    is rejected, never silently treated as a completed Vision execution.
    Readiness uses durable qualified receipts, never shadow observations.
    """
    from execution_lease import execution_scope
    from upload_waves import MAX_PAGE, digest
    ownership, wave = claim_release(leases, store, library, wave_id, 'vision', outbox_key, 'vision')
    if ownership is None:
        return None
    if ownership.status == 'succeeded':
        return wave
    def renew():
        _execution_check(leases, ownership, store, outbox_key)
        leases.renew(ownership)
    with execution_scope(leases, ownership, store, lambda: _execution_check(leases, ownership, store, outbox_key)):
        # Revisit earlier pending members on every pass; a cursor is not proof
        # their asynchronous processing completed.
        cursor = ''
        while True:
            renew()
            members = store._page(f'e!{wave_id}!m!', cursor)
            for member in members:
                renew()
                completion, row = _observe_qualified(store, member, metadata_table)
                if completion is None and row is not None:
                    if process_source is None:
                        leases.fail(ownership, 'source_adapter_required')
                        raise NotImplementedError('A fenced source-processing adapter is required')
                    process_source(library, member['filename'], member['processingRevision'], renew)
                    renew()
                    completion, row = _observe_qualified(store, member, metadata_table)
                store.observe(wave_id, member['photoId'], member['processingRevision'],
                    outcome='settled' if completion else 'pending',
                    evidence_id=completion[0]['id'] if completion else digest(['pending', member['photoId'], member['processingRevision']]),
                    evidence_qualified=bool(completion))
            cursor = members[-1]['RowKey'] if len(members) == MAX_PAGE else ''
            leases.checkpoint(ownership, cursor)
            if not cursor:
                break
        while True:
            renew()
            wave = store.evaluate(wave_id)
            if wave['state'] == 'ready' or not wave.get('evaluation'):
                break
    if wave['state'] != 'ready':
        leases.release(ownership)
        return wave
    leases.complete(ownership, wave['frozenRevision'])
    release_and_relay(work_store, store, library, 'shadow-cluster!' + wave_id, 'cluster', cluster_send)
    return wave


def _load_wave_faces(store, wave_id, face_table=None, face_ids=None, *, renew=lambda: None):
    """Only immutable evidence selected at readiness may supply face inputs."""
    from processing_receipts import ArtifactReceipts
    from upload_waves import MAX_PAGE
    receipts = ArtifactReceipts(store.table, store.library)
    members, face_rows, after = {}, {}, ''
    while True:
        renew()
        page = store._page(f'e!{wave_id}!m!', after, size=MAX_PAGE)
        for member in page:
            renew()
            if member['outcome'] != 'settled' or not member.get('evidenceQualified'):
                raise ValueError('Frozen member lacks qualified completion evidence')
            completion = _completion(store, member, member['evidenceId'])
            if completion is None:
                raise ValueError('Frozen completion receipt is missing')
            intent, manifest = completion
            ids = []
            for snapshot_id in manifest['faceSnapshots']:
                row = json.loads(receipts._get('face!' + snapshot_id)['payload'])
                face_id = row['RowKey']
                if (row.get('filename') != member['filename']
                        or row.get('sourcePhotoId') != member['photoId']
                        or row.get('processingRevision') != member['processingRevision']
                        or face_id in face_rows):
                    raise ValueError('Frozen face identity mismatch')
                ids.append(face_id)
                face_rows[face_id] = row
            if len(ids) != manifest['faceCount']:
                raise ValueError('Frozen face coverage mismatch')
            members[member['filename']] = ids
        if len(page) < MAX_PAGE:
            return members, face_rows
        after = page[-1]['RowKey']


def run_cluster_release(leases, store, library, wave_id, outbox_key, *,
                        assigner, journal, obligations_table, face_table, face_ids,
                        required_scopes=('people',), chunk_size=None):
    """Validate frozen inputs. Nonempty assignment stays blocked until step 4.

    The legacy assigner reloads live rows and has no stage-fenced per-face
    commit path. Passing snapshots as ordering inputs does not fix that;
    reject it before any mutations rather than claim crash-safe execution.
    """
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
        members, face_rows = _load_wave_faces(store, wave_id, renew=renew)
        renew()
        if face_rows:
            leases.fail(ownership, 'fenced_assignment_not_implemented')
            raise NotImplementedError('Nonempty wave assignment requires the step 4 fenced journal')
        ledger = ObligationLedger(obligations_table, wave_id, library=library, frozen_revision=wave['frozenRevision'])
        frontier = ledger.record_required(compute_obligations(library, wave_id, wave['frozenRevision'], list(required_scopes)))
        next_wave = store.commit_assignment(wave_id, frontier, manifest_digest=wave['manifestDigest'])
    leases.complete(ownership, frontier)
    return next_wave


def run_index_release(leases, store, library, wave_id, outbox_key, ledger):
    from execution_lease import execution_scope
    ownership, wave = claim_release(leases, store, library, wave_id, 'index', outbox_key, 'index')
    if ownership is None:
        return None
    if ownership.status == 'succeeded':
        return wave
    with execution_scope(leases, ownership, store, lambda: _execution_check(leases, ownership, store, outbox_key)):
        if not ledger.is_complete():
            leases.fail(ownership, 'publication_incomplete')
            return wave
        result = store.complete_indexing(wave_id, ledger)
    leases.complete(ownership, result.get('completionReceiptId', ''))
    return result
