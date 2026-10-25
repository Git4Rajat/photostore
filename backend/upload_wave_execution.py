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


def _execution_check(leases, ownership, store, outbox_key):
    from execution_lease import OwnershipLost
    if not execution_enabled():
        raise OwnershipLost('Wave execution is disabled')
    policy=store._get('policy')
    if policy and policy.get('cleanupState'):
        raise OwnershipLost('Library cleanup has fenced wave execution')
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
            ):
        # Never ask the adapter to process a replacement, deleted source or
        # somebody else's active source lease for this frozen obligation.
        return None, None
    if any(row.get(k) for k in ('processing_lease_owner','processing_lease','processing_lease_expires_at')):
        return None, row
    identity = row.get('processingCompletionId')
    if not identity:
        return None, row
    completion = _completion(store, member, identity)
    if completion is None and row.get('processingCompletionOutbox'):
        from processing_completion import relay_completion
        from processing_receipts import ArtifactReceipts
        head = json.loads(row['processingCompletionOutbox'])[0]
        if (all(head.get(key) == value for key, value in dict(library=store.library,
                filename=member['filename'], photoId=member['photoId'],
                revision=member['processingRevision'], generation=row.get('processingGeneration')).items())
                and ArtifactReceipts(store.table, store.library).is_video_status_mismatch(head)):
            return None, row  # Requalify under a source lease before replacing this invalid intent.
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


def _process_vision_member(store, wave_id, member, metadata_table, process_source, renew, progressed):
    from upload_waves import digest
    from wave_settlement import source_state, attempt, exhausted, verified
    from storage_utils import _processing_is_expired
    if member['outcome'] not in ('pending', 'settled') and verified(store, member): return
    if source_state(store, wave_id, member, metadata_table): return
    completion, row = _observe_qualified(store, member, metadata_table)
    leased = bool(row and any(row.get(k) for k in ('processing_lease_owner', 'processing_lease', 'processing_lease_expires_at')))
    if completion is None and row is not None and (not leased or _processing_is_expired(row)):
        if process_source is None:
            raise NotImplementedError('A fenced source-processing adapter is required')
        count = attempt(store, member)
        progressed()  # Start a source's own watchdog window.
        try:
            process_source(store.library, member['filename'], member['processingRevision'], renew)
        except Exception:
            renew()  # Ownership failures are never source failures.
            # exhausted() is the single authority on whether this source still
            # has budget left -- it settles terminally once count reaches
            # MAX_SOURCE_ATTEMPTS, independent of what kept failing (a step
            # result, or a bare exception with no step to blame). Below budget,
            # re-raise: the caller isolates this one member so the exception
            # never blocks its wave-mates, and the next wake retries.
            if not exhausted(store, wave_id, member, metadata_table, count):
                raise
        renew()
        completion, row = _observe_qualified(store, member, metadata_table)
        if completion is None: exhausted(store, wave_id, member, metadata_table, count)
    leased = bool(row and any(row.get(k) for k in ('processing_lease_owner', 'processing_lease', 'processing_lease_expires_at')) and not _processing_is_expired(row))
    store.observe(wave_id, member['photoId'], member['processingRevision'],
        outcome='settled' if completion else 'pending',
        evidence_id=completion[0]['id'] if completion else digest(['pending', member['photoId'], member['processingRevision']]),
        evidence_qualified=bool(completion), outstanding_leases=int(leased and completion is None))


