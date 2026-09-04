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
import re
from datetime import datetime, timedelta, timezone

import pytest
from azure.core.exceptions import ResourceExistsError, ResourceModifiedError, ResourceNotFoundError
from azure.data.tables import TableEntity

import app
from fakes import FakeTable


class _FakeMaintenanceTable:
    """Combines the plain-dict FakeTable surface (upsert_entity/query_entities,
    used by this file's 'jobs' partition seeding) with real ETag semantics
    for create_entity/get_entity/update_entity -- _clustering_maintenance_due
    needs both against the same metadata_table_client now that its cooldown
    claim is atomic. Mirrors test_ipwork_sweep.py's _FakeLockTable, which
    covers the identical create-then-conditional-update shape for the ipwork
    sweep lock."""

    def __init__(self) -> None:
        self.rows: dict = {}
        self._etags: dict = {}
        self._version = 0

    def _bump_etag(self, key) -> None:
        self._version += 1
        self._etags[key] = f'W/"{self._version}"'

    def upsert_entity(self, entity):
        key = (entity['PartitionKey'], entity['RowKey'])
        self.rows[key] = dict(entity)
        self._bump_etag(key)

    def create_entity(self, entity):
        key = (entity['PartitionKey'], entity['RowKey'])
        if key in self.rows:
            raise ResourceExistsError('already exists')
        self.rows[key] = dict(entity)
        self._bump_etag(key)

    def get_entity(self, partition_key, row_key):
        key = (partition_key, row_key)
        if key not in self.rows:
            raise ResourceNotFoundError(f'{key} not found')
        entity = TableEntity(dict(self.rows[key]))
        entity._metadata = {'etag': self._etags[key], 'timestamp': None}
        return entity

    def update_entity(self, entity, mode=None, *, etag=None, match_condition=None):
        key = (entity['PartitionKey'], entity['RowKey'])
        if key not in self.rows:
            raise ResourceNotFoundError(f'{key} not found')
        if etag is not None and etag != self._etags[key]:
            raise ResourceModifiedError('etag mismatch')
        self.rows[key] = dict(entity)
        self._bump_etag(key)

    def delete_entity(self, partition_key, row_key):
        self.rows.pop((partition_key, row_key), None)

    def query_entities(self, filter_str):
        m = re.match(r"PartitionKey eq '(.*)' and (\w+) eq '(.*)'$", filter_str.strip())
        if m:
            pk, field, value = m.group(1), m.group(2), m.group(3)
            return [
                dict(v) for (p, _), v in self.rows.items()
                if p == pk and str(v.get(field, '')) == value
            ]
        m = re.match(r"PartitionKey eq '(.*)'$", filter_str.strip())
        if m:
            pk = m.group(1)
            return [dict(v) for (p, _), v in self.rows.items() if p == pk]
        raise ValueError(f'Unsupported filter: {filter_str}')


@pytest.fixture(autouse=True)
def metadata_table(monkeypatch):
    table = _FakeMaintenanceTable()
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


def test_burst_of_concurrent_callers_only_one_wins_the_claim(metadata_table):
    """Regression test for a real production incident (2026-09-03): a burst
    of uploads whose face-processing lands within the same
    _jobs_partition_scan_cache TTL window used to each independently read
    the old blind upsert's pre-claim state and each proceed, producing 9
    concurrent full-library clustering jobs for one user from a single
    upload batch instead of 1. The atomic create-then-conditional-update
    claim must let exactly one caller through per cooldown window regardless
    of how many check at once -- this simulates that burst with no prior row
    (the exact scenario observed live)."""
    results = [app._clustering_maintenance_due('lib-A') for _ in range(9)]
    assert results == [True] + [False] * 8


def test_burst_of_concurrent_callers_after_cooldown_elapsed_only_one_wins(metadata_table):
    stale = datetime.now(timezone.utc) - timedelta(seconds=app.PEOPLE_CLUSTER_MAINTENANCE_COOLDOWN_SECONDS + 60)
    metadata_table.upsert_entity({
        'PartitionKey': 'clustering_maintenance', 'RowKey': 'lib-A',
        'lastStartedAt': stale.isoformat(), 'updatedAt': stale.isoformat(),
    })

    results = [app._clustering_maintenance_due('lib-A') for _ in range(9)]
    assert results == [True] + [False] * 8


