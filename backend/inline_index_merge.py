"""Bounded, best-effort inline merge of small CRUD-triggered index rebuilds.

CRUD edits to people/albums/sort(ratings)/access(likes) already enqueue a
durable Azure Queue wakeup for the dedicated `index-worker` role -- that
path is untouched and always runs regardless of anything in this module.
`core` is already warm handling the CRUD request itself, so when the
triggering change is small enough to be confident it'll take the existing
bounded merge path (not a full/embedding-heavy rebuild), this module ALSO
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
exception to the index-worker-only catalog gate -- restricted to exactly the
four bounded lexical scopes below. Cross-process mutual exclusion against a
concurrent real worker run for the same (user, scope) is provided by
`index_work.WorkStore.claim`, the same mechanism `_run_index_maintenance_payload`
already depends on -- NOT by `serialize_catalog`/`catalog_serialization`,
which is a no-op for any library not migrated onto the wave pipeline and
doesn't etag-fence these writes even when it is real.
"""
import logging
import os
import queue as queue_module
import threading
from typing import Optional

import index_ownership
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


def _eligible(user_id: str, scope: str):
    """Re-derive eligibility from the live dirty-tracking state -- returns
    (ok, dirty_people, dirty_albums). Mirrors the exact scope!='full' branch
    _run_index_build_job (app.py) already runs, so the inline path and the
    real worker path can never disagree about what counts as eligible."""
    import app
    if scope == 'people':
        if app._pending_person_labels(user_id):
            return False, None, None  # label acknowledgement path -- not replicated inline
        dirty_people = app.get_dirty_people(user_id)
        dirty_albums = app.get_dirty_albums(user_id)
        if dirty_albums:
            # Legacy clustering producers mark both sets; a people-only
            # inline attempt must not silently drop the albums side.
            return False, None, None
        if not storage_utils._people_merge_eligible(user_id, set(dirty_people or ()) or None):
            return False, None, None
        return True, dirty_people, None
    if scope == 'albums':
        dirty_albums = app.get_dirty_albums(user_id)
        if not storage_utils._albums_merge_eligible(user_id, dirty_albums or None):
            return False, None, None
        return True, None, dirty_albums
    if scope in ('sort', 'access'):
        if not storage_utils._rows_merge_eligible(user_id, scope):
            return False, None, None
        return True, None, None
    return False, None, None


def _attempt(user_id: str, scope: str, capture: Optional[list]) -> None:
    import app
    ok, dirty_people, dirty_albums = _eligible(user_id, scope)
    if not ok:
        return
    if storage_utils._library_row_count(user_id) > _MAX_PREV_ROWS:
        return
    store = index_work.WorkStore(app.jobs_table_client)
    key = store.key(user_id, scope)
    try:
        claim = store.claim(key)
    except RuntimeError:
        return  # already leased by the real worker (or a concurrent inline attempt)
    if claim is None:
        return  # no pending intent -- nothing to do, or already handled
    try:
        kinds = index_updates.SCOPE_KINDS[scope]
        with index_ownership.allow_inline_merge(scope):
            bad = storage_utils.prime_all_user_indexes_sequentially(
                user_id, wait=True, kinds=kinds,
                affected_person_ids=set(dirty_people or ()) or None,
                affected_album_ids=dirty_albums or None,
            )
        if bad:
            raise RuntimeError(f'inline index merge produced no {", ".join(bad)} index')
        if scope == 'people':
            app._drain_incremental_search_db(user_id, scope)
        if dirty_people:
            app.clear_dirty_people(user_id, dirty_people)
        if dirty_albums:
            app.clear_dirty_albums(user_id, dirty_albums)
        newer = store.finish(claim)
    except Exception:
        store.release(claim)
        raise
    if newer:
        # A second mutation advanced this key's revision while this merge
        # was running (finish() left the intent row open for it instead of
        # deleting it -- mirrors _run_index_maintenance_payload's own
        # `if newer: store.notify(...)` handling). The already-captured
        # backstop message is still sitting durably in the queue (inline
        # attempts are never triggered by consuming it, only by the local
        # submit() queue), so leaving it in place is enough: whenever it's
        # eventually delivered, the real worker's claim() reads whatever is
        # current and correctly picks up the newer revision. Cancelling it
        # here would drop that newer work until the next unrelated mutation
        # or the 60s recover_pending_index_work() sweep.
        return
    _cancel_backstop(capture)


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
