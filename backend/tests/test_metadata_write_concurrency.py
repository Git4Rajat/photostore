"""Coverage for real ETag/optimistic-concurrency on photometadata writes.

Every write path here used to be a blind read-modify-write upsert_entity with
no etag/match_condition -- confirmed live via grep returning zero hits for
either across storage_utils.py, despite _update_metadata_fields already
having retry-loop scaffolding that implied one was intended (it caught
ResourceModifiedError, an exception that can only be raised by a conditional
write, which never happened). Two concurrent writers to the same row (two
ipwork steps, a user edit racing a background step, two lease-claim
attempts) could silently clobber each other. This uses a fake table that
actually simulates etag conflicts (unlike tests/fakes.py's FakeTable, which
just accepts every write) to prove the retry logic really recovers instead
of just not crashing.
"""
from __future__ import annotations

import threading

import pytest
from azure.core.exceptions import ResourceModifiedError, ResourceNotFoundError

import storage_utils


class _EtagEntity(dict):
    """Minimal stand-in for azure.data.tables.TableEntity: a dict that also
    exposes a `.metadata['etag']` property, which update_entity's real
    match_condition handling reads when no explicit etag= is passed."""

    def __init__(self, data, etag) -> None:
        super().__init__(data)
        self._etag = etag

    @property
    def metadata(self):
        return {'etag': self._etag, 'timestamp': None}


class _OneShotBarrierWrapper:
    """Wraps a threading.Barrier so it only synchronizes its first release --
    a retried caller's second get_entity call must not block waiting for a
    party that will never arrive."""

    def __init__(self, table: '_RacyTable', barrier: threading.Barrier) -> None:
        self._table = table
        self._barrier = barrier
        self._lock = threading.Lock()

    def wait(self, timeout=None):
        result = self._barrier.wait(timeout=timeout)
        with self._lock:
            if self._table.read_barrier is self:
                self._table.read_barrier = None
        return result


class _RacyTable:
    """Fake Table Storage client with real etag-conflict simulation: each row
    carries a version counter bumped on every successful write; update_entity
    raises ResourceModifiedError if the caller's etag doesn't match the
    current one, exactly like the real service's match_condition handling."""

    def __init__(self) -> None:
        self.rows: dict = {}
        self.etags: dict = {}
        self.lock = threading.Lock()
        self.read_barrier = None  # set by a test to synchronize concurrent reads

    def upsert_entity(self, entity):
        key = (entity['PartitionKey'], entity['RowKey'])
        with self.lock:
            self.etags[key] = self.etags.get(key, 0) + 1
            self.rows[key] = dict(entity)

    def get_entity(self, partition_key, row_key):
        key = (partition_key, row_key)
        with self.lock:
            if key not in self.rows:
                raise ResourceNotFoundError(str(key))
            data = dict(self.rows[key])
            etag = self.etags.get(key, 0)
        barrier = self.read_barrier
        if barrier is not None:
            barrier.wait(timeout=5)
        return _EtagEntity(data, etag)

    def update_entity(self, entity, mode=None, *, etag=None, match_condition=None):
        key = (entity['PartitionKey'], entity['RowKey'])
        check_etag = etag if etag is not None else entity.metadata.get('etag')
        with self.lock:
            if key not in self.rows:
                raise ResourceNotFoundError(str(key))
            current_etag = self.etags.get(key, 0)
            if match_condition is not None and check_etag != current_etag:
                raise ResourceModifiedError('etag mismatch')
            self.etags[key] = current_etag + 1
            self.rows[key] = dict(entity)


@pytest.fixture
def racy_ctx(monkeypatch):
    table = _RacyTable()
    monkeypatch.setitem(storage_utils._CTX, 'metadata_table_client', table)
    monkeypatch.setitem(storage_utils._CTX, 'embeddings_table_client', None)
    monkeypatch.setitem(storage_utils._CTX, 'search_index_dirty_table_client', None)
    monkeypatch.setitem(storage_utils._CTX, 'blob_service_client', None)
    return table


def _seed(table: _RacyTable, user_id: str, filename: str, **overrides) -> None:
    table.upsert_entity({'PartitionKey': user_id, 'RowKey': filename, 'processing_metadata': '{}', **overrides})


