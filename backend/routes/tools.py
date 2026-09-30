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
    return _cb


@tools_bp.route('/api/tools/indexes/build', methods=['POST'])
def tools_build_indexes():
    # THE index builder. Moved off the `backend` role (1Gi, serves the gallery
    # hot path) onto `tools` (2vCPU/4Gi) precisely because building the lexical
    # index scans a user's full metadata partition (OCR/tags/faces per row) and
    # was OOM-ing backend. Kicks the existing single-flighted sequential
    # builder; a build already in flight (or one triggered by a second tab / an
    # ipworker milestone) no-ops against the prime lock. Fire-and-forget: the
    # build runs off-thread, this returns the current readiness immediately so
    # the frontend can start polling /api/tools/indexes/status.
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
    state = app.get_user_index_build_state(user_id)
    building = False
    if state['needs_rebuild']:
        try:
            app.prime_all_user_indexes_sequentially(
                user_id, on_progress=_index_build_progress_callback(user_id),
            )
            building = True
        except Exception:
            app.app.logger.exception('Index build failed to start for %s', user_id)
    return app.jsonify({'ok': True, 'ready': state['ready'], 'building': building, 'indexes': state['indexes']})


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
