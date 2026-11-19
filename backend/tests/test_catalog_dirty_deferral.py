"""Dirty-manifest touches during a catalog publication are deferred, not written.

A publication pins its aliases' ETags (BuildGuard) for its whole build; a
touch_user_*_index_state write to the same manifest in that window used to
fail the publication with 412 on every retry while bulk metadata writes kept
touching (wave 143580e0, 2026-11-19).
"""
from types import SimpleNamespace

import pytest
from azure.core.exceptions import ResourceNotFoundError

import index_publication as publication
import storage_utils
from test_clustering_lease import FakeBlob, FakeService


class LeaseAwareBlob(FakeBlob):
    def get_blob_properties(self):
        if self.raw is None and self.active is None:
            raise ResourceNotFoundError('missing blob')
        return SimpleNamespace(lease=SimpleNamespace(state='leased' if self.active else 'available'))

    def upload_blob(self, data, *, overwrite, lease=None):
        if isinstance(data, bytes) and data == b'1':
            self.raw = data
            return
        return super().upload_blob(data, overwrite=overwrite, lease=lease)

    def delete_blob(self):
        if self.raw is None:
            raise ResourceNotFoundError('missing blob')
        self.raw = None


class LeaseAwareService(FakeService):
    def get_blob_client(self, *, container, blob):
        self.calls.append((container, blob))
        return self.blobs.setdefault((container, blob), LeaseAwareBlob())


@pytest.fixture
def blobs(monkeypatch):
    service = LeaseAwareService()
    replays = []
    monkeypatch.setattr(storage_utils, 'replay_catalog_dirty_touches', replays.append)
    publication._LEASE_STATE.clear()
    yield service, replays
    publication._LEASE_STATE.clear()


def marker(service, library):
    return service.get_blob_client(container='catalog', blob=publication._deferred_dirty_blob_name(library)).raw


def test_touch_during_publication_is_deferred_and_replayed_on_release(blobs):
    service, replays = blobs
    with publication.catalog_serialization(None, 'A', service, 'catalog'):
        assert publication.defer_dirty_touch(service, 'catalog', 'A') is True
        assert marker(service, 'A') == b'1'
        assert replays == []
    assert replays == ['A']
    assert marker(service, 'A') is None


def test_touch_without_a_publication_writes_through(blobs):
    service, replays = blobs
    assert publication.defer_dirty_touch(service, 'catalog', 'A') is False
    assert marker(service, 'A') is None
    with publication.catalog_serialization(None, 'A', service, 'catalog'):
        pass
    assert replays == []


def test_touch_racing_the_release_is_applied_by_the_writer(blobs, monkeypatch):
    service, _ = blobs
    states = iter([True, False])  # held at the cached check, released at the fresh one
    monkeypatch.setattr(publication, 'catalog_lease_held', lambda *a, **k: next(states))
    assert publication.defer_dirty_touch(service, 'catalog', 'A') is False
    assert marker(service, 'A') is None


def test_replay_writes_through_even_while_another_publication_holds_the_lease(blobs, monkeypatch):
    service, _ = blobs
    monkeypatch.setattr(publication, 'catalog_lease_held', lambda *a, **k: True)
    token = publication._REPLAYING.set(True)
    try:
        assert publication.defer_dirty_touch(service, 'catalog', 'A') is False
    finally:
        publication._REPLAYING.reset(token)


def test_deferral_fails_open_when_storage_errors(blobs, monkeypatch):
    service, _ = blobs

    def broken(*args, **kwargs):
        raise RuntimeError('storage down')

    monkeypatch.setattr(publication, 'catalog_lease_held', broken)
    assert publication.defer_dirty_touch(service, 'catalog', 'A') is False


def test_deferred_lexical_touch_skips_the_manifest_write(monkeypatch):
    monkeypatch.setattr(storage_utils, '_defer_manifest_touch', lambda user_id: True)

    def unexpected(*args, **kwargs):
        raise AssertionError('manifest must not be written while deferred')

    monkeypatch.setattr(storage_utils, '_lexical_index_blob_client', unexpected)
    monkeypatch.setattr(storage_utils, '_sort_index_blob_client', unexpected)
    assert storage_utils.touch_user_lexical_index_state('A') == ''
    assert storage_utils.touch_user_sort_index_state('A') == ''
