"""Tests for GET /photos/trash -- specifically the earliestPurgeAt summary
field added for the Activity drawer's "Recently Deleted -- N photos, purges
in M days" footer strip. Computed for free from the already-in-memory
`trashed` list (sorted deletedAt descending), no extra scan.
"""
from __future__ import annotations

import app
from routes.photos import list_trashed_photos


def _patch_common(monkeypatch, rows):
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('owner', None))
    monkeypatch.setattr(app, '_query_metadata_rows_for_user', lambda *a, **k: rows)
    monkeypatch.setattr(app, '_load_people_name_index', lambda uid: ({}, {}))
    monkeypatch.setattr(app, '_build_photo_summaries_page', lambda uid, items, pid_to_name: [{'filename': name} for name, _ in items])


def test_earliest_purge_at_uses_oldest_deletion(monkeypatch):
    rows = [
        {'RowKey': 'newest.jpg', 'processing_state': 'deleted', 'deletedAt': '2026-09-20T00:00:00+00:00'},
        {'RowKey': 'oldest.jpg', 'processing_state': 'deleted', 'deletedAt': '2026-09-01T00:00:00+00:00'},
        {'RowKey': 'middle.jpg', 'processing_state': 'deleted', 'deletedAt': '2026-09-10T00:00:00+00:00'},
    ]
    _patch_common(monkeypatch, rows)

    with app.app.test_request_context('/photos/trash'):
        response = list_trashed_photos()

    body = response.get_json()
    assert body['total'] == 3
    expected = app._compute_trash_purge_at('2026-09-01T00:00:00+00:00', app.TRASH_RETENTION_DAYS)
    assert body['earliestPurgeAt'] == expected


def test_no_earliest_purge_at_when_trash_empty(monkeypatch):
    _patch_common(monkeypatch, [])

    with app.app.test_request_context('/photos/trash'):
        response = list_trashed_photos()

    body = response.get_json()
    assert body['total'] == 0
    assert 'earliestPurgeAt' not in body
