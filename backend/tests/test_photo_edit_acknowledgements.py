import json

import pytest
from azure.core.exceptions import ResourceModifiedError

import app
import storage_utils
from routes import photos
from tests.fakes import FakeTable


@pytest.fixture
def edit_table(monkeypatch):
    table = FakeTable()
    for name in ('a.jpg', 'b.jpg'):
        table.upsert_entity(dict(PartitionKey='owner', RowKey=name, rating=1,
            rotation=0, likes=0, likedBy='[]', thumbnail_status='done', processing_complete=True))
    monkeypatch.setattr(app, 'metadata_table_client', table)
    monkeypatch.setattr(app, '_require_user_id', lambda: ('owner', None))
    monkeypatch.setattr(app, '_get_metadata_entity', lambda uid, name: table.rows.get((uid, name)))
    monkeypatch.setattr(app, '_invalidate_metadata_scan_cache', lambda *_: None)
    monkeypatch.setattr(app, '_photo_processing_complete', lambda _: True)
    monkeypatch.setattr(app.time, 'sleep', lambda _: None)
    monkeypatch.setattr(storage_utils, 'DURABLE_INDEX_UPDATES', True)
    monkeypatch.setattr(storage_utils, 'prepare_metadata_index_intent',
        lambda entity, changes: entity.update(metadataIndexIntents='recoverable'))
    monkeypatch.setattr(storage_utils, 'publish_metadata_index_intent', lambda *_: None)
    return table


@pytest.mark.parametrize('route,payload,handler', [
    ('rating', {'rating': 5}, photos.set_photo_rating),
    ('like', {'liked': True}, photos.toggle_like_photo),
    ('rotation', {'rotation': 90}, photos.set_photo_rotation),
])
@pytest.mark.parametrize('failure', ['read', 'write'])
def test_photo_edit_does_not_acknowledge_failed_persistence(edit_table, monkeypatch, route, payload, handler, failure):
    before = dict(edit_table.rows[('owner', 'a.jpg')])
    def unavailable(*args, **kwargs):
        raise RuntimeError('storage unavailable')
    monkeypatch.setattr(edit_table, 'get_entity' if failure == 'read' else 'update_entity', unavailable)
    with app.app.test_request_context(f'/photos/a.jpg/{route}', method='POST', json=payload):
        response, status = handler('a.jpg')
    assert status == 503
    assert response.get_json()['code'] == 'metadata_write_failed'
    assert edit_table.rows[('owner', 'a.jpg')] == before


def test_committed_edit_is_not_replayed_when_publication_delivery_fails(edit_table, monkeypatch):
    def unavailable(*_):
        raise RuntimeError('queue unavailable')
    monkeypatch.setattr(storage_utils, 'publish_metadata_index_intent', unavailable)
    writes = []
    original = edit_table.update_entity
    def update(*args, **kwargs):
        writes.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(edit_table, 'update_entity', update)
    with app.app.test_request_context('/photos/a.jpg/like', method='POST', json={}):
        body = photos.toggle_like_photo('a.jpg').get_json()
    assert body['success'] and body['liked'] and body['likes'] == 1
    assert writes == [1]
    assert edit_table.rows[('owner', 'a.jpg')]['metadataIndexIntents'] == 'recoverable'


def test_bulk_rating_acknowledges_only_committed_files(edit_table, monkeypatch):
    original = app._update_metadata_entity_fields
    monkeypatch.setattr(app, '_update_metadata_entity_fields',
        lambda uid, name, fields: None if name == 'b.jpg' else original(uid, name, fields))
    with app.app.test_request_context('/photos/rate-multiple', method='POST',
        json={'filenames': ['a.jpg', 'b.jpg'], 'rating': 5}):
        body = photos.rate_multiple_photos().get_json()
    assert body['rated'] == ['a.jpg']
    assert body['errors'] == ['b.jpg: Could not save rating']
    assert edit_table.rows[('owner', 'a.jpg')]['rating'] == 5
    assert edit_table.rows[('owner', 'b.jpg')]['rating'] == 1


def test_legacy_projection_delivery_retries_without_replaying_the_source(edit_table, monkeypatch):
    monkeypatch.setattr(storage_utils, 'DURABLE_INDEX_UPDATES', False)
    monkeypatch.setattr(app, 'metadata_updates_affect_search_indexes', lambda _: False)
    monkeypatch.setattr(app, 'metadata_updates_affect_sort_index', lambda _: True)
    attempts = []
    def deliver(*_):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError('transient dirty-table failure')
    monkeypatch.setattr(app, 'touch_user_sort_index_dirty', deliver)
    before = edit_table.versions[('owner', 'a.jpg')]
    with app.app.test_request_context('/photos/a.jpg/rating', method='POST', json={'rating': 5}):
        assert photos.set_photo_rating('a.jpg').get_json()['rating'] == 5
    assert attempts == [1, 1]
    assert edit_table.versions[('owner', 'a.jpg')] == before + 1


def test_desired_like_state_survives_repeated_delivery(edit_table):
    for desired, expected in [(True, 1), (True, 1), (False, 0), (False, 0)]:
        with app.app.test_request_context('/photos/a.jpg/like', method='POST', json={'liked': desired}):
            body = photos.toggle_like_photo('a.jpg').get_json()
        assert body['liked'] == desired and body['likes'] == expected


def test_legacy_like_toggle_remains_supported(edit_table):
    for desired in (True, False):
        with app.app.test_request_context('/photos/a.jpg/like', method='POST', json={}):
            assert photos.toggle_like_photo('a.jpg').get_json()['liked'] == desired


def test_like_retry_preserves_another_concurrent_like(edit_table, monkeypatch):
    original = edit_table.update_entity
    calls = []
    def concurrent_update(entity, **kwargs):
        if not calls:
            calls.append(1)
            current = dict(edit_table.rows[('owner', 'a.jpg')], likes=1, likedBy='["other"]')
            edit_table.upsert_entity(current)
            raise ResourceModifiedError('concurrent edit')
        return original(entity, **kwargs)
    monkeypatch.setattr(edit_table, 'update_entity', concurrent_update)
    with app.app.test_request_context('/photos/a.jpg/like', method='POST', json={'liked': True}):
        assert photos.toggle_like_photo('a.jpg').get_json()['likes'] == 2
    assert json.loads(edit_table.rows[('owner', 'a.jpg')]['likedBy']) == ['other', 'owner']


def test_like_rejects_non_boolean_desired_state(edit_table):
    with app.app.test_request_context('/photos/a.jpg/like', method='POST', json={'liked': 'false'}):
        _, status = photos.toggle_like_photo('a.jpg')
    assert status == 400
