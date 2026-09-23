"""Blueprint: Explore (Places + Things browsing), see app._explore_places_and_things
for the grouping logic -- kept in app.py alongside the smart-album grouping
rules it mirrors."""
from flask import Blueprint

import app

explore_bp = Blueprint('explore', __name__)


@explore_bp.route('/explore', methods=['GET'])
@explore_bp.route('/explore/', methods=['GET'])
@explore_bp.route('/api/explore', methods=['GET'])
@explore_bp.route('/api/explore/', methods=['GET'])
def explore_summary():
    user_id, error = app._require_user_id()
    if error:
        return error
    return app.jsonify(app._explore_places_and_things(user_id))
