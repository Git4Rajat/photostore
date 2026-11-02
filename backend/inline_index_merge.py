"""Bounded, best-effort inline merge of small CRUD-triggered index rebuilds.

CRUD edits to people/albums/sort(ratings)/access(likes)/search already enqueue a
durable Azure Queue wakeup for the dedicated `index-worker` role -- that
path is untouched and always runs regardless of anything in this module.
`core` is already warm handling the CRUD request itself, so when the
triggering change is small enough to be confident it'll take the existing
bounded merge or Search correction-slip path (not a full/embedding-heavy rebuild), this module ALSO
tries to finish the work right there, through a small bounded local queue
(never unbounded -- `core` is a 0.5 vCPU/1Gi container and must not OOM or
have its HTTP-serving threads starved by this). On success it cancels the
now-redundant durable message so `index-worker` never wakes for nothing.

Eligibility is re-derived from the same durable dirty-tracking signals the
real worker reads (get_dirty_people/get_dirty_albums/dirty-filenames), not
from whatever a caller happened to know at submission time -- the set may
have grown between submit() and the worker actually running, and trusting
only the fresh read is what keeps this safe (see
storage_utils._people_merge_eligible and friends).

Correctness note: `index_ownership.allow_inline_merge` is a narrow, explicit
exception to the index-worker-only catalog gate -- restricted to the bounded
People/Albums/Sort/Access merge scopes and the Search correction-slip scope
below. The WorkStore claim is renewed under the
same publication guard as the durable worker. A real library catalog lease
also serializes overlapping scopes; legacy libraries where that lease is a
no-op defer to durable maintenance. Strict publishers capture artifact and
manifest ETags before reading the base and condition both writes.
"""
import logging
import os
import queue as queue_module
import threading
from typing import Optional

import index_ownership
import index_publication
import index_updates
import index_work
import storage_utils

_LOGGER = logging.getLogger(__name__)

_SCOPES = index_updates.INLINE_MERGE_SCOPES

_ENABLED = os.getenv('INLINE_INDEX_MERGE_ENABLED', 'false').strip().lower() in ('1', 'true', 'yes')
_QUEUE_MAXSIZE = int(os.getenv('INLINE_INDEX_MERGE_QUEUE_SIZE', '32'))
_WORKERS = int(os.getenv('INLINE_INDEX_MERGE_WORKERS', '2'))
# Defense-in-depth independent of the per-scope eligibility caps: even a
# falsely-eligible attempt must not be allowed to run an unbounded-memory
# rebuild on a 1Gi container.
_MAX_PREV_ROWS = int(os.getenv('INLINE_INDEX_MERGE_MAX_ROWS', '5000'))
# Core performs only one small Search correction-slip pass. Larger dirty sets
# remain with the durable index-worker path.
_SEARCH_MAX_NAMES = int(os.getenv('INLINE_SEARCH_MAX_NAMES', '5000'))
# Only used by callers that also attempt inline -- see enqueue_index_build's
# delay_override. Gives the inline attempt a real window to finish and
# cancel before the durable backstop message would even become visible,
# instead of racing the default ~2s coalescing delay.
BACKSTOP_DELAY_SECONDS = int(os.getenv('INLINE_INDEX_MERGE_BACKSTOP_DELAY_SECONDS', '20'))

_queue: Optional['queue_module.Queue'] = None
_start_lock = threading.Lock()


def enabled() -> bool:
    return _ENABLED and os.getenv('APP_ROLE', 'core').strip().lower() == 'core'


def has_capacity() -> bool:
    """Cheap, non-blocking, advisory check: is the local bounded queue not
    (yet) full? No I/O -- just an in-process size check, safe to call from
    dispatch_scoped_rebuild before deciding whether to also stretch the
    durable backstop message's delay. Racy by nature (another submission
    can fill the queue between this check and the real submit() call) --
    that's fine, it only has to avoid the common case of applying a longer
    delay when inline has no chance of being attempted at all."""
    if not enabled():
        return False
    q = _queue
    return q is None or not q.full()


def _ensure_started() -> None:
    global _queue
    if _queue is not None:
        return
    with _start_lock:
        if _queue is not None:
            return
        q = queue_module.Queue(maxsize=_QUEUE_MAXSIZE)
        for i in range(_WORKERS):
            threading.Thread(target=_worker_loop, args=(q,),
                              name=f'inline-index-merge-{i}', daemon=True).start()
        _queue = q


def submit(user_id: str, scope: str, *, capture: Optional[list] = None) -> bool:
    """Non-blocking: returns False immediately (never raises, never blocks
    the calling HTTP thread) if inline merge is disabled/wrong-role, the
    scope isn't one of the bounded lexical scopes, or the local bounded
    queue is already full. A False return means nothing happens here --
    the already-sent durable queue message is the only thing that will do
    the work, exactly as before this module existed."""
    key = str(user_id or '').strip()
    if not key or not enabled() or scope not in _SCOPES:
        return False
    _ensure_started()
    try:
        _queue.put_nowait((key, scope, capture))
    except queue_module.Full:
        return False
    return True


