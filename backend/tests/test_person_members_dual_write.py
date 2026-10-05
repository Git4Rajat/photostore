"""Unit tests for the photopersonmembers dual-write (Milestone 3 of the
incremental-clustering redesign).

photopeople.faceIds is a single JSON-encoded array on the person row --
Azure Table strings cap at 64 KiB, and every read/write of it re-serializes
the whole array regardless of how much actually changed. photopersonmembers
(PartitionKey=personId, RowKey=faceId) is dual-written alongside every
faceIds mutation so a future paginated reader doesn't need that array at
all. faceIds stays authoritative for every current reader -- these tests
only assert the two stay in sync, not that anything reads the new table yet.
"""
from __future__ import annotations

import json
import threading
import time

import pytest

import app
import storage_utils
from fakes import FakeTable


class _EtagEntity(dict):
    """Minimal stand-in for azure.data.tables.TableEntity: exposes
    .metadata['etag'], which _update_person_entity_with_retry and
    _remove_face_from_person_with_retry read before calling update_entity/
    delete_entity(..., match_condition=IfNotModified). Mirrors
    test_clustering_incremental_assign.py's identical fixture."""

    @property
    def metadata(self):
        return {'etag': 'v1', 'timestamp': None}


class _EtagAwareFakeTable(FakeTable):
    def get_entity(self, partition_key, row_key):
        return _EtagEntity(super().get_entity(partition_key, row_key))

    def update_entity(self, entity, mode=None, *, etag=None, match_condition=None):
        self.upsert_entity(entity)

    def delete_entity(self, partition_key, row_key, *, etag=None, match_condition=None):
        self.rows.pop((partition_key, row_key), None)


@pytest.fixture(autouse=True)
def person_tables(monkeypatch):
    # Never inherit configured live clients from another test/import.
    for key in vars(app):
        if key.endswith('_table_client'):
            monkeypatch.setattr(app, key, None)
    monkeypatch.setattr(app, 'blob_service_client', None)
    face_table = FakeTable()
    person_table = _EtagAwareFakeTable()
    members_table = FakeTable()
    monkeypatch.setattr(app, 'face_table_client', face_table)
    monkeypatch.setattr(app, 'person_table_client', person_table)
    monkeypatch.setattr(app, 'person_members_table_client', members_table)
    context = dict(storage_utils._CTX)
    for key in context:
        if key.endswith('_table_client') or key == 'blob_service_client':
            context[key] = None
    context.update(face_table_client=face_table, person_table_client=person_table,
                   person_members_table_client=members_table)
    monkeypatch.setattr(storage_utils, '_CTX', context)
    monkeypatch.setattr(app, '_person_scan_cache', app._UserScanCache(app.PEOPLE_SCAN_CACHE_TTL_SECONDS))
    monkeypatch.setattr(app, '_face_summary_scan_cache', app._UserScanCache(app.PEOPLE_SCAN_CACHE_TTL_SECONDS))
    monkeypatch.setattr(app, '_people_embedding_index_cache', app._UserScanCache(app.PEOPLE_SCAN_CACHE_TTL_SECONDS))
    return face_table, person_table, members_table


def _member_face_ids(members_table, person_id):
    return {rk for (pk, rk) in members_table.rows if pk == person_id}


def _seed_face(face_table, user_id, face_id, filename, *, person_id=None, embedding=None):
    row = {
        'PartitionKey': user_id, 'RowKey': face_id, 'filename': filename,
        'embedding': json.dumps(embedding or [0.1, 0.2, 0.3]),
        'confidence': 0.9,
    }
    if person_id:
        row['personId'] = person_id
    face_table.upsert_entity(row)
    return row


def test_create_person_entity_writes_membership_for_every_face(person_tables):
    _face_table, person_table, members_table = person_tables
    person_id = app._create_person_entity('u1', ['f1', 'f2'], [0.1, 0.2, 0.3], name='Alice')

    assert _member_face_ids(members_table, person_id) == {'f1', 'f2'}
    assert json.loads(person_table.get_entity('u1', person_id)['faceIds']) == ['f1', 'f2']


def test_create_person_entity_with_defer_waits_for_person_commit(person_tables):
    """No phantom membership while the authoritative person is only staged."""
    _face_table, person_table, members_table = person_tables
    staged: dict = {}
    person_id = app._create_person_entity(
        'u1', ['f1', 'f2'], [0.1, 0.2, 0.3], name='Bob', _defer_into=staged,
    )

    assert person_id in staged
    assert ('u1', person_id) not in person_table.rows  # not written yet
    assert _member_face_ids(members_table, person_id) == set()
    app._batch_upsert_entities(person_table, staged.values())
    assert _member_face_ids(members_table, person_id) == {'f1', 'f2'}


def test_failed_person_create_propagates_without_phantom_members(person_tables, monkeypatch):
    _, people, members = person_tables
    def fail(entity):
        raise RuntimeError('person write failed')
    monkeypatch.setattr(people, 'upsert_entity', fail)
    with pytest.raises(RuntimeError, match='person write failed'):
        app._create_person_entity('u1', ['f1'], [], person_id='p1')
    assert members.rows == {}


def test_deferred_last_write_wins_and_syncs_overwrite_delta(person_tables):
    _, people, members = person_tables
    app._create_person_entity('u1', ['keep', 'remove'], [], person_id='p1')
    original = dict(members.rows[('p1', 'keep')])
    members.submit_transaction_calls.clear()
    staged = {}
    app._create_person_entity('u1', ['intermediate'], [], person_id='p1', _defer_into=staged)
    app._create_person_entity('u1', ['keep', 'add'], [], person_id='p1', _defer_into=staged)
    app._batch_upsert_entities(people, staged.values())
    assert _member_face_ids(members, 'p1') == {'keep', 'add'}
    assert members.rows[('p1', 'keep')] == original
    assert all(row['RowKey'] != 'keep' for batch in members.submit_transaction_calls for _, row in batch)


def test_partial_person_batch_failure_syncs_only_committed_people(person_tables, monkeypatch):
    _, people, members = person_tables
    staged = {}
    for pid in ('good', 'bad'):
        app._create_person_entity('u1', [pid + '-face'], [], person_id=pid, _defer_into=staged)
    def reject_batch(operations):
        raise RuntimeError('transaction rejected')
    original = people.upsert_entity
    def reject_bad(entity):
        if entity['RowKey'] == 'bad':
            raise RuntimeError('bad person')
        original(entity)
    monkeypatch.setattr(people, 'submit_transaction', reject_batch)
    monkeypatch.setattr(people, 'upsert_entity', reject_bad)
    with pytest.raises(RuntimeError, match='bad person'):
        app._batch_upsert_entities(people, staged.values())
    assert _member_face_ids(members, 'good') == {'good-face'}
    assert _member_face_ids(members, 'bad') == set()


def test_shadow_failure_retry_repairs_without_rewriting_unchanged(person_tables, monkeypatch):
    _, people, members = person_tables
    real_upsert = members.upsert_entity
    def fail(entity):
        raise RuntimeError('shadow unavailable')
    monkeypatch.setattr(members, 'upsert_entity', fail)
    pid = app._create_person_entity('u1', ['f1', 'f2'], [], person_id='p1')
    assert ('u1', pid) in people.rows
    assert members.rows == {}
    monkeypatch.setattr(members, 'upsert_entity', real_upsert)
    app._batch_upsert_entities(people, people.rows.values())
    assert _member_face_ids(members, pid) == {'f1', 'f2'}
    members.submit_transaction_calls.clear()
    app._batch_upsert_entities(people, people.rows.values())
    assert members.submit_transaction_calls == []


