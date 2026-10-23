"""Blueprint: tools routes, extracted from app.py.

Auto-extracted 2026-09-15 as part of the backend modularity pass -- shared
helpers/caches/table clients stay in app.py (imported as `app` and referenced
as app.<name> throughout, matching the exact late-binding lookup semantics
the code relied on when these functions lived in app.py directly, so
test-time monkeypatching of app.<name> globals still works unchanged).
"""
import os

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
        app._trigger_indexer_rebuild(user_id, reason='workbench-run', scope='light')
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
    data = app.request.get_json(silent=True) or {}
    requested_scope = str(data.get('scope') or '').strip().lower()
    import index_updates
    if requested_scope not in ('', 'auto') and requested_scope not in index_updates.SCOPE_KINDS:
        return app.jsonify({'error': 'invalid index scope'}), 400
    wait_seconds = 0.0
    try:
        wait_seconds = max(0.0, min(90.0, float(data.get('waitSeconds') or 0)))
    except Exception:
        wait_seconds = 0.0
    if data.get('wait') is True and wait_seconds <= 0:
        wait_seconds = 60.0

    outcome = 'not_needed'
    scope = 'none'
    if requested_scope not in ('', 'auto'):
        scope = requested_scope
        outcome = app.enqueue_index_build(
            user_id, reason=str(data.get('reason') or 'client'), scope=scope,
            coalesce=not bool(data.get('force') or data.get('forceImmediate')),
        )
    elif app.index_build_needed(user_id):
        scope = 'full'
        outcome = app.enqueue_index_build(user_id, reason=str(data.get('reason') or 'client'))
    elif app.INDEX_MAINTENANCE_QUEUE_NAME:
        details = app.storage_utils_module.get_user_index_status_detailed(user_id)
        wanted = [index_updates.scope_for_index(kind) for kind, info in details['indexes'].items()
                  if not info.get('ready') or info.get('dirty')]
        outcomes = [app.enqueue_index_build(user_id, reason='client', scope=kind) for kind in wanted]
        if wanted:
            scope = wanted[0] if len(wanted) == 1 else 'multiple'
            outcome = ('queued' if 'queued' in outcomes else
                       ('already_active' if 'already_active' in outcomes else 'unavailable'))
    elif app.light_index_build_needed(user_id):
        scope = 'light'
        outcome = app.enqueue_index_build(
            user_id,
            reason=str(data.get('reason') or 'client-light'),
            scope='light',
            coalesce=not bool(data.get('force') or data.get('forceImmediate')),
        )

    wait_result = None
    if wait_seconds > 0 and outcome in ('queued', 'already_active') and scope not in ('none', 'multiple'):
        wait_result = _wait_for_index_build(user_id, scope, wait_seconds)
    state = app.get_user_index_build_state(user_id)
    building = outcome in ('queued', 'already_active')
    if wait_result and wait_result.get('status') in ('done', 'failed', 'skipped'):
        building = False
    response = app.jsonify({
        'ok': outcome != 'unavailable',
        'ready': state['ready'],
        'building': building,
        'queued': outcome,
        'scope': scope,
        'wait': wait_result,
        'indexes': state['indexes'],
    })
    return (response, 503) if outcome == 'unavailable' else response


def _wait_for_index_build(user_id: str, scope: str, timeout_seconds: float):
    """One-shot long wait used by upload completion. The browser makes one
    request, then refreshes its cached index after this returns."""
    if app.jobs_table_client is None:
        return {'status': 'unavailable'}
    scope = scope if scope in ('full', 'light', 'people') else 'full'
    job_id = app._index_build_job_id(user_id) + (f'-{scope}' if scope != 'full' else '')
    deadline = app.time.monotonic() + max(0.0, timeout_seconds)
    last_status = ''
    while True:
        row = app._get_job_row(user_id, job_id) or {}
        last_status = str(row.get('status') or '').lower()
        if last_status in ('done', 'failed', 'skipped'):
            return {'jobId': job_id, 'status': last_status, 'completed': last_status == 'done'}
        remaining = deadline - app.time.monotonic()
        if remaining <= 0:
            return {'jobId': job_id, 'status': last_status or 'unknown', 'completed': False, 'timedOut': True}
        app.time.sleep(min(1.0, remaining))


