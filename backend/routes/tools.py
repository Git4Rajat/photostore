"""Blueprint: tools routes, extracted from app.py.

Auto-extracted 2026-09-15 as part of the backend modularity pass -- shared
helpers/caches/table clients stay in app.py (imported as `app` and referenced
as app.<name> throughout, matching the exact late-binding lookup semantics
the code relied on when these functions lived in app.py directly, so
test-time monkeypatching of app.<name> globals still works unchanged).
"""
from flask import Blueprint

import app

tools_bp = Blueprint('tools', __name__)

@tools_bp.route('/api/tools/workbench/actions', methods=['POST'])
def record_workbench_action():
    # Best-effort history row for a user-triggered Workbench run. Purely
    # in-browser runs (the default processingMode) never otherwise touch the
    # backend, so the frontend calls this explicitly after each run -- mirrors
    # _write_merge_record's try/except/pass semantics, never blocking the
    # caller on a logging failure.
    user_id, error = app._require_user_id()
    if error:
        return error
    data = app.request.get_json(silent=True) or {}
    action = str(data.get('action') or '').strip()
    steps = data.get('steps')
    scope = str(data.get('scope') or '').strip()
    if not action or not isinstance(steps, list) or not steps or scope not in ('selected', 'view', 'library'):
        return app.jsonify({'error': 'action, steps, and a valid scope are required', 'code': 'invalid_action'}), 400
    try:
        filename_count = int(data.get('filenameCount') or 0)
    except Exception:
        filename_count = 0
    raw_filenames = data.get('filenames')
    filenames = [str(f) for f in raw_filenames][:50] if isinstance(raw_filenames, list) else []
    action_id = str(app.uuid.uuid4())
    try:
        app.workbench_actions_table_client.upsert_entity({
            'PartitionKey': user_id,
            'RowKey': action_id,
            'action': action,
            'steps': app.json.dumps(steps),
            'scope': scope,
            'filenameCount': filename_count,
            'filenames': app.json.dumps(filenames),
            'force': bool(data.get('force')),
            'createdAt': app.datetime.now(app.timezone.utc).isoformat(),
        })
    except Exception:
        pass
    return app.jsonify({'success': True, 'actionId': action_id})

@tools_bp.route('/api/tools/workbench/actions', methods=['GET'])
def list_workbench_actions():
    user_id, error = app._require_user_id()
    if error:
        return error
    # Project only the small columns the history list needs -- same reasoning
    # as list_merges() excluding its heavy `payload` column: never pull the
    # (capped but still non-trivial) `filenames` blob just to list rows.
    select_cols = ['RowKey', 'action', 'steps', 'scope', 'filenameCount', 'force', 'createdAt']
    try:
        rows_iter = app.workbench_actions_table_client.query_entities(
            f"PartitionKey eq '{app._escape_odata(user_id)}'",
            select=select_cols,
        )
    except Exception:
        return app.jsonify({'actions': []})
    rows = []
    try:
        for row in rows_iter:
            try:
                rows.append({
                    'actionId': row['RowKey'],
                    'action': row.get('action'),
                    'steps': app.json.loads(row.get('steps', '[]') or '[]'),
                    'scope': row.get('scope'),
                    'filenameCount': row.get('filenameCount'),
                    'force': row.get('force'),
                    'createdAt': row.get('createdAt'),
                })
            except Exception:
                continue
    except Exception:
        # A mid-stream paging error still returns whatever was collected.
        pass
    rows.sort(key=lambda r: r.get('createdAt') or '', reverse=True)
    return app.jsonify({'actions': rows[:100]})

@tools_bp.route('/api/tools/workbench/actions/<action_id>', methods=['GET'])
def get_workbench_action(action_id: str):
    # Single-row detail fetch -- the only place `filenames` is ever returned,
    # so a "select these photos again" affordance can work without the list
    # endpoint above paying for that column on every row.
    user_id, error = app._require_user_id()
    if error:
        return error
    try:
        row = app.workbench_actions_table_client.get_entity(partition_key=user_id, row_key=action_id)
    except Exception:
        return app.jsonify({'error': 'Not found'}), 404
    return app.jsonify({'filenames': app.json.loads(row.get('filenames', '[]') or '[]')})


