"""Renewable Blob lease injected into ``FaissAssigner(lease=factory)``.

Reuse one factory for the lifetime of the process/assigner: its owner UUID is
stable, unlike an Azure lease ID. Rebuilding the factory for every assignment
changes ownership/generation and defeats warm-cache reuse. Ownership transfer
and the separately published source revision both invalidate assignment caches.

Only cooperating assignment participants are serialized, NOT whole-system
mutations. Call ``guard.check()`` immediately before every Table write. Azure
Blob leases do not fence Azure Tables transactions: a write already in flight
may finish after lease loss, and there is an unavoidable check/write race.
The injected client retains existing managed-identity credentials and SDK
transport/retry configuration. No containers or credentials are created here.

Application wrappers must call ``mark_library_changed`` before and after actual
curation/ingestion mutations. Assigner delta writes must use raw Table clients,
bypassing these wrappers so they do not invalidate their own warm caches.
Acquire a fresh guard per message: source revision is read once on acquisition,
not remotely on every ``guard.check()``. Revision publication is not a storage
fence or a transaction with mutations: concurrent edits during assignment,
in-flight writes and failed before/after publication remain mutation races.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import logging
import math
import threading
import time
import uuid

from azure.core.exceptions import HttpResponseError, ResourceExistsError, ResourceNotFoundError


_LOGGER = logging.getLogger(__name__)
_MAX_STATE_BYTES = 4096


class RetryableLibraryLeaseError(RuntimeError):
    """Assignment must be retried by its caller; never wait for ownership here."""


class _LeaseGuard:
    def __init__(self, lease, duration, renew_seconds, acquired_at):
        self.cache_generation = None
        self._lease = lease
        self._duration = duration
        self._renew_seconds = renew_seconds
        self._last_renewed = acquired_at
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._lost = None
        self._closed = False
        self._thread = threading.Thread(
            target=self._run, name='faiss-assignment-lease', daemon=True)

    def check(self):
        """Fail closed, including when the renewal thread is stalled or stopped."""
        with self._lock:
            if (self._lost is None
                    and time.monotonic() - self._last_renewed >= self._duration):
                self._lost = RetryableLibraryLeaseError('Library lease expired')
            if self._lost is not None:
                raise RetryableLibraryLeaseError('Library lease lost') from self._lost
            if self._closed:
                raise RetryableLibraryLeaseError('Library lease guard is closed')

    def _renew(self):
        # Never hold the state lock across network I/O: checks must fail closed
        # even when an SDK call hangs. Use request START time conservatively.
        try:
            self.check()
            started = time.monotonic()
            self._lease.renew()
            with self._lock:
                if self._closed or self._lost is not None:
                    return
                if time.monotonic() - self._last_renewed >= self._duration:
                    self._lost = RetryableLibraryLeaseError('Library lease expired during renewal')
                else:
                    self._last_renewed = started
        except Exception as error:
            with self._lock:
                if self._lost is None:
                    self._lost = error
            self._stop.set()

    def _run(self):
        while not self._stop.wait(self._renew_seconds):
            self._renew()
            with self._lock:
                if self._lost is not None or self._closed:
                    return

    def _close(self):
        with self._lock:
            self._closed = True
        self._stop.set()
        # A stuck transport must not keep the assignment caller waiting forever.
        if self._thread.ident is not None:
            self._thread.join(timeout=min(self._renew_seconds, 1.0))
        try:
            self._lease.release()
        except Exception:
            # In particular, do not replace a build/write/renewal exception.
            _LOGGER.warning('Failed to release library assignment lease', exc_info=True)


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate state field')
        result[key] = value
    return result


def _source_blob(blob_service_client, container, user_id):
    if not isinstance(container, str) or not container:
        raise ValueError('container must be a nonempty string')
    if not isinstance(user_id, str) or not user_id:
        raise ValueError('user_id must be a nonempty library ID string')
    name = 'faiss-source-revisions/' + hashlib.sha256(user_id.encode('utf-8')).hexdigest() + '.json'
    return blob_service_client.get_blob_client(container=container, blob=name)


def mark_library_changed(blob_service_client, container, user_id):
    """Publish a fresh invalidation token; all SDK/transport failures propagate."""
    blob = _source_blob(blob_service_client, container, user_id)
    revision = str(uuid.uuid4())
    blob.upload_blob(json.dumps({'revision': revision}), overwrite=True)


def read_source_revision(blob_service_client, container, user_id):
    """Read a strict UUID token; only a missing blob means no revision (``''``)."""
    blob = _source_blob(blob_service_client, container, user_id)
    try:
        raw = blob.download_blob(offset=0, length=_MAX_STATE_BYTES + 1).readall()
    except ResourceNotFoundError:
        return ''
    # SDK failures must never be converted into a reusable empty cache token.
    try:
        if len(raw) > _MAX_STATE_BYTES:
            raise ValueError('Source revision is too large')
        state = json.loads(raw, object_pairs_hook=_unique_fields)
        if not isinstance(state, dict) or set(state) != {'revision'}:
            raise ValueError('Invalid source revision fields')
        revision = state['revision']
        if not isinstance(revision, str) or str(uuid.UUID(revision)) != revision:
            raise ValueError('Invalid source revision UUID')
        return revision
    except (ValueError, TypeError, UnicodeError, AttributeError) as error:
        raise RetryableLibraryLeaseError('Invalid library source revision') from error


def _state(raw):
    """Reject corruption rather than silently losing a cache invalidation token."""

    try:
        if len(raw) > _MAX_STATE_BYTES:
            raise ValueError('Lease state is too large')
        state = json.loads(raw, object_pairs_hook=_unique_fields)
        if not isinstance(state, dict) or set(state) != {'owner', 'generation'}:
            raise ValueError('Invalid lease state fields')
        owner, generation = state['owner'], state['generation']
        if not isinstance(owner, str) or not isinstance(generation, str):
            raise ValueError('Invalid lease state types')
        if owner:
            uuid.UUID(owner)
        uuid.UUID(generation)
        return state
    except (ValueError, TypeError, UnicodeError, AttributeError) as error:
        raise RetryableLibraryLeaseError('Invalid library lease state') from error


class BlobLibraryLeaseFactory:
    """``factory(user_id)`` is a context manager yielding a renewable guard.

    ``duration`` is an Azure finite lease duration (integer 15..60 seconds);
    ``renew_seconds`` must be positive and strictly less than that duration.
    The existing container must grant Blob read/write/lease permissions.
    ``guard.cache_generation`` is the hashable ``(ownership, source_revision)``
    tuple, stable only while both tokens remain unchanged.
    """

    def __init__(self, blob_service_client, container, *, duration=60, renew_seconds=15):
        if type(duration) is not int or not 15 <= duration <= 60:
            raise ValueError('duration must be an integer from 15 to 60')
        if (type(renew_seconds) not in (int, float)
                or not math.isfinite(renew_seconds) or not 0 < renew_seconds < duration):
            raise ValueError('renew_seconds must be finite, positive and less than duration')
        if not isinstance(container, str) or not container:
            raise ValueError('container must be a nonempty string')
        self._client = blob_service_client
        self._container = container
        self._duration = duration
        self._renew_seconds = renew_seconds
        self._owner = str(uuid.uuid4())

    @contextmanager
    def __call__(self, user_id):
        if not isinstance(user_id, str) or not user_id:
            raise ValueError('user_id must be a nonempty library ID string')
        name = 'faiss-assignment-locks/' + hashlib.sha256(user_id.encode('utf-8')).hexdigest() + '.json'
        blob = self._client.get_blob_client(container=self._container, blob=name)
        initial = {'owner': '', 'generation': str(uuid.uuid4())}
        try:
            blob.upload_blob(json.dumps(initial), overwrite=False)
        except ResourceExistsError:
            pass  # Only an already-existing blob is benign; transport errors propagate.
        acquired_at = time.monotonic()
        try:
            lease = blob.acquire_lease(lease_duration=self._duration)
        except HttpResponseError as error:
            if error.status_code in (409, 412):
                raise RetryableLibraryLeaseError('Library assignment lease is busy') from error
            raise
        guard = _LeaseGuard(lease, self._duration, self._renew_seconds, acquired_at)
        try:
            guard._thread.start()
            raw = blob.download_blob(lease=lease, offset=0, length=_MAX_STATE_BYTES + 1).readall()
            state = _state(raw)
            if state['owner'] != self._owner:
                state = {'owner': self._owner, 'generation': str(uuid.uuid4())}
            guard.check()
            # The state publication itself is atomically fenced by the Blob lease.
            blob.upload_blob(json.dumps(state), overwrite=True, lease=lease)
            source_revision = read_source_revision(self._client, self._container, user_id)
            guard.cache_generation = (state['generation'], source_revision)
            guard.check()
            yield guard
        finally:
            guard._close()