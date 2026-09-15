"""Regression test for the second half of the 2026-09-04 incident: once
_split_cluster_by_max_pair_distance's O(n^3) hang was fixed, a full recluster
was still slow because cluster_user_faces wrote its results with a plain
upsert-per-entity loop in three places (per-cluster person creation,
per-face personId assignment, per-photo peopleIds update) -- each mislabeled
"batch" in a comment, none of them actually batched. At ~15k faces that's
thousands of sequential Table Storage round-trips. Fixed by routing all
three through _batch_upsert_entities (already used elsewhere in the
codebase for exactly this, e.g. /upload/init-batch).

This test proves the fix two ways: the real Azure Table Storage transaction
call is used (not a fallback), and the number of individual upsert_entity
calls made during the clustering run itself does NOT scale with the number
of faces/clusters -- the actual regression this guards against.
"""
from __future__ import annotations

import json

import pytest

import app
from fakes import FakeTable


class _BatchTrackingFakeTable(FakeTable):
    """FakeTable plus submit_transaction, so batched vs. one-by-one writes
    are distinguishable in a test -- FakeTable itself has no
    submit_transaction, so _batch_upsert_entities would otherwise silently
    fall back to individual upsert_entity calls and this test would pass
    for the wrong reason."""

    def __init__(self) -> None:
        super().__init__()
        self.transaction_calls: list = []
        self.direct_upsert_count = 0

    def upsert_entity(self, entity):
        self.direct_upsert_count += 1
        super().upsert_entity(entity)

    def submit_transaction(self, operations):
        ops = list(operations)
        self.transaction_calls.append(len(ops))
        for op, entity, *_rest in ops:
            assert op == 'upsert'
            self.rows[(entity['PartitionKey'], entity['RowKey'])] = dict(entity)
        return [{} for _ in ops]

    def reset_call_tracking(self) -> None:
        """Seeding a fake with upsert_entity shouldn't count against the
        assertions below -- only calls made during the clustering run itself
        should."""
        self.transaction_calls = []
        self.direct_upsert_count = 0


@pytest.fixture
def clustering_tables(monkeypatch):
    face_table = _BatchTrackingFakeTable()
    person_table = _BatchTrackingFakeTable()
    metadata_table = _BatchTrackingFakeTable()
    monkeypatch.setattr(app, 'face_table_client', face_table)
    monkeypatch.setattr(app, 'person_table_client', person_table)
    monkeypatch.setattr(app, 'metadata_table_client', metadata_table)
    # Fresh scan caches per test -- writes here go straight to the fakes,
    # bypassing the real invalidating-table-client wrapper that would
    # normally invalidate these on write (see test_clustering_incremental_assign.py).
    monkeypatch.setattr(app, '_person_scan_cache', app._UserScanCache(app.PEOPLE_SCAN_CACHE_TTL_SECONDS))
    monkeypatch.setattr(app, '_face_summary_scan_cache', app._UserScanCache(app.PEOPLE_SCAN_CACHE_TTL_SECONDS))
    monkeypatch.setattr(app, '_people_embedding_index_cache', app._UserScanCache(app.PEOPLE_SCAN_CACHE_TTL_SECONDS))
    return face_table, person_table, metadata_table


def _seed_face(face_table, user_id, face_id, filename, embedding) -> None:
    face_table.upsert_entity({
        'PartitionKey': user_id,
        'RowKey': face_id,
        'filename': filename,
        'embedding': json.dumps(embedding),
        'alignmentMethod': 'landmark-5pt',
        'embeddingVersion': app.IPWORKER_FACE_CLUSTER_EMBEDDING_VERSION,
        'confidence': 0.95,
        'personId': '',
    })


def _seed_metadata(metadata_table, user_id, filename) -> None:
    metadata_table.upsert_entity({
        'PartitionKey': user_id,
        'RowKey': filename,
        'processing_state': 'active',
        'peopleIds': '[]',
    })


# Three near-orthogonal directions (well beyond any plausible eps) with tiny
# per-face jitter (well within any plausible eps), so DBSCAN reliably forms
# exactly 3 clusters of 4 faces each regardless of the exact threshold used.
_GROUP_DIRECTIONS = [
    [1.0, 0.0, 0.0, 0.0],
    [0.0, 1.0, 0.0, 0.0],
    [0.0, 0.0, 1.0, 0.0],
]


