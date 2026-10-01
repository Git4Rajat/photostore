"""Tests for POST /api/photos/access-batch's resolution of filenames via the
access index, and its fallback to a per-filename metadata lookup for
whatever that index doesn't (yet) cover.

Unlike the listing index /photos uses, the access index (see
storage_utils.py's "Access index" section) deliberately INCLUDES trashed
rows -- the prior design excluded them, which meant every filename on the
Recently Deleted page was guaranteed to miss the fast path and fall back to
a per-filename app._get_metadata_entity lookup. At up to 200 filenames a
page, slow enough to tie up this backend's thin GUNICORN_THREADS pool and
starve the liveness probe (live ContainerBackOff crash loop, 2026-10-01).
The remaining fallback here now only covers genuine index misses (e.g. a
brand-new upload not yet merged into the index), fanned out concurrently.
"""
from __future__ import annotations

import app
from routes.photos import photo_access_url_batch


def _patch_common(monkeypatch, access_index_rows, entities_by_name):
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('owner', None))
    monkeypatch.setattr(app, 'blob_service_client', object())
    monkeypatch.setattr(app, 'account_name', 'anystorageaccount')
    access_index = {'rows': access_index_rows} if access_index_rows is not None else None
    monkeypatch.setattr(app, 'get_user_access_index', lambda *a, **k: access_index)
    monkeypatch.setattr(app, '_trigger_tools_index_rebuild', lambda *a, **k: None)
    monkeypatch.setattr(app, '_get_metadata_entity', lambda uid, name: entities_by_name.get(name))


def test_trashed_filename_resolves_via_access_index_not_fallback(monkeypatch):
    # A trashed row still shows up in the access index (unlike the old
    # cached-scan fast path, which excluded processing_state == 'deleted')
    # -- it must resolve WITHOUT ever calling the per-filename fallback.
    # thumbnailStatus deliberately not 'done': exercises the cheap
    # proxy-fallback branch (_thumbnail_access_response) rather than
    # requiring a real SAS mint, same as this test's original premise.
    access_index_rows = [
        {'RowKey': 'kept.jpg', 'blobName': '', 'thumbnailStatus': '', 'previewStatus': ''},
        {'RowKey': 'trashed.jpg', 'blobName': '', 'thumbnailStatus': '', 'previewStatus': ''},
    ]

    def _fallback_should_not_be_called(uid, name):
        raise AssertionError(f'fallback _get_metadata_entity should not be called for {name!r}')

    _patch_common(monkeypatch, access_index_rows, entities_by_name={})
    monkeypatch.setattr(app, '_get_metadata_entity', _fallback_should_not_be_called)

    with app.app.test_request_context(
        '/api/photos/access-batch',
        method='POST',
        json={'kind': 'thumbnail', 'filenames': ['kept.jpg', 'trashed.jpg']},
    ):
        response = photo_access_url_batch()

    body = response.get_json()
    assert body['urls']['kept.jpg'] == '/api/photos/thumbnail/kept.jpg'
    assert body['urls']['trashed.jpg'] == '/api/photos/thumbnail/trashed.jpg'


def test_filename_missing_from_index_still_resolves_via_fallback(monkeypatch):
    # A genuinely cold/unbuilt access index (None) must not error the whole
    # batch -- every filename falls back to the per-filename lookup, same as
    # before this index existed.
    entities_by_name = {'brand_new.jpg': {'RowKey': 'brand_new.jpg'}}
    _patch_common(monkeypatch, access_index_rows=None, entities_by_name=entities_by_name)

    with app.app.test_request_context(
        '/api/photos/access-batch',
        method='POST',
        json={'kind': 'thumbnail', 'filenames': ['brand_new.jpg']},
    ):
        response = photo_access_url_batch()

    body = response.get_json()
    assert body['urls']['brand_new.jpg'] == '/api/photos/thumbnail/brand_new.jpg'


def test_filename_missing_everywhere_is_dropped_not_errored(monkeypatch):
    _patch_common(monkeypatch, access_index_rows=[], entities_by_name={})

    with app.app.test_request_context(
        '/api/photos/access-batch',
        method='POST',
        json={'kind': 'thumbnail', 'filenames': ['ghost.jpg']},
    ):
        response = photo_access_url_batch()

    body = response.get_json()
    assert 'ghost.jpg' not in body['urls']