def _index_build_progress_callback(user_id: str):
    # Mirrors prime_all_user_indexes_sequentially's live progress into a single
    # per-user index_build jobs-table row (updated in place, deterministic
    # RowKey) so the build is visible cross-replica and to the notification
    # bell -- storage_utils can't write this itself without importing app
    # (circular), so it hands each step out through this callback instead.
    def _cb(indexes: dict, building: bool) -> None:
        ready = all(indexes.values())
        status = 'running' if building else ('done' if ready else 'failed')
        app._upsert_job_status(
            app._index_build_job_id(user_id),
            user_id,
            app.INDEX_BUILD_JOB_TYPE,
            status,
            result={'indexes': indexes, 'ready': ready},
        )
        # When the build finishes (building=False) and the lexical index is
        # built, recompute the Explore summary here on tools (4Gi) from the
        # freshly-built, still-cached lexical snapshot and store it as a small
        # blob. The backend's /explore route then serves that blob without ever
        # loading the lexical index into its 1Gi memory. Best-effort: a failure
        # here never fails the index build itself.
        if not building and indexes.get('lexical'):
            try:
                app.refresh_user_explore_summary(user_id)
            except Exception:
                app.app.logger.exception('Explore summary refresh failed for %s', user_id)
            try:
                app.refresh_user_timeline_summary(user_id)
            except Exception:
                app.app.logger.exception('Timeline summary refresh failed for %s', user_id)
    return _cb


@tools_bp.route('/api/tools/indexes/build', methods=['POST'])
def tools_build_indexes():
    # THE index builder. Moved off the `backend` role (1Gi, serves the gallery
    # hot path) onto `tools` (2vCPU/4Gi) precisely because building the lexical
    # index scans a user's full metadata partition (OCR/tags/faces per row) and
    # was OOM-ing backend. Kicks the existing single-flighted sequential
    # builder; a build already in flight (or one triggered by a second tab / an
    # ipworker milestone) no-ops against the prime lock.
    #
    # Blocking, not fire-and-forget: tools runs minReplicas=0, and Container
    # Apps' autoscaler counts in-flight HTTP requests to decide when to scale
    # back to 0. A build that returned immediately and kept running on a
    # background thread left nothing holding the replica open, so a routine
    # scale-down could (and did, live on microsvcpoc-dev 2026-10-01) kill the
    # build mid-run -- the orphaned job row then surfaced to the user as
    # "Library index build failed: Job did not finish (worker restarted or
    # timed out)". wait=True (see prime_all_user_indexes_sequentially) makes
    # this request span the whole build so the replica stays alive for it.
    # gunicorn's gthread workers (--threads 4) mean this doesn't stall other
    # requests on the same replica, and --timeout 600 / the frontend's 600s
    # client timeout both already cover a full cold-account build.
    user_id, error = app._require_user_id()
    if error:
        return error
    # Kick a build when any index is MISSING or DIRTY (needs_rebuild) -- the
    # dirty case is what preserves the freshness the backend's per-GET-route
    # background rebuild used to provide before that was moved off the 1Gi
    # container. prime runs a cheap incremental merge for a dirty index and a
    # full build only for a missing one, all here on the 4Gi tools role.
    # `ready` (all built) is what the frontend gate waits on -- a built-but-
    # dirty index is still usable, so it doesn't hold the gate.
    try:
        app.storage_utils_ensure_sort_current(user_id)
    except Exception:
        app.app.logger.exception('Sort index schema upgrade failed for %s', user_id)
    state = app.get_user_index_build_state(user_id)
    if state['indexes'].get('lexical') and not state['needs_rebuild']:
        # Lexical is built and clean but the slim browser index may predate
        # this deploy's schema -- derive it from the existing blob.
        try:
            app.storage_utils_ensure_slim(user_id)
        except Exception:
            app.app.logger.exception('Slim search index ensure failed for %s', user_id)
    if state['needs_rebuild']:
        try:
            app.prime_all_user_indexes_sequentially(
                user_id, on_progress=_index_build_progress_callback(user_id), wait=True,
            )
        except Exception:
            app.app.logger.exception('Index build failed for %s', user_id)
        state = app.get_user_index_build_state(user_id)
    return app.jsonify({'ok': True, 'ready': state['ready'], 'building': False, 'indexes': state['indexes']})


