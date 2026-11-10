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
        if pid == 'A':
            row['shardIds'] = json.dumps(['S'])       # A's overflow row, as the pointer records it
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


def test_marking_a_shard_face_not_a_face_uses_the_row_that_owns_it(monkeypatch):
    persons, faces, members = _world(monkeypatch)
    monkeypatch.setattr(app, '_rebuild_metadata_faces_for_filenames', lambda *a, **k: {})
    monkeypatch.setattr(app, '_resync_person_rule_albums', lambda *a, **k: None)
    response = app.app.test_client().post('/api/persons/A/not-face', json={'faceId': 'f3'})
    assert response.status_code == 200, response.get_json()
    assert ('u1', 'S') not in persons.rows          # the shard's only face left, so the empty row went with it
    assert json.loads(persons.get_entity('u1', 'A')['faceIds']) == ['f1', 'f2']


def test_deleting_a_grouped_person_deletes_its_shards(monkeypatch):
    persons, faces, members = _world(monkeypatch)
    monkeypatch.setattr(app, '_rebuild_metadata_faces_for_filenames', lambda *a, **k: {})
    monkeypatch.setattr(app, '_resync_person_rule_albums', lambda *a, **k: None)
    monkeypatch.setattr(app, '_remove_person_member', lambda *a, **k: None)
    app._person_light_scan_cache.invalidate('u1')
    result = app._delete_person_cluster('u1', 'A')
    assert result['deleted'] and result['facesUpdated'] == 3
    assert ('u1', 'A') not in persons.rows and ('u1', 'S') not in persons.rows and ('u1', 'B') in persons.rows
    assert all(faces.get_entity('u1', fid).get('rejected') for fid in ('f1', 'f2', 'f3'))


def test_group_roots_map_each_shard_to_its_first_person(monkeypatch):
    persons, faces, members = _world(monkeypatch)
    persons.upsert_entity({**persons.get_entity('u1', 'A'), 'name': 'Asha'})
    persons.upsert_entity({**persons.get_entity('u1', 'S'), 'name': 'Asha'})
    app._person_light_scan_cache.invalidate('u1')
    assert app._person_group_roots('u1') == {'S': 'A'}


def test_a_photos_people_ids_name_the_first_person_for_a_shard_face(monkeypatch):
    persons, faces, members = _world(monkeypatch)
    metadata = FakeTable()
    metadata.upsert_entity({'PartitionKey': 'u1', 'RowKey': 'f3.jpg', 'faces': '[]', 'faceCount': 0, 'peopleIds': '[]'})
    monkeypatch.setattr(app, 'metadata_table_client', metadata)
    monkeypatch.setattr(app, 'get_face_ids_for_filename', lambda uid, fn: ['f3'])
    updates = []
    monkeypatch.setattr(app, '_update_metadata_entity_fields', lambda uid, fn, fields, **kw: updates.append(fields))
    app._person_light_scan_cache.invalidate('u1')
    app._rebuild_metadata_faces_for_filename('u1', 'f3.jpg', searchable_person_index={'S': 'Asha', 'A': 'Asha'})
    assert updates and json.loads(updates[0]['peopleIds']) == ['A']


def _person_with(persons, pid, count, name=''):
    persons.upsert_entity({'PartitionKey': 'u1', 'RowKey': pid, 'name': name,
                           'faceIds': json.dumps([f'{pid}{i}' for i in range(count)])})


def test_a_big_merge_that_still_fits_one_row_is_done_by_retagging(merge_env, monkeypatch):
    persons, faces, blobs = merge_env
    monkeypatch.setattr(app, 'GROUP_MERGE_MIN_FACES', 150)
    _person_with(persons, 'P', 20, 'Pia')
    _person_with(persons, 'Q', 150)
    assert app._merge_persons_core('u1', 'P', ['Q'])['mergeId']
    assert persons.get_entity('u1', 'Q')['groupId'] == 'P'          # kept as a row of P's group
    assert len(json.loads(persons.get_entity('u1', 'P')['faceIds'])) == 20


def test_a_small_merge_still_moves_the_faces(merge_env, monkeypatch):
    persons, faces, blobs = merge_env
    monkeypatch.setattr(app, 'GROUP_MERGE_MIN_FACES', 150)
    _person_with(persons, 'P', 20)
    _person_with(persons, 'Q', 149)
    _person_with(persons, 'R', 150)
    rows = lambda *ids: [persons.get_entity('u1', pid) for pid in ids]
    assert app._merge_needs_group_mode('u1', rows('P', 'Q')) is False
    assert app._merge_needs_group_mode('u1', rows('P', 'R')) is True