def test_non_person_batch_does_not_write_membership_and_retains_fallback(person_tables, monkeypatch):
    _, _, members = person_tables
    unrelated = FakeTable()
    row = {'PartitionKey': 'u1', 'RowKey': 'other', 'faceIds': '["f1"]'}
    def reject_batch(operations):
        raise RuntimeError('no transactions')
    monkeypatch.setattr(unrelated, 'submit_transaction', reject_batch)
    app._batch_upsert_entities(unrelated, iter([row]))
    assert unrelated.rows[('u1', 'other')] == row
    assert members.rows == {}


def test_membership_transactions_are_partitioned_and_size_bounded(person_tables):
    _, people, members = person_tables
    staged = {}
    for pid in ('p1', 'p2'):
        ids = [f'f{i}' for i in range(205)]
        app._create_person_entity('u1', ids, [], person_id=pid, _defer_into=staged)
    app._batch_upsert_entities(people, staged.values(), chunk_size=1000)
    for batch in members.submit_transaction_calls:
        assert 0 < len(batch) <= 100
        assert len({row['PartitionKey'] for _, row in batch}) == 1
        assert sum(len(json.dumps(row).encode()) + 2048 for _, row in batch) < 4 * 1024 * 1024
    assert len(members.rows) == 410


def test_transaction_chunks_bound_payload_and_reject_oversized_single_rows():
    # Valid string-property sizes, large enough to hit the payload limit before
    # 100 operations. No live table or oversized faceIds person write involved.
    operations = (('upsert', {'PartitionKey': 'p1', 'RowKey': str(i),
                               'value': 'x' * 40000}) for i in range(205))
    batches = list(app._table_operation_chunks(operations, chunk_size=1000))
    assert sum(map(len, batches)) == 205
    assert all(len(batch) < 100 for batch in batches)
    assert all(sum(len(json.dumps(row).encode()) + 2048 for _, row in batch)
               < 4 * 1024 * 1024 for batch in batches)
    with pytest.raises(ValueError, match='payload limit'):
        list(app._table_operation_chunks([('upsert', {
            'PartitionKey': 'p1', 'RowKey': 'too-large', 'value': 'x' * (4 * 1024 * 1024),
        })]))


def test_shadow_read_failure_does_not_rewrite_members(person_tables, monkeypatch, caplog):
    _, people, members = person_tables
    app._create_person_entity('u1', ['f1'], [], person_id='p1')
    original = dict(members.rows)
    members.submit_transaction_calls.clear()
    def fail(*args, **kwargs):
        raise RuntimeError('shadow query failed')
    monkeypatch.setattr(members, 'query_entities', fail)
    app._batch_upsert_entities(people, people.rows.values())
    assert members.rows == original
    assert members.submit_transaction_calls == []
    assert 'Failed to synchronize' in caplog.text


