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
    monkeypatch.setattr(app, '_albums_feature_available', lambda: True)
    tokens = {}
    monkeypatch.setattr(app, '_store_album_token_index', lambda token, user, album: tokens.__setitem__(token, (user, album)))
    monkeypatch.setattr(app, '_delete_album_token_index', lambda token: tokens.pop(token, None))
    app.app.config['PROPAGATE_EXCEPTIONS'] = True
    http = app.app.test_client()
    http.tokens = tokens
    return http, table


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


def test_rename_validates_and_updates(client):
    http, _ = client
    album = _create(http, 'Old')
    assert http.post(f"/albums/{album['id']}/rename", json={'name': '  '}).status_code == 400
    assert http.post('/albums/missing/rename', json={'name': 'X'}).status_code == 404
    renamed = http.post(f"/albums/{album['id']}/rename", json={'name': ' New '})
    assert renamed.status_code == 200 and renamed.json['album']['name'] == 'New'


def test_rename_refuses_a_deleted_album(client):
    http, _ = client
    album = _create(http)
    http.post(f"/albums/{album['id']}/delete", json={})
    assert http.post(f"/albums/{album['id']}/rename", json={'name': 'Nope'}).status_code == 404


def test_share_creates_a_token_expiry_and_index_entry(client):
    http, table = client
    album = _create(http)
    response = http.post(f"/albums/{album['id']}/share", json={'expiresInDays': 7})
    assert response.status_code == 200
    entity = table.get_entity('u1', album['id'])
    assert entity['isPublic'] and entity['publicToken'] and entity['publicExpiresAt']
    assert http.tokens[entity['publicToken']] == ('u1', album['id'])


def test_share_keeps_the_same_token_when_called_again(client):
    http, table = client
    album = _create(http)
    http.post(f"/albums/{album['id']}/share", json={})
    first = table.get_entity('u1', album['id'])['publicToken']
    http.post(f"/albums/{album['id']}/share", json={'expiresInDays': 30})
    assert table.get_entity('u1', album['id'])['publicToken'] == first


def test_share_rejects_a_short_access_code(client):
    http, table = client
    album = _create(http)
    response = http.post(f"/albums/{album['id']}/share", json={'accessCode': 'ab'})
    assert response.status_code == 400
    assert not table.get_entity('u1', album['id']).get('isPublic')


def test_share_update_without_a_code_keeps_the_existing_one(client):
    # The web app changes the expiry this way; it must not unprotect the album.
    http, table = client
    album = _create(http)
    http.post(f"/albums/{album['id']}/share", json={'expiresInDays': 7, 'accessCode': 'secret'})
    stored = table.get_entity('u1', album['id'])['accessCode']
    assert stored
    response = http.post(f"/albums/{album['id']}/share", json={'expiresInDays': 30, 'accessCode': '', 'clearAccessCode': False})
    assert response.status_code == 200 and response.json['album']['hasAccessCode']
    assert table.get_entity('u1', album['id'])['accessCode'] == stored


def test_share_clear_access_code_removes_it(client):
    http, table = client
    album = _create(http)
    http.post(f"/albums/{album['id']}/share", json={'expiresInDays': 7, 'accessCode': 'secret'})
    response = http.post(f"/albums/{album['id']}/share", json={'expiresInDays': 7, 'accessCode': '', 'clearAccessCode': True})
    assert response.status_code == 200 and not response.json['album']['hasAccessCode']
    assert table.get_entity('u1', album['id'])['accessCode'] == ''


def test_share_disabled_clears_the_token_and_index(client):
    http, table = client
    album = _create(http)
    http.post(f"/albums/{album['id']}/share", json={})
    token = table.get_entity('u1', album['id'])['publicToken']
    http.post(f"/albums/{album['id']}/share", json={'enabled': False})
    assert table.get_entity('u1', album['id'])['publicToken'] == ''
    assert token not in http.tokens


def test_revoke_clears_token_expiry_code_and_index(client):
    http, table = client
    album = _create(http)
    http.post(f"/albums/{album['id']}/share", json={'expiresInDays': 7, 'accessCode': 'secret'})
    token = table.get_entity('u1', album['id'])['publicToken']
    response = http.post(f"/albums/{album['id']}/revoke", json={})
    assert response.status_code == 200
    entity = table.get_entity('u1', album['id'])
    assert not entity['isPublic'] and entity['publicToken'] == '' and entity['publicExpiresAt'] == '' and entity['accessCode'] == ''
    assert token not in http.tokens
    assert http.post('/albums/missing/revoke', json={}).status_code == 404


def test_delete_multiple_reports_missing_albums_and_validates_types(client):
    http, _ = client
    keep, drop = _create(http, 'Keep'), _create(http, 'Drop')
    assert http.post('/albums/delete-multiple', json={'albumIds': 'nope'}).status_code == 400
    response = http.post('/albums/delete-multiple', json={'albumIds': [drop['id'], 'ghost']})
    assert response.status_code == 200
    ids = {a['id'] for a in http.get('/albums').json['albums']}
    assert drop['id'] not in ids and keep['id'] in ids
