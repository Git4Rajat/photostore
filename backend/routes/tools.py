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