def test_high_unique_memberships_use_bounded_concurrent_io(person_tables, monkeypatch):
    _, people, members = person_tables
    monkeypatch.setattr(app.os, 'cpu_count', lambda: 2)
    real_query = members.query_entities
    lock = threading.Lock()
    active = peak = 0
    def query(*args, **kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(0.003)
            return real_query(*args, **kwargs)
        finally:
            with lock:
                active -= 1
    monkeypatch.setattr(members, 'query_entities', query)
    staged = {}
    for i in range(150):
        app._create_person_entity('u1', [f'f{i}'], [], person_id=f'p{i}', _defer_into=staged)
    app._batch_upsert_entities(people, staged.values())
    assert 1 < peak <= 4
    assert len(members.rows) == 150
    assert all(len(batch) == 1 for batch in members.submit_transaction_calls)


def test_add_face_to_person_adds_membership_row(person_tables):
    face_table, person_table, members_table = person_tables
    person_id = app._create_person_entity('u1', ['f1'], [0.1, 0.2, 0.3], name='Alice')
    _seed_face(face_table, 'u1', 'f2', 'photo2.jpg')

    changed = app._add_face_to_person('u1', person_id, 'f2')

    assert changed is True
    assert _member_face_ids(members_table, person_id) == {'f1', 'f2'}
    assert sorted(json.loads(person_table.get_entity('u1', person_id)['faceIds'])) == ['f1', 'f2']


def test_remove_face_from_person_removes_membership_row(person_tables):
    _face_table, person_table, members_table = person_tables
    person_id = app._create_person_entity('u1', ['f1', 'f2'], [0.1, 0.2, 0.3], name='Alice')

    app._remove_face_from_person('u1', person_id, 'f1')

    assert _member_face_ids(members_table, person_id) == {'f2'}
    assert json.loads(person_table.get_entity('u1', person_id)['faceIds']) == ['f2']


def test_remove_face_from_person_keeps_named_person_empty_but_clears_membership(person_tables):
    _face_table, person_table, members_table = person_tables
    person_id = app._create_person_entity('u1', ['f1'], [0.1, 0.2, 0.3], name='Alice')

    app._remove_face_from_person('u1', person_id, 'f1')

    # Named person survives empty (not deleted) -- but its one membership row
    # must still be gone, matching its now-empty faceIds.
    assert json.loads(person_table.get_entity('u1', person_id)['faceIds']) == []
    assert _member_face_ids(members_table, person_id) == set()


def test_remove_face_from_person_deletes_unnamed_person_and_membership(person_tables):
    _face_table, person_table, members_table = person_tables
    person_id = app._create_person_entity('u1', ['f1'], [0.1, 0.2, 0.3], name='')

    app._remove_face_from_person('u1', person_id, 'f1')

    assert ('u1', person_id) not in person_table.rows
    assert _member_face_ids(members_table, person_id) == set()


def test_remove_face_from_person_with_retry_removes_membership_row(person_tables):
    _face_table, person_table, members_table = person_tables
    person_id = app._create_person_entity('u1', ['f1', 'f2'], [0.1, 0.2, 0.3], name='Alice')

    outcome = app._remove_face_from_person_with_retry('u1', person_id, 'f1')

    assert outcome == 'updated'
    assert _member_face_ids(members_table, person_id) == {'f2'}


def test_delete_person_cluster_removes_all_membership_rows(person_tables):
    face_table, person_table, members_table = person_tables
    person_id = app._create_person_entity('u1', ['f1', 'f2'], [0.1, 0.2, 0.3], name='Alice')
    _seed_face(face_table, 'u1', 'f1', 'photo1.jpg', person_id=person_id)
    _seed_face(face_table, 'u1', 'f2', 'photo2.jpg', person_id=person_id)

    app._delete_person_cluster('u1', person_id)

    assert ('u1', person_id) not in person_table.rows
    assert _member_face_ids(members_table, person_id) == set()


def test_repair_reconciles_stale_membership_against_authoritative_faceids(person_tables):
    """Simulates the exact drift an overwrite-style bulk writer (e.g.
    cluster_user_faces) can leave behind: faceIds says one thing,
    photopersonmembers says another. _repair_face_memberships must bring the
    membership table back in line with whatever it computes as correct."""
    face_table, person_table, members_table = person_tables
    person_id = app._create_person_entity('u1', ['f1', 'f2'], [0.1, 0.2, 0.3], name='Alice')
    _seed_face(face_table, 'u1', 'f1', 'photo1.jpg', person_id=person_id)
    _seed_face(face_table, 'u1', 'f2', 'photo2.jpg', person_id=person_id)

    # Introduce drift directly: a stray membership row for a face this person
    # doesn't actually own (f3, never added to faceIds), and a missing one
    # for a face it does own per the face table (f2 -- simulate a dual-write
    # that silently failed).
    members_table.upsert_entity({'PartitionKey': person_id, 'RowKey': 'f3', 'userId': 'u1'})
    members_table.delete_entity(person_id, 'f2')
    assert _member_face_ids(members_table, person_id) == {'f1', 'f3'}

    result = app._repair_face_memberships('u1', dry_run=False)

    assert result['success'] is True
    assert _member_face_ids(members_table, person_id) == {'f1', 'f2'}
