"""Live person albums: a person-rule album keeps following its person through
merges, renames, and clustering instead of staying the one-time snapshot every
other smart-album type still is. Mirrors test_merge_persons_core.py's fixture
style for the merge-pipeline cases.
"""
from __future__ import annotations

import json

import app
import storage_utils
from routes import albums, people
from tests.fakes import FakeTable


def _seed_person(table: FakeTable, user_id: str, person_id: str, name: str = '', face_ids=None) -> None:
    table.upsert_entity({'PartitionKey': user_id, 'RowKey': person_id, 'name': name,
                          'faceIds': json.dumps(face_ids or [])})


def _seed_face(table: FakeTable, user_id: str, face_id: str, filename: str, person_id: str = '') -> None:
    table.upsert_entity({'PartitionKey': user_id, 'RowKey': face_id, 'filename': filename,
                          'personId': person_id, 'confidence': 0.9, 'rejected': False})


def _seed_rule_album(table: FakeTable, user_id: str, album_id: str, person_id: str, label: str, filenames=None) -> dict:
    entity = {'PartitionKey': user_id, 'RowKey': album_id,
              'name': app.PERSON_ALBUM_NAME_TEMPLATE.format(label),
              'ruleType': 'person', 'rulePersonId': person_id, 'ruleLabelSnapshot': label}
    app.album_store.set_all(entity, filenames or [])
    table.upsert_entity(entity)
    return entity


def _world(monkeypatch):
    """Same shape as test_merge_persons_core.py's _world, plus an albums table."""
    persons, faces, albums_table, merges = FakeTable(), FakeTable(), FakeTable(), FakeTable()
    monkeypatch.setattr(app, 'person_table_client', persons)
    monkeypatch.setattr(app, 'face_table_client', faces)
    monkeypatch.setattr(app, 'albums_table_client', albums_table)
    monkeypatch.setattr(app, 'merge_table_client', merges)
    monkeypatch.setattr(app, 'person_members_table_client', None)
    blobs: dict = {}
    monkeypatch.setattr(app, 'upload_file_to_blob',
                         lambda container, filename, content, content_type: blobs.__setitem__((container, filename), content))
    monkeypatch.setattr(app, 'download_file_from_blob', lambda container, filename: blobs[(container, filename)])
    monkeypatch.setattr(app, '_update_person_rep_embedding', lambda *a, **k: [])
    monkeypatch.setattr(app, '_rebuild_metadata_faces_for_filenames', lambda uid, names, **k: {'files': list(names)})
    monkeypatch.setattr(app, '_update_person_entity',
                         lambda uid, pid, updates: persons.upsert_entity({**persons.get_entity(uid, pid), **updates}) or True)
    app._face_summary_scan_cache.invalidate('u1')
    return persons, faces, albums_table


# --- 1. Creation stamps rule fields ------------------------------------------

def test_autocreate_person_album_stamps_rule_fields(monkeypatch):
    persons, faces, albums_table = _world(monkeypatch)
    _seed_person(persons, 'u1', 'A', name='Asha')
    rows = [{'RowKey': 'a.jpg', 'peopleIds': json.dumps(['A'])}]
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('u1', None))
    monkeypatch.setattr(app, '_albums_table_available', lambda: True)
    import search_db
    monkeypatch.setattr(search_db, 'open_database', lambda uid, **k: None)
    monkeypatch.setattr(app, '_iter_metadata_rows_for_user', lambda *a, **k: iter(rows))
    with app.app.test_request_context('/api/albums/autocreate', method='POST', json={'rule': 'person'}):
        payload = albums.autocreate_albums().get_json()
    assert payload['count'] == 1
    saved = albums_table.rows[('u1', payload['album']['id'])]
    assert saved['ruleType'] == 'person'
    assert saved['rulePersonId'] == 'A'
    assert saved['ruleLabelSnapshot'] == 'Asha'
    assert payload['album']['rulePersonId'] == 'A'


# --- 2/3. Merge redirect + resync, both directions ---------------------------

