import json

import app
from tests.test_live_person_albums import _world, _seed_person, _seed_face, _seed_rule_album


def test_transient_person_read_preserves_membership(monkeypatch):
    persons, faces, albums = _world(monkeypatch)
    _seed_person(persons, 'u1', 'A', 'Asha', ['f1'])
    _seed_face(faces, 'u1', 'f1', 'a.jpg', 'A')
    _seed_rule_album(albums, 'u1', 'al1', 'A', 'Asha', ['a.jpg'])
    monkeypatch.setattr(persons, 'get_entity', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('temporary storage outage')))
    app._resync_person_rule_albums('u1', 'A')
    assert app.album_store.all_names(albums.rows['u1', 'al1']) == ['a.jpg']


def test_rejecting_face_updates_live_album(monkeypatch):
    persons, faces, albums = _world(monkeypatch)
    _seed_person(persons, 'u1', 'A', 'Asha', ['f1', 'f2'])
    _seed_face(faces, 'u1', 'f1', 'a.jpg', 'A')
    _seed_face(faces, 'u1', 'f2', 'b.jpg', 'A')
    _seed_rule_album(albums, 'u1', 'al1', 'A', 'Asha', ['a.jpg', 'b.jpg'])
    result = app._delete_faces_bulk('u1', ['f2'])
    assert result['deleted'] == ['f2']
    assert json.loads(persons.rows['u1', 'A']['faceIds']) == ['f1']
    assert app.album_store.all_names(albums.rows['u1', 'al1']) == ['a.jpg']


def test_membership_resync_preserves_concurrent_share_revocation(monkeypatch):
    persons, faces, albums = _world(monkeypatch)
    _seed_person(persons, 'u1', 'A', 'Asha', ['f1', 'f2'])
    _seed_face(faces, 'u1', 'f1', 'a.jpg', 'A')
    _seed_face(faces, 'u1', 'f2', 'b.jpg', 'A')
    entity = _seed_rule_album(albums, 'u1', 'al1', 'A', 'Asha', ['a.jpg'])
    entity.update(isPublic=True, publicToken='old-share-token')
    albums.upsert_entity(entity)
    def resolve_after_revoke(*a, **k):
        revoked = albums.get_entity('u1', 'al1')
        revoked.update(isPublic=False, publicToken='', accessCode='')
        app._save_album_entity(revoked)
        return ['a.jpg', 'b.jpg']
    monkeypatch.setattr(app, '_filenames_for_face_ids', resolve_after_revoke)
    app._resync_person_rule_albums('u1', 'A')
    assert app.album_store.all_names(albums.rows['u1', 'al1']) == ['a.jpg', 'b.jpg']
    assert albums.rows['u1', 'al1']['isPublic'] is False
    assert albums.rows['u1', 'al1']['publicToken'] == ''


def test_confirm_face_adds_photo_to_target_live_album(monkeypatch):
    from routes import people
    persons, faces, albums = _world(monkeypatch)
    _seed_person(persons, 'u1', 'A', 'Asha', ['f1'])
    _seed_person(persons, 'u1', 'B', 'Bo', ['f2'])
    _seed_face(faces, 'u1', 'f1', 'a.jpg', 'A')
    _seed_face(faces, 'u1', 'f2', 'b.jpg', 'B')
    _seed_rule_album(albums, 'u1', 'al1', 'B', 'Bo', ['b.jpg'])
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('u1', None))
    monkeypatch.setattr(app, '_people_features_available', lambda: True)
    monkeypatch.setattr(app, '_rebuild_metadata_faces_for_filename', lambda *a, **k: {})
    with app.app.test_request_context('/api/persons/B/confirm-face', method='POST', json={'faceId': 'f1'}):
        response = people.confirm_face('B')
    assert response.get_json()['success'] is True
    assert set(json.loads(persons.rows['u1', 'B']['faceIds'])) == {'f1', 'f2'}
    assert sorted(app.album_store.all_names(albums.rows['u1', 'al1'])) == ['a.jpg', 'b.jpg']
