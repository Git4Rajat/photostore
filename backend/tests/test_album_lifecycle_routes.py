"""Album lifecycle through the real HTTP routes (create -> list -> remove photos -> delete -> restore -> purge)."""
import json
from types import SimpleNamespace

import pytest

import app
from tests.fakes import FakeTable


@pytest.fixture
def client(monkeypatch):
    table = FakeTable()
    monkeypatch.setattr(app, 'albums_table_client', table)
    monkeypatch.setattr(app, 'albums_dirty_set_table_client', FakeTable())
    monkeypatch.setattr(app, 'library_ops_queue_client', SimpleNamespace(send_message=lambda *a, **k: None))
    monkeypatch.setattr(app, 'touch_user_albums_index_state', lambda *a, **k: '')
    monkeypatch.setattr(app, 'mark_albums_dirty', lambda *a, **k: None)
    monkeypatch.setattr(app, '_albums_table_available', lambda: True)
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('u1', None))
    app.app.config['PROPAGATE_EXCEPTIONS'] = True
    return app.app.test_client(), table


def _create(client, name='Holiday'):
    response = client.post('/albums', json={'name': name})
    assert response.status_code == 200
    return response.json['album']


def _seed_photos(table, album_id, filenames):
    entity = table.get_entity('u1', album_id)
    entity['filenames'] = json.dumps(filenames)
    table.upsert_entity(entity)


def test_create_requires_a_name_and_returns_the_album(client):
    http, _ = client
    assert http.post('/albums', json={'name': '   '}).status_code == 400
    album = _create(http)
    assert album['name'] == 'Holiday' and album['id']


def test_list_returns_created_albums(client):
    http, _ = client
    first, second = _create(http, 'A'), _create(http, 'B')
    listed = http.get('/albums').json
    ids = {a['id'] for a in listed['albums']}
    assert {first['id'], second['id']} <= ids


def test_remove_photos_keeps_the_others(client):
    http, table = client
    album = _create(http)
    _seed_photos(table, album['id'], ['a.jpg', 'b.jpg', 'c.jpg'])
    response = http.post(f"/albums/{album['id']}/photos/remove", json={'filenames': ['b.jpg']})
    assert response.status_code == 200
    stored = json.loads(table.get_entity('u1', album['id'])['filenames'])
    assert stored == ['a.jpg', 'c.jpg']


def test_remove_photos_unknown_album_is_404(client):
    http, _ = client
    assert http.post('/albums/missing/photos/remove', json={'filenames': ['a.jpg']}).status_code == 404


def test_delete_moves_to_trash_and_restore_brings_it_back(client):
    http, _ = client
    album = _create(http)
    assert http.post(f"/albums/{album['id']}/delete", json={}).status_code == 200
    assert album['id'] not in {a['id'] for a in http.get('/albums').json['albums']}
    assert album['id'] in {a['id'] for a in http.get('/albums/trash').json['albums']}
    assert http.post(f"/albums/{album['id']}/restore", json={}).status_code == 200
    assert album['id'] in {a['id'] for a in http.get('/albums').json['albums']}


def test_purge_removes_a_trashed_album_for_good(client):
    http, _ = client
    album = _create(http)
    http.post(f"/albums/{album['id']}/delete", json={})
    assert http.post(f"/albums/{album['id']}/purge", json={}).status_code == 200
    assert album['id'] not in {a['id'] for a in http.get('/albums/trash').json['albums']}
    assert http.post(f"/albums/{album['id']}/restore", json={}).status_code == 404
