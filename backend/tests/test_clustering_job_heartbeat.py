"""Regression test for a live bug found 2026-09-04: a full people_cluster
pass over a large-enough face table can legitimately run longer than
CLUSTERING_ACTIVE_JOB_STALE_MINUTES, and _handle_clustering_queue_payload
never touched the job row's updatedAt between the initial 'running' write
and the final done/failed write. /api/jobs/status's staleness sweep then
force-flips a perfectly healthy, still-computing job to 'failed' ("worker
restarted or timed out") -- confirmed live via a worker showing 0 restarts
whose in-flight job still got killed at exactly the 15-minute mark.

Mirrors the fix (and the test style) already proven for the identical
failure mode in _execute_library_download -- see
test_library_download.py::test_execute_library_download_heartbeats_job_status.
"""
from __future__ import annotations

import threading

import pytest

import app
from fakes import FakeTable


@pytest.fixture
def metadata_table(monkeypatch):
    table = FakeTable()
    monkeypatch.setattr(app, 'jobs_table_client', table)
    monkeypatch.setattr(app, '_people_features_available', lambda: True)
    monkeypatch.setattr(app, '_maybe_enqueue_coalesced_rerun', lambda job_id, user_id: None)
    return table


def test_slow_people_cluster_job_gets_heartbeated_before_it_finishes(monkeypatch, metadata_table):
    monkeypatch.setattr(app, 'CLUSTERING_JOB_HEARTBEAT_SECONDS', 0.05)

    release = threading.Event()

    def _slow_cluster_user_faces(user_id, eps=None, min_samples=None):
        # Block long enough for several heartbeat intervals to fire before
        # the job's own terminal write happens.
        release.wait(timeout=5)
        return {'clusters': {}, 'created': []}

    monkeypatch.setattr(app, 'cluster_user_faces', _slow_cluster_user_faces)
    monkeypatch.setattr(app, '_cleanup_stale_people_state', lambda user_id: {})

    job_id = 'cluster:u1:job1'
    thread = threading.Thread(
        target=app._handle_clustering_queue_payload,
        args=({'trigger': 'upload_face_ready'}, job_id, 'u1', 'people_cluster'),
    )
    thread.start()

    def _updated_at_write_count() -> int:
        row = metadata_table.rows.get(('u1', job_id))
        return 0 if row is None else 1

    # Wait for the initial 'running' write, then give the heartbeat thread
    # room to fire multiple times while cluster_user_faces is still blocked.
    for _ in range(100):
        if _updated_at_write_count():
            break
        threading.Event().wait(0.01)
    row_after_start = metadata_table.get_entity('u1', job_id)
    assert row_after_start['status'] == 'running'
    first_updated_at = row_after_start['updatedAt']

    heartbeat_seen = False
    for _ in range(200):
        row = metadata_table.get_entity('u1', job_id)
        if row['status'] == 'running' and row['updatedAt'] != first_updated_at:
            heartbeat_seen = True
            break
        threading.Event().wait(0.01)

    assert heartbeat_seen, (
        'updatedAt never refreshed while cluster_user_faces was still running -- '
        'a real full recluster this slow would get falsely flagged failed by the '
        '15-minute staleness sweep even though the worker never crashed'
    )

    release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()

    final_row = metadata_table.get_entity('u1', job_id)
    assert final_row['status'] == 'done'


def test_heartbeat_thread_stops_after_job_finishes(monkeypatch, metadata_table):
    """The heartbeat must not keep writing 'running' after the job's own
    terminal write, or it could race the final status and flip it back."""
    monkeypatch.setattr(app, 'CLUSTERING_JOB_HEARTBEAT_SECONDS', 0.02)
    monkeypatch.setattr(app, 'cluster_user_faces', lambda user_id, eps=None, min_samples=None: {'clusters': {}, 'created': []})
    monkeypatch.setattr(app, '_cleanup_stale_people_state', lambda user_id: {})

    job_id = 'cluster:u1:job2'
    app._handle_clustering_queue_payload({'trigger': 'upload_face_ready'}, job_id, 'u1', 'people_cluster')

    row = metadata_table.get_entity('u1', job_id)
    assert row['status'] == 'done'

    # Give a leftover heartbeat thread, if any, a chance to fire and corrupt
    # the terminal status before asserting it stayed put.
    threading.Event().wait(0.2)
    row_after_wait = metadata_table.get_entity('u1', job_id)
    assert row_after_wait['status'] == 'done'


def test_slow_library_clean_job_gets_heartbeated_before_it_finishes(monkeypatch, metadata_table):
    """library_clean is dispatched before _handle_clustering_queue_payload's
    shared clustering-job heartbeat setup (it returns early), so it needs its
    own heartbeat -- this was the actual live gap: a large-enough library's
    walk-and-delete legitimately outran the 15-minute staleness cutoff with
    no heartbeat ever refreshing updatedAt in between."""
    monkeypatch.setattr(app, 'CLUSTERING_JOB_HEARTBEAT_SECONDS', 0.05)

    class _FakeLibraryStore:
        def set_cleanup_completed(self, library_id, photos_deleted, blobs_deleted):
            pass

        def set_cleanup_failed(self, library_id, reason):
            pass

    monkeypatch.setattr(app, 'library_store', _FakeLibraryStore())
    monkeypatch.setattr(app, '_notify_cleanup_completed', lambda library_id, summary: None)

    release = threading.Event()

    def _slow_execute_library_clean(library_id):
        release.wait(timeout=5)
        return {'photosDeleted': 0, 'blobsDeleted': 0, 'blobErrors': 0}

    monkeypatch.setattr(app, '_execute_library_clean', _slow_execute_library_clean)

    job_id = 'libclean:lib1:job1'
    thread = threading.Thread(
        target=app._handle_clustering_queue_payload,
        args=({'libraryId': 'lib1'}, job_id, 'u1', 'library_clean'),
    )
    thread.start()

    for _ in range(100):
        row = metadata_table.get_entity('u1', job_id)
        if row is not None:
            break
        threading.Event().wait(0.01)
    row_after_start = metadata_table.get_entity('u1', job_id)
    assert row_after_start['status'] == 'running'
    first_updated_at = row_after_start['updatedAt']

    heartbeat_seen = False
    for _ in range(200):
        row = metadata_table.get_entity('u1', job_id)
        if row['status'] == 'running' and row['updatedAt'] != first_updated_at:
            heartbeat_seen = True
            break
        threading.Event().wait(0.01)

    assert heartbeat_seen, (
        'updatedAt never refreshed while _execute_library_clean was still running -- '
        'a real cleanup this slow would get falsely flagged failed by the '
        '15-minute staleness sweep even though the worker never crashed'
    )

    release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()

    final_row = metadata_table.get_entity('u1', job_id)
    assert final_row['status'] == 'done'