def _worker_loop(q: 'queue_module.Queue') -> None:
    while True:
        user_id, scope, capture = q.get()
        try:
            _attempt(user_id, scope, capture)
        except Exception:
            _LOGGER.exception('Inline index merge failed for %s/%s', user_id, scope)
        finally:
            q.task_done()


def _bounded_ids(user_id, scope):
    import app
    table = getattr(app, f'{scope}_dirty_set_table_client')
    if table is not None:
        partition = user_id.replace("'", "''")
        return index_work.DirtySnapshot(table.query_entities(f"PartitionKey eq '{partition}'"), max_rows=100)
    # Legacy adapters used by tests/local configurations have no dirty table.
    return index_work.read_bounded_dirty(getattr(app, f'get_dirty_{scope}'), user_id, max_rows=100)


def _serialized_library(user_id):
    # A per-scope claim cannot fence full/light/wave publishers. Legacy
    # libraries have no shared catalog lease: retain their durable worker.
    import app
    from wave_policy import owned
    return bool(app.jobs_table_client is not None and app.blob_service_client is not None
                and owned(app.jobs_table_client, user_id))


def _conflicting_work(store, user_id, scope):
    kinds = set(index_updates.SCOPE_KINDS[scope])
    for other, other_kinds in index_updates.SCOPE_KINDS.items():
        if scope == 'search' and other != 'search':
            # Search deltas share the catalog lineage and the source rows with
            # every catalog publication.  Do not race a full/light or targeted
            # artifact publisher even though the search scope has no entries in
            # SCOPE_KINDS itself.
            row = store._get(store.key(user_id, other))
            if row is not None and float(row.get('leaseUntil') or 0) > store.clock():
                return True
        if other != scope and (kinds.intersection(other_kinds)
                or scope == 'albums' and other in index_updates.SEARCH_SCOPES):
            row = store._get(store.key(user_id, other))
            if row is not None and (not row.get('failed')
                    or float(row.get('leaseUntil') or 0) > store.clock()):
                return True
    return False


def _eligible(user_id: str, scope: str):
    """Re-derive eligibility from the live dirty-tracking state -- returns
    (ok, dirty_people, dirty_albums). Mirrors the exact scope!='full' branch
    _run_index_build_job (app.py) already runs, so the inline path and the
    real worker path can never disagree about what counts as eligible."""
    import app
    if scope != 'search' and not storage_utils._inline_artifact_within_limit(user_id, scope, _MAX_PREV_ROWS):
        return False, None, None
    if scope == 'people':
        table = app.people_dirty_set_table_client
        labels = (index_work.DirtySnapshot(table.query_entities(
            f"PartitionKey eq '{app._label_dirty_partition(user_id).replace(chr(39), chr(39) * 2)}'"), max_rows=1)
            if table is not None else app._pending_person_labels(user_id))
        if labels is None or labels or getattr(labels, 'has_more', False) or getattr(labels, 'truncated', False):
            return False, None, None  # label acknowledgement path -- not replicated inline
        dirty_people = _bounded_ids(user_id, 'people')
        dirty_albums = _bounded_ids(user_id, 'albums')
        if dirty_people is None or getattr(dirty_people, 'has_more', False) or getattr(dirty_people, 'truncated', False) or len(dirty_people) > 100:
            return False, None, None
        if dirty_albums is None or dirty_albums or getattr(dirty_albums, 'has_more', False) or getattr(dirty_albums, 'truncated', False):
            # Legacy clustering producers mark both sets; a people-only
            # inline attempt must not silently drop the albums side.
            return False, None, None
        if not index_work.read_bounded_dirty(storage_utils._people_merge_eligible,
            user_id, dirty_people or None, max_rows=_MAX_PREV_ROWS):
            return False, None, None
        return True, dirty_people, None
    if scope == 'albums':
        dirty_albums = _bounded_ids(user_id, 'albums')
        if dirty_albums is None or getattr(dirty_albums, 'has_more', False) or getattr(dirty_albums, 'truncated', False) or len(dirty_albums) > 100:
            return False, None, None
        if not index_work.read_bounded_dirty(storage_utils._albums_merge_eligible,
            user_id, dirty_albums or None, max_rows=_MAX_PREV_ROWS):
            return False, None, None
        return True, None, dirty_albums
    if scope in ('sort', 'access'):
        if not storage_utils._rows_merge_eligible(user_id, scope):
            return False, None, None
        return True, None, None
    if scope == 'search':
        # Search already has a reader-compatible base+delta format.  Core may
        # publish only a bounded delta against an existing compatible base;
        # missing bases and exhausted delta capacity stay with the index app.
        import search_db
        manifest = search_db.load_manifest(user_id)
        if (not manifest.get('sourceVersion')
                or manifest.get('schemaVersion') != search_db.SCHEMA_VERSION
                or search_db.available_delta_row_capacity(
                    manifest, storage_utils.SEARCH_DB_DELTA_CHUNK) <= 0):
            return False, None, None
        dirty = index_work.read_bounded_dirty(
            storage_utils._get_dirty_search_index_filenames, user_id, 'lexical',
            max_rows=_SEARCH_MAX_NAMES)
        if (dirty is None or not dirty or getattr(dirty, 'has_more', False)
                or getattr(dirty, 'truncated', False)
                or len(dirty) > _SEARCH_MAX_NAMES):
            return False, None, None
        return True, None, None
    return False, None, None


