"""POST /photos/trash/purge-all: "Delete all" in Recently Deleted purges every
photo in the trash index, not just the ones the caller had loaded."""
from __future__ import annotations

import app
from routes.photos import purge_all_trashed_photos


def _patch(monkeypatch, names, threshold=2000):
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('owner', None))
    monkeypatch.setattr(app, '_trash_index_entries', lambda uid: [{'RowKey': n, 'deletedAt': ''} for n in names])
    monkeypatch.setattr(app, 'BULK_MUTATION_ASYNC_THRESHOLD', threshold)
    calls = {'now': [], 'queued': []}
    monkeypatch.setattr(app, '_hard_delete_photos_now', lambda uid, ns: (calls['now'].append(list(ns)) or (list(ns), [])))
    monkeypatch.setattr(app, '_enqueue_photo_hard_delete_job',
                        lambda uid, ns: (calls['queued'].append(list(ns)) or {'status': 'queued', 'jobId': 'j'}))
    return calls


def test_purges_every_trashed_photo_inline(monkeypatch):
    calls = _patch(monkeypatch, ['a.jpg', 'b.jpg', 'c.jpg'])
    with app.app.test_request_context('/photos/trash/purge-all', method='POST'):
        response = purge_all_trashed_photos()
    assert response.get_json()['deleted'] == ['a.jpg', 'b.jpg', 'c.jpg']
    assert calls['queued'] == []


def test_large_trash_is_queued_whole(monkeypatch):
    names = [f'{i}.jpg' for i in range(5)]
    calls = _patch(monkeypatch, names, threshold=3)
    with app.app.test_request_context('/photos/trash/purge-all', method='POST'):
        response, status = purge_all_trashed_photos()
    assert status == 202 and response.get_json()['accepted'] == 5
    assert calls['queued'] == [names] and calls['now'] == []


def test_empty_trash_is_a_no_op(monkeypatch):
    calls = _patch(monkeypatch, [])
    with app.app.test_request_context('/photos/trash/purge-all', method='POST'):
        response = purge_all_trashed_photos()
    assert response.get_json()['success'] is True
    assert calls['now'] == [] and calls['queued'] == []
