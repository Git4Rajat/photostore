"""Blueprint: system routes, extracted from app.py.

Auto-extracted 2026-09-15 as part of the backend modularity pass -- shared
helpers/caches/table clients stay in app.py (imported as `app` and referenced
as app.<name> throughout, matching the exact late-binding lookup semantics
the code relied on when these functions lived in app.py directly, so
test-time monkeypatching of app.<name> globals still works unchanged).
"""
from flask import Blueprint

import app

system_bp = Blueprint('system', __name__)

@system_bp.route('/health', methods=['GET'])
def health_check():
    return app.jsonify({
        'status': 'healthy',
        'service': 'photo-store-api',
        'storage_account': app.account_name,
        'uses_managed_identity': app.credential is not None,
    })

@system_bp.route('/geocode/reverse', methods=['GET'])
@system_bp.route('/api/geocode/reverse', methods=['GET'])
def geocode_reverse():
    """Server-side reverse geocode, used by the browser AI pipeline's
    map_detection step. Proxying through the backend (instead of the browser
    calling a third-party geocoder directly) avoids depending on that
    service's CORS/availability from an arbitrary browser origin -- the same
    maps_utils.reverse_geocode() call already runs cheaply (1-116ms) from the
    ipworker path."""
    _user_id, error = app._require_user_id()
    if error:
        return error
    latitude = (app.request.args.get('lat') or '').strip()
    longitude = (app.request.args.get('lon') or '').strip()
    if not latitude or not longitude:
        return app.jsonify({'error': 'lat and lon are required'}), 400
    import maps_utils
    try:
        result = maps_utils.reverse_geocode(latitude, longitude)
    except Exception:
        app.worker_logger.exception('geocode_reverse failed')
        result = {}
    return app.jsonify(result or {})

@system_bp.route('/performance/throughput', methods=['GET'])
@system_bp.route('/performance/throughput/', methods=['GET'])
@system_bp.route('/api/performance/throughput', methods=['GET'])
@system_bp.route('/api/performance/throughput/', methods=['GET'])
def performance_throughput():
    return app.jsonify(app._get_throughput_metrics())

# jobs_status moved to routes/tools.py (APP_ROLE=tools) 2026-10-01: polled
# continuously by every session (every ~15-30s, indefinitely), competing with
# interactive gallery/photo traffic for backend's thin GUNICORN_WORKERS=2/
# THREADS=2 pool -- the same scarce resource the 2026-10-01 crash-loop fix
# was about. tools has no such contention (no interactive request traffic),
# so this background polling belongs there instead.
