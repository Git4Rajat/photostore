"""Unit tests for _assign_faces_to_people_incrementally, promoted from a
fallback-only path (used only when the clustering queue was unconfigured) to
the primary, synchronous face-to-person matcher called on every upload -- see
_queue_people_clustering_after_face_processing. These tests exercise the
matching logic itself (existing-person match vs. new-person creation vs.
within-call fragmentation avoidance) against a real numpy/DBSCAN-free
similarity check, not just wiring.
"""
from __future__ import annotations

import json
import random

import pytest

import app
from fakes import FakeTable

# Two embeddings deliberately far enough apart (near-orthogonal) that cosine
# similarity sits well below any of the assign-threshold presets (0.68-0.80),
# and one embedding pair deliberately near-identical (cosine ~1.0) so tests
# don't need to know the exact resolved threshold for the test environment.
PERSON_A_EMBEDDING = [1.0, 0.2, 0.1, 0.05, 0.0, 0.0, 0.0, 0.0]
PERSON_A_EMBEDDING_CLOSE = [0.98, 0.22, 0.11, 0.04, 0.01, 0.0, 0.0, 0.0]
PERSON_B_EMBEDDING = [0.0, 0.0, 0.0, 0.0, 1.0, 0.2, 0.1, 0.05]


@pytest.fixture(autouse=True)
def clustering_tables(monkeypatch):
    face_table = FakeTable()
    person_table = FakeTable()
    monkeypatch.setattr(app, 'face_table_client', face_table)
    monkeypatch.setattr(app, 'person_table_client', person_table)
    # These helpers cache per-user scans for PEOPLE_SCAN_CACHE_TTL_SECONDS;
    # fresh instances per test avoid cross-test staleness since writes here go
    # straight to FakeTable, bypassing the real _InvalidatingTableClient
    # wrapper that would normally invalidate the cache on write.
    monkeypatch.setattr(app, '_person_scan_cache', app._UserScanCache(app.PEOPLE_SCAN_CACHE_TTL_SECONDS))
    monkeypatch.setattr(app, '_face_summary_scan_cache', app._UserScanCache(app.PEOPLE_SCAN_CACHE_TTL_SECONDS))
    monkeypatch.setattr(app, '_people_embedding_index_cache', app._UserScanCache(app.PEOPLE_SCAN_CACHE_TTL_SECONDS))
    return face_table, person_table


def _seed_face(face_table: FakeTable, user_id: str, face_id: str, filename: str, embedding, **overrides) -> None:
    row = {
        'PartitionKey': user_id,
        'RowKey': face_id,
        'filename': filename,
        'embedding': json.dumps(embedding),
        'alignmentMethod': 'landmark-5pt',
        # _face_embedding_allowed_for_clustering rejects rows whose
        # embeddingVersion isn't in _face_embedding_allowed_versions(), which
        # defaults non-empty (IPWORKER_FACE_CLUSTER_EMBEDDING_VERSION has a
        # real default, not '') -- match it so these rows are clusterable.
        'embeddingVersion': app.IPWORKER_FACE_CLUSTER_EMBEDDING_VERSION,
        'personId': '',
    }
    row.update(overrides)
    face_table.upsert_entity(row)


def _seed_person(person_table: FakeTable, user_id: str, person_id: str, face_ids, embedding, name: str = '') -> None:
    person_table.upsert_entity({
        'PartitionKey': user_id,
        'RowKey': person_id,
        'name': name,
        'faceIds': json.dumps(face_ids),
        'repEmbedding': json.dumps(embedding),
    })


def test_empty_face_ids_is_a_no_op(clustering_tables):
    face_table, person_table = clustering_tables
    assignments, created = app._assign_faces_to_people_incrementally('lib-A', 'photo.jpg', [])
    assert (assignments, created) == ({}, set())
    assert face_table.rows == {}
    assert person_table.rows == {}


def test_confident_match_assigns_to_existing_person_without_creating_new_one(clustering_tables):
    face_table, person_table = clustering_tables
    user_id = 'lib-A'
    _seed_person(person_table, user_id, 'person-1', ['old-face'], PERSON_A_EMBEDDING, name='')
    _seed_face(face_table, user_id, 'old-face', 'earlier.jpg', PERSON_A_EMBEDDING, personId='person-1')
    _seed_face(face_table, user_id, 'new-face', 'photo.jpg', PERSON_A_EMBEDDING_CLOSE)

    assignments, created = app._assign_faces_to_people_incrementally(user_id, 'photo.jpg', ['new-face'])

    assert assignments == {'new-face': 'person-1'}
    assert created == set()
    assert face_table.get_entity(user_id, 'new-face')['personId'] == 'person-1'
    # Only the one pre-existing person row -- no new person was minted.
    assert len(person_table.rows) == 1