def test_a_group_merge_does_not_rewrite_photo_metadata_inside_the_request(merge_env, monkeypatch):
    persons, faces, blobs = merge_env
    inline = []
    monkeypatch.setattr(app, '_rebuild_metadata_faces_for_filenames', lambda *a, **k: inline.append(a) or {})
    persons.upsert_entity({**persons.get_entity('u1', 'S'), 'name': 'Asha'})
    app._merge_persons_core('u1', 'B', ['A'])
    assert inline == []
    for pid in ('A', 'S', 'B'):
        assert persons.get_entity('u1', pid)['name'] == 'Asha'
    assert persons.get_entity('u1', 'A')['labelPending']             # the worker publishes the new name


def test_undo_restores_names_through_the_label_path_too(merge_env):
    persons, faces, blobs = merge_env
    persons.upsert_entity({**persons.get_entity('u1', 'A'), 'name': 'Anu'})
    merge_id = app._merge_persons_core('u1', 'B', ['A'])['mergeId']
    assert app.app.test_client().post(f'/api/persons/merge/{merge_id}/undo').status_code == 200
    assert persons.get_entity('u1', 'A')['name'] == 'Anu'


def _full_person(persons, pid='FULL', name='Fay'):
    persons.upsert_entity({'PartitionKey': 'u1', 'RowKey': pid, 'name': name,
                           'faceIds': json.dumps([f'{i:0>50}' for i in range(1250)])})


def _loose_face(faces, face_id):
    faces.upsert_entity({'PartitionKey': 'u1', 'RowKey': face_id, 'filename': f'{face_id}.jpg', 'personId': '',
                         'confidence': 0.5, 'rejected': False, 'bbox': '{}', 'embedding': json.dumps([0.1, 0.2])})


def test_a_person_with_room_keeps_the_face_on_its_own_row(merge_env):
    persons, faces, blobs = merge_env
    _loose_face(faces, 'n1')
    assert app._owner_row_for_new_face('u1', 'B', 'n1') == 'B'


def test_a_full_person_gets_an_overflow_row_for_the_face(merge_env):
    persons, faces, blobs = merge_env
    _full_person(persons)
    _loose_face(faces, 'n1')
    _loose_face(faces, 'n2')
    shard = app._owner_row_for_new_face('u1', 'FULL', 'n1')
    assert shard != 'FULL'
    row = persons.get_entity('u1', shard)
    assert row['groupId'] == 'FULL' and row['name'] == 'Fay'
    assert app._person_group_ids('u1', 'FULL') == ['FULL', shard]
    assert app._owner_row_for_new_face('u1', 'FULL', 'n2') == shard       # the next face reuses the room there
    persons.upsert_entity({**persons.get_entity('u1', shard), 'faceIds': json.dumps(['n1'])})
    assert app._owner_row_for_new_face('u1', 'FULL', 'n1') == shard       # a face already in the group stays put


def test_confirming_a_face_into_a_full_person_lands_on_its_overflow_row(merge_env, monkeypatch):
    persons, faces, blobs = merge_env
    _full_person(persons)
    _loose_face(faces, 'n1')
    added = []
    monkeypatch.setattr(app, '_remove_face_from_person', lambda *a, **k: None)
    monkeypatch.setattr(app, '_remove_face_from_other_people', lambda *a, **k: None)
    monkeypatch.setattr(app, '_add_face_to_person', lambda user, pid, fid, *a, **k: added.append((pid, fid)) or True)
    monkeypatch.setattr(app, '_update_person_rep_embedding', lambda *a, **k: None)
    monkeypatch.setattr(app, '_rebuild_metadata_faces_for_filename', lambda *a, **k: None)
    response = app.app.test_client().post('/api/persons/FULL/confirm-face', json={'faceId': 'n1'})
    assert response.status_code == 200
    (shard, face_id), = added
    assert face_id == 'n1' and shard != 'FULL' and persons.get_entity('u1', shard)['groupId'] == 'FULL'
    assert faces.get_entity('u1', 'n1')['personId'] == shard


