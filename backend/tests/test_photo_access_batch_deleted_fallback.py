"""Tests for POST /api/photos/access-batch's fallback to a per-filename
metadata lookup on a cache miss.

_cached_metadata_list_rows_for_user deliberately excludes trashed rows
(processing_state == 'deleted'), so every filename requested from the
Recently Deleted page misses that cache -- not just brand-new uploads. This
used to fall back to a *serial* per-filename app._get_metadata_entity loop,
turning a trash-page batch (up to 200 filenames) into 200 sequential Table
Storage round trips. Slow enough that the whole request could fail and leave
every tile on that page showing the empty-thumbnail placeholder. The fix
fans those misses out concurrently instead.
"""
from __future__ import annotations

import app
from routes.photos import photo_access_url_batch


def _patch_common(monkeypatch, cached_rows, entities_by_name):
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('owner', None))
    monkeypatch.setattr(app, 'blob_service_client', object())
    monkeypatch.setattr(app, 'account_name', 'anystorageaccount')
    monkeypatch.setattr(app, '_cached_metadata_list_rows_for_user', lambda *a, **k: cached_rows)
    monkeypatch.setattr(app, '_get_metadata_entity', lambda uid, name: entities_by_name.get(name))


def test_deleted_photo_missing_from_cache_still_resolves_via_fallback(monkeypatch):
    # 'kept.jpg' is a normal photo the cached scan would surface; 'trashed.jpg'
    # is soft-deleted, so the cached scan (which excludes deleted rows) never
    # returns it -- only the per-filename point lookup does.
    cached_rows = [{'RowKey': 'kept.jpg'}]
    entities_by_name = {'trashed.jpg': {'RowKey': 'trashed.jpg', 'processing_state': 'deleted'}}
    _patch_common(monkeypatch, cached_rows, entities_by_name)

    with app.app.test_request_context(
        '/api/photos/access-batch',
        method='POST',
        json={'kind': 'thumbnail', 'filenames': ['kept.jpg', 'trashed.jpg']},
    ):
        response = photo_access_url_batch()

    body = response.get_json()
    assert body['urls']['kept.jpg'] == '/api/photos/thumbnail/kept.jpg'
    assert body['urls']['trashed.jpg'] == '/api/photos/thumbnail/trashed.jpg'


def test_filename_missing_everywhere_is_dropped_not_errored(monkeypatch):
    _patch_common(monkeypatch, cached_rows=[], entities_by_name={})

    with app.app.test_request_context(
        '/api/photos/access-batch',
        method='POST',
        json={'kind': 'thumbnail', 'filenames': ['ghost.jpg']},
    ):
        response = photo_access_url_batch()

    body = response.get_json()
    assert 'ghost.jpg' not in body['urls']
