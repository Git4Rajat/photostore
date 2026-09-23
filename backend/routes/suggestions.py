"""Blueprint: ambient suggestions (Nudge), see app._compute_suggestions for
the candidate logic -- kept in app.py alongside the smart-album/Explore
grouping it reuses primitives from."""
from flask import Blueprint

import app

suggestions_bp = Blueprint('suggestions', __name__)


@suggestions_bp.route('/api/suggestions', methods=['GET'])
@suggestions_bp.route('/api/suggestions/', methods=['GET'])
def get_suggestions():
    user_id, error = app._require_user_id()
    if error:
        return error
    return app.jsonify({'suggestions': app._compute_suggestions(user_id)})