def test_finding_a_persons_overflow_rows_never_scans_the_person_table(merge_env, monkeypatch):
    persons, faces, blobs = merge_env
    monkeypatch.setattr(persons, 'query_entities', lambda *a, **k: (_ for _ in ()).throw(AssertionError('scan')),
                        raising=False)
    assert app._person_group_ids('u1', 'A') == ['A', 'S']
    assert app._person_shard_ids('u1', 'B') == [] and app._person_shard_ids('u1', 'S') == []


def test_a_group_merge_records_the_joined_rows_and_undo_restores_the_pointers(merge_env):
    persons, faces, blobs = merge_env
    merge_id = app._merge_persons_core('u1', 'B', ['A'])['mergeId']
    assert json.loads(persons.get_entity('u1', 'B')['shardIds']) == ['A', 'S']
    assert not persons.get_entity('u1', 'A').get('shardIds')          # A is no longer a first person
    assert app._person_group_ids('u1', 'B') == ['B', 'A', 'S']
    assert app.app.test_client().post(f'/api/persons/merge/{merge_id}/undo').status_code == 200
    assert not persons.get_entity('u1', 'B').get('shardIds')
    assert json.loads(persons.get_entity('u1', 'A')['shardIds']) == ['S']
    assert app._person_group_ids('u1', 'B') == ['B'] and app._person_group_ids('u1', 'A') == ['A', 'S']


def test_a_new_overflow_row_is_recorded_on_the_first_person(merge_env):
    persons, faces, blobs = merge_env
    _full_person(persons)
    _loose_face(faces, 'n1')
    shard = app._owner_row_for_new_face('u1', 'FULL', 'n1')
    assert json.loads(persons.get_entity('u1', 'FULL')['shardIds']) == [shard]


def test_refresh_adds_overflow_rows_clustering_created(merge_env):
    persons, faces, blobs = merge_env
    persons.upsert_entity({'PartitionKey': 'u1', 'RowKey': 'S2', 'name': '', 'faceIds': '[]', 'groupId': 'A'})
    assert list(app._refresh_group_pointers('u1', dry_run=True)) == ['A']
    assert json.loads(persons.get_entity('u1', 'A')['shardIds']) == ['S']       # a dry run writes nothing
    app._refresh_group_pointers('u1')
    assert json.loads(persons.get_entity('u1', 'A')['shardIds']) == ['S', 'S2']
    assert app._refresh_group_pointers('u1') == {}


def test_faces_that_do_not_fit_move_to_overflow_rows_the_group_owns(merge_env, monkeypatch):
    persons, faces, blobs = merge_env
    members = app.person_members_table_client
    persons.upsert_entity({'PartitionKey': 'u1', 'RowKey': 'FULL', 'name': 'Fay', 'faceIds': '[]'})
    ids = [f'g{i:03d}' for i in range(5)]
    for fid in ids:
        faces.upsert_entity({'PartitionKey': 'u1', 'RowKey': fid, 'filename': f'{fid}.jpg', 'personId': 'FULL',
                             'confidence': 1.0, 'rejected': False, 'embedding': json.dumps([0.1, 0.2])})
        members.upsert_entity({'PartitionKey': 'FULL', 'RowKey': fid, 'filename': f'{fid}.jpg'})
    monkeypatch.setattr(app, 'OVERFLOW_ROW_FACES', 2)
    monkeypatch.setattr(app, '_batch_upsert_entities',
                        lambda table, rows, **k: [table.upsert_entity(r, mode='merge') for r in rows])
    monkeypatch.setattr(app, '_sync_person_members', lambda entity: [
        members.upsert_entity({'PartitionKey': entity['RowKey'], 'RowKey': f, 'filename': f'{f}.jpg'})
        for f in json.loads(entity['faceIds'])])
    monkeypatch.setattr(app, '_remove_person_member', lambda pid, fid: members.delete_entity('' + pid, fid))
    made = app._move_faces_to_overflow_rows('u1', 'FULL', ids)
    assert len(made) == 3
    assert sorted(f for sid in made for f in json.loads(persons.get_entity('u1', sid)['faceIds'])) == ids
    assert {faces.get_entity('u1', f)['personId'] for f in ids} == set(made)
    assert all(persons.get_entity('u1', sid)['groupId'] == 'FULL' and persons.get_entity('u1', sid)['name'] == 'Fay'
               for sid in made)
    assert json.loads(persons.get_entity('u1', 'FULL')['shardIds']) == sorted(made)
    assert app._person_group_ids('u1', 'FULL') == ['FULL', *sorted(made)]
