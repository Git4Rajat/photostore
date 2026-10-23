"""Source observation adapter for the opt-in shadow API.

Legacy source writers have no wave-generation fence. These are observations of
current persisted data, never qualified production completion receipts.
"""
import json
import math

from azure.core.exceptions import ResourceNotFoundError

from storage_utils import BROWSER_PROCESSING_STEPS as STEPS
from upload_waves import MAX_PAGE, Conflict, digest

TERMINAL = {'done', 'no_data', 'skipped', 'unsupported'}


def source_identity(row):
    """Returns (photo_id, processing_revision, qualified).

    qualified is True once the row carries a real source-allocated
    processingRevision (stamped once, immutably, by storage_utils.
    finalize_uploaded_file). Rows finalized before that field existed fall
    back to a physical-identity/hash proxy and report qualified=False --
    that proxy is sufficient only for observational fresh-upload
    comparisons; it is not a real reprocessing generation fence.
    """
    photo = str(row.get('anonymousImageId') or '')
    if not photo or row.get('processing_state') == 'deleted' or row.get('pendingAnonymousBlob'):
        raise Conflict('A finalized, nondeleted photo is required')
    real_revision = str(row.get('processingRevision') or '')
    if real_revision:
        return photo, real_revision, True
    return photo, digest(['shadow-source-v1', photo, row.get('fileHash') or '']), False


def resolve_source(store, metadata_table, session, item, outcome, filename):
    if outcome not in ('accepted', 'duplicate'):
        return store.resolve(session, item, outcome=outcome)
    row = metadata_table.get_entity(store.library, filename)
    photo, revision, _qualified = source_identity(row)
    return store.resolve(session, item, outcome=outcome, filename=filename,
                         photo_id=photo, processing_revision=revision, required_steps=STEPS)


def observe_source(store, member, metadata_table, face_table, face_ids):
    """Read authoritative metadata + complete face lookup; missing proof waits.

    An expired but uncleared lease remains unresolved. Missing source is not a
    deletion receipt; a tombstone must be present. No failed step is silently
    interpreted as exhausted retries. Transport errors propagate to the caller.
    """
    try:
        row = metadata_table.get_entity(store.library, member['filename'])
    except ResourceNotFoundError:
        return dict(outcome='pending', evidence_id=digest(['source_missing', member['photoId']]))
    proof = {'photo': row.get('anonymousImageId'), 'hash': row.get('fileHash'),
             'etag': getattr(row, 'metadata', {}).get('etag'), 'qualified': False}
    if str(row.get('anonymousImageId') or '') != member['photoId']:
        return dict(outcome='pending', evidence_id=digest([proof, 'photo_replaced']))
    if row.get('processing_state') == 'deleted':
        return dict(outcome='deleted', evidence_id=digest([proof, row.get('deletedAt')]))
    _, revision, qualified = source_identity(row)
    proof['qualified'] = qualified
    if revision != member['processingRevision']:
        return dict(outcome='pending', evidence_id=digest([proof, 'source_changed']))
    leased = bool(row.get('processing_lease_owner') or row.get('processing_lease')
                  or row.get('processing_lease_expires_at'))
    steps = json.loads(member['requiredSteps'])
    proof['statuses'] = {step: str(row.get(step + '_status') or '') for step in steps}
    outcome = 'pending'
    if not leased and all(value in TERMINAL for value in proof['statuses'].values()):
        outcome = 'settled'
        # Even a zero-face result needs a complete empty lookup, not absence.
        if row.get('face_status') in ('done', 'no_data'):
            ids = face_ids(store.library, member['filename'])
            if (ids is None or len(ids) > 128 or len(set(ids)) != len(ids)
                    or type(row.get('faceCount')) is not int or row['faceCount'] != len(ids)
                    or (ids and face_table is None)):
                outcome = 'pending'
            else:
                proof['faces'] = []
                for face_id in ids:
                    try:
                        face = face_table.get_entity(store.library, face_id)
                    except ResourceNotFoundError:
                        outcome = 'pending'
                        break
                    if face.get('filename') != member['filename']:
                        outcome = 'pending'
                        break
                    try:
                        vector = json.loads(face.get('embedding') or '[]')
                    except (TypeError, ValueError):
                        vector = []
                    if (not isinstance(vector, list) or not vector
                            or any(type(v) not in (int, float) or not math.isfinite(v) for v in vector)
                            or not any(vector) or not face.get('embeddingVersion')):
                        outcome = 'pending'
                        break
                    proof['faces'].append([face_id, face['embeddingVersion'], digest(vector),
                                          getattr(face, 'metadata', {}).get('etag')])
    return dict(outcome=outcome, evidence_id=digest(proof), outstanding_leases=int(leased),
                evidence_qualified=qualified)


