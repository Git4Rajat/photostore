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
    # Serve the PRECOMPUTED Explore summary (built on the tools role right after
    # the lexical index, see refresh_user_explore_summary). Backend must NOT
    # call _explore_places_and_things here -- that groups over the full lexical
    # index, loading it into this 1Gi process, which OOM-ed it (2026-09-30).
    # On a cold account with no summary yet, return empty immediately and nudge
    # tools to build it (deduped + cooled down); the next load serves it.
    user_id, error = app._require_user_id()
    if error:
        return error
    summary = app.load_explore_summary(user_id)
    if summary is None:
        app._trigger_tools_index_rebuild(user_id)
        return app.jsonify({'places': [], 'things': []})
    return app.jsonify({'places': summary.get('places', []), 'things': summary.get('things', [])})


@explore_bp.route('/api/search/suggest', methods=['GET'])
@explore_bp.route('/api/search/suggest/', methods=['GET'])
def search_typeahead():
    user_id, error = app._require_user_id()
    if error:
        return error
    partial = (app.request.args.get('q') or '').strip()
    if not partial:
        return app.jsonify({'suggestions': []})
    return app.jsonify({'suggestions': app._search_typeahead_suggestions(user_id, partial)})