@tools_bp.route('/api/tools/ipwork/release', methods=['POST'])
def tools_release_ipwork():
    # Manual counterpart to the automatic release at upload end: hands every photo
    # in this library that still has pending vision steps to the vision worker.
    user_id, error = app._require_user_id()
    if error:
        return error
    status = app.enqueue_ipwork_release(user_id, 'manual', force=True)
    return app.jsonify({'status': status}), 503 if status == 'unavailable' else 202


@tools_bp.route('/api/tools/cluster/flush', methods=['POST'])
def tools_flush_cluster_batch():
    # Manual counterpart to the automatic idle/count-cap flush: asks every
    # ipworker replica to drain this library's pending incremental-assign
    # buffer on its next poll tick, rather than waiting for the 1500-filename
    # cap or the vision-pipeline idle timer.
    user_id, error = app._require_user_id()
    if error:
        return error
    status = app.request_cluster_batch_flush(user_id)
    return app.jsonify({'status': status}), 503 if status == 'unavailable' else 202


@tools_bp.route('/api/tools/indexes/status', methods=['GET'])
def tools_indexes_status():
    # Read-only poll target. Returns detailed per-index status and current build status.
    # Readiness comes from manifest blobs; building status from shared index_build job row.
    user_id, error = app._require_user_id()
    if error:
        return error

    # Enhanced status with per-index details
    status = app.storage_utils_module.get_user_index_status_detailed(user_id)

    # Add current build status if one is in progress
    building_job = None
    if app.INDEX_MAINTENANCE_QUEUE_NAME:
        import index_updates
        base = app._index_build_job_id(user_id)
        for scope in index_updates.SCOPE_KINDS:
            job_id = base + (f'-{scope}' if scope != 'full' else '')
            row = app._get_job_row(user_id, job_id)
            if not row or row.get('status') not in ('queued', 'running'):
                continue
            cutoff = (app.datetime.now(app.timezone.utc) - app.timedelta(
                minutes=app.INDEX_BUILD_STALE_MINUTES_BY_SCOPE.get(scope, 45))).isoformat()
            if row['status'] == 'running' and str(row.get('updatedAt') or '') < cutoff:
                continue
            building_job = {'scope': scope, 'started_at': row.get('Timestamp')}
            break
    elif app._index_build_job_active(user_id):
        job_row = app._get_job_row(user_id, app._index_build_job_id(user_id))
        if job_row:
            scope = str(job_row.get('indexScope') or 'full').lower()
            started_at = job_row.get('Timestamp')  # Table Storage auto-timestamp
            building_job = {
                'scope': scope,
                'started_at': started_at,
            }

    result = {
        'ready': status['ready'],
        'needs_rebuild': status['needs_rebuild'],
        'indexes': status['indexes'],
        'building': building_job is not None,
    }
    if building_job:
        result['building_job'] = building_job

    return app.jsonify(result)


def jsonify_status(ready, indexes, building):
    return app.jsonify({'ready': ready, 'building': bool(building), 'indexes': indexes})


@tools_bp.route('/api/tools/indexes/rebuild/<index_kind>', methods=['POST'])
def tools_rebuild_index(index_kind: str):
    """Manually trigger a rebuild for a specific index type.
    The selected index never expands to unrelated kinds."""
    user_id, error = app._require_user_id()
    if error:
        return error

    kind = str(index_kind or '').strip().lower()
    if kind not in ('sort', 'lexical', 'albums', 'people', 'all'):
        return app.jsonify({'error': 'invalid index_kind'}), 400

    # Map index kinds to build scopes
    scope_map = {
        'sort': 'sort',
        'lexical': 'search',
        'albums': 'albums',
        'people': 'people',
        'all': 'full',
    }
    scope = scope_map.get(kind, 'full')

    # Enqueue the build
    outcome = app.enqueue_index_build(
        user_id,
        reason=f'user-rebuild-{kind}',
        scope=scope,
        coalesce=False,
    )

    # Return current status
    status = app.storage_utils_module.get_user_index_status_detailed(user_id)
    return app.jsonify({
        'ok': outcome != 'unavailable',
        'queued': outcome in ('queued', 'already_active'),
        'status': outcome,
        'indexes': status['indexes'],
    }), 202 if outcome in ('queued', 'already_active') else 503


