"""Regression tests for the person-entity lost-update race (ipworker
intra-replica concurrency, P0-1): _add_face_to_person,
_remove_face_from_other_people, and _update_person_entity used to
read-modify-write the person table via an unconditional upsert_entity, so
two threads/replicas concurrently touching the same person could silently
clobber each other's write. They now go through
_update_person_entity_with_retry / _remove_face_from_person_with_retry,
which use ETag optimistic concurrency (update_entity/delete_entity with
match_condition=IfNotModified) and retry on ResourceModifiedError,
re-deriving the write from fresh state each attempt.
"""
from __future__ import annotations

import json
import re
import threading

import pytest
from azure.core.exceptions import ResourceModifiedError, ResourceNotFoundError
from azure.data.tables import TableEntity

import app


class _FakePersonTable:
    """In-memory azure.data.tables.TableClient stand-in with real ETag
    semantics: update_entity/delete_entity honor match_condition and raise
    ResourceModifiedError on a stale etag, the same as the real service."""

    def __init__(self) -> None:
        self.rows: dict = {}
        self._etags: dict = {}
        self._version = 0
        self.update_calls: list = []
        self.delete_calls: list = []

    def seed(self, user_id: str, person_id: str, **fields) -> None:
        key = (user_id, person_id)
        self.rows[key] = {'PartitionKey': user_id, 'RowKey': person_id, **fields}
        self._version += 1
        self._etags[key] = f'W/"{self._version}"'

    def _entity_for(self, key) -> TableEntity:
        entity = TableEntity(dict(self.rows[key]))
        entity._metadata = {'etag': self._etags[key], 'timestamp': None}
        return entity

    def get_entity(self, partition_key, row_key):
        key = (partition_key, row_key)
        if key not in self.rows:
            raise ResourceNotFoundError(f'{key} not found')
        return self._entity_for(key)

    def update_entity(self, entity, mode=None, *, etag=None, match_condition=None):
        key = (entity['PartitionKey'], entity['RowKey'])
        if key not in self.rows:
            raise ResourceNotFoundError(f'{key} not found')
        if etag is not None and etag != self._etags[key]:
            raise ResourceModifiedError('etag mismatch')
        self.rows[key] = dict(entity)
        self._version += 1
        self._etags[key] = f'W/"{self._version}"'
        self.update_calls.append(key)

    def delete_entity(self, partition_key, row_key, *, etag=None, match_condition=None):
        key = (partition_key, row_key)
        if etag is not None and self._etags.get(key) != etag:
            raise ResourceModifiedError('etag mismatch')
        self.rows.pop(key, None)
        self._etags.pop(key, None)
        self.delete_calls.append(key)

    def query_entities(self, filter_str):
        m = re.match(r"PartitionKey eq '(.*)'$", filter_str.strip())
        pk = m.group(1)
        return [self._entity_for(key) for key in list(self.rows.keys()) if key[0] == pk]


@pytest.fixture
def person_table(monkeypatch):
    table = _FakePersonTable()
    monkeypatch.setattr(app, 'person_table_client', table)
    monkeypatch.setattr(app, 'face_table_client', None)
    return table


def test_retry_helper_recovers_from_concurrent_write(person_table):
    person_table.seed('u1', 'p1', faceIds='[]')
    calls = {'n': 0}

    def mutate(person):
        calls['n'] += 1
        if calls['n'] == 1:
            # Simulate a second thread/replica's write landing between our
            # read and our write.
            person_table.rows[('u1', 'p1')]['faceIds'] = json.dumps(['other-face'])
            person_table._version += 1
            person_table._etags[('u1', 'p1')] = f'W/"{person_table._version}"'
        current = json.loads(person['faceIds'])
        person['faceIds'] = json.dumps([*current, 'new-face'])
        return person

    result = app._update_person_entity_with_retry('u1', 'p1', mutate)

    assert calls['n'] == 2  # first attempt lost the race and retried once
    assert result is not None
    stored = json.loads(person_table.rows[('u1', 'p1')]['faceIds'])
    assert 'other-face' in stored  # the concurrent writer's data wasn't lost
    assert 'new-face' in stored  # ...and this call's data landed too


def test_retry_helper_gives_up_after_max_attempts(person_table):
    person_table.seed('u1', 'p1', faceIds='[]')

    def always_conflicting_mutate(person):
        # Every attempt observes a write that just landed underneath it.
        person_table.rows[('u1', 'p1')]['faceIds'] = json.dumps(['churn'])
        person_table._version += 1
        person_table._etags[('u1', 'p1')] = f'W/"{person_table._version}"'
        person['faceIds'] = json.dumps(['mine'])
        return person

    result = app._update_person_entity_with_retry('u1', 'p1', always_conflicting_mutate, max_attempts=3)

    assert result is None  # gives up rather than looping forever
    assert person_table.rows[('u1', 'p1')]['faceIds'] == json.dumps(['churn'])


