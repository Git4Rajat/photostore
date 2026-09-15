"""Unit tests for _handle_ipwork_queue_payload's dispatch wiring.

The browser reaches _queue_people_clustering_after_face_processing through two
HTTP routes (/upload and /upload/client-processing) right after it POSTs its
own face results. ipworker writes results directly through
apply_client_processing_results_for_file instead of an HTTP call, so it needs
its own explicit call to the same trigger -- otherwise, in processingMode
'backend', ipworker would detect faces that never get clustered into people.
These tests isolate that wiring from the rest of app.py's global state by
stubbing out claim/release lease, step execution, and the result-application
call, and just asserting the clustering trigger fires (or doesn't) correctly.
"""
from __future__ import annotations

import pytest

import app


class _Calls:
    def __init__(self) -> None:
        self.enqueue_clustering: list = []
        self.job_statuses: list = []
        self.incremental_assign: list = []


@pytest.fixture
def dispatch_ctx(monkeypatch):
    calls = _Calls()
    monkeypatch.setattr(app, 'claim_processing_lease', lambda *a, **k: {'ownerId': a[2] if len(a) > 2 else k.get('owner_id')})
    monkeypatch.setattr(app, 'release_processing_lease', lambda *a, **k: None)
    monkeypatch.setattr(
        app, '_upsert_job_status',
        lambda job_id, user_id, job_type, status, **fields: calls.job_statuses.append((job_type, status)),
    )
    monkeypatch.setattr(app, '_people_features_available', lambda: True)
    monkeypatch.setattr(
        app, '_enqueue_clustering_job',
        lambda user_id, **kwargs: calls.enqueue_clustering.append((user_id, kwargs)) or {'status': 'queued', 'jobId': 'cluster:job'},
    )
    # Default to "maintenance is due" so existing tests keep exercising the
    # enqueue path; test_maintenance_cooldown_skips_enqueue overrides this.
    monkeypatch.setattr(app, '_clustering_maintenance_due', lambda user_id: True)
    # Incremental face-to-person assignment is queued for the standalone
    # clustering worker (see _enqueue_incremental_assign_job), not run
    # in-process here -- see test_clustering_incremental_assign.py for the
    # worker-side dispatch of the resulting 'people_incremental_assign' job.
    monkeypatch.setattr(
        app, '_enqueue_incremental_assign_job',
        lambda user_id, filename: calls.incremental_assign.append((user_id, filename)) or {'status': 'queued'},
    )
    yield calls


def test_face_step_success_triggers_clustering(monkeypatch, dispatch_ctx):
    monkeypatch.setattr(app, '_run_ipwork_steps', lambda user_id, filename, steps: {'face': {'faces': [{}]}})
    monkeypatch.setattr(
        app, 'apply_client_processing_results_for_file',
        lambda *a, **k: {'processing_state': 'active', 'face_status': 'done', 'faceCount': 1},
    )

    app._handle_ipwork_queue_payload(
        {'filename': 'photo.jpg', 'steps': ['face']}, 'job-1', 'lib-A',
    )

    # Incremental face-to-person assignment is queued for the clustering
    # worker (not run in-process) before the (now cooldown-gated) maintenance
    # enqueue decision -- see _enqueue_incremental_assign_job's docstring for
    # why this moved off the request path.
    assert dispatch_ctx.incremental_assign == [('lib-A', 'photo.jpg')]

    assert len(dispatch_ctx.enqueue_clustering) == 1
    user_id, kwargs = dispatch_ctx.enqueue_clustering[0]
    assert user_id == 'lib-A'
    assert kwargs['job_type'] == 'people_cluster'
    assert kwargs['payload']['filename'] == 'photo.jpg'
    assert ('ipwork', 'done') in dispatch_ctx.job_statuses


def test_face_step_skips_enqueue_when_maintenance_cooldown_active(monkeypatch, dispatch_ctx):
    monkeypatch.setattr(app, '_run_ipwork_steps', lambda user_id, filename, steps: {'face': {'faces': [{}]}})
    monkeypatch.setattr(
        app, 'apply_client_processing_results_for_file',
        lambda *a, **k: {'processing_state': 'active', 'face_status': 'done', 'faceCount': 1},
    )
    # A sustained drip of faces (e.g. a large backfill) should not enqueue a
    # fresh maintenance recluster on every single one -- this is the behavior
    # that used to keep ownphotostore-worker alive continuously.
    monkeypatch.setattr(app, '_clustering_maintenance_due', lambda user_id: False)

    app._handle_ipwork_queue_payload(
        {'filename': 'photo.jpg', 'steps': ['face']}, 'job-1', 'lib-A',
    )

    # Incremental assignment (the fast path) is still queued regardless of the
    # maintenance cooldown -- only the (now rare) DBSCAN maintenance pass is
    # gated, not the per-face matching.
    assert dispatch_ctx.incremental_assign == [('lib-A', 'photo.jpg')]
    assert dispatch_ctx.enqueue_clustering == []
    assert ('ipwork', 'done') in dispatch_ctx.job_statuses