@tools_bp.route('/api/tools/indexes/recover-pending', methods=['POST'])
def recover_pending_indexes():
    user_id, error = app._require_user_id()
    if error:
        return error
    if not app.INDEX_MAINTENANCE_QUEUE_NAME:
        return app.jsonify({'error': 'Dedicated index maintenance is not enabled'}), 409
    data = app.request.get_json(silent=True) or {}
    try:
        count = app.recover_pending_index_work(user_id, retry_failed=data.get('retryFailed') is True)
        return app.jsonify({'queued': count}), 202
    except Exception:
        app.app.logger.exception('Pending index recovery failed')
        return app.jsonify({'error': 'Pending index recovery unavailable'}), 503


@tools_bp.route('/api/tools/index-events/negotiate', methods=['POST'])
def negotiate_index_events():
    library, error = app._require_user_id()
    if error:
        return error
    import index_events
    try:
        response = app.jsonify(index_events.negotiate(library))
        response.headers['Cache-Control'] = 'no-store'
        return response
    except Exception:
        app.app.logger.warning('Index push negotiation unavailable', exc_info=True)
        return app.jsonify({'available': False}), 503


@tools_bp.route('/api/tools/indexes/maintenance/<scope>', methods=['POST'])
def schedule_index_maintenance(scope):
    user_id, error = app._require_user_id()
    if error:
        return error
    if scope not in ('compact', 'reconcile'):
        return app.jsonify({'error': 'invalid maintenance scope'}), 400
    outcome = app.enqueue_index_build(user_id, reason='user-maintenance', scope=scope, coalesce=False)
    return app.jsonify({'status': outcome}), 503 if outcome == 'unavailable' else 202


# Moved from routes/system.py (APP_ROLE=core) 2026-10-01: polled
# continuously by every session (every ~15-30s, indefinitely) to surface
# background-job completions. On core this competed with interactive
# gallery/photo traffic for the thin GUNICORN_WORKERS=2/THREADS=2 pool --
# the same scarce resource the 2026-10-01 crash-loop fix was about. indexer
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
            if row.get('forwardedTo'):
                # A migrated legacy request is not a completed build. Its
                # canonical index job reports the actual running/result state.
                continue
            status = str(row.get('status') or '').lower()
            updated_at = str(row.get('updatedAt') or '')
            job_type = str(row.get('jobType') or '')
            row_stale_cutoff = stale_cutoff
            eligible_for_expiry = status in {'queued', 'running'}
            if job_type == app.INDEX_BUILD_JOB_TYPE:
                # A queued job has not crashed: it may be waiting for a stopped
                # worker or a bulk batch. Only expired running heartbeats fail.
                eligible_for_expiry = status == 'running'
                minutes = app.INDEX_BUILD_STALE_MINUTES_BY_SCOPE.get(str(row.get('indexScope') or 'full'), 45)
                row_stale_cutoff = (app.datetime.now(app.timezone.utc) - app.timedelta(minutes=minutes)).isoformat()
            if eligible_for_expiry and updated_at and updated_at < row_stale_cutoff:
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
        # Per-photo processing rows must not crowd an index completion out of
        # the bounded response: that completion also drives browser refreshes.
        jobs.sort(key=lambda job: (job.get('kind') == 'index_build', job.get('updatedAt') or ''), reverse=True)
        return app.jsonify({'jobs': jobs[:50]})
    except Exception as exc:
        app.app.logger.exception('Job status route failed')
        return app.jsonify({'jobs': [], 'error': 'Internal server error'}), 500