def test_no_match_creates_exactly_one_new_unnamed_person(clustering_tables):
    face_table, person_table = clustering_tables
    user_id = 'lib-A'
    _seed_person(person_table, user_id, 'person-1', ['old-face'], PERSON_A_EMBEDDING, name='')
    _seed_face(face_table, user_id, 'old-face', 'earlier.jpg', PERSON_A_EMBEDDING, personId='person-1')
    _seed_face(face_table, user_id, 'new-face', 'photo.jpg', PERSON_B_EMBEDDING)

    assignments, created = app._assign_faces_to_people_incrementally(user_id, 'photo.jpg', ['new-face'])

    assert set(assignments) == {'new-face'}
    new_person_id = assignments['new-face']
    assert new_person_id != 'person-1'
    assert created == {new_person_id}
    assert face_table.get_entity(user_id, 'new-face')['personId'] == new_person_id
    # The pre-existing person plus exactly one freshly minted one.
    assert len(person_table.rows) == 2


def test_second_new_face_in_same_call_matches_the_first_freshly_created_person(clustering_tables):
    """The property most load-bearing for 'don't fragment within one photo':
    _assign_faces_to_people_incrementally appends each newly-created person to
    its in-call session_embedding_index immediately, so a second brand-new
    face of the same (not-yet-known) person in the same call matches it
    instead of minting a second new person."""
    face_table, person_table = clustering_tables
    user_id = 'lib-A'
    _seed_face(face_table, user_id, 'face-1', 'photo.jpg', PERSON_B_EMBEDDING)
    _seed_face(face_table, user_id, 'face-2', 'photo.jpg', PERSON_B_EMBEDDING)

    assignments, created = app._assign_faces_to_people_incrementally(user_id, 'photo.jpg', ['face-1', 'face-2'])

    assert len(created) == 1
    assert assignments['face-1'] == assignments['face-2']
    assert set(assignments.values()) == created
    assert len(person_table.rows) == 1


def test_face_failing_clusterable_gate_is_skipped(clustering_tables):
    face_table, person_table = clustering_tables
    user_id = 'lib-A'
    _seed_face(
        face_table, user_id, 'rejected-face', 'photo.jpg', PERSON_B_EMBEDDING,
        rejected=True,
    )

    assignments, created = app._assign_faces_to_people_incrementally(user_id, 'photo.jpg', ['rejected-face'])

    assert assignments == {}
    assert created == set()
    assert face_table.get_entity(user_id, 'rejected-face')['personId'] == ''
    assert person_table.rows == {}


def test_face_failing_embedding_alignment_gate_is_skipped(clustering_tables):
    face_table, person_table = clustering_tables
    user_id = 'lib-A'
    _seed_face(
        face_table, user_id, 'unaligned-face', 'photo.jpg', PERSON_B_EMBEDDING,
        alignmentMethod='none',
    )

    assignments, created = app._assign_faces_to_people_incrementally(user_id, 'photo.jpg', ['unaligned-face'])

    assert assignments == {}
    assert created == set()
    assert person_table.rows == {}


def _reference_best_two_matches(face_norm, session_embedding_index, np):
    """Literal reimplementation of the pre-vectorization per-entry scan that
    _best_two_person_matches replaced, used as an independent oracle."""
    best_score = 0.0
    second_best_score = 0.0
    best_person = None
    for entry in session_embedding_index:
        existing_norm = app._normalized_embedding_for_entry(entry, np)
        if existing_norm is None:
            continue
        score = app._embedding_similarity_between_normalized(face_norm, existing_norm, np)
        if score is None:
            continue
        if score > best_score:
            second_best_score = best_score
            best_score = score
            best_person = entry
        elif score > second_best_score:
            second_best_score = score
    return best_score, second_best_score, best_person


def test_vectorized_best_two_matches_agrees_with_reference_scan_including_mixed_dimensions():
    """_best_two_person_matches batches same-dimension entries into one numpy
    matmul instead of the original one-Python-call-per-entry scan (see its
    docstring for why -- unvectorized per-entry work was a real, previously
    undocumented source of the GIL contention that made raising
    GUNICORN_THREADS make burst latency worse, not better). This proves the
    batched path is numerically equivalent to the original scan across many
    random libraries, including ones with a minority of entries on a
    different embedding dimension (an older taxonomy version, still scored
    via the original per-entry fallback)."""
    import numpy as np

    rng = random.Random(1234)

    for trial in range(30):
        dim = 8
        n_people = rng.randint(0, 40)
        session_embedding_index = []
        for i in range(n_people):
            # ~20% of entries simulate a stale, shorter embedding dimension
            # from an older model version.
            this_dim = dim - 2 if rng.random() < 0.2 else dim
            rep = [rng.uniform(-1, 1) for _ in range(this_dim)]
            session_embedding_index.append({
                'personId': f'person-{trial}-{i}',
                'repEmbedding': rep,
            })
        face_vec = [rng.uniform(-1, 1) for _ in range(dim)]
        face_norm = app._normalized_embedding(face_vec, np)

        got_best, got_second, got_person = app._best_two_person_matches(face_norm, session_embedding_index, np)
        want_best, want_second, want_person = _reference_best_two_matches(
            face_norm, [dict(e) for e in session_embedding_index], np,
        )

        assert got_best == pytest.approx(want_best, abs=1e-9)
        assert got_second == pytest.approx(want_second, abs=1e-9)
        got_id = got_person.get('personId') if got_person else None
        want_id = want_person.get('personId') if want_person else None
        assert got_id == want_id
