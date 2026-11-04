"""User-triggered upload recovery and durable notices, owned by Core."""
import threading
import time
from itertools import islice
from flask import Blueprint, g
from azure.core.exceptions import AzureError

import app
from upload_waves import ShadowWaves, Conflict
from upload_session_registry import Sessions, configured

upload_sessions_bp = Blueprint('upload_sessions', __name__)
_lock = threading.Lock()
_last = {}


def reconcile(library):
    from routes.upload_wave_shadow import _drive
    from wave_policy import owned, require_capability
    if not owned(app.jobs_table_client, library):
        return dict(notifications=[], pending=False)
    store = ShadowWaves(app.jobs_table_client, library)
    require_capability(store, 'core')
    if store._required('policy').get('cleanupState'):
        return dict(notifications=[], pending=False)
    sessions = Sessions(configured(), library)
    # One-time, bounded adoption of sessions created before the queue was
    # retired. Cursor survives replica loss; new admissions already index first.
    cursor = sessions.get('migration')
    migrating = not cursor or not cursor.get('complete')
    if migrating:
        query = (f"PartitionKey eq '{store.partition}' and RowKey ge 'session!' "
                 "and RowKey lt 'session!~' and state eq 'open'")
        if cursor and cursor.get('after'):
            query += f" and RowKey gt '{cursor['after']}'"
        rows = list(islice(store.table.query_entities(query, results_per_page=20), 20))
        for row in rows:
            if row['state'] == 'open':
                sessions.track(row['sessionId'], created_at=row.get('createdAt', 0))
        marker = dict(PartitionKey=sessions.partition, RowKey='migration',
            after=rows[-1]['RowKey'] if rows else '', complete=len(rows) < 20)
        try:
            sessions.table.submit_transaction([sessions.change(cursor, marker) if cursor else ('create', marker)])
        except AzureError as error:
            # Another Core may have advanced the same migration cursor.
            if getattr(error, 'status_code', None) not in (409, 412):
                raise
        migrating = not marker['complete']
    pending = sessions.reconcile(store, lambda wave: _drive(store, wave))
    return dict(notifications=sessions.notices(), pending=pending or migrating)


@upload_sessions_bp.route('/api/upload-sessions/reconcile', methods=['POST'])
def reconcile_upload_sessions():
    _, library, error = app._require_library_context(require_auth=True)
    if error:
        return error
    g.upload_reconciled = True
    try:
        response = app.jsonify(reconcile(library))
        response.headers['Cache-Control'] = 'no-store'
        return response
    except AzureError:
        app.app.logger.exception('Upload session reconciliation unavailable')
        return app.jsonify(error='Upload recovery is temporarily unavailable'), 503
    except Conflict as error:
        return app.jsonify(error=str(error)), 409
    except RuntimeError:
        app.app.logger.exception('Upload session handoff deferred')
        return app.jsonify(error='Upload recovery handoff is temporarily unavailable'), 503


@upload_sessions_bp.route('/api/upload-sessions/notifications/<session>/ack', methods=['POST'])
def acknowledge_upload_notice(session):
    _, library, error = app._require_library_context(require_auth=True)
    if error:
        return error
    g.upload_reconciled = True
    try:
        Sessions(configured(), library).acknowledge(session)
        return app.jsonify(acknowledged=True)
    except ValueError:
        return app.jsonify(error='Invalid upload session'), 400
    except AzureError:
        return app.jsonify(error='Notification acknowledgment unavailable'), 503


@upload_sessions_bp.after_app_request
def reconcile_on_core_access(response):
    # Authentication resolves the library on real user requests. Health probes
    # and unauthenticated requests never trigger a table survey. No timer,
    # process-wide startup scan or background worker is created.
    library = getattr(g, 'upload_session_library', None)
    if (not library or getattr(g, 'upload_reconciled', False)
            or app.upload_sessions_table_client is None
            or app.os.getenv('APP_ROLE', 'core') not in ('', 'core')):
        return response
    key = (id(app.upload_sessions_table_client), library)
    now = time.monotonic()
    with _lock:
        if now - _last.get(key, -60) < 60:
            return response
        if len(_last) >= 1000:
            _last.clear()
        _last[key] = now
    try:
        reconcile(library)
    except Exception:
        app.app.logger.exception('User-triggered upload recovery deferred')
    return response
