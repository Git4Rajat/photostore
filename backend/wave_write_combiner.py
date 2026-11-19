"""Coalesce concurrent wave resolutions for one upload session in this process.

Every finalize request binds its files to the wave by rewriting the same shared
rows (library control and policy, the session, its wave). Concurrent requests
in one process raced each other for those rows; during a 10k-file upload on
2026-11-19 a quarter of finalize requests lost every CAS retry and answered 503
although their photos were stored. Here they queue instead: one request at a
time commits a page that may also carry other waiting requests' entries, so
requests in a process never contend with each other. Replicas still contend
with each other, which the store's own CAS retries absorb.

A combined page fails as a unit, so a failed page is retried request by
request: one request's binding conflict never fails another's.
"""
import threading

# Entries per combined commit; matches upload_waves.MAX_PAGE (one transaction).
PAGE = 20
# A committer hands over after this many pages so its own reply isn't held.
MAX_PAGES_PER_COMMITTER = 8

_LOCK = threading.Lock()
_QUEUES = {}


class _Request:
    def __init__(self, entries):
        self.entries = entries
        self.done = threading.Event()
        self.result = None
        self.error = None


class _Queue:
    def __init__(self):
        self.waiting = []
        self.committing = False


def combine(key, entries, commit):
    """commit(entries) -> result for 1..PAGE entries; returns this request's last result.

    key scopes coalescing (store table, library, session). Entries beyond one
    page are split across pages; the request finishes when all of them did.
    """
    chunks = [_Request(entries[offset:offset + PAGE]) for offset in range(0, len(entries), PAGE)]
    if not chunks:
        raise ValueError('Nothing to resolve')
    with _LOCK:
        queue = _QUEUES.setdefault(key, _Queue())
        queue.waiting.extend(chunks)
    while not all(chunk.done.is_set() for chunk in chunks):
        with _LOCK:
            lead = not queue.committing and bool(queue.waiting)
            if lead:
                queue.committing = True
        if lead:
            try:
                _drain(key, queue, commit)
            finally:
                with _LOCK:
                    queue.committing = False
                    if not queue.waiting and _QUEUES.get(key) is queue:
                        del _QUEUES[key]
        else:
            # The committer may finish without reaching this chunk; then
            # take over (wake periodically rather than per-queue signaling).
            next(chunk for chunk in chunks if not chunk.done.is_set()).done.wait(0.05)
    for chunk in chunks:
        if chunk.error is not None:
            raise chunk.error
    return chunks[-1].result


def _drain(key, queue, commit):
    for _ in range(MAX_PAGES_PER_COMMITTER):
        with _LOCK:
            page, size = [], 0
            while queue.waiting and size + len(queue.waiting[0].entries) <= PAGE:
                request = queue.waiting.pop(0)
                page.append(request)
                size += len(request.entries)
        if not page:
            return
        try:
            result = commit([entry for request in page for entry in request.entries])
            for request in page:
                request.result = result
        except Exception as error:
            if len(page) == 1:
                page[0].error = error
            else:
                for request in page:
                    try:
                        request.result = commit(request.entries)
                    except Exception as own:
                        request.error = own
        finally:
            for request in page:
                request.done.set()
