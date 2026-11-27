"""Marking a photo's faces deleted/restored must never re-create a removed membership row.

``photopersonmembers`` rows carry ``userId`` when the wave assignment or the dual-write creates them. The
source-deleted marker used to upsert ``filename``/``sourceDeleted`` onto the row, so a row removed earlier came
back with no ``userId``. The next wave that created the same membership (same photos uploaded again after a
clean) found that row, read it as another library's and failed its cluster stage on every retry.
"""
import app
import person_photos
from tests.fakes import FakeTable

USER, PERSON, FACE = 'u1', 'p1', 'face-v1-aa'


def _setup(monkeypatch, members):
    faces = FakeTable()
    faces.upsert_entity({'PartitionKey': USER, 'RowKey': FACE, 'filename': 'a.jpg', 'personId': PERSON})
    monkeypatch.setattr(app, 'face_table_client', faces)
    monkeypatch.setattr(app, 'person_members_table_client', members)
    monkeypatch.setattr(app, 'person_table_client', None)
    monkeypatch.setattr(app, 'get_face_ids_for_filename', lambda user, name: [FACE])
    monkeypatch.setattr(app, 'touch_user_people_index_state', lambda *a, **k: None)
    monkeypatch.setattr(app, 'mark_people_dirty', lambda *a, **k: None)
    return faces


def test_marker_does_not_recreate_a_removed_member_row(monkeypatch):
    members = FakeTable()
    _setup(monkeypatch, members)

    result = app._set_faces_source_deleted_for_filenames(USER, ['a.jpg'], True)

    assert members.rows == {}
    assert result['facesUpdated'] == 1 and result['membersUpdated'] == 0


def test_marker_still_updates_an_existing_member_row_and_keeps_its_user(monkeypatch):
    members = FakeTable()
    members.upsert_entity({'PartitionKey': PERSON, 'RowKey': FACE, 'userId': USER, 'addedAt': 't'})
    _setup(monkeypatch, members)

    result = app._set_faces_source_deleted_for_filenames(USER, ['a.jpg'], True)
    row = members.rows[(PERSON, FACE)]
    assert result['membersUpdated'] == 1
    assert (row['userId'], row['filename'], row['sourceDeleted']) == (USER, 'a.jpg', '1')

    app._set_faces_source_deleted_for_filenames(USER, ['a.jpg'], False)
    row = members.rows[(PERSON, FACE)]
    assert (row['userId'], row['sourceDeleted']) == (USER, '')


def test_person_photos_hydrate_does_not_recreate_a_row_removed_after_the_query():
    members, faces = FakeTable(), FakeTable()
    members.upsert_entity({'PartitionKey': PERSON, 'RowKey': FACE, 'userId': USER})
    faces.upsert_entity({'PartitionKey': USER, 'RowKey': FACE, 'filename': 'a.jpg', 'personId': PERSON})
    real_get = faces.get_entity

    def get_then_remove(partition_key, row_key, **kwargs):
        row = real_get(partition_key, row_key, **kwargs)
        members.delete_entity(PERSON, FACE)       # another writer removes the member between query and write-back
        return row

    faces.get_entity = get_then_remove

    names = person_photos.ranked_filenames(USER, PERSON, members, faces, use_cache=False)

    assert names == ['a.jpg']                      # still usable this time
    assert members.rows == {}                      # and nothing was re-created without its userId