def test_concurrent_update_processing_status_neither_write_is_lost(racy_ctx):
    """Two ipwork steps finishing at the same instant for the same photo must
    both land -- not have one silently overwrite the other's status."""
    table = racy_ctx
    _seed(table, 'u1', 'a.jpg')
    barrier = threading.Barrier(2)
    table.read_barrier = _OneShotBarrierWrapper(table, barrier)

    def _run(step):
        storage_utils.update_processing_status('u1', 'a.jpg', step, 'done')

    t1 = threading.Thread(target=_run, args=('face',))
    t2 = threading.Thread(target=_run, args=('ocr',))
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert not t1.is_alive() and not t2.is_alive()

    final = table.get_entity('u1', 'a.jpg')
    assert final['face_status'] == 'done'
    assert final['ocr_status'] == 'done'
    # Both writes actually landed as two separate versions, not one clobbering
    # the other silently in a single version.
    assert table.etags[('u1', 'a.jpg')] >= 2


def test_concurrent_update_metadata_fields_neither_write_is_lost(racy_ctx):
    """A user rating edit racing a background field update on the same row --
    same invariant as the ipwork-vs-ipwork case above, different call site."""
    table = racy_ctx
    _seed(table, 'u1', 'a.jpg')
    barrier = threading.Barrier(2)
    table.read_barrier = _OneShotBarrierWrapper(table, barrier)

    def _run(updates):
        storage_utils._update_metadata_fields('u1', 'a.jpg', updates)

    t1 = threading.Thread(target=_run, args=({'rating': 5},))
    t2 = threading.Thread(target=_run, args=({'likes': 3},))
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert not t1.is_alive() and not t2.is_alive()

    final = table.get_entity('u1', 'a.jpg')
    assert final['rating'] == 5
    assert final['likes'] == 3


def test_concurrent_lease_claims_the_loser_is_rejected_not_silently_overwritten(racy_ctx):
    """Two workers racing to claim the same unowned lease: exactly one must
    win: the other's retry re-reads the winner's claim and raises, instead of
    both succeeding or the second blindly overwriting the first's ownership."""
    table = racy_ctx
    _seed(table, 'u1', 'a.jpg')
    barrier = threading.Barrier(2)
    table.read_barrier = _OneShotBarrierWrapper(table, barrier)

    results = {}
    errors = {}

    def _run(owner):
        try:
            results[owner] = storage_utils.claim_processing_lease('u1', 'a.jpg', owner)
        except Exception as exc:
            errors[owner] = exc

    t1 = threading.Thread(target=_run, args=('worker-A',))
    t2 = threading.Thread(target=_run, args=('worker-B',))
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert not t1.is_alive() and not t2.is_alive()

    # Exactly one of the two must have won the claim.
    assert len(results) == 1, f'expected exactly one winner, got results={results} errors={errors}'
    assert len(errors) == 1
    winner = next(iter(results))
    assert 'already held by another client' in str(errors[next(iter(errors))])

    final = table.get_entity('u1', 'a.jpg')
    assert final['processing_lease_owner'] == winner


def test_lease_release_rechecks_ownership_against_fresh_read(racy_ctx):
    """A release call for a stale owner_id must not clear a lease that's
    since been (re)claimed by someone else -- even if release's own read
    conflicts with a concurrent reclaim."""
    table = racy_ctx
    _seed(table, 'u1', 'a.jpg', processing_lease_owner='worker-A', processing_lease_expires_at='2099-01-01T00:00:00+00:00')

    # worker-A releases; meanwhile the row has already moved on to worker-B
    # (simulated directly, no thread needed for this one -- release's own
    # ownership pre-check is what's under test).
    table.upsert_entity({
        'PartitionKey': 'u1', 'RowKey': 'a.jpg', 'processing_metadata': '{}',
        'processing_lease_owner': 'worker-B', 'processing_lease_expires_at': '2099-01-01T00:00:00+00:00',
    })

    storage_utils.release_processing_lease('u1', 'a.jpg', 'worker-A')

    final = table.get_entity('u1', 'a.jpg')
    assert final['processing_lease_owner'] == 'worker-B'  # untouched
