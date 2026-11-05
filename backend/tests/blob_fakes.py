"""Reusable in-memory blob storage with atomic Azure-style CAS semantics."""
from __future__ import annotations

import threading
from types import SimpleNamespace

from azure.core import MatchConditions
from azure.core.exceptions import ResourceExistsError, ResourceModifiedError, ResourceNotFoundError


class FakeBlobServiceClient:
    def __init__(self):
        self.blobs = {}
        self._etags = {}
        self._revision = 0
        self._lock = threading.RLock()

    def get_blob_client(self, container, blob):
        return FakeBlob(self, f'{container}/{blob}')


class FakeBlob:
    def __init__(self, service, key):
        self._service = service
        self._key = key

    def _properties(self):
        if self._key not in self._service.blobs:
            raise ResourceNotFoundError(self._key)
        return SimpleNamespace(size=len(self._service.blobs[self._key]),
                               etag=self._service._etags[self._key])

    def get_blob_properties(self):
        with self._service._lock:
            return self._properties()

    def _check(self, etag, match_condition):
        if match_condition is None:
            if etag is not None:
                raise AssertionError('ETag requires a match condition')
            return
        if match_condition != MatchConditions.IfNotModified or not etag:
            raise AssertionError('Fake supports only explicit IfNotModified CAS')
        if self._properties().etag != etag:
            raise ResourceModifiedError('CAS conflict')

    def upload_blob(self, data, overwrite=False, content_settings=None, *, etag=None, match_condition=None):
        payload = data.read() if hasattr(data, 'read') else data
        payload = payload.encode('utf-8') if isinstance(payload, str) else bytes(payload)
        with self._service._lock:
            self._check(etag, match_condition)
            if not overwrite and self._key in self._service.blobs:
                raise ResourceExistsError(self._key)
            self._service._revision += 1
            revision = str(self._service._revision)
            self._service.blobs[self._key] = payload
            self._service._etags[self._key] = revision
            return {'etag': revision}

    def download_blob(self, *, etag=None, match_condition=None):
        with self._service._lock:
            self._check(etag, match_condition)
            self._properties()
            return FakeDownloaded(self._service.blobs[self._key])

    def delete_blob(self, *, etag=None, match_condition=None):
        with self._service._lock:
            self._check(etag, match_condition)
            self._properties()
            del self._service.blobs[self._key]
            del self._service._etags[self._key]


class FakeDownloaded:
    def __init__(self, data):
        self._data = data

    def readall(self):
        return self._data

    def chunks(self):
        for start in range(0, len(self._data), 65536):
            yield self._data[start:start + 65536]

    def readinto(self, stream):
        return stream.write(self._data)