"""Tests for GET /api/photos/search-index -- hands the browser a SAS URL to
the per-user lexical-index blob plus the people-name index, so client-side
lexical search (frontend/src/services/localSearchIndex.ts) doesn't need to
proxy the blob's bytes through a backend request. See
backend-cpu-optimization-2026-09 memory for the feature this backs.

2026-09-30: this route now reads only the lexical index MANIFEST
(get_index_manifest_summary) to mint the URL, never get_user_lexical_index --
that loaded the whole data blob into the 1Gi backend's memory and, polled
every ~15s by the frontend warm-up, OOM-crash-looped it. The browser fetches
the blob itself via the SAS URL.
"""
from __future__ import annotations

import app
from routes.photos import photos_search_index


def _happy_manifest(**over):
    base = {'source_version': 'v42', 'updated_at': '2026-09-15T00:00:00+00:00', 'dirty': False}
    base.update(over)
    return base


def test_returns_200_available_false_when_lexical_index_unavailable(monkeypatch):
    # Deliberately 200, not 503 -- a 503 here hits httpClient.ts's cold-start
    # retry loop (isRetriableColdStart treats any 503 as "ingress rejected
    # before reaching the app, safe to retry"), stalling for ~90s before
    # runLocalSemanticSearch's caller (AskPage.tsx) ever sees the null it
    # needs to fall back to server-side /photos/search.
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('owner', None))
    monkeypatch.setattr(app, 'get_index_manifest_summary', lambda uid, kind: None)

    with app.app.test_request_context('/api/photos/search-index'):
        response = photos_search_index()

    assert not isinstance(response, tuple)  # no explicit status -> Flask's default 200
    assert response.status_code == 200
    assert response.get_json()['available'] is False


def test_never_loads_the_data_blob_into_backend_memory(monkeypatch):
    """Regression pin for the 2026-09-30 OOM: this route must mint the URL from
    the manifest only. Calling get_user_lexical_index here loads the entire
    lexical blob (OCR/tags/faces per row) into this process -- the exact thing
    that OOM-crash-looped the 1Gi backend under the frontend's ~15s poll."""
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('owner', None))
    monkeypatch.setattr(app, 'get_index_manifest_summary', lambda uid, kind: _happy_manifest())
    monkeypatch.setattr(app, 'get_lexical_index_blob_location', lambda uid: ('lexical-index', f'{uid}.json.gz'))
    monkeypatch.setattr(app, '_create_stable_read_sas_url', lambda c, b: (f'https://x/{c}/{b}?sas', 'exp'))
    monkeypatch.setattr(app, '_load_people_name_index', lambda uid: ({}, {}))
    monkeypatch.setattr(app, 'get_vector_index_manifest_summary', lambda uid: None)

    def _boom(*a, **k):
        raise AssertionError('search-index must not call get_user_lexical_index (loads the blob)')

    monkeypatch.setattr(app, 'get_user_lexical_index', _boom)

    with app.app.test_request_context('/api/photos/search-index'):
        response = photos_search_index()

    assert response.get_json()['available'] is True


def test_dirty_manifest_fires_tools_rebuild_but_still_serves(monkeypatch):
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('owner', None))
    monkeypatch.setattr(app, 'get_index_manifest_summary', lambda uid, kind: _happy_manifest(dirty=True))
    monkeypatch.setattr(app, 'get_lexical_index_blob_location', lambda uid: ('lexical-index', f'{uid}.json.gz'))
    monkeypatch.setattr(app, '_create_stable_read_sas_url', lambda c, b: (f'https://x/{c}/{b}?sas', 'exp'))
    monkeypatch.setattr(app, '_load_people_name_index', lambda uid: ({}, {}))
    monkeypatch.setattr(app, 'get_vector_index_manifest_summary', lambda uid: None)
    triggered = []
    monkeypatch.setattr(app, '_trigger_tools_index_rebuild', lambda uid: triggered.append(uid))

    with app.app.test_request_context('/api/photos/search-index'):
        response = photos_search_index()

    assert triggered == ['owner']  # dirty -> nudge tools to rebuild (backend never builds)
    assert response.get_json()['available'] is True  # still serves the stale-but-usable index


def test_clean_manifest_does_not_trigger_rebuild(monkeypatch):
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('owner', None))
    monkeypatch.setattr(app, 'get_index_manifest_summary', lambda uid, kind: _happy_manifest(dirty=False))
    monkeypatch.setattr(app, 'get_lexical_index_blob_location', lambda uid: ('lexical-index', f'{uid}.json.gz'))
    monkeypatch.setattr(app, '_create_stable_read_sas_url', lambda c, b: (f'https://x/{c}/{b}?sas', 'exp'))
    monkeypatch.setattr(app, '_load_people_name_index', lambda uid: ({}, {}))
    monkeypatch.setattr(app, 'get_vector_index_manifest_summary', lambda uid: None)
    triggered = []
    monkeypatch.setattr(app, '_trigger_tools_index_rebuild', lambda uid: triggered.append(uid))

    with app.app.test_request_context('/api/photos/search-index'):
        photos_search_index()

    assert triggered == []