def run_vision_release(leases, work_store, store, library, wave_id, outbox_key,
                       metadata_table, face_table, face_ids, cluster_send, *, process_source=None,
                       deadline=None, progress=None, before_freeze=None):
    """Resume bounded source/evaluation passes using qualified receipts.

    A checkpoint is a scan position, never proof of source completion. Each
    complete pass evaluates all evidence, then revisits unresolved sources on
    the next pass. Healthy yields release ownership and keep their dispatch
    revision; the runtime queues the continuation before acknowledging its wake.
    """
    import time
    from execution_lease import execution_scope, OwnershipLost
    from upload_waves import MAX_PAGE
    ownership, wave = claim_release(leases, store, library, wave_id, 'vision', outbox_key, 'vision')
    if ownership is None:
        return None
    if ownership.status == 'succeeded':
        return wave
    def renew():
        _execution_check(leases, ownership, store, outbox_key)
        leases.renew(ownership)
    def checkpoint(phase, after=''):
        leases.checkpoint(ownership, 'vision:' + json.dumps(dict(phase=phase, after=after)))
    def expired():
        return deadline is not None and time.monotonic() >= deadline
    def progressed():
        if progress is not None: progress()
    saved = leases.row(ownership).get('progressCheckpoint', '')
    position = json.loads(saved[7:]) if saved.startswith('vision:') else {}
    phase, cursor = position.get('phase', 'sources'), position.get('after', '')
    try:
        with execution_scope(leases, ownership, store, lambda: _execution_check(leases, ownership, store, outbox_key)), leases.keepalive(ownership):
            while phase == 'sources':
                if expired(): return store._required(store.wave_key(wave_id))
                renew()
                members = store._page(f'e!{wave_id}!m!', cursor)
                for member in members:
                    if expired(): return store._required(store.wave_key(wave_id))
                    renew()
                    try:
                        _process_vision_member(store, wave_id, member, metadata_table,
                            process_source, renew, progressed)
                    except (OwnershipLost, NotImplementedError):
                        raise  # Ownership loss and a missing adapter are never per-source outcomes.
                    except Exception:
                        # No single member may abort the whole pass -- the exact
                        # production incident this closes: one source's
                        # exception used to propagate out of this loop
                        # uncaught, so every other member in the page never
                        # got its turn. Only reaches here while this source is
                        # still under its retry budget (_process_vision_member
                        # re-raises in that case only -- once exhausted() fires
                        # it settles the source terminally and does not
                        # re-raise), so the next wake simply retries it; this
                        # pass moves on to the rest of the page now.
                        import app
                        try:
                            app.app.logger.exception(
                                'Vision member processing failed for %s/%s; continuing wave',
                                library, member.get('filename'))
                        except Exception:
                            pass
                    cursor = member['RowKey']
                    checkpoint('sources', cursor)
                    progressed()
                if len(members) < MAX_PAGE:
                    phase = 'evaluate'
                    checkpoint(phase)
            if before_freeze is not None:
                if expired(): return store._required(store.wave_key(wave_id))
                before_freeze()
            while True:
                if expired(): return store._required(store.wave_key(wave_id))
                renew()
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
        # keepalive has stopped before releasing, so it cannot renew a yielded
        # lease. A replacement owner is never cleared by this attempt's cleanup.
        current = leases._get(ownership.key)
        if current and current.get('owner') == ownership.owner:
            try: leases.release(ownership)
            except OwnershipLost: pass


def _iter_wave_faces(store, wave_id, *, renew=lambda: None):
    """Read one bounded member page and one immutable face snapshot at a time."""
    from processing_receipts import ArtifactReceipts
    from upload_waves import MAX_PAGE
    receipts = ArtifactReceipts(store.table, store.library)
    after = ''
    while True:
        renew()
        page = store._page(f'e!{wave_id}!m!', after, size=MAX_PAGE)
        for member in page:
            renew()
            if member['outcome']!='settled':
                from wave_settlement import verified
                if verified(store,member):continue
            if member['outcome'] != 'settled' or not member.get('evidenceQualified'):
                raise ValueError('Frozen member lacks qualified completion evidence')
            completion = _completion(store, member, member['evidenceId'])
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
        with leases.keepalive(ownership) as owned:
            def check():
                owned()
                _execution_check(leases, ownership, store, outbox_key)
            assignment_receipt = run_frozen_assignment(store, wave, assigner,
                lambda: _iter_wave_faces(store, wave_id, renew=check), check,
                page_size=128 if chunk_size is None else chunk_size,
                progress=lambda count: leases.checkpoint(ownership, 'faces-applied:' + str(count)))
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