def test_merge_drains_the_merged_away_persons_live_album(monkeypatch):
    persons, faces, albums_table = _world(monkeypatch)
    _seed_person(persons, 'u1', 'A', name='Asha', face_ids=['f1'])
    _seed_person(persons, 'u1', 'B', name='Bo', face_ids=['f2'])
    _seed_face(faces, 'u1', 'f1', 'a.jpg', 'A')
    _seed_face(faces, 'u1', 'f2', 'b.jpg', 'B')
    _seed_rule_album(albums_table, 'u1', 'al1', 'A', 'Asha', ['a.jpg'])
    result = app._merge_persons_core('u1', 'B', ['A'])   # A merged into B -- B survives
    assert result and result['mergeId']
    al = albums_table.rows[('u1', 'al1')]
    # al1 followed A, which no longer exists after the merge -- it's drained
    # rather than redirected onto the survivor, so it goes empty instead of
    # duplicating whatever album B might already have.
    assert al['rulePersonId'] == 'A'
    assert app.album_store.all_names(al) == []


def test_merge_resyncs_live_album_already_following_the_survivor_b_into_a(monkeypatch):
    persons, faces, albums_table = _world(monkeypatch)
    _seed_person(persons, 'u1', 'A', name='Asha', face_ids=['f1'])
    _seed_person(persons, 'u1', 'B', name='Bo', face_ids=['f2'])
    _seed_face(faces, 'u1', 'f1', 'a.jpg', 'A')
    _seed_face(faces, 'u1', 'f2', 'b.jpg', 'B')
    _seed_rule_album(albums_table, 'u1', 'al1', 'A', 'Asha', ['a.jpg'])   # already following the survivor
    result = app._merge_persons_core('u1', 'A', ['B'])   # B merged into A -- A survives
    assert result and result['mergeId']
    al = albums_table.rows[('u1', 'al1')]
    assert al['rulePersonId'] == 'A'
    assert sorted(app.album_store.all_names(al)) == ['a.jpg', 'b.jpg']


# --- 4. Rename updates name only when untouched -------------------------------

def test_rename_updates_live_album_name_when_untouched(monkeypatch):
    persons, faces, albums_table = _world(monkeypatch)
    _seed_person(persons, 'u1', 'A', name='Asha')
    _seed_rule_album(albums_table, 'u1', 'al1', 'A', 'Asha')
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('u1', None))
    monkeypatch.setattr(app, '_people_features_available', lambda: True)
    monkeypatch.setattr(app, 'publish_person_label_intent', lambda *a, **k: True)
    with app.app.test_request_context('/api/persons/A/label', method='POST', json={'name': 'Asha Verma'}):
        response = people.label_person('A')
    assert response.get_json()['success'] is True
    al = albums_table.rows[('u1', 'al1')]
    assert al['name'] == 'Person: Asha Verma'
    assert al['ruleLabelSnapshot'] == 'Asha Verma'


def test_rename_leaves_a_user_customized_album_name_alone(monkeypatch):
    persons, faces, albums_table = _world(monkeypatch)
    _seed_person(persons, 'u1', 'A', name='Asha')
    entity = _seed_rule_album(albums_table, 'u1', 'al1', 'A', 'Asha')
    entity['name'] = 'Family Trip'   # the user manually renamed the album
    albums_table.upsert_entity(entity)
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('u1', None))
    monkeypatch.setattr(app, '_people_features_available', lambda: True)
    monkeypatch.setattr(app, 'publish_person_label_intent', lambda *a, **k: True)
    with app.app.test_request_context('/api/persons/A/label', method='POST', json={'name': 'Asha Verma'}):
        people.label_person('A')
    al = albums_table.rows[('u1', 'al1')]
    assert al['name'] == 'Family Trip'          # left alone
    assert al['ruleLabelSnapshot'] == 'Asha'    # untouched too, since nothing was overwritten


# --- 5. Clustering add triggers resync ---------------------------------------

def test_clustering_add_pulls_in_a_newly_matched_photo(monkeypatch):
    persons, faces, albums_table = _world(monkeypatch)
    _seed_person(persons, 'u1', 'A', name='Asha', face_ids=['f1', 'f2'])
    _seed_face(faces, 'u1', 'f1', 'a.jpg', 'A')
    _seed_face(faces, 'u1', 'f2', 'b.jpg', 'A')
    _seed_rule_album(albums_table, 'u1', 'al1', 'A', 'Asha', ['a.jpg'])   # stale -- missing b.jpg
    app._resync_person_rule_albums_for_many('u1', {'A'})
    al = albums_table.rows[('u1', 'al1')]
    assert sorted(app.album_store.all_names(al)) == ['a.jpg', 'b.jpg']


