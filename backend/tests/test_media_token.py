"""Token-based media: GET /api/photos/media-token, directMedia summaries, and
thumb blob names in the sort index."""
from __future__ import annotations

import pytest

import app
import storage_utils
from routes.photos import photos_media_token, lookup_photos_batch


def test_media_token_returns_one_container_scoped_token(monkeypatch):
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('owner', None))
    monkeypatch.setattr(app, 'MEDIA_URL_MODE', 'sas')
    monkeypatch.setattr(app, 'blob_service_client', object())
    monkeypatch.setattr(app, 'account_name', 'acct')
    monkeypatch.setattr(app, '_stable_container_read_sas',
                        lambda c: (f'https://acct.blob.core.windows.net/{c}', 'sp=r&sr=c&sig=x', '2026-10-06T00:00:00+00:00'))
    with app.app.test_request_context('/api/photos/media-token'):
        resp = photos_media_token()
    body = resp.get_json()
    assert body['available'] and body['baseUrl'].endswith('/' + app.BLOB_THUMBNAIL_CONTAINER)
    assert 'sr=c' in body['sas'] and body['previewPrefix'] == 'preview/'
    assert 'max-age' in resp.headers['Cache-Control']


def test_media_token_unavailable_in_proxy_mode(monkeypatch):
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('owner', None))
    monkeypatch.setattr(app, 'MEDIA_URL_MODE', 'proxy')
    with app.app.test_request_context('/api/photos/media-token'):
        assert photos_media_token().get_json() == {'available': False}


def _meta(**kw):
    return {'thumbnail_status': 'done', 'anonymousImageId': 'uuid-1', **kw}


def test_direct_media_summary_has_blob_not_signed_url(monkeypatch):
    monkeypatch.setattr(app, 'MEDIA_URL_MODE', 'sas')
    monkeypatch.setattr(app, 'make_media_url', lambda *a, **k: (_ for _ in ()).throw(AssertionError('must not sign')))
    with app.app.test_request_context('/x'):
        app.g.direct_media = True
        s = app._build_photo_summary('u', 'a.jpg', _meta(), include_props=False, head_missing=False)
    assert s['thumbnailBlob'] == 'uuid-1' and s['thumbnailUrl'] == ''


def test_direct_media_keeps_proxy_fallback_when_thumbnail_not_ready(monkeypatch):
    monkeypatch.setattr(app, 'MEDIA_URL_MODE', 'sas')
    with app.app.test_request_context('/x'):
        app.g.direct_media = True
        s = app._build_photo_summary('u', 'a.jpg', _meta(thumbnail_status='pending', preview_status='done'),
                                     include_props=False, head_missing=False)
    assert 'thumbnailBlob' not in s and s['thumbnailUrl'].endswith('/preview/a.jpg')


def test_default_summary_unchanged_without_flag(monkeypatch):
    monkeypatch.setattr(app, 'MEDIA_URL_MODE', 'sas')
    monkeypatch.setattr(app, 'make_media_url', lambda f, kind='thumbnail', blob_name=None: f'https://signed/{blob_name}')
    with app.app.test_request_context('/x'):
        s = app._build_photo_summary('u', 'a.jpg', _meta(), include_props=False, head_missing=False)
    assert s['thumbnailUrl'] == 'https://signed/uuid-1' and 'thumbnailBlob' not in s


def test_sort_row_carries_thumb_only_when_thumbnail_done():
    done = storage_utils._sort_index_row({'RowKey': 'a.jpg', 'thumbnail_status': 'done', 'anonymousImageId': 'uuid-1'})
    pending = storage_utils._sort_index_row({'RowKey': 'b.jpg', 'thumbnail_status': 'pending', 'anonymousImageId': 'uuid-2'})
    plain = storage_utils._sort_index_row({'RowKey': 'c.jpg', 'thumbnail_status': 'done'})
    assert done['thumb'] == 'uuid-1' and 'thumb' not in pending and plain['thumb'] == 'c.jpg'


def test_ensure_sort_current_rebuilds_old_schema_only(monkeypatch):
    calls = []
    monkeypatch.setattr(storage_utils, 'refresh_user_sort_index', lambda k, force_full=False, **kw: calls.append(force_full))
    monkeypatch.setattr(storage_utils, '_load_sort_index_manifest', lambda k: {'sourceVersion': 'v', 'schemaVersion': 'v1'})
    assert storage_utils.ensure_user_sort_index_current('lib') is True and calls == [True]
    monkeypatch.setattr(storage_utils, '_load_sort_index_manifest', lambda k: {'sourceVersion': 'v', 'schemaVersion': storage_utils._SORT_INDEX_SCHEMA_VERSION})
    assert storage_utils.ensure_user_sort_index_current('lib') is False and calls == [True]
    monkeypatch.setattr(storage_utils, '_load_sort_index_manifest', lambda k: {})
    assert storage_utils.ensure_user_sort_index_current('lib') is False


def test_lookup_batch_sets_direct_flag_from_body(monkeypatch):
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('owner', None))
    monkeypatch.setattr(app, '_load_people_name_index', lambda uid: ({}, {}))
    monkeypatch.setattr(app, '_get_metadata_entity', lambda uid, n: _meta())
    monkeypatch.setattr(app, 'MEDIA_URL_MODE', 'sas')
    with app.app.test_request_context('/api/photos/lookup-batch', method='POST',
                                      json={'filenames': ['a.jpg', 'b.jpg'], 'directMedia': True}):
        photos = lookup_photos_batch().get_json()['photos']
    assert [p['thumbnailBlob'] for p in photos] == ['uuid-1', 'uuid-1']