def test_returns_200_available_false_when_sas_minting_fails(monkeypatch):
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('owner', None))
    monkeypatch.setattr(app, 'get_index_manifest_summary', lambda uid, kind: _happy_manifest())

    def _raise(*a, **k):
        raise RuntimeError('storage not configured')

    monkeypatch.setattr(app, 'get_lexical_index_blob_location', _raise)

    with app.app.test_request_context('/api/photos/search-index'):
        response = photos_search_index()

    assert not isinstance(response, tuple)
    assert response.status_code == 200
    assert response.get_json()['available'] is False


def test_happy_path_returns_sas_url_and_people_index(monkeypatch):
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('owner', None))
    monkeypatch.setattr(app, 'get_index_manifest_summary', lambda uid, kind: _happy_manifest())
    monkeypatch.setattr(app, 'get_lexical_index_blob_location', lambda uid: ('lexical-index', f'{uid}.json.gz'))
    monkeypatch.setattr(app, '_create_stable_read_sas_url', lambda container, blob: (f'https://example.blob/{container}/{blob}?sas=1', '2026-09-17T00:00:00+00:00'))
    monkeypatch.setattr(app, '_load_people_name_index', lambda uid: ({'p1': 'Alice'}, {'alice': ['p1']}))
    monkeypatch.setattr(app, 'get_vector_index_manifest_summary', lambda uid: None)

    with app.app.test_request_context('/api/photos/search-index'):
        response = photos_search_index()

    body = response.get_json()
    assert body['available'] is True
    assert body['indexUrl'] == 'https://example.blob/lexical-index/owner.json.gz?sas=1'
    assert body['sourceVersion'] == 'v42'
    assert body['updatedAt'] == '2026-09-15T00:00:00+00:00'
    assert body['peopleNameIndex'] == {'pidToName': {'p1': 'Alice'}, 'nameToIds': {'alice': ['p1']}}
    assert 'vectorIndexUrl' not in body


def _patch_lexical_happy_path(monkeypatch):
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('owner', None))
    monkeypatch.setattr(app, 'get_index_manifest_summary', lambda uid, kind: _happy_manifest())
    monkeypatch.setattr(app, 'get_lexical_index_blob_location', lambda uid: ('lexical-index', f'{uid}.json.gz'))
    monkeypatch.setattr(app, '_load_people_name_index', lambda uid: ({}, {}))


def test_includes_vector_index_url_when_a_real_index_exists(monkeypatch):
    _patch_lexical_happy_path(monkeypatch)
    monkeypatch.setattr(app, 'get_vector_index_manifest_summary', lambda uid: {
        'source_version': 'v7', 'embedding_version': 'clip-vit-base-patch32:openai:browser-v1',
        'updated_at': 'now', 'dirty': False,
    })
    monkeypatch.setattr(app, 'get_vector_index_blob_location', lambda uid: ('vector-index', f'{uid}.npz'))

    def _fake_sas(container, blob):
        return f'https://example.blob/{container}/{blob}?sas=1', '2026-09-17T00:00:00+00:00'
    monkeypatch.setattr(app, '_create_stable_read_sas_url', _fake_sas)

    with app.app.test_request_context('/api/photos/search-index'):
        response = photos_search_index()

    body = response.get_json()
    assert body['available'] is True
    assert body['vectorIndexUrl'] == 'https://example.blob/vector-index/owner.npz?sas=1'
    assert body['embeddingVersion'] == 'clip-vit-base-patch32:openai:browser-v1'


def test_omits_vector_index_when_manifest_marks_it_dirty(monkeypatch):
    _patch_lexical_happy_path(monkeypatch)
    monkeypatch.setattr(app, 'get_vector_index_manifest_summary', lambda uid: {
        'source_version': 'v7', 'embedding_version': 'hashing-v1:1024', 'updated_at': 'now', 'dirty': True,
    })
    monkeypatch.setattr(app, '_create_stable_read_sas_url', lambda container, blob: (f'https://example.blob/{container}/{blob}?sas=1', 'exp'))

    with app.app.test_request_context('/api/photos/search-index'):
        response = photos_search_index()

    body = response.get_json()
    assert body['available'] is True  # lexical search still works
    assert 'vectorIndexUrl' not in body


def test_omits_vector_index_when_manifest_read_raises(monkeypatch):
    _patch_lexical_happy_path(monkeypatch)

    def _raise(*a, **k):
        raise RuntimeError('storage not configured')
    monkeypatch.setattr(app, 'get_vector_index_manifest_summary', _raise)
    monkeypatch.setattr(app, '_create_stable_read_sas_url', lambda container, blob: (f'https://example.blob/{container}/{blob}?sas=1', 'exp'))

    with app.app.test_request_context('/api/photos/search-index'):
        response = photos_search_index()

    body = response.get_json()
    assert body['available'] is True
    assert 'vectorIndexUrl' not in body
