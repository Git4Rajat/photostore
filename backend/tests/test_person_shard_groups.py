"""A person whose membership list filled its table column spills into shard rows (groupId): detail, photos,
label and merge treat the group as one person."""
import json

import pytest

import app
import person_photos
from tests.fakes import FakeTable


def _world(monkeypatch):
    persons, faces, members, merges = FakeTable(), FakeTable(), FakeTable(), FakeTable()
    rows = (('A', '', ['f1', 'f2']), ('S', 'A', ['f3']), ('B', '', ['f4']))
    for pid, group, fids in rows:
        row = {'PartitionKey': 'u1', 'RowKey': pid, 'name': '', 'faceIds': json.dumps(fids)}
        if group:
            row['groupId'] = group
        persons.upsert_entity(row)
        for fid in fids:
            faces.upsert_entity({'PartitionKey': 'u1', 'RowKey': fid, 'filename': f'{fid}.jpg', 'personId': pid,
                                 'confidence': 0.5, 'rejected': False, 'bbox': '{}'})
            members.upsert_entity({'PartitionKey': pid, 'RowKey': fid, 'filename': f'{fid}.jpg',
                                   'confidence': 0.9 if fid == 'f3' else 0.5, 'confirmed': ''})
    monkeypatch.setattr(app, 'person_table_client', persons)
    monkeypatch.setattr(app, 'face_table_client', faces)
    monkeypatch.setattr(app, 'person_members_table_client', members)
    monkeypatch.setattr(app, 'merge_table_client', merges)
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('u1', None))
    monkeypatch.setattr(app, '_people_features_available', lambda: True)
    app.app.config['PROPAGATE_EXCEPTIONS'] = True
    return persons, faces, members


def test_group_ids_list_the_person_then_its_shards(monkeypatch):
    _world(monkeypatch)
    assert app._person_group_ids('u1', 'A') == ['A', 'S']
    assert app._person_group_ids('u1', 'B') == ['B']


def test_person_detail_lists_faces_of_every_row_in_the_group(monkeypatch):
    _world(monkeypatch)
    monkeypatch.setattr(app, '_load_face_rows_by_ids',
                        lambda user, ids: {fid: app.face_table_client.get_entity('u1', fid) for fid in ids})
    response = app.app.test_client().get('/api/persons/A')
    body = response.get_json()
    assert response.status_code == 200
    assert sorted(face['faceId'] for face in body['faces']) == ['f1', 'f2', 'f3'] and body['total'] == 3


def test_person_photos_rank_the_whole_group_together(monkeypatch):
    persons, faces, members = _world(monkeypatch)
    person_photos._CACHE.clear()
    names = person_photos.ranked_filenames('u1', 'A', members, faces, group_ids=['A', 'S'])
    assert names == ['f3.jpg', 'f1.jpg', 'f2.jpg']
    person_photos.invalidate('u1', 'S')
    assert not person_photos._CACHE


def test_labelling_a_person_names_its_shards_too(monkeypatch):
    persons, faces, members = _world(monkeypatch)
    from routes import people as people_routes
    monkeypatch.setattr(app, '_apply_live_album_rename', lambda *a, **k: None)
    monkeypatch.setattr(app, 'publish_person_label_intent', lambda *a, **k: True)
    monkeypatch.setattr(app, '_invalidate_people_scan_cache', lambda *a, **k: None)
    monkeypatch.setattr(app, '_person_entity_is_named', lambda row: bool(row.get('name')))
    monkeypatch.setattr(people_routes, '_save_person_label',
                        lambda user, pid, name: persons.upsert_entity({**persons.get_entity(user, pid), 'name': name}) or {})
    response = app.app.test_client().post('/api/persons/A/label', json={'name': 'Asha'})
    assert response.status_code == 200
    assert [persons.get_entity('u1', pid)['name'] for pid in ('A', 'S')] == ['Asha', 'Asha']
    assert persons.get_entity('u1', 'B')['name'] == ''


@pytest.mark.parametrize('base,merged', [('A', ['B']), ('B', ['A']), ('B', ['S'])])
def test_merge_with_a_grouped_person_is_refused_before_any_change(monkeypatch, base, merged):
    persons, faces, members = _world(monkeypatch)
    monkeypatch.setattr(app, 'upload_file_to_blob', lambda *a, **k: None)
    before = dict(persons.rows)
    with pytest.raises(app.PersonMergeUnsupported):
        app._merge_persons_core('u1', base, merged)
    assert dict(persons.rows) == before and not app.merge_table_client.rows


def test_merge_route_answers_409_for_a_grouped_person(monkeypatch):
    _world(monkeypatch)
    monkeypatch.setattr(app, 'upload_file_to_blob', lambda *a, **k: None)
    response = app.app.test_client().post('/api/persons/B/merge', json={'mergeIds': ['A']})
    assert response.status_code == 409 and 'merged yet' in response.get_json()['error']


def test_merge_whose_result_would_overflow_a_row_is_refused(monkeypatch):
    persons, faces, members = _world(monkeypatch)
    monkeypatch.setattr(app, 'upload_file_to_blob', lambda *a, **k: None)
    persons.upsert_entity({'PartitionKey': 'u1', 'RowKey': 'X', 'name': '',
                           'faceIds': json.dumps([f'{i:0>50}' for i in range(300)])})
    persons.upsert_entity({'PartitionKey': 'u1', 'RowKey': 'Y', 'name': '',
                           'faceIds': json.dumps([f'z{i:0>49}' for i in range(300)])})
    with pytest.raises(app.PersonMergeUnsupported, match='too many'):
        app._merge_persons_core('u1', 'X', ['Y'])
