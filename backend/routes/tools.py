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
    # A Workbench/tools run changes tags, faces and metadata: the heavy indexes
    # are rebuilt now (not after plain uploads).
    try:
        app._trigger_tools_index_rebuild(user_id, reason='workbench-run', scope='full')
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


@tools_bp.route('/api/tools/indexes/build', methods=['POST'])
def tools_build_indexes():
    # Kicks the library's index build and returns immediately. The build itself
    # runs on the always-awake `worker` role from the library-ops queue (see
    # app.enqueue_index_build): it used to run inline in this request on the
    # scale-to-zero tools app, where ingress cut the request at ~240s and
    # nothing then held the replica open, so a scale-down or restart mid-build
    # orphaned the job ("Job did not finish (worker restarted or timed out)").
    # Idempotent: a build already queued/running for this library is reported,
    # not duplicated. Clients poll /api/tools/indexes/status.
    user_id, error = app._require_user_id()
    if error:
        return error
    state = app.get_user_index_build_state(user_id)
    outcome = 'not_needed'
    if app.index_build_needed(user_id):
        outcome = app.enqueue_index_build(user_id, reason='client')
    return app.jsonify({
        'ok': True,
        'ready': state['ready'],
        'building': outcome in ('queued', 'already_active'),
        'queued': outcome,
        'indexes': state['indexes'],
    })


@tools_bp.route('/api/tools/indexes/status', methods=['GET'])
def tools_indexes_status():
    # Read-only poll target. Readiness comes from the manifest blobs; `building`
    # from the shared index_build job row (cross-replica, unlike the old
    # per-process prime lock).
    user_id, error = app._require_user_id()
    if error:
        return error
    indexes = app.get_user_index_readiness(user_id)
    ready = all(indexes.values())
    return jsonify_status(ready, indexes, app._index_build_job_active(user_id))


def jsonify_status(ready, indexes, building):
    return app.jsonify({'ready': ready, 'building': bool(building), 'indexes': indexes})


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
        app._maybe_sweep_old_job_rows(user_id)  # background, hourly per user
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
            # Server-side filter: only what the response can include (in-flight, or
            # recently updated). The partition holds every job the library ever ran, and
            # this endpoint is polled continuously -- reading all of it was ~160 storage
            # pages per call.
            rows = list(app.jobs_table_client.query_entities(
                f"PartitionKey eq '{app._escape_odata(user_id)}' and "
                f"(updatedAt ge '{cutoff}' or status eq 'queued' or status eq 'running')"
            ))
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