def test_full_recluster_writes_in_batches_not_one_row_at_a_time(clustering_tables):
    face_table, person_table, metadata_table = clustering_tables
    user_id = 'owner'

    expected_groups = {}
    for group_idx, direction in enumerate(_GROUP_DIRECTIONS):
        face_ids = []
        for member_idx in range(4):
            face_id = f'face-{group_idx}-{member_idx}'
            filename = f'{face_id}.jpg'
            jitter = member_idx * 1e-4
            embedding = [v + jitter if v else jitter for v in direction]
            _seed_face(face_table, user_id, face_id, filename, embedding)
            _seed_metadata(metadata_table, user_id, filename)
            face_ids.append(face_id)
        expected_groups[group_idx] = set(face_ids)

    for table in (face_table, person_table, metadata_table):
        table.reset_call_tracking()

    result = app.cluster_user_faces(user_id, eps=0.3, min_samples=2)

    assert 'error' not in result, result
    assert len(result['clusters']) == 3

    actual_groups = {frozenset(members) for members in result['clusters'].values()}
    expected = {frozenset(members) for members in expected_groups.values()}
    assert actual_groups == expected

    # The actual regression guard: writes went through one real transaction
    # per table, not a fallback loop of individual upserts -- 3 people, 12
    # faces, 12 photos, all well under the 100-op chunk size, so each table
    # sees exactly one submit_transaction call covering everything.
    assert person_table.transaction_calls == [3]
    assert person_table.direct_upsert_count == 0

    assert face_table.transaction_calls == [12]
    assert face_table.direct_upsert_count == 0

    assert metadata_table.transaction_calls == [12]
    assert metadata_table.direct_upsert_count == 0

    # Functional correctness, not just "some batching happened": every face
    # ended up assigned to the right person, and every photo's peopleIds
    # reflects it.
    person_id_by_face = {}
    for group_idx, face_ids in expected_groups.items():
        person_ids_seen = set()
        for face_id in face_ids:
            face_row = face_table.get_entity(user_id, face_id)
            assert face_row['personId']
            person_id_by_face[face_id] = face_row['personId']
            person_ids_seen.add(face_row['personId'])
        assert len(person_ids_seen) == 1, f'group {group_idx} split across people: {person_ids_seen}'

    for face_id, person_id in person_id_by_face.items():
        filename = f'{face_id}.jpg'
        metadata_row = metadata_table.get_entity(user_id, filename)
        people_ids = json.loads(metadata_row['peopleIds'])
        assert person_id in people_ids


def test_batching_holds_at_larger_scale(clustering_tables):
    """Same shape, more groups/members -- the transaction count should grow
    with the number of distinct people, never with the number of individual
    direct upserts (that's exactly the axis that used to make this slow)."""
    face_table, person_table, metadata_table = clustering_tables
    user_id = 'owner'

    directions = [
        [1.0, 0.0, 0.0, 0.0, 0.0],
        [0.0, 1.0, 0.0, 0.0, 0.0],
        [0.0, 0.0, 1.0, 0.0, 0.0],
        [0.0, 0.0, 0.0, 1.0, 0.0],
        [0.0, 0.0, 0.0, 0.0, 1.0],
    ]
    for group_idx, direction in enumerate(directions):
        for member_idx in range(6):
            face_id = f'face-{group_idx}-{member_idx}'
            filename = f'{face_id}.jpg'
            jitter = member_idx * 1e-4
            embedding = [v + jitter if v else jitter for v in direction]
            _seed_face(face_table, user_id, face_id, filename, embedding)
            _seed_metadata(metadata_table, user_id, filename)

    for table in (face_table, person_table, metadata_table):
        table.reset_call_tracking()

    result = app.cluster_user_faces(user_id, eps=0.3, min_samples=2)

    assert 'error' not in result, result
    assert len(result['clusters']) == 5

    assert person_table.direct_upsert_count == 0
    assert face_table.direct_upsert_count == 0
    assert metadata_table.direct_upsert_count == 0
    assert person_table.transaction_calls == [5]
    assert face_table.transaction_calls == [30]
    assert metadata_table.transaction_calls == [30]