# --- 6. Resync drops a non-matching photo, including a manual addition -------
# ("resync always wins" -- product decision 1)

def test_resync_drops_a_photo_no_longer_matching_including_a_manual_addition(monkeypatch):
    persons, faces, albums_table = _world(monkeypatch)
    _seed_person(persons, 'u1', 'A', name='Asha', face_ids=['f1'])
    _seed_face(faces, 'u1', 'f1', 'a.jpg', 'A')
    _seed_rule_album(albums_table, 'u1', 'al1', 'A', 'Asha', ['a.jpg', 'manually-added.jpg'])
    app._resync_person_rule_albums('u1', 'A')
    al = albums_table.rows[('u1', 'al1')]
    assert app.album_store.all_names(al) == ['a.jpg']


# --- 7. Existence-check short circuit -----------------------------------------

def test_resync_makes_zero_person_reads_when_no_live_album_follows(monkeypatch):
    persons, faces, albums_table = _world(monkeypatch)
    calls = []
    real_get = persons.get_entity
    persons.get_entity = lambda *a, **k: (calls.append(1), real_get(*a, **k))[1]
    app._resync_person_rule_albums('u1', 'nobody-follows-this-id')
    assert calls == []


# --- 8. Invalidation fix -------------------------------------------------------

def test_people_only_metadata_update_does_not_dirty_albums(monkeypatch):
    meta = FakeTable()
    meta.upsert_entity({'PartitionKey': 'u1', 'RowKey': 'a.jpg', 'processing_state': 'active'})
    monkeypatch.setattr(app, 'metadata_table_client', meta)
    monkeypatch.setattr(storage_utils, 'DURABLE_INDEX_UPDATES', False)
    touched_albums = []
    monkeypatch.setattr(storage_utils, 'touch_user_albums_index_state', lambda uid: touched_albums.append(uid))
    app._update_metadata_entity_fields('u1', 'a.jpg', {'peopleIds': json.dumps(['A'])})
    assert touched_albums == []


def test_rating_only_metadata_update_still_dirties_albums(monkeypatch):
    meta = FakeTable()
    meta.upsert_entity({'PartitionKey': 'u1', 'RowKey': 'a.jpg', 'processing_state': 'active'})
    monkeypatch.setattr(app, 'metadata_table_client', meta)
    monkeypatch.setattr(storage_utils, 'DURABLE_INDEX_UPDATES', False)
    touched_albums = []
    monkeypatch.setattr(storage_utils, 'touch_user_albums_index_state', lambda uid: touched_albums.append(uid))
    app._update_metadata_entity_fields('u1', 'a.jpg', {'rating': 5})
    assert touched_albums == ['u1']


# --- 9. Post-undo-merge resync -------------------------------------------------

def test_undo_merge_resyncs_survivor_to_its_restored_face_set(monkeypatch):
    persons, faces, albums_table = _world(monkeypatch)
    _seed_person(persons, 'u1', 'A', name='Asha', face_ids=['f1'])
    _seed_person(persons, 'u1', 'B', name='Bo', face_ids=['f2'])
    _seed_face(faces, 'u1', 'f1', 'a.jpg', 'A')
    _seed_face(faces, 'u1', 'f2', 'b.jpg', 'B')
    _seed_rule_album(albums_table, 'u1', 'al1', 'A', 'Asha', ['a.jpg'])
    merge_result = app._merge_persons_core('u1', 'A', ['B'])   # B merged into A
    al = albums_table.rows[('u1', 'al1')]
    assert sorted(app.album_store.all_names(al)) == ['a.jpg', 'b.jpg']   # gained b.jpg

    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('u1', None))
    monkeypatch.setattr(app, '_people_features_available', lambda: True)
    with app.app.test_request_context(f"/api/persons/merges/{merge_result['mergeId']}/undo", method='POST'):
        response = people.undo_merge(merge_result['mergeId'])
    assert response.get_json()['success'] is True
    al = albums_table.rows[('u1', 'al1')]
    # Does not reverse the redirect (there was none here -- the album already
    # followed the survivor A), but membership reflects A's restored faceIds.
    assert app.album_store.all_names(al) == ['a.jpg']