@tools_bp.route('/api/tools/indexes/status', methods=['GET'])
def tools_indexes_status():
    # Read-only poll target for the frontend's "Building your library index"
    # gate. Readiness is sourced from the manifest blobs (the source of truth
    # for "is this index built"); `building` additionally reflects an in-process
    # prime on this replica, so a just-kicked build reports building=true even
    # in the instant before the first index lands.
    user_id, error = app._require_user_id()
    if error:
        return error
    indexes = app.get_user_index_readiness(user_id)
    ready = all(indexes.values())
    building = (not ready) and app.index_prime_in_progress(user_id)
    return app.jsonify({'ready': ready, 'building': building, 'indexes': indexes})


# Moved from routes/system.py (APP_ROLE=backend) 2026-10-01: polled
# continuously by every session (every ~15-30s, indefinitely) to surface
# background-job completions. On backend this competed with interactive
# gallery/photo traffic for the thin GUNICORN_WORKERS=2/THREADS=2 pool --
# the same scarce resource the 2026-10-01 crash-loop fix was about. tools
# has no interactive request traffic to contend with, so this belongs here.
@tools_bp.route('/api/jobs/status', methods=['GET'])
@tools_bp.route('/jobs/status', methods=['GET'])
def jobs_status():
    """Return the current user's recent background jobs so the in-app notifier
    can surface completions (reclustering, find-more-faces, library cleanup,
    preview generation, ...). Includes anything still queued/running plus jobs
    that finished within the recent window; the client dedupes what it has
    already shown so a completed job is only ever announced once.
    """
    try:
        user_id, error = app._require_user_id()
        if error:
            return error
        if app.jobs_table_client is None:
            return app.jsonify({'jobs': []})
        cutoff = (app.datetime.now(app.timezone.utc) - app.timedelta(minutes=app.JOB_STATUS_WINDOW_MINUTES)).isoformat()
        # A job of ANY type (clustering, ipwork, library_clean, preview, ...)
        # this old and still queued/running is dead, not in-flight — the
        # worker/ipworker crashed mid-job (e.g. OOM) and never wrote a
        # terminal status. This used to only cover job_type == 'clustering'
        # (mirroring _has_active_clustering_job's de-dupe cutoff), but any job
        # type can be orphaned the same way — an old stuck 'ipwork' row was
        # found stuck 15 days "running", keeping the server-processing
        # indicator on forever with nothing left to actually process. Without
        # this cutoff a dead row shows as perpetually "in flight" and its
        # activity indicator never clears.
        stale_cutoff = (app.datetime.now(app.timezone.utc) - app.timedelta(minutes=app.CLUSTERING_ACTIVE_JOB_STALE_MINUTES)).isoformat()
        try:
            # Jobs are userId-partitioned (library_clean/library_download are
            # mirrored into the initiator's userId partition too -- see
            # _upsert_job_status), so this is a normal scoped partition query,
            # not the fleet-wide 219k+-row scan this used to share with
            # _has_active_clustering_job.
            rows = list(app.jobs_table_client.query_entities(f"PartitionKey eq '{app._escape_odata(user_id)}'"))
        except Exception:
            app.app.logger.exception('Failed to query job status rows for %s', user_id)
            return app.jsonify({'jobs': []})
        jobs = []
        for row in rows:
            status = str(row.get('status') or '').lower()
            updated_at = str(row.get('updatedAt') or '')
            job_type = str(row.get('jobType') or '')
            if status in {'queued', 'running'} and updated_at and updated_at < stale_cutoff:
                # Passing libraryId through (when present) re-derives the same
                # authoritative partition_key as the original write, so this
                # updates the real (library_clean/library_download) row, not
                # just this userId-partition mirror.
                app._upsert_job_status(
                    str(row.get('jobId') or ''), user_id, job_type, 'failed',
                    error='Job did not finish (worker restarted or timed out)',
                    libraryId=row.get('libraryId'),
                )
                continue
            # Keep in-flight jobs, plus terminal ones that finished recently.
            # updatedAt is a UTC isoformat string, so lexicographic comparison
            # against the cutoff is a valid recency test.
            if status in {'queued', 'running'} or updated_at >= cutoff:
                jobs.append(app._humanize_job(row))
        jobs.sort(key=lambda job: job.get('updatedAt') or '', reverse=True)
        return app.jsonify({'jobs': jobs[:50]})
    except Exception as exc:
        app.app.logger.exception('Job status route failed')
        return app.jsonify({'jobs': [], 'error': 'Internal server error'}), 500