def compare_against_legacy(store, wave_id):
    """Read-only diagnostic: how much longer the library-wide shadow barrier
    made each photo wait versus its own completion time.

    There is no durable, queryable record of when legacy per-photo/early-
    trigger clustering would have fired for a given filename --
    _buffer_incremental_assign_filename (app.py) is an in-process, ephemeral
    buffer, not a persisted dispatch log (see docs/indexing-clustering-
    target-state.md's "Producer batching" row). The nearest durable proxy is
    each member's own observe() evidence (`observedAt`, written once that
    photo's required processing settled): that is the earliest instant a
    per-photo trigger could plausibly have started clustering on it. This
    compares that against the wave's barrier (frozenAt once ready, else the
    last evaluation pass) to report the extra wait the wave protocol adds.
    Never writes, never dispatches -- purely reads already-persisted shadow
    evidence.
    """
    from upload_waves import identifier
    identifier(wave_id)
    wave = dict(store._required(store.wave_key(wave_id)))
    members, after = [], ''
    while True:
        page = store._page(f'e!{wave_id}!m!', after)
        members.extend(page)
        if len(page) < MAX_PAGE:
            break
        after = page[-1]['RowKey']
    settled = [member for member in members if member['outcome'] != 'pending' and member.get('observedAt')]
    barrier_at = wave.get('frozenAt') or wave.get('lastEvaluatedAt')
    waits = sorted(max(0.0, barrier_at - member['observedAt']) for member in settled) if barrier_at else []
    qualified = sum(1 for member in settled if member.get('evidenceQualified'))
    return dict(
        waveId=wave_id, state=wave['state'],
        memberCount=len(members), settledMemberCount=len(settled), qualifiedMemberCount=qualified,
        barrierAt=barrier_at,
        extraWaitSecondsMin=waits[0] if waits else None,
        extraWaitSecondsMedian=waits[len(waits) // 2] if waits else None,
        extraWaitSecondsMax=waits[-1] if waits else None,
    )


def reconcile_page(store, wave_id, metadata_table, face_table, face_ids, *, after=''):
    from upload_waves import identifier
    identifier(wave_id)
    prefix = f'e!{wave_id}!m!'
    if after and (not after.startswith(prefix) or len(after) != len(prefix) + 64
                  or any(char not in '0123456789abcdef' for char in after[len(prefix):])):
        raise ValueError('Invalid reconciliation cursor')
    members = store._page(prefix, after)
    qualified_flags = []
    for member in members:
        from processing_completion import completion_capture_enabled, relay_completion
        if completion_capture_enabled():
            try:
                relay_completion(metadata_table, store.library, member['filename'], store.accept_source_completion)
            except ResourceNotFoundError:
                pass  # Missing source is still a pending observation below.
        observation = observe_source(store, member, metadata_table, face_table, face_ids)
        qualified_flags.append(bool(observation.get('evidence_qualified')))
        store.observe(wave_id, member['photoId'], member['processingRevision'], **observation)
    # True only when every member scanned on THIS page carries a real
    # source-allocated processingRevision (see source_identity), not the
    # legacy physical-identity/hash proxy. Page-scoped, like `scanned` --
    # a caller aggregating across pages must AND this across every page it
    # has seen, not just trust the last one.
    return dict(scanned=len(members), nextCursor=members[-1]['RowKey'] if len(members) == MAX_PAGE else '',
                evidenceQualified=bool(qualified_flags) and all(qualified_flags), dispatchable=False)
