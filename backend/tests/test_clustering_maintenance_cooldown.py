"""Unit tests for the maintenance-recluster cooldown gate added to fix the
clustering-worker cost spiral: a sustained drip of face-ready triggers (a
long backfill, not a burst) used to chain _maybe_enqueue_coalesced_rerun
forever, keeping ownphotostore-worker alive continuously doing a full
DBSCAN pass every ~2 minutes. _clustering_maintenance_due bounds how often
that pass can start; these tests cover the gate itself, its two firing
points, and the self-healing property the whole redesign depends on: that a
DBSCAN maintenance pass can still merge two person rows that fragmented
because incremental assignment ran concurrently.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import app
from fakes import FakeTable


@pytest.fixture(autouse=True)
def metadata_table(monkeypatch):
    table = FakeTable()
    monkeypatch.setattr(app, 'metadata_table_client', table)
    return table


# --- _clustering_maintenance_due --------------------------------------------

def test_due_when_no_prior_row_and_stamps_one(metadata_table):
    assert app._clustering_maintenance_due('lib-A') is True
    row = metadata_table.get_entity('clustering_maintenance', 'lib-A')
    assert row['lastStartedAt']


def test_not_due_within_cooldown_and_does_not_touch_the_row(metadata_table):
    recent = datetime.now(timezone.utc).isoformat()
    metadata_table.upsert_entity({
        'PartitionKey': 'clustering_maintenance', 'RowKey': 'lib-A',
        'lastStartedAt': recent, 'updatedAt': recent,
    })

    assert app._clustering_maintenance_due('lib-A') is False
    assert metadata_table.get_entity('clustering_maintenance', 'lib-A')['lastStartedAt'] == recent


def test_due_again_once_cooldown_elapses_and_updates_the_row(metadata_table):
    stale = datetime.now(timezone.utc) - timedelta(seconds=app.PEOPLE_CLUSTER_MAINTENANCE_COOLDOWN_SECONDS + 60)
    metadata_table.upsert_entity({
        'PartitionKey': 'clustering_maintenance', 'RowKey': 'lib-A',
        'lastStartedAt': stale.isoformat(), 'updatedAt': stale.isoformat(),
    })

    assert app._clustering_maintenance_due('lib-A') is True
    new_last = metadata_table.get_entity('clustering_maintenance', 'lib-A')['lastStartedAt']
    assert new_last != stale.isoformat()


def test_cooldown_is_per_user(metadata_table):
    """A different user's cooldown state must not gate this one -- the row is
    keyed by RowKey=user_id specifically so this can't regress into the same
    shared-partition-scan shape as the jobs partition."""
    recent = datetime.now(timezone.utc).isoformat()
    metadata_table.upsert_entity({
        'PartitionKey': 'clustering_maintenance', 'RowKey': 'lib-A',
        'lastStartedAt': recent, 'updatedAt': recent,
    })
    assert app._clustering_maintenance_due('lib-B') is True


# --- _maybe_enqueue_coalesced_rerun's new gate ------------------------------

@pytest.fixture
def enqueue_spy(monkeypatch):
    calls = []
    monkeypatch.setattr(
        app, '_enqueue_clustering_job',
        lambda user_id, **kwargs: calls.append((user_id, kwargs)) or {'status': 'queued'},
    )
    return calls


def _seed_rerun_requested_job(metadata_table, job_id: str) -> None:
    metadata_table.upsert_entity({
        'PartitionKey': 'jobs',
        'RowKey': app._job_row_key(job_id),
        'jobId': job_id,
        'rerunRequested': True,
    })


def test_coalesced_rerun_skipped_when_maintenance_cooldown_active(metadata_table, enqueue_spy):
    job_id = 'cluster:lib-A:job-1'
    _seed_rerun_requested_job(metadata_table, job_id)
    recent = datetime.now(timezone.utc).isoformat()
    metadata_table.upsert_entity({
        'PartitionKey': 'clustering_maintenance', 'RowKey': 'lib-A',
        'lastStartedAt': recent, 'updatedAt': recent,
    })

    app._maybe_enqueue_coalesced_rerun(job_id, 'lib-A')

    assert enqueue_spy == []


def test_coalesced_rerun_fires_once_cooldown_elapsed(metadata_table, enqueue_spy):
    job_id = 'cluster:lib-A:job-1'
    _seed_rerun_requested_job(metadata_table, job_id)
    stale = datetime.now(timezone.utc) - timedelta(seconds=app.PEOPLE_CLUSTER_MAINTENANCE_COOLDOWN_SECONDS + 60)
    metadata_table.upsert_entity({
        'PartitionKey': 'clustering_maintenance', 'RowKey': 'lib-A',
        'lastStartedAt': stale.isoformat(), 'updatedAt': stale.isoformat(),
    })

    app._maybe_enqueue_coalesced_rerun(job_id, 'lib-A')

    assert len(enqueue_spy) == 1
    user_id, kwargs = enqueue_spy[0]
    assert user_id == 'lib-A'
    assert kwargs['job_type'] == 'people_cluster'
    assert kwargs['payload']['trigger'] == 'coalesced_rerun'


def test_coalesced_rerun_not_fired_when_rerun_was_never_requested(metadata_table, enqueue_spy):
    job_id = 'cluster:lib-A:job-1'
    metadata_table.upsert_entity({
        'PartitionKey': 'jobs', 'RowKey': app._job_row_key(job_id),
        'jobId': job_id, 'rerunRequested': False,
    })

    app._maybe_enqueue_coalesced_rerun(job_id, 'lib-A')

    assert enqueue_spy == []


# --- fragmentation self-heal: the core premise the redesign relies on ------
#
# Two brand-new photos of the same real person, processed by different
# ipworker replicas concurrently, can each independently fail to find a
# match and mint their own new unnamed person (see the plan's caveat 4).
# This is accepted as self-healing because cluster_user_faces (DBSCAN) folds
# any non-sticky/non-named face into its candidate pool regardless of which
# unnamed person currently owns it, and _cleanup_stale_people_state removes
# the resulting empty person row. If this test ever fails, that premise no
# longer holds and the "fragmentation is fine, maintenance cleans it up"
# reasoning needs to be revisited.

NEAR_IDENTICAL_A = [1.0, 0.2, 0.1, 0.05, 0.0, 0.0, 0.0, 0.0]
NEAR_IDENTICAL_B = [0.99, 0.21, 0.11, 0.04, 0.0, 0.0, 0.0, 0.0]


@pytest.fixture
def face_and_person_tables(monkeypatch):
    face_table = FakeTable()
    person_table = FakeTable()
    monkeypatch.setattr(app, 'face_table_client', face_table)
    monkeypatch.setattr(app, 'person_table_client', person_table)
    monkeypatch.setattr(app, '_person_scan_cache', app._UserScanCache(app.PEOPLE_SCAN_CACHE_TTL_SECONDS))
    monkeypatch.setattr(app, '_face_summary_scan_cache', app._UserScanCache(app.PEOPLE_SCAN_CACHE_TTL_SECONDS))
    monkeypatch.setattr(app, '_people_embedding_index_cache', app._UserScanCache(app.PEOPLE_SCAN_CACHE_TTL_SECONDS))
    return face_table, person_table


def test_cluster_user_faces_merges_two_fragmented_unnamed_people(face_and_person_tables):
    face_table, person_table = face_and_person_tables
    user_id = 'lib-A'

    person_table.upsert_entity({
        'PartitionKey': user_id, 'RowKey': 'person-frag-1', 'name': '',
        'faceIds': json.dumps(['face-1']), 'repEmbedding': json.dumps(NEAR_IDENTICAL_A),
    })
    person_table.upsert_entity({
        'PartitionKey': user_id, 'RowKey': 'person-frag-2', 'name': '',
        'faceIds': json.dumps(['face-2']), 'repEmbedding': json.dumps(NEAR_IDENTICAL_B),
    })
    for face_id, embedding, owner in (
        ('face-1', NEAR_IDENTICAL_A, 'person-frag-1'),
        ('face-2', NEAR_IDENTICAL_B, 'person-frag-2'),
    ):
        face_table.upsert_entity({
            'PartitionKey': user_id, 'RowKey': face_id, 'filename': f'{face_id}.jpg',
            'embedding': json.dumps(embedding),
            'alignmentMethod': 'landmark-5pt',
            'embeddingVersion': app.IPWORKER_FACE_CLUSTER_EMBEDDING_VERSION,
            'personId': owner,
        })

    app.cluster_user_faces(user_id)
    app._cleanup_stale_people_state(user_id)

    remaining_people = [row for row in person_table.rows.values() if row['PartitionKey'] == user_id]
    assert len(remaining_people) == 1
    surviving = remaining_people[0]
    surviving_face_ids = set(json.loads(surviving['faceIds']))
    assert surviving_face_ids == {'face-1', 'face-2'}
    assert face_table.get_entity(user_id, 'face-1')['personId'] == surviving['RowKey']
    assert face_table.get_entity(user_id, 'face-2')['personId'] == surviving['RowKey']
