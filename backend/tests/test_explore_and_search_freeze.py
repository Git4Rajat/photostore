"""2026-09-30: backend must never load the full lexical index into its 1Gi
memory. Two routes did:

- GET /explore grouped Places/Things over every lexical row. It now serves a
  PRECOMPUTED summary (built on the 4Gi tools role, stored as a small blob);
  the backend route only reads that blob.
- GET /photos/search loaded the lexical index + ran an inline CLIP encode. It
  is now FROZEN by default (SERVER_SEARCH_ENABLED); the browser does the same
  search client-side against the index it already downloads.
"""
from __future__ import annotations

import app
from routes.explore import explore_summary
from routes.photos import search_photos


# --- /photos/search freeze ----------------------------------------------------

def test_search_frozen_by_default_returns_empty_without_touching_lexical(monkeypatch):
    monkeypatch.setattr(app, 'SERVER_SEARCH_ENABLED', False)
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('owner', None))

    def _boom(*a, **k):
        raise AssertionError('frozen server search must not load the lexical index')

    monkeypatch.setattr(app, 'get_user_lexical_index', _boom)
    monkeypatch.setattr(app, '_cached_metadata_rows_for_user', _boom)

    with app.app.test_request_context('/api/photos/search?q=beach'):
        response = search_photos()

    body = response.get_json() if hasattr(response, 'get_json') else response[0].get_json()
    assert body == {'photos': [], 'total': 0, 'serverSearchDisabled': True}


def test_search_freeze_short_circuits_before_reading_the_query(monkeypatch):
    # The freeze check must come first -- even a well-formed query returns the
    # frozen result, never reaching the scoring path.
    monkeypatch.setattr(app, 'SERVER_SEARCH_ENABLED', False)
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('owner', None))

    with app.app.test_request_context('/api/photos/search?q=red%20car&offset=0&limit=24'):
        response = search_photos()

    assert response.get_json()['serverSearchDisabled'] is True


# --- /explore serves the precomputed summary ---------------------------------

def test_explore_serves_precomputed_summary_without_loading_lexical(monkeypatch):
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('owner', None))
    monkeypatch.setattr(app, 'load_explore_summary', lambda uid: {
        'sourceVersion': 'v9', 'updatedAt': 'now',
        'places': [{'label': 'Paris', 'count': 3, 'photo': {'filename': 'a.jpg'}}],
        'things': [{'label': 'Dog', 'count': 5, 'photo': {'filename': 'b.jpg'}}],
    })

    def _boom(*a, **k):
        raise AssertionError('/explore must not group over the lexical index on the backend')

    monkeypatch.setattr(app, '_explore_places_and_things', _boom)
    triggered = []
    monkeypatch.setattr(app, '_trigger_tools_index_rebuild', lambda uid: triggered.append(uid))

    with app.app.test_request_context('/api/explore'):
        response = explore_summary()

    body = response.get_json()
    assert [p['label'] for p in body['places']] == ['Paris']
    assert [t['label'] for t in body['things']] == ['Dog']
    assert triggered == []  # summary present -> no rebuild nudge


def test_explore_cold_returns_empty_and_nudges_tools(monkeypatch):
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('owner', None))
    monkeypatch.setattr(app, 'load_explore_summary', lambda uid: None)  # never built
    triggered = []
    monkeypatch.setattr(app, '_trigger_tools_index_rebuild', lambda uid: triggered.append(uid))

    with app.app.test_request_context('/api/explore'):
        response = explore_summary()

    assert response.get_json() == {'places': [], 'things': []}
    assert triggered == ['owner']  # cold -> ask tools to build it


# --- refresh_user_explore_summary (runs on tools) ----------------------------

def test_refresh_user_explore_summary_computes_and_stores(monkeypatch):
    monkeypatch.setattr(app, '_explore_places_and_things', lambda uid: {
        'places': [{'label': 'Rome', 'count': 2}], 'things': [{'label': 'Cat', 'count': 4}],
    })
    monkeypatch.setattr(app, 'get_index_manifest_summary', lambda uid, kind: {'source_version': 'lex-v3', 'updated_at': 'now', 'dirty': False})
    stored = {}
    monkeypatch.setattr(app, 'store_explore_summary', lambda uid, payload: stored.update({uid: payload}))

    result = app.refresh_user_explore_summary('owner')

    assert stored['owner']['sourceVersion'] == 'lex-v3'
    assert [p['label'] for p in stored['owner']['places']] == ['Rome']
    assert [t['label'] for t in stored['owner']['things']] == ['Cat']
    assert result is stored['owner']
