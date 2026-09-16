"""Tests for GET /api/photos/search-index -- hands the browser a SAS URL to
the per-user lexical-index blob plus the people-name index, so client-side
lexical search (frontend/src/services/localSearchIndex.ts) doesn't need to
proxy the blob's bytes through a backend request. See
backend-cpu-optimization-2026-09 memory for the feature this backs.
"""
from __future__ import annotations

import app
from routes.photos import photos_search_index


def test_returns_503_when_lexical_index_unavailable(monkeypatch):
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('owner', None))
    monkeypatch.setattr(app, 'get_user_lexical_index', lambda *a, **k: None)

    with app.app.test_request_context('/api/photos/search-index'):
        response, status = photos_search_index()

    assert status == 503
    assert response.get_json()['available'] is False


def test_returns_503_when_sas_minting_fails(monkeypatch):
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('owner', None))
    monkeypatch.setattr(app, 'get_user_lexical_index', lambda *a, **k: {'source_version': 'v1', 'updated_at': 'now'})

    def _raise(*a, **k):
        raise RuntimeError('storage not configured')

    monkeypatch.setattr(app, 'get_lexical_index_blob_location', _raise)

    with app.app.test_request_context('/api/photos/search-index'):
        response, status = photos_search_index()

    assert status == 503
    assert response.get_json()['available'] is False


def test_happy_path_returns_sas_url_and_people_index(monkeypatch):
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('owner', None))
    monkeypatch.setattr(app, 'get_user_lexical_index', lambda *a, **k: {
        'source_version': 'v42',
        'updated_at': '2026-09-15T00:00:00+00:00',
    })
    monkeypatch.setattr(app, 'get_lexical_index_blob_location', lambda uid: ('lexical-index', f'{uid}.json.gz'))
    monkeypatch.setattr(app, '_create_stable_read_sas_url', lambda container, blob: (f'https://example.blob/{container}/{blob}?sas=1', '2026-09-17T00:00:00+00:00'))
    monkeypatch.setattr(app, '_load_people_name_index', lambda uid: ({'p1': 'Alice'}, {'alice': ['p1']}))

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
    monkeypatch.setattr(app, 'get_user_lexical_index', lambda *a, **k: {
        'source_version': 'v42',
        'updated_at': '2026-09-15T00:00:00+00:00',
    })
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
