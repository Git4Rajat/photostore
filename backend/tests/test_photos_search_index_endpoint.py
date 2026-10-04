"""GET /api/photos/search-index is retired: search runs on the backend over the
SQLite search database, so browsers no longer download an index. The route stays
as a cheap stub so already-open clients stop quietly."""
from __future__ import annotations

import app
from routes.photos import photos_search_index


def test_search_index_route_is_a_retired_stub(monkeypatch):
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('owner', None))
    with app.app.test_request_context('/api/photos/search-index'):
        response = photos_search_index()
    assert response.status_code == 200
    assert response.get_json() == {'available': False, 'retired': True}
