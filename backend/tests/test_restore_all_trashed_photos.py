"""Tests for POST /photos/trash/restore-all -- the Activity drawer's
"Restore all" strip action. Unlike /photos/trash/restore (used by
RecentlyDeletedPage's selection flow), the caller doesn't supply filenames --
every currently-trashed photo for the user is restored.
"""
from __future__ import annotations

import app
from routes.photos import restore_all_trashed_photos


def _patch_common(monkeypatch, rows, restorable):
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('owner', None))
    calls = []

    def _fake_scan(user_id, select=None, include_deleted=False, extra_filter='', purpose=''):
        # emulate the server-side filter the routes now push down
        calls.append(extra_filter)
        return iter([r for r in rows if r.get('processing_state') == 'deleted'] if 'deleted' in extra_filter else rows)

    monkeypatch.setattr(app, '_iter_metadata_rows_for_user', _fake_scan)
    monkeypatch.setattr(app, '_scan_filters_seen', calls, raising=False)
    monkeypatch.setattr(app, '_restore_deleted_file', lambda uid, name: ({'RowKey': name} if name in restorable else None))
    monkeypatch.setattr(app, '_invalidate_metadata_scan_cache', lambda uid: None)
    monkeypatch.setattr(app, 'touch_user_search_indexes_state', lambda uid, filenames=None: None)


def test_restores_every_trashed_photo(monkeypatch):
    rows = [
        {'RowKey': 'a.jpg', 'processing_state': 'deleted'},
        {'RowKey': 'b.jpg', 'processing_state': 'deleted'},
        {'RowKey': 'c.jpg', 'processing_state': 'active'},
    ]
    _patch_common(monkeypatch, rows, {'a.jpg', 'b.jpg'})

    with app.app.test_request_context('/photos/trash/restore-all', method='POST'):
        response = restore_all_trashed_photos()

    body = response.get_json()
    assert body['success'] is True
    assert sorted(body['restored']) == ['a.jpg', 'b.jpg']
    assert body['errors'] == []


def test_no_trashed_photos_is_a_no_op(monkeypatch):
    _patch_common(monkeypatch, [], set())

    with app.app.test_request_context('/photos/trash/restore-all', method='POST'):
        response = restore_all_trashed_photos()

    body = response.get_json()
    assert body['success'] is False
    assert body['restored'] == []
