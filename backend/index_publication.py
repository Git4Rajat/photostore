"""Fail-closed lease checks and conditional legacy index publication."""
from contextlib import contextmanager
from contextvars import ContextVar
import threading

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

    def __getattr__(self, name):
        return getattr(self.client, name)

    def upload_blob(self, data, **kwargs):
        self.guard.check()
        # Search manifests already have their own CAS publication protocol.
        if 'etag' not in kwargs and 'match_condition' not in kwargs:
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
        return result

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