def test_mutate_fn_returning_none_skips_the_write(person_table):
    person_table.seed('u1', 'p1', faceIds=json.dumps(['face-a']))

    result = app._update_person_entity_with_retry('u1', 'p1', lambda person: None)

    assert result is None
    assert person_table.update_calls == []


def test_concurrent_add_face_to_same_person_no_lost_update(person_table):
    """Two threads adding *different* faces to the *same* person must both
    land -- this is the actual regression test for the P0-1 bug: before
    the fix, an unconditional upsert_entity meant whichever thread wrote
    second would silently overwrite the first thread's appended face."""
    person_table.seed('u1', 'p1', faceIds='[]', name='Unnamed 1')

    barrier = threading.Barrier(2)
    thread_state = threading.local()
    original_get_entity = person_table.get_entity

    def synced_get_entity(partition_key, row_key):
        entity = original_get_entity(partition_key, row_key)
        # Only the first get_entity call per thread rendezvous-waits, so a
        # later retry (which only one thread takes) never blocks on a
        # barrier the other thread has already passed.
        if not getattr(thread_state, 'synced', False):
            thread_state.synced = True
            barrier.wait(timeout=5)
        return entity

    person_table.get_entity = synced_get_entity  # type: ignore[method-assign]

    def worker(face_id):
        thread_state.synced = False
        app._add_face_to_person('u1', 'p1', face_id)

    t1 = threading.Thread(target=worker, args=('face-a',))
    t2 = threading.Thread(target=worker, args=('face-b',))
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    final = json.loads(person_table.rows[('u1', 'p1')]['faceIds'])
    assert set(final) == {'face-a', 'face-b'}


def test_concurrent_updates_to_different_persons_dont_interfere(person_table):
    """Concurrency safety shouldn't come at the cost of serializing writes
    to unrelated entities -- two photos landing on two different existing
    persons for the same user must both persist without spurious retries."""
    person_table.seed('u1', 'p1', faceIds='[]')
    person_table.seed('u1', 'p2', faceIds='[]')

    def worker(person_id, face_id):
        app._add_face_to_person('u1', person_id, face_id)

    t1 = threading.Thread(target=worker, args=('p1', 'face-a'))
    t2 = threading.Thread(target=worker, args=('p2', 'face-b'))
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert json.loads(person_table.rows[('u1', 'p1')]['faceIds']) == ['face-a']
    assert json.loads(person_table.rows[('u1', 'p2')]['faceIds']) == ['face-b']
    # Different entities never actually conflict -- exactly one write each,
    # no extra churn from spurious ResourceModifiedError retries.
    assert person_table.update_calls.count(('u1', 'p1')) == 1
    assert person_table.update_calls.count(('u1', 'p2')) == 1


def test_remove_face_retry_rereads_fresh_branch_decision(person_table):
    """A conflict during the delete branch must re-derive which branch
    applies from fresh state, not just retry the same delete -- otherwise
    a person that was concurrently repopulated (no longer empty) could
    still get deleted, discarding another thread's just-added face."""
    person_table.seed('u1', 'p1', faceIds=json.dumps(['face-x']))
    calls = {'n': 0}
    original_delete = person_table.delete_entity

    def flaky_delete_entity(partition_key, row_key, *, etag=None, match_condition=None):
        calls['n'] += 1
        if calls['n'] == 1:
            # A concurrent _add_face_to_person lands between our read and
            # our delete attempt: this person is no longer empty.
            person_table.rows[('u1', 'p1')]['faceIds'] = json.dumps(['face-y'])
            person_table._version += 1
            person_table._etags[('u1', 'p1')] = f'W/"{person_table._version}"'
            raise ResourceModifiedError('etag mismatch')
        return original_delete(partition_key, row_key, etag=etag, match_condition=match_condition)

    person_table.delete_entity = flaky_delete_entity  # type: ignore[method-assign]

    outcome = app._remove_face_from_person_with_retry('u1', 'p1', 'face-x')

    # On retry, fresh state shows face-x is already gone (only face-y
    # remains) -- correctly a no-op, not a delete of a now-non-empty person.
    assert outcome is None
    assert json.loads(person_table.rows[('u1', 'p1')]['faceIds']) == ['face-y']
    assert person_table.delete_calls == []
