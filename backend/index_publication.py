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
        catalog = _CATALOG.get()
        self.catalog_guard = catalog[1] if catalog is not None else None

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
    if client is None:
        return None
    if guard is not None:
        return ConditionalBlob(client, guard)
    catalog = _CATALOG.get()
    return CatalogBlob(client, catalog[1]) if catalog is not None else client


def check():
    catalog_check()
    guard = _ACTIVE.get()
    if guard is not None:
        guard.check()


def declared_build_alias_conditions(client):
    """Capture a full wave alias using the guard's pre-build ETag only.

    None means this is not a wave BuildGuard; ordinary publishers retain
    their independent immutable-data/manifest-CAS protocol. Never declare
    dynamic outputs or re-read properties to replace the guard's snapshot.
    The fenced uploader remains authoritative at commit time.
    """
    from wave_catalog import BuildGuard
    guard = _ACTIVE.get()
    if not isinstance(guard, BuildGuard):
        return None
    guard.check()
    if client is None:
        raise RuntimeError('Declared full-build alias storage unavailable')
    options = {'overwrite': True}
    guard.before_upload(client, options)
    return options


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


class CatalogBlob:
    """Keep a captured live guard on uploads, including full legacy publishers."""
    def __init__(self, client, guard):
        self.client, self.guard = client, guard

    def __getattr__(self, name):
        return getattr(self.client, name)

    def upload_blob(self, data, **kwargs):
        self.guard.check()
        result = self.client.upload_blob(data, **kwargs)
        self.guard.check()
        return result

    def upload_file(self, path, **kwargs):
        with open(path, 'rb') as data:
            return self.upload_blob(data, **kwargs)


def catalog_check():
    active = _CATALOG.get()
    if active is not None:
        active[1].check()


def require_catalog_guard(library, guard):
    active = _CATALOG.get()
    if active is None or active[0] != library or active[1] is not guard:
        raise RuntimeError('Active shared real catalog guard required')
    guard.check()


@contextmanager
def catalog_serialization(table,library,blobs,container, *, require_real=False):
    """Universal renewable library lock, held BEFORE base capture through commit.

    Nested same-library publishers reuse the live guard. Legacy storage fakes
    without lease support may publish under their own CAS tests, but yield None:
    they can NEVER authorize GC or quiesced recovery. Native Azure failures do
    not fall back to a no-op. No wave ownership predicate is involved.
    """
    active = _CATALOG.get()
    if active is not None:
        if active[0] != library:
            raise RuntimeError('Cross-library nested catalog publication')
        active[1].check()
        yield active[1]
        active[1].check()
        return
    if blobs is None or not container or not callable(getattr(blobs, 'get_blob_client', None)):
        if require_real:
            raise RuntimeError('Real catalog serialization storage unavailable')
        yield None
        return
    import hashlib
    probe = blobs.get_blob_client(container=container, blob='catalog-publication-locks/' +
                                  hashlib.sha256(library.encode()).hexdigest() + '.json')
    if not callable(getattr(probe, 'acquire_lease', None)):
        if require_real:
            raise RuntimeError('Real catalog lease required')
        yield None
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
        token=_CATALOG.set((library, guard))
        try:
            guard.check();yield guard;guard.check()
        finally:_CATALOG.reset(token)


def serialize_catalog(function):
    from functools import wraps
    @wraps(function)
    def wrapped(*args,**kwargs):
        import storage_utils as su
        first = args[0] if args else kwargs.get('user_id', kwargs.get('library_id',
                            kwargs.get('user', kwargs.get('library'))))
        library = getattr(first, 'user_id', getattr(getattr(first, 'store', None), 'library', first))
        if not isinstance(library, str) or not library:
            return function(*args, **kwargs)
        with catalog_serialization(None,library,su._CTX.get('blob_service_client'),su._lexical_index_container_name()):
            return function(*args,**kwargs)
    return wrapped