def test_non_face_step_does_not_trigger_clustering(monkeypatch, dispatch_ctx):
    """An ipworker job covering only e.g. ocr/exif shouldn't touch clustering --
    _queue_people_clustering_after_face_processing's own face_status=='done'
    guard should no-op, not error."""
    monkeypatch.setattr(app, '_run_ipwork_steps', lambda user_id, filename, steps: {'ocr': {'text': 'hi'}})
    monkeypatch.setattr(
        app, 'apply_client_processing_results_for_file',
        lambda *a, **k: {'processing_state': 'active', 'ocr_status': 'done', 'face_status': 'pending'},
    )

    app._handle_ipwork_queue_payload(
        {'filename': 'photo.jpg', 'steps': ['ocr']}, 'job-2', 'lib-A',
    )

    assert dispatch_ctx.enqueue_clustering == []
    assert ('ipwork', 'done') in dispatch_ctx.job_statuses


def test_clustering_trigger_failure_does_not_fail_the_job(monkeypatch, dispatch_ctx):
    """A bug in the clustering auto-trigger must not turn a successful face
    detection into a reported job failure -- results are already durably
    written by this point."""
    monkeypatch.setattr(app, '_run_ipwork_steps', lambda user_id, filename, steps: {'face': {'faces': [{}]}})
    monkeypatch.setattr(
        app, 'apply_client_processing_results_for_file',
        lambda *a, **k: {'processing_state': 'active', 'face_status': 'done', 'faceCount': 1},
    )

    def _boom(user_id, filename, metadata):
        raise RuntimeError('clustering queue unavailable')

    monkeypatch.setattr(app, '_queue_people_clustering_after_face_processing', _boom)

    app._handle_ipwork_queue_payload(
        {'filename': 'photo.jpg', 'steps': ['face']}, 'job-3', 'lib-A',
    )

    assert ('ipwork', 'done') in dispatch_ctx.job_statuses


def test_success_does_not_redundantly_release_already_cleared_lease(monkeypatch, dispatch_ctx):
    """apply_client_processing_results_for_file already clears the lease
    fields unconditionally once it returns -- calling release_processing_lease
    again on the success path pays a wasted read+write for nothing on every
    single photo. Only a failed attempt (which never reached that clearing)
    should still fall back to an explicit release."""
    monkeypatch.setattr(app, '_run_ipwork_steps', lambda user_id, filename, steps: {'ocr': {'text': 'hi'}})
    monkeypatch.setattr(
        app, 'apply_client_processing_results_for_file',
        lambda *a, **k: {'processing_state': 'active', 'ocr_status': 'done', 'face_status': 'pending'},
    )
    released = []
    monkeypatch.setattr(app, 'release_processing_lease', lambda *a, **k: released.append(a))

    outcome = app._handle_ipwork_queue_payload(
        {'filename': 'photo.jpg', 'steps': ['ocr']}, 'job-6', 'lib-A',
    )

    assert outcome == 'done'
    assert released == []


def test_failure_still_releases_lease(monkeypatch, dispatch_ctx):
    """A crash before apply_client_processing_results_for_file returns means
    the lease was never cleared -- the finally block must still release it so
    it doesn't sit held until it naturally expires."""
    monkeypatch.setattr(app, '_run_ipwork_steps', lambda user_id, filename, steps: {'ocr': {'text': 'hi'}})

    def _boom(*a, **k):
        raise RuntimeError('table storage unavailable')

    monkeypatch.setattr(app, 'apply_client_processing_results_for_file', _boom)
    released = []
    monkeypatch.setattr(app, 'release_processing_lease', lambda *a, **k: released.append(a))

    with pytest.raises(RuntimeError):
        app._handle_ipwork_queue_payload(
            {'filename': 'photo.jpg', 'steps': ['ocr']}, 'job-7', 'lib-A',
        )

    assert len(released) == 1


