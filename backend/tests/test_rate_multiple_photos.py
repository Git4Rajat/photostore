"""Tests for POST /photos/rate-multiple -- the Select command bar's bulk
"Rate" action, mirroring the existing bulk-delete endpoint's
{filenames: [...]} body shape (see _parse_filenames_request in routes/photos.py)
plus a single shared rating applied to every valid photo.
"""
from __future__ import annotations

import app
from routes.photos import rate_multiple_photos


def _patch_common(monkeypatch, known_filenames):
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('owner', None))
    monkeypatch.setattr(app, '_validate_media_filename', lambda name: name if name in known_filenames else None)
    monkeypatch.setattr(
        app, '_get_metadata_entity',
        lambda uid, name: {'RowKey': name} if name in known_filenames else None,
    )


def test_rates_every_valid_photo(monkeypatch):
    _patch_common(monkeypatch, {'a.jpg', 'b.jpg'})
    updated = []
    monkeypatch.setattr(app, '_update_metadata_entity_fields', lambda uid, name, fields: updated.append((name, fields)))

    with app.app.test_request_context('/photos/rate-multiple', method='POST', json={'filenames': ['a.jpg', 'b.jpg'], 'rating': 4}):
        response = rate_multiple_photos()

    body = response.get_json()
    assert body['success'] is True
    assert sorted(body['rated']) == ['a.jpg', 'b.jpg']
    assert body['rating'] == 4
    assert body['errors'] == []
    assert sorted(updated) == [('a.jpg', {'rating': 4}), ('b.jpg', {'rating': 4})]


def test_reports_errors_for_missing_photos(monkeypatch):
    _patch_common(monkeypatch, {'a.jpg'})
    monkeypatch.setattr(app, '_update_metadata_entity_fields', lambda *a, **k: None)

    with app.app.test_request_context('/photos/rate-multiple', method='POST', json={'filenames': ['a.jpg', 'missing.jpg'], 'rating': 5}):
        response = rate_multiple_photos()

    body = response.get_json()
    assert body['success'] is True
    assert body['rated'] == ['a.jpg']
    assert any('missing.jpg' in e for e in body['errors'])


def test_rejects_rating_out_of_range(monkeypatch):
    _patch_common(monkeypatch, {'a.jpg'})

    with app.app.test_request_context('/photos/rate-multiple', method='POST', json={'filenames': ['a.jpg'], 'rating': 6}):
        response, status = rate_multiple_photos()

    assert status == 400
    assert 'Rating must be between 0 and 5' in response.get_json()['error']


def test_rejects_empty_filenames(monkeypatch):
    _patch_common(monkeypatch, set())

    with app.app.test_request_context('/photos/rate-multiple', method='POST', json={'filenames': [], 'rating': 3}):
        response = rate_multiple_photos()

    body = response.get_json()
    assert body['success'] is False
    assert body['rated'] == []
