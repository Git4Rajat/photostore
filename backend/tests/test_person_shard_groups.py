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


@pytest.fixture
def merge_env(monkeypatch):
    persons, faces, members = _world(monkeypatch)
    blobs = {}
    monkeypatch.setattr(app, 'upload_file_to_blob', lambda container, name, data, *a, **k: blobs.__setitem__(name, data))
    monkeypatch.setattr(app, 'download_file_from_blob', lambda container, name: blobs[name])
    monkeypatch.setattr(app, '_delete_blob_if_present', lambda container, name: blobs.pop(name, None))
    monkeypatch.setattr(app, '_update_person_entity',
                        lambda uid, pid, updates: persons.upsert_entity({**persons.get_entity(uid, pid), **updates}) or True)
    monkeypatch.setattr(app, '_rebuild_metadata_faces_for_filenames', lambda *a, **k: {})
    monkeypatch.setattr(app, '_resync_person_rule_albums_for_many', lambda *a, **k: None)
    monkeypatch.setattr(app, '_resync_person_rule_albums', lambda *a, **k: None)
    monkeypatch.setattr(app, 'mark_people_dirty', lambda *a, **k: None)
    monkeypatch.setattr(app, 'touch_user_people_index_state', lambda *a, **k: None)
    return persons, faces, blobs


def test_merging_into_a_grouped_person_retags_the_source_rows_without_moving_faces(merge_env):
    persons, faces, blobs = merge_env
    persons.upsert_entity({**persons.get_entity('u1', 'B'), 'name': 'Bina'})
    result = app._merge_persons_core('u1', 'B', ['A'])        # A has shard S; neither moves a face
    assert result and result['mergeId']
    for pid in ('A', 'S'):
        row = persons.get_entity('u1', pid)
        assert row['groupId'] == 'B' and row['name'] == 'Bina'
    assert app._person_group_ids('u1', 'B') == ['B', 'A', 'S']
    assert {faces.get_entity('u1', f)['personId'] for f in ('f1', 'f2', 'f3')} == {'A', 'S'}
    assert json.loads(persons.get_entity('u1', 'A')['faceIds']) == ['f1', 'f2']


def test_undoing_a_group_merge_restores_the_original_rows(merge_env):
    persons, faces, blobs = merge_env
    before = {pid: dict(persons.get_entity('u1', pid)) for pid in ('A', 'S', 'B')}
    merge_id = app._merge_persons_core('u1', 'B', ['A'])['mergeId']
    response = app.app.test_client().post(f'/api/persons/merge/{merge_id}/undo')
    assert response.status_code == 200
    after = {pid: persons.get_entity('u1', pid) for pid in ('A', 'S', 'B')}
    assert after['A'].get('groupId', '') == '' and after['A']['name'] == before['A']['name']
    assert after['S']['groupId'] == 'A' and after['B'].get('groupId', '') == ''
    assert app._person_group_ids('u1', 'A') == ['A', 'S'] and app._person_group_ids('u1', 'B') == ['B']
    assert not app.merge_table_client.rows and not blobs


def test_a_merge_that_would_overflow_one_row_joins_the_groups_instead(merge_env):
    persons, faces, blobs = merge_env
    persons.upsert_entity({'PartitionKey': 'u1', 'RowKey': 'X', 'name': 'Xan',
                           'faceIds': json.dumps([f'{i:0>50}' for i in range(300)])})
    persons.upsert_entity({'PartitionKey': 'u1', 'RowKey': 'Y', 'name': '',
                           'faceIds': json.dumps([f'z{i:0>49}' for i in range(300)])})
    assert app._merge_persons_core('u1', 'X', ['Y'])['mergeId']
    assert persons.get_entity('u1', 'Y')['groupId'] == 'X' and persons.get_entity('u1', 'Y')['name'] == 'Xan'
    assert len(json.loads(persons.get_entity('u1', 'X')['faceIds'])) == 300


def test_a_group_merge_adopts_the_name_of_the_largest_named_source(merge_env):
    persons, faces, blobs = merge_env
    persons.upsert_entity({**persons.get_entity('u1', 'S'), 'name': 'Asha'})
    app._merge_persons_core('u1', 'B', ['A'])
    assert persons.get_entity('u1', 'B')['name'] == 'Asha'
    assert persons.get_entity('u1', 'A')['name'] == 'Asha'


def test_merging_into_a_shard_is_refused(merge_env):
    with pytest.raises(app.PersonMergeUnsupported):
        app._merge_persons_core('u1', 'S', ['B'])
