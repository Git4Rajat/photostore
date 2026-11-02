"""Lightweight subscription negotiation belongs to the gallery-facing core API."""
from flask import Blueprint

import app

index_events_bp = Blueprint('index_events', __name__)


@index_events_bp.route('/api/index-events/negotiate', methods=['POST'])
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
