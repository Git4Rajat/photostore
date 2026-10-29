"""Fail-closed lease checks and conditional legacy index publication."""
from contextlib import contextmanager, ExitStack
from contextvars import ContextVar
import threading
import time

from azure.core import MatchConditions
from azure.core.exceptions import ResourceNotFoundError

_ACTIVE = ContextVar('index_publication_guard', default=None)


class Guard:
    def __init__(self, store, claim):
        self.store, self.claim = store, claim
        self.deadline = store.clock() + store.lease_seconds
        self.lost = None
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._renew, name='index-work-lease', daemon=True)

    def check(self):
        if self.lost is not None or self.store.clock() >= self.deadline:
            raise RuntimeError('Index publication lease lost') from self.lost

    def _renew(self):
        while not self.stop.wait(self.store.lease_seconds / 3):
            try:
                self.check()
                started = self.store.clock()
                self.store.renew(self.claim)
                self.deadline = started + self.store.lease_seconds
            except Exception as error:
                self.lost = error
                return


@contextmanager
def publication(store, claim):
    guard = Guard(store, claim)
    token = _ACTIVE.set(guard)
    guard.thread.start()
    try:
        yield guard
        guard.check()
    finally:
        _ACTIVE.reset(token)
        guard.stop.set()
        guard.thread.join(timeout=1)


class ConditionalBlob:
    def __init__(self, client, guard):
        self.client, self.guard = client, guard
        self.catalog_guard = _CATALOG.get()

    def __getattr__(self, name):
        return getattr(self.client, name)

    def upload_blob(self, data, **kwargs):
        if self.catalog_guard is not None:self.catalog_guard.check()
        catalog_check()
        self.guard.check()
        prepare = getattr(self.guard, 'before_upload', None)
        if prepare is not None:
            prepare(self.client, kwargs)
        # Search manifests already have their own CAS publication protocol.
        if prepare is None and 'etag' not in kwargs and 'match_condition' not in kwargs:
            try:
                props = self.client.get_blob_properties()
                kwargs.update(etag=props.etag, match_condition=MatchConditions.IfNotModified)
            except ResourceNotFoundError:
                kwargs['overwrite'] = False
            except Exception as error:
                self.guard.lost = error
                raise
        self.guard.check()
        try:
            result = self.client.upload_blob(data, **kwargs)
        except Exception as error:
            self.guard.lost = error
            raise
        self.guard.check()
        observed = getattr(self.guard, 'after_upload', None)
        if self.catalog_guard is not None:self.catalog_guard.check()
        catalog_check()
        if observed is not None:
            observed(self.client, result)
        return result

    def upload_file(self, path, **kwargs):
        # Preserve streaming through the wrapper; storage_utils must not fall
        # back to reading an entire catalog file into Python memory.
        with open(path, 'rb') as data:
            return self.upload_blob(data, **kwargs)

    def delete_blob(self, **kwargs):
        self.guard.check()
        if 'etag' not in kwargs and 'match_condition' not in kwargs:
            props = self.client.get_blob_properties()
            kwargs.update(etag=props.etag, match_condition=MatchConditions.IfNotModified)
        self.guard.check()
        return self.client.delete_blob(**kwargs)


def fenced(client):
    guard = _ACTIVE.get()
    return ConditionalBlob(client, guard) if client is not None and guard is not None else client


def check():
    guard = _ACTIVE.get()
    if guard is not None:
        guard.check()


def propagate_build_failure(error):
    """Required publications preserve the builder's original failure.

    Legacy background builders may return None for a failed refresh. A guarded
    wave build must retain the actual scan/upload error for its retry and logs.
    """
    if _ACTIVE.get() is not None:
        raise error


@contextmanager
def guarded_publication(guard):
    """Use a wave stage guard with the same actual publisher blob wrappers."""
    token = _ACTIVE.set(guard)
    try:
        yield guard
        guard.check()
    finally:
        _ACTIVE.reset(token)


_CATALOG = ContextVar('library_catalog_lease',default=None)


def catalog_check():
    guard=_CATALOG.get()
    if guard is not None:guard.check()


@contextmanager
def catalog_serialization(table,library,blobs,container):
    """Wave and foreground CRUD builders read/build/publish under one library lease.
    This prevents a CRUD build started earlier from replacing a wave's aliases.
    """
    from wave_policy import owned
    if not owned(table,library):
        yield
        return
    from clustering_lease import BlobLibraryLeaseFactory, RetryableLibraryLeaseError
    factory=BlobLibraryLeaseFactory(blobs,container,namespace='catalog-publication-locks')
    with ExitStack() as stack:
        # A contending lease is valid for at most BlobLibraryLeaseFactory's
        # own max duration (60s), whether its holder is still alive and
        # renewing it or died right after a renewal. Retry past that worst
        # case (margin added) with capped exponential backoff instead of
        # failing the whole (often expensive, worker-side) caller on the
        # first collision -- almost all contention, including an abandoned
        # lease, clears well within this window.
        deadline=time.monotonic()+65
        attempt=0
        while True:
            try:
                guard=stack.enter_context(factory(library))
                break
            except RetryableLibraryLeaseError:
                remaining=deadline-time.monotonic()
                if remaining<=0:raise
                attempt+=1
                time.sleep(min(2**attempt,8,remaining))
        token=_CATALOG.set(guard)
        try:
            guard.check();yield;guard.check()
        finally:_CATALOG.reset(token)


def serialize_catalog(function):
    from functools import wraps
    @wraps(function)
    def wrapped(library,*args,**kwargs):
        import app
        with catalog_serialization(app.jobs_table_client,library,app.blob_service_client,app.BLOB_LEXICAL_INDEX_CONTAINER):
            return function(library,*args,**kwargs)
    return wrapped
