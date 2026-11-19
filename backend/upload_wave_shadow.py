"""Source observation adapter for the opt-in shadow API.

Legacy source writers have no wave-generation fence. These are observations of
current persisted data, never qualified production completion receipts.
"""
import json
import math
import re

from azure.core.exceptions import ResourceNotFoundError

from storage_utils import BROWSER_PROCESSING_STEPS as STEPS
from processing_completion import TERMINAL
from processing_generation import required_steps_for_media
from image_utils import is_video_file
from upload_waves import MAX_PAGE, Conflict, digest


def source_identity(row, allow_pending=False):
    """Returns (photo_id, processing_revision, qualified).

    qualified is True once the row carries a real source-allocated
    processingRevision (stamped once, immutably, by storage_utils.
    finalize_uploaded_file). Rows finalized before that field existed fall
    back to a physical-identity/hash proxy and report qualified=False --
    that proxy is sufficient only for observational fresh-upload
    comparisons; it is not a real reprocessing generation fence.
    """
    photo = str(row.get('anonymousImageId') or '')
    if not photo or row.get('processing_state') == 'deleted' or (row.get('pendingAnonymousBlob') and not allow_pending):
        raise Conflict('A finalized, nondeleted photo is required')
    real_revision = str(row.get('processingRevision') or '')
    if real_revision:
        return photo, real_revision, True
    return photo, digest(['shadow-source-v1', photo, row.get('fileHash') or '']), False


def resolve_source(store, metadata_table, session, item, outcome, filename):
    return resolve_sources(store, metadata_table, session, [(item, outcome, filename)])


def _stored_outcome(item_filename, stored_filename):
    """accepted when this item's own upload created the stored photo -- under
    its filename, or the NAME-xxxxxxxx.ext rename a filename clash gives it --
    otherwise duplicate of a photo some other upload created."""
    if stored_filename == item_filename:
        return 'accepted'
    stem, dot, ext = item_filename.rpartition('.')
    if not dot:
        stem, ext = item_filename, ''
    renamed = re.fullmatch(re.escape(stem) + r'-[0-9a-f]{8}' + (re.escape('.' + ext) if dot else ''),
                           stored_filename)
    return 'accepted' if renamed else 'duplicate'


def stored_source(item, find_stored):
    """(outcome, filename) when the library already holds this reservation's
    exact bytes, else None.

    A reservation binds the source SHA-256 at admission. Finalize stores the
    photo and its hash-index entry before the wave binding is written, so a
    finalize whose binding lost (503, lost reply, browser gave up) leaves the
    photo stored with its item still reserved. The browser can then report it
    failed or the session be discarded; neither may leave that stored photo
    outside every wave. find_stored(sha) returns the finalized filename
    holding those bytes (see storage_utils.detect_duplicates), or ''.
    """
    sha = item.get('sourceHash') or ''
    if not sha or find_stored is None:
        return None
    filename = find_stored(sha)
    if not filename:
        return None
    return _stored_outcome(item.get('filename') or '', filename), filename


def stored_filename_lookup(hash_index_table, metadata_table, library):
    """find_stored for stored_source: the finalized filename holding these
    exact bytes, or '' when the library proves it holds none.

    Unlike storage_utils.detect_duplicates (a best-effort dedup hint), storage
    errors propagate. Treating an unreadable index as "not stored" would let a
    discard cancel a photo that is stored.
    """
    def find(sha):
        if hash_index_table is None:
            raise Conflict('Stored-source lookup is unavailable')
        try:
            filename = str(hash_index_table.get_entity(library, sha).get('filename') or '')
        except ResourceNotFoundError:
            return ''
        if not filename:
            return ''
        try:
            row = metadata_table.get_entity(library, filename)
        except ResourceNotFoundError:
            return ''
        # The index row is written by finalize after the metadata row, and a
        # stale index entry (deleted or replaced photo) must not count.
        if (row.get('fileHash') != sha or row.get('processing_state') == 'deleted'
                or not row.get('anonymousImageId')):
            return ''
        return filename
    return find


def resolve_sources(store, metadata_table, session, sources, *, find_stored=None):
    """Resolve a bounded set of finalized sources with one wave mutation.

    With find_stored, a failed/cancelled report for a reservation whose bytes
    the library already stores resolves that source as a member instead (see
    stored_source). The result lists those items under adoptedItems.
    """
    if not isinstance(sources, list) or not 0 < len(sources) <= MAX_PAGE:
        raise ValueError('Expected 1-20 finalized sources')
    from table_reads import read_entities
    items = read_entities(store.table, store.partition,
        [store.item_key(session, item) for item, _outcome, _filename in sources], workers=2)
    adopted = {}
    if find_stored is not None:
        for item, outcome, _filename in sources:
            previous = items.get(store.item_key(session, item))
            if outcome in ('failed', 'cancelled') and previous is not None and previous['outcome'] == 'reserved':
                found = stored_source(previous, find_stored)
                if found:
                    adopted[item] = found
    sources = [(item, *adopted[item]) if item in adopted else (item, outcome, filename)
               for item, outcome, filename in sources]
    filenames = [filename for _item, outcome, filename in sources
                 if outcome in ('accepted', 'duplicate')]
    rows = read_entities(metadata_table, store.library, filenames, workers=4)
    resolutions = []
    for item, outcome, filename in sources:
        if outcome not in ('accepted', 'duplicate'):
            resolutions.append(dict(item=item, outcome=outcome))
            continue
        row = rows.get(filename)
        if row is None:
            raise Conflict('Finalized photo not found')
        if item in adopted and row.get('fileHash') != items[store.item_key(session, item)].get('sourceHash'):
            raise Conflict('Stored source changed while it was being adopted')
        # A duplicate resolves to an already-finalized canonical row; a pending
        # reservation left on it by a concurrent upload of the same filename is
        # irrelevant to that finalized source and must not block admission.
        # The same holds for an adopted source: its bytes are proven stored.
        photo, revision, _qualified = source_identity(row, allow_pending=outcome == 'duplicate' or item in adopted)
        previous = items.get(store.item_key(session, item))
        if previous is None:
            raise Conflict('Unknown session, member or wave')
        if previous['outcome'] in ('accepted', 'duplicate'):
            # Finalize retries can discover their own prior upload in the dedup
            # index. Keep the committed classification while validating every
            # immutable binding in resolve_many.
            outcome = previous['outcome']
        resolutions.append(dict(item=item, outcome=outcome, filename=filename,
            photo_id=photo, processing_revision=revision,
            required_steps=required_steps_for_media(is_video_file(filename))))
    result = store.resolve_many(session, resolutions)
    if adopted:
        result = dict(result, adoptedItems=[dict(itemId=item, outcome=outcome, filename=filename)
                                            for item, (outcome, filename) in adopted.items()])
    return result


def adopt_stored_reservations(store, metadata_table, session, reservations, find_stored):
    """Resolve still-reserved items whose bytes are already stored as members,
    so a discard or lease expiry that follows cancels only true unfinished
    uploads. Returns the adopted item ids."""
    found = [(item['itemId'], stored) for item in reservations
             if item['outcome'] == 'reserved' and (stored := stored_source(item, find_stored))]
    for offset in range(0, len(found), MAX_PAGE):
        page = found[offset:offset + MAX_PAGE]
        resolve_sources(store, metadata_table, session,
                        [(item, outcome, filename) for item, (outcome, filename) in page])
    return [item for item, _stored in found]


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
