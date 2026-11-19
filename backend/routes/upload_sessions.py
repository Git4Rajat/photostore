"""User-triggered upload recovery and durable notices, owned by Core."""
import json
import threading
import time
from collections import OrderedDict
from itertools import islice
from flask import Blueprint, g
from azure.core.exceptions import AzureError

import app
from upload_waves import ShadowWaves, Conflict, identifier
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


# A finished upload's notification says how processing went ("All processed in 4
# minutes. 9 people found."). The browser asks about its own session until the
# wave it joined completes; a complete wave never changes, so its answer is kept.
PEOPLE_MAX_PHOTOS = 600
_summaries = OrderedDict()
_summaries_lock = threading.Lock()


def _people_in_wave(store, library, wave_id):
    """Distinct people in the wave's photos, from their metadata rows; None past PEOPLE_MAX_PHOTOS
    (the count would cost a metadata read per 15 photos, so big uploads just leave it out)."""
    rows = islice(store.table.query_entities(
        f"PartitionKey eq '{store.partition}' and RowKey ge 'e!{wave_id}!m!' and RowKey lt 'e!{wave_id}!m!~'"),
        PEOPLE_MAX_PHOTOS + 1)
    names = [str(row['filename']) for row in rows if row.get('filename')]
    if len(names) > PEOPLE_MAX_PHOTOS:
        return None
    people = set()
    for metadata in app._get_metadata_entities(library, names).values():
        try:
            ids = json.loads((metadata or {}).get('peopleIds') or '[]')
        except (TypeError, ValueError):
            continue
        if isinstance(ids, list):
            people.update(str(person) for person in ids if person)
    return len(people)


def processing_summary(library, session):
    from wave_policy import owned
    if not owned(app.jobs_table_client, library):
        raise LookupError('Uploads in this library are not processed in waves')
    store = ShadowWaves(app.jobs_table_client, library)
    row = store._get(store.session_key(identifier(session)))
    if not row:
        raise LookupError('Unknown upload session')
    key = (library, row['waveId'])
    with _summaries_lock:
        if key in _summaries:
            return _summaries[key]
    wave = store._get(store.wave_key(row['waveId']))
    if not wave:
        raise LookupError('Unknown wave')
    if wave['state'] == 'cancelled':
        return dict(done=True, cancelled=True)
    if wave['state'] != 'complete':
        return dict(done=False, state=wave['state'])
    started = float(row.get('closedAt') or wave.get('frozenAt') or wave['createdAt'])
    summary = dict(done=True, processedSeconds=max(0, round(float(wave['stateEnteredAt']) - started)),
                   failed=int(wave.get('failedMembers') or 0),
                   people=_people_in_wave(store, library, row['waveId']))
    with _summaries_lock:
        _summaries[key] = summary
        while len(_summaries) > 500:
            _summaries.popitem(last=False)
    return summary


@upload_sessions_bp.route('/api/upload-sessions/<session>/processing', methods=['GET'])
def upload_processing(session):
    _, library, error = app._require_library_context(require_auth=True)
    if error:
        return error
    # A poll, not a visit: it must not set off the recovery survey below.
    g.upload_reconciled = True
    try:
        response = app.jsonify(processing_summary(library, session))
        response.headers['Cache-Control'] = 'no-store'
        return response
    except ValueError:
        return app.jsonify(error='Invalid upload session'), 400
    except LookupError as error:
        return app.jsonify(error=str(error)), 404
    except AzureError:
        app.app.logger.warning('Upload processing summary unavailable', exc_info=True)
        return app.jsonify(error='Processing status is temporarily unavailable'), 503


@upload_sessions_bp.route('/api/processing', methods=['GET'])
def library_processing():
    """Every wave of the library that is still processing, or finished and not dismissed.

    Library-scoped rather than session-scoped, so any browser sees the same bars and a
    long stage outlives the old 3 hour session watcher."""
    user, library, error = app._require_library_context(require_auth=True)
    if error:
        return error
    g.upload_reconciled = True
    try:
        from processing_progress import ProgressStore
        from wave_policy import owned
        if not owned(app.jobs_table_client, library):
            return _no_store(app.jsonify(waves=[]))
        waves = ProgressStore(ShadowWaves(app.jobs_table_client, library)).active(
            user, can_repair=bool(app.library_store.is_owner(user, library)))
        return _no_store(app.jsonify(waves=waves))
    except AzureError:
        app.app.logger.warning('Processing progress unavailable', exc_info=True)
        return app.jsonify(error='Processing status is temporarily unavailable'), 503


@upload_sessions_bp.route('/api/processing/<wave>/dismiss', methods=['POST'])
def dismiss_processing(wave):
    user, library, error = app._require_library_context(require_auth=True)
    if error:
        return error
    g.upload_reconciled = True
    try:
        from processing_progress import ProgressStore
        ProgressStore(ShadowWaves(app.jobs_table_client, library)).dismiss(identifier(wave), user)
        return _no_store(app.jsonify(ok=True))
    except ValueError:
        return app.jsonify(error='Invalid wave'), 400
    except AzureError:
        app.app.logger.warning('Processing dismissal failed', exc_info=True)
        return app.jsonify(error='Processing status is temporarily unavailable'), 503


def _no_store(response):
    response.headers['Cache-Control'] = 'no-store'
    return response


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