def test_undo_merge_repopulates_the_merged_away_persons_drained_album(monkeypatch):
    persons, faces, albums_table = _world(monkeypatch)
    _seed_person(persons, 'u1', 'A', name='Asha', face_ids=['f1'])
    _seed_person(persons, 'u1', 'B', name='Bo', face_ids=['f2'])
    _seed_face(faces, 'u1', 'f1', 'a.jpg', 'A')
    _seed_face(faces, 'u1', 'f2', 'b.jpg', 'B')
    _seed_rule_album(albums_table, 'u1', 'alB', 'B', 'Bo', ['b.jpg'])
    merge_result = app._merge_persons_core('u1', 'A', ['B'])   # B merged into A -- alB drains
    assert app.album_store.all_names(albums_table.rows[('u1', 'alB')]) == []

    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('u1', None))
    monkeypatch.setattr(app, '_people_features_available', lambda: True)
    with app.app.test_request_context(f"/api/persons/merges/{merge_result['mergeId']}/undo", method='POST'):
        response = people.undo_merge(merge_result['mergeId'])
    assert response.get_json()['success'] is True
    # B is restored with its original faceIds, so its album picks b.jpg back up.
    assert app.album_store.all_names(albums_table.rows[('u1', 'alB')]) == ['b.jpg']


# --- 10. Resync does not clobber a concurrent share revocation -----------------

def test_resync_does_not_overwrite_a_concurrent_share_revocation(monkeypatch):
    persons, faces, albums_table = _world(monkeypatch)
    _seed_person(persons, 'u1', 'A', name='Asha', face_ids=['f1', 'f2'])
    _seed_face(faces, 'u1', 'f1', 'a.jpg', 'A')
    _seed_face(faces, 'u1', 'f2', 'b.jpg', 'A')
    entity = _seed_rule_album(albums_table, 'u1', 'al1', 'A', 'Asha', ['a.jpg'])   # stale -- missing b.jpg
    entity['isPublic'] = True
    entity['publicToken'] = 'tok123'
    albums_table.upsert_entity(entity)

    real_get = albums_table.get_entity
    calls = []

    def _get_with_concurrent_revoke(partition_key, row_key):
        result = real_get(partition_key, row_key)
        if not calls:
            # Simulate another request's share-revoke completing in the gap
            # between our read and our write.
            revoked = dict(albums_table.rows[(partition_key, row_key)])
            revoked['isPublic'] = False
            revoked['publicToken'] = ''
            albums_table.upsert_entity(revoked)
        calls.append(1)
        return result

    albums_table.get_entity = _get_with_concurrent_revoke
    app._resync_person_rule_albums('u1', 'A')
    al = albums_table.rows[('u1', 'al1')]
    assert al['isPublic'] is False and al['publicToken'] == ''        # revocation survives
    assert sorted(app.album_store.all_names(al)) == ['a.jpg', 'b.jpg']   # membership change still applied
    assert len(calls) >= 2   # a retry actually happened


# --- 11. A transient person-read failure preserves membership ------------------

def test_resync_preserves_membership_on_a_transient_person_read_failure(monkeypatch):
    persons, faces, albums_table = _world(monkeypatch)
    _seed_person(persons, 'u1', 'A', name='Asha', face_ids=['f1'])
    _seed_face(faces, 'u1', 'f1', 'a.jpg', 'A')
    _seed_rule_album(albums_table, 'u1', 'al1', 'A', 'Asha', ['a.jpg', 'b.jpg'])

    def _flaky_get_entity(partition_key, row_key):
        raise TimeoutError('transient network blip')

    persons.get_entity = _flaky_get_entity
    app._resync_person_rule_albums('u1', 'A')
    al = albums_table.rows[('u1', 'al1')]
    assert sorted(app.album_store.all_names(al)) == ['a.jpg', 'b.jpg']   # untouched, not drained


def test_resync_drains_the_album_when_the_person_is_genuinely_gone(monkeypatch):
    persons, faces, albums_table = _world(monkeypatch)
    _seed_rule_album(albums_table, 'u1', 'al1', 'A', 'Asha', ['a.jpg'])   # person 'A' was never seeded -- 404
    app._resync_person_rule_albums('u1', 'A')
    al = albums_table.rows[('u1', 'al1')]
    assert app.album_store.all_names(al) == []


# --- 12. Face confirmation/rejection resync live albums -------------------------