def test_lease_busy_backs_off_without_running_steps(monkeypatch, dispatch_ctx):
    """When another owner (a browser tab, typically) already holds the
    lease, ipworker must back off without running any inference, and report
    'lease_busy' so run_ipworker's caller retries later instead of silently
    dropping the job -- see IPWORK_LEASE_RETRY_LIMIT."""
    def _claim_denied(*a, **k):
        raise RuntimeError('Processing lease is already held by another client.')

    monkeypatch.setattr(app, 'claim_processing_lease', _claim_denied)
    ran_steps = []
    monkeypatch.setattr(app, '_run_ipwork_steps', lambda *a, **k: ran_steps.append(a) or {})

    outcome = app._handle_ipwork_queue_payload(
        {'filename': 'photo.jpg', 'steps': ['face']}, 'job-4', 'lib-A',
    )

    assert outcome == 'lease_busy'
    assert ran_steps == []
    assert ('ipwork', 'skipped') in dispatch_ctx.job_statuses


def test_already_done_steps_are_not_rerun(monkeypatch, dispatch_ctx):
    """A redelivered retry (or a duplicate ipwork message) whose requested
    step already completed elsewhere -- e.g. the browser finished it before
    its tab closed -- must not redo the inference."""
    monkeypatch.setattr(
        app, 'claim_processing_lease',
        lambda *a, **k: {'ownerId': 'ipworker-job-5', 'statuses': {'faceStatus': 'done'}},
    )
    released = []
    monkeypatch.setattr(app, 'release_processing_lease', lambda *a, **k: released.append(a))
    ran_steps = []
    monkeypatch.setattr(app, '_run_ipwork_steps', lambda *a, **k: ran_steps.append(a) or {})

    outcome = app._handle_ipwork_queue_payload(
        {'filename': 'photo.jpg', 'steps': ['face']}, 'job-5', 'lib-A',
    )

    assert outcome == 'noop'
    assert ran_steps == []
    assert len(released) == 1
    assert ('ipwork', 'skipped') in dispatch_ctx.job_statuses


def test_stale_face_embedding_version_forces_rerun_despite_done_lease_status(monkeypatch, dispatch_ctx):
    """claim_processing_lease reports the raw (stale) faceStatus='done', but
    the stored embedding predates FACE_CLUSTER_EMBEDDING_VERSION -- the same
    class of work _browser_processing_pending_item already re-queues for the
    browser (_browser_processing_face_version_stale). Without re-checking
    staleness here too, every sweep-enqueued stale-embedding job would
    round-trip through this 'already done' skip and never actually get
    reprocessed -- this is exactly what happened to ~15.6k real jobs before
    this fix (all landed status='skipped', reason='already_done')."""
    monkeypatch.setattr(
        app, 'claim_processing_lease',
        lambda *a, **k: {'ownerId': 'ipworker-job-8', 'statuses': {'faceStatus': 'done'}},
    )
    monkeypatch.setattr(app, '_get_metadata_entity', lambda user_id, filename: {'RowKey': filename})
    monkeypatch.setattr(app, '_browser_processing_face_version_stale', lambda entity: True)
    ran_steps = []
    monkeypatch.setattr(
        app, '_run_ipwork_steps',
        lambda user_id, filename, steps: ran_steps.append(steps) or {'face': {'faces': [{}]}},
    )
    monkeypatch.setattr(
        app, 'apply_client_processing_results_for_file',
        lambda *a, **k: {'processing_state': 'active', 'face_status': 'done', 'faceCount': 1},
    )

    outcome = app._handle_ipwork_queue_payload(
        {'filename': 'photo.jpg', 'steps': ['face']}, 'job-8', 'lib-A',
    )

    assert outcome == 'done'
    assert ran_steps == [['face']]
    assert ('ipwork', 'skipped') not in dispatch_ctx.job_statuses


def test_current_face_embedding_version_still_skips_done_step(monkeypatch, dispatch_ctx):
    """The inverse of the above -- a genuinely current embedding must still
    take the cheap already-done skip, not be forced to rerun on every job."""
    monkeypatch.setattr(
        app, 'claim_processing_lease',
        lambda *a, **k: {'ownerId': 'ipworker-job-9', 'statuses': {'faceStatus': 'done'}},
    )
    monkeypatch.setattr(app, '_get_metadata_entity', lambda user_id, filename: {'RowKey': filename})
    monkeypatch.setattr(app, '_browser_processing_face_version_stale', lambda entity: False)
    ran_steps = []
    monkeypatch.setattr(app, '_run_ipwork_steps', lambda *a, **k: ran_steps.append(a) or {})

    outcome = app._handle_ipwork_queue_payload(
        {'filename': 'photo.jpg', 'steps': ['face']}, 'job-9', 'lib-A',
    )

    assert outcome == 'noop'
    assert ran_steps == []
    assert ('ipwork', 'skipped') in dispatch_ctx.job_statuses
