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
    cover = body['cover']
    assert cover['baseUrl'].endswith('/' + app.BLOB_COVER_CONTAINER) and 'sr=c' in cover['sas']
    assert cover['prefix'] == app.hashlib.sha256(b'owner').hexdigest()[:16] + '/' and cover['prefix'].endswith('/')
    image = body['image']
    assert image['baseUrl'].endswith('/' + app.BLOB_IMAGE_CONTAINER) and 'sr=c' in image['sas']


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


def test_direct_media_exposes_blob_even_when_thumbnail_not_ready_yet(monkeypatch):
    """The blob name is assigned at upload time, before any processing runs --
    the client attempts it directly and treats a 404 as "still generating"
    instead of asking this summary whether it's ready first."""
    monkeypatch.setattr(app, 'MEDIA_URL_MODE', 'sas')
    with app.app.test_request_context('/x'):
        app.g.direct_media = True
        s = app._build_photo_summary('u', 'a.jpg', _meta(thumbnail_status='pending', preview_status='pending'),
                                     include_props=False, head_missing=False)
    assert s['thumbnailBlob'] == 'uuid-1' and s['thumbnailUrl'] == ''


def test_default_summary_unchanged_without_flag(monkeypatch):
    monkeypatch.setattr(app, 'MEDIA_URL_MODE', 'sas')
    monkeypatch.setattr(app, 'make_media_url', lambda f, kind='thumbnail', blob_name=None: f'https://signed/{blob_name}')
    with app.app.test_request_context('/x'):
        s = app._build_photo_summary('u', 'a.jpg', _meta(), include_props=False, head_missing=False)
    assert s['thumbnailUrl'] == 'https://signed/uuid-1' and 'thumbnailBlob' not in s


def test_sort_row_always_carries_thumb_regardless_of_processing_status():
    """thumb is the physical blob name (anonymousImageId or filename), known from
    upload time -- present even before thumbnail/preview processing finishes, so
    the client can attempt the direct URL instead of asking first."""
    done = storage_utils._sort_index_row({'RowKey': 'a.jpg', 'thumbnail_status': 'done', 'anonymousImageId': 'uuid-1'})
    pending = storage_utils._sort_index_row({'RowKey': 'b.jpg', 'thumbnail_status': 'pending', 'anonymousImageId': 'uuid-2'})
    plain = storage_utils._sort_index_row({'RowKey': 'c.jpg', 'thumbnail_status': 'done'})
    assert done['thumb'] == 'uuid-1' and pending['thumb'] == 'uuid-2' and plain['thumb'] == 'c.jpg'


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


def test_index_status_diagnostics_reports_queue_depths_and_processing_mode(monkeypatch):
    from routes.photos import photos_index_status

    class Q:
        def __init__(self, n): self.n = n
        def get_queue_properties(self):
            class P: approximate_message_count = self.n
            return P()

    class Broken:
        def get_queue_properties(self): raise RuntimeError('denied')

    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('owner', None))
    monkeypatch.setattr(app, 'get_user_index_readiness', lambda uid: {'sort': True})
    monkeypatch.setattr(app, 'ipwork_queue_client', Q(25))
    monkeypatch.setattr(app, 'clustering_queue_client', Broken())
    monkeypatch.setattr(app, 'library_ops_queue_client', None)
    monkeypatch.setattr(app, 'PROCESSING_MODE', 'backend')
    monkeypatch.setattr(storage_utils.search_db if hasattr(storage_utils, 'search_db') else __import__('search_db'), 'load_manifest', lambda uid: {})
    with app.app.test_request_context('/api/photos/index-status?diagnostics=1'):
        body = photos_index_status().get_json()
    assert body['diagnostics'] == {'processingMode': 'backend',
                                   'queues': {'ipwork': 25, 'clustering': 'error: RuntimeError', 'libraryOps': None}}
    with app.app.test_request_context('/api/photos/index-status'):
        assert 'diagnostics' not in photos_index_status().get_json()