def _attempt(user_id: str, scope: str, capture: Optional[list]) -> None:
    import app
    if not _serialized_library(user_id):
        return
    # Hold the same real library lease as durable/wave publishers across
    # eligibility, source reads, publication and acknowledgement.
    with index_publication.catalog_serialization(app.jobs_table_client, user_id,
            app.blob_service_client, app.BLOB_LEXICAL_INDEX_CONTAINER):
        _attempt_serialized(user_id, scope, capture)


def _attempt_serialized(user_id, scope, capture):
    import app
    ok, dirty_people, dirty_albums = _eligible(user_id, scope)
    if not ok:
        return
    store = index_work.WorkStore(app.jobs_table_client)
    if _conflicting_work(store, user_id, scope):
        return
    key = store.key(user_id, scope)
    try:
        claim = store.claim(key)
    except RuntimeError:
        return
    if claim is None:
        return
    try:
        with index_publication.publication(store, claim) as guard:
            completed = _merge_claimed(store, claim, guard, user_id, scope)
        if not completed:
            store.release(claim, deferred=True)
            return
        newer = store.finish(claim)
    except Exception:
        try:
            store.release(claim)
        except Exception:
            _LOGGER.warning('Inline lease could not be released', exc_info=True)
        raise
    if newer is None or newer:
        return
    _cancel_backstop(capture)


def _merge_claimed(store, claim, guard, user_id, scope):
    import app
    # Dirty IDs/base size may have changed while waiting for the claim.
    ok, dirty_people, dirty_albums = _eligible(user_id, scope)
    if not ok or _conflicting_work(store, user_id, scope):
        return
    if scope == 'search':
        # The Search SQLite path is already a correction-slip publisher.  It
        # acknowledges dirty rows itself only after the delta CAS succeeds.
        # A deferred/no-base/capacity result deliberately keeps the durable
        # backstop alive for the index worker.
        with index_ownership.allow_inline_merge(scope):
            outcome = app._drain_incremental_search_db(
                user_id, scope, max_names=_SEARCH_MAX_NAMES, max_passes=1)
        if str(outcome.get('status') or '') not in ('noop', 'delta'):
            return False
        guard.check()
        return True
    if scope == 'sort':
        # Sort now has the same reader-compatible base-plus-delta contract as
        # Search. A missing base, large dirty capture, or exhausted delta log
        # stays with the durable worker, which can compact or rebuild.
        with index_ownership.allow_inline_merge(scope):
            outcome = storage_utils.refresh_user_sort_index_incremental(user_id)
        if str(outcome.get('status') or '') != 'delta':
            return False
        guard.check()
        return True
    kinds = index_updates.SCOPE_KINDS[scope]
    with index_ownership.allow_inline_merge(scope):
        bad = storage_utils.prime_all_user_indexes_sequentially(
            user_id, wait=True, kinds=kinds,
            affected_person_ids=dirty_people or None,
            affected_album_ids=dirty_albums or None,
            incremental_only=True,
        )
    if bad:
        raise RuntimeError(f'inline index merge produced no {", ".join(bad)} index')
    if scope == 'people':
        # This is a backstop-retention probe, NOT dependency readiness.
        # Never drain search on core; incomplete/unavailable captures retain
        # the durable worker's wakeup even after a successful people merge.
        dirty_search = index_work.read_bounded_dirty(
            storage_utils._get_dirty_search_index_filenames, user_id, 'lexical', max_rows=1)
        if dirty_search is None or dirty_search or getattr(dirty_search, 'has_more', False) or getattr(dirty_search, 'truncated', False):
            return
    guard.check()
    if dirty_people:
        app.clear_dirty_people(user_id, dirty_people)
    if dirty_albums:
        app.clear_dirty_albums(user_id, dirty_albums)
    guard.check()
    return True


def _cancel_backstop(capture: Optional[list]) -> None:
    """Best-effort: a stale pop_receipt (the real worker already dequeued
    it) just means cancellation fails harmlessly -- WorkStore.finish already
    deleted the intent row above, so the real worker's later claim() of that
    message safely no-ops."""
    if not capture:
        return
    for queue_client, sent_message in capture:
        try:
            queue_client.delete_message(sent_message.id, sent_message.pop_receipt)
        except Exception:
            pass