def _make_due_after_cooldown(metadata_table, user_id: str, runs_since_upload: int) -> None:
    stale = datetime.now(timezone.utc) - timedelta(seconds=app.PEOPLE_CLUSTER_MAINTENANCE_COOLDOWN_SECONDS + 60)
    metadata_table.upsert_entity({
        'PartitionKey': 'clustering_maintenance', 'RowKey': user_id,
        'lastStartedAt': stale.isoformat(), 'updatedAt': stale.isoformat(),
        'runsSinceUpload': runs_since_upload,
    })


def test_maintenance_pauses_once_run_cap_hit_without_a_fresh_upload(metadata_table):
    """Regression test for a real cost concern (2026-09-04): with only a time
    cooldown, a non-upload drip (ipwork sweep recovering stale photos,
    client-processing resubmissions, coalesced reruns) can re-arm the full
    ~9000+-face DBSCAN pass every 30 minutes forever, even weeks after the
    user's last real upload. Once PEOPLE_CLUSTER_MAX_MAINTENANCE_RUNS_WITHOUT_UPLOAD
    passes have fired back-to-back with no fresh upload in between, further
    triggers must stop being granted regardless of how stale lastStartedAt is."""
    _make_due_after_cooldown(metadata_table, 'lib-A', app.PEOPLE_CLUSTER_MAX_MAINTENANCE_RUNS_WITHOUT_UPLOAD)

    assert app._clustering_maintenance_due('lib-A') is False


def test_maintenance_still_fires_at_exactly_the_cap_boundary(metadata_table):
    _make_due_after_cooldown(metadata_table, 'lib-A', app.PEOPLE_CLUSTER_MAX_MAINTENANCE_RUNS_WITHOUT_UPLOAD - 1)

    assert app._clustering_maintenance_due('lib-A') is True
    row = metadata_table.get_entity('clustering_maintenance', 'lib-A')
    assert row['runsSinceUpload'] == app.PEOPLE_CLUSTER_MAX_MAINTENANCE_RUNS_WITHOUT_UPLOAD


def test_fresh_upload_reset_does_not_bypass_the_separate_cooldown_timer(metadata_table):
    """The run-cap reset and the per-pass cooldown are two independent
    gates -- resetting one must not short-circuit the other. Set up a
    *recent* lastStartedAt (still within the cooldown window) alongside a
    capped counter, reset the counter, and confirm the cooldown alone still
    blocks the next pass."""
    recent = datetime.now(timezone.utc).isoformat()
    metadata_table.upsert_entity({
        'PartitionKey': 'clustering_maintenance', 'RowKey': 'lib-A',
        'lastStartedAt': recent, 'updatedAt': recent,
        'runsSinceUpload': app.PEOPLE_CLUSTER_MAX_MAINTENANCE_RUNS_WITHOUT_UPLOAD,
    })
    assert app._clustering_maintenance_due('lib-A') is False  # capped

    app._mark_fresh_upload_activity('lib-A')

    # Cooldown alone (lastStartedAt is still recent) must keep this blocked,
    # independent of the counter having been reset.
    assert app._clustering_maintenance_due('lib-A') is False
    row = metadata_table.get_entity('clustering_maintenance', 'lib-A')
    assert row['runsSinceUpload'] == 0
    assert row['lastStartedAt'] == recent  # reset must not touch the cooldown timer


def test_fresh_upload_reset_lets_maintenance_resume_once_cooldown_also_elapses(metadata_table):
    stale = datetime.now(timezone.utc) - timedelta(seconds=app.PEOPLE_CLUSTER_MAINTENANCE_COOLDOWN_SECONDS + 60)
    metadata_table.upsert_entity({
        'PartitionKey': 'clustering_maintenance', 'RowKey': 'lib-A',
        'lastStartedAt': stale.isoformat(), 'updatedAt': stale.isoformat(),
        'runsSinceUpload': app.PEOPLE_CLUSTER_MAX_MAINTENANCE_RUNS_WITHOUT_UPLOAD,
    })
    assert app._clustering_maintenance_due('lib-A') is False  # capped

    app._mark_fresh_upload_activity('lib-A')

    # lastStartedAt from the setup above is already outside the cooldown
    # window, and the cap is now reset -- a genuinely fresh upload should be
    # able to earn the library a maintenance pass again.
    assert app._clustering_maintenance_due('lib-A') is True


def test_reset_does_not_touch_a_nonexistent_row(metadata_table):
    """A user with no maintenance row yet (never had a maintenance pass)
    uploading for the first time must not error or create a broken partial
    row -- _clustering_maintenance_due's own create_entity path still owns
    first-row creation."""
    app._mark_fresh_upload_activity('lib-fresh')
    assert app._clustering_maintenance_due('lib-fresh') is True


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