def test_confirm_face_resyncs_both_old_and_new_person_albums(monkeypatch):
    persons, faces, albums_table = _world(monkeypatch)
    _seed_person(persons, 'u1', 'A', name='Asha', face_ids=['f1'])
    _seed_person(persons, 'u1', 'B', name='Bo', face_ids=[])
    _seed_face(faces, 'u1', 'f1', 'a.jpg', 'A')
    _seed_rule_album(albums_table, 'u1', 'alA', 'A', 'Asha', ['a.jpg'])
    _seed_rule_album(albums_table, 'u1', 'alB', 'B', 'Bo', [])
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('u1', None))
    monkeypatch.setattr(app, '_people_features_available', lambda: True)
    with app.app.test_request_context('/api/persons/B/confirm-face', method='POST', json={'faceId': 'f1'}):
        response = people.confirm_face('B')
    assert response.get_json()['success'] is True
    assert app.album_store.all_names(albums_table.rows[('u1', 'alA')]) == []     # old person's album lost it
    assert app.album_store.all_names(albums_table.rows[('u1', 'alB')]) == ['a.jpg']   # new person's album gained it


def test_mark_face_not_a_face_triggers_resync(monkeypatch):
    persons, faces, albums_table = _world(monkeypatch)
    _seed_person(persons, 'u1', 'A', name='Asha', face_ids=['f1', 'f2'])
    _seed_face(faces, 'u1', 'f1', 'a.jpg', 'A')
    _seed_face(faces, 'u1', 'f2', 'b.jpg', 'A')
    _seed_rule_album(albums_table, 'u1', 'al1', 'A', 'Asha', ['a.jpg', 'b.jpg'])
    result = app._mark_face_not_a_face('u1', 'A', 'f2')
    assert result['success'] is True
    al = albums_table.rows[('u1', 'al1')]
    assert app.album_store.all_names(al) == ['a.jpg']


def test_delete_faces_bulk_triggers_resync(monkeypatch):
    persons, faces, albums_table = _world(monkeypatch)
    _seed_person(persons, 'u1', 'A', name='Asha', face_ids=['f1', 'f2'])
    _seed_face(faces, 'u1', 'f1', 'a.jpg', 'A')
    _seed_face(faces, 'u1', 'f2', 'b.jpg', 'A')
    _seed_rule_album(albums_table, 'u1', 'al1', 'A', 'Asha', ['a.jpg', 'b.jpg'])
    result = app._delete_faces_bulk('u1', ['f2'])
    assert result['deleted'] == ['f2']
    al = albums_table.rows[('u1', 'al1')]
    assert app.album_store.all_names(al) == ['a.jpg']


# --- 13. Deleting a person drains (rather than orphans with stale photos) its live album ---

def test_delete_person_cluster_drains_its_live_album(monkeypatch):
    persons, faces, albums_table = _world(monkeypatch)
    _seed_person(persons, 'u1', 'A', name='Asha', face_ids=['f1'])
    _seed_face(faces, 'u1', 'f1', 'a.jpg', 'A')
    _seed_rule_album(albums_table, 'u1', 'al1', 'A', 'Asha', ['a.jpg'])
    result = app._delete_person_cluster('u1', 'A')
    assert result['deleted'] is True
    al = albums_table.rows[('u1', 'al1')]
    assert app.album_store.all_names(al) == []


# --- 14. Merging two people who each have their own live album drains the loser, not duplicates it ---

def test_merge_with_live_albums_on_both_sides_drains_the_losers(monkeypatch):
    persons, faces, albums_table = _world(monkeypatch)
    _seed_person(persons, 'u1', 'A', name='Asha', face_ids=['f1'])
    _seed_person(persons, 'u1', 'B', name='Bo', face_ids=['f2'])
    _seed_face(faces, 'u1', 'f1', 'a.jpg', 'A')
    _seed_face(faces, 'u1', 'f2', 'b.jpg', 'B')
    _seed_rule_album(albums_table, 'u1', 'alA', 'A', 'Asha', ['a.jpg'])
    _seed_rule_album(albums_table, 'u1', 'alB', 'B', 'Bo', ['b.jpg'])
    result = app._merge_persons_core('u1', 'A', ['B'])   # B merged into A -- A survives
    assert result and result['mergeId']
    al_a = albums_table.rows[('u1', 'alA')]
    al_b = albums_table.rows[('u1', 'alB')]
    assert al_a['rulePersonId'] == 'A'
    assert sorted(app.album_store.all_names(al_a)) == ['a.jpg', 'b.jpg']
    assert al_b['rulePersonId'] == 'B'
    assert app.album_store.all_names(al_b) == []