@tools_bp.route('/api/tools/pending-edits/reconcile', methods=['POST'])
def reconcile_pending_edits():
    """Read exact authoritative rows; never infer mutation completion from an index timestamp."""
    user_id, error = app._require_user_id()
    if error:
        return error
    data = app.request.get_json(silent=True) or {}
    edits = data.get('edits') if isinstance(data, dict) else None
    allowed = {'photo': {'rating', 'likes', 'liked', 'rotation'}, 'person': {'name'}, 'album': {'name'}}
    if not isinstance(edits, list) or len(edits) > 50:
        return app.jsonify({'error': 'At most 50 edits are required'}), 400
    for edit in edits:
        if (not isinstance(edit, dict) or not isinstance(edit.get('kind'), str) or edit['kind'] not in allowed
                or not isinstance(edit.get('id'), str) or not edit['id'] or len(edit['id']) > 1024
                or not isinstance(edit.get('fields'), dict) or not edit['fields']
                or not set(edit['fields']).issubset(allowed[edit['kind']])
                or any(not isinstance(token, str) or len(token) > 256 for token in edit['fields'].values())):
            return app.jsonify({'error': 'Invalid edit'}), 400

    def read(edit):
        kind, entity_id = edit['kind'], edit['id']
        table = {'photo': app.metadata_table_client, 'person': app.person_table_client,
                 'album': app.albums_table_client}[kind]
        if table is None:
            return None
        try:
            entity = table.get_entity(partition_key=user_id, row_key=entity_id)
            deleted = (str(entity.get('processing_state') or '').lower() == 'deleted' if kind == 'photo'
                       else app._coerce_bool(entity.get('deleted')) or bool(entity.get('deletedAt')))
            if kind == 'photo':
                liked_by = entity.get('likedBy') or '[]'
                if isinstance(liked_by, str):
                    liked_by = app.json.loads(liked_by)
                values = {'rating': entity.get('rating') or 0, 'likes': entity.get('likes') or 0,
                          'liked': user_id in liked_by, 'rotation': app._normalize_rotation(entity.get('rotation', 0))}
            else:
                values = {'name': str(entity.get('name') or '')}
            return {'kind': kind, 'id': entity_id, 'deleted': deleted,
                    'values': {name: values[name] for name in edit['fields']}}
        except app.ResourceNotFoundError:
            return {'kind': kind, 'id': entity_id, 'deleted': True, 'values': {}}
        except Exception:
            # Partial unavailability is not proof of deletion or confirmation.
            app.app.logger.warning('Pending edit source check failed', exc_info=True)
            return None

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as executor:
        rows = [row for row in executor.map(read, edits) if row is not None]
    response = app.jsonify({'libraryId': user_id, 'rows': rows})
    response.headers['Cache-Control'] = 'no-store'
    return response


@tools_bp.route('/api/tools/waves/shadow/<wave>/compare', methods=['GET'])
def compare_wave_shadow_to_legacy(wave: str):
    """Operator diagnostic for the shadow-validation period (step 3 of
    docs/indexing-clustering-target-state.md's delivery sequence): how much
    longer the library-wide shadow barrier made each photo in this wave wait
    versus its own completion time. Read-only; never dispatches."""
    if os.getenv('UPLOAD_WAVE_SHADOW_ENABLED', '').strip().lower() != 'true':
        return app.jsonify({'error': 'Shadow upload protocol is disabled'}), 404
    _, library, error = app._require_library_context(require_auth=True)
    if error:
        return error
    if app.jobs_table_client is None:
        return app.jsonify({'error': 'Shadow protocol storage unavailable'}), 503
    from upload_waves import Conflict, ShadowWaves
    from upload_wave_shadow import compare_against_legacy
    try:
        result = compare_against_legacy(ShadowWaves(app.jobs_table_client, library), wave)
    except Conflict:
        return app.jsonify({'error': 'Unknown wave'}), 404
    except ValueError:
        return app.jsonify({'error': 'Invalid wave id'}), 400
    response = app.jsonify(result)
    response.headers['Cache-Control'] = 'no-store'
    return response
