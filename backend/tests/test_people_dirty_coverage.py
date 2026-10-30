"""Audit reproduction: a dirty manifest alone does not prove that supplied IDs
cover every source edit. Previously "fixed" by keeping refresh_user_people_index's
ID-targeted path gated on the manifest's blanket `dirty` flag -- but that flag is
set by _InvalidatingTableClient on every write, including the one that
produces affected_person_ids itself, so the gate was unconditionally true and
the ID-targeted path never actually ran (see
docs/benchmarks/people-index-incremental-guard-20261030.md). The real fix is
closing the gap at its source: every writer of person/face rows, including
the soft-delete/restore visibility path, must call mark_people_dirty with its
touched IDs, so the live dirty set (what affected_person_ids is built from in
production) is always complete on its own -- no blanket flag required."""
import app
import storage_utils
from tests.test_people_index import people_ctx, _seed_person, _seed_face


def test_visibility_change_is_tracked_so_a_later_targeted_refresh_sees_it(people_ctx, monkeypatch):
    people, faces, blobs = people_ctx
    user = 'pending-commit-audit'
    _seed_person(people, user, 'p1', name='Alice', faceIds='["f1"]')
    _seed_person(people, user, 'p2', name='Bob', faceIds='["f2"]')
    _seed_face(faces, user, 'f1', filename='one.jpg', personId='p1')
    _seed_face(faces, user, 'f2', filename='two.jpg', personId='p2')
    storage_utils.refresh_user_people_index(user, source_version='v1')

    dirty_people = {}
    monkeypatch.setattr(app, 'person_table_client', people)
    monkeypatch.setattr(app, 'face_table_client', faces)
    monkeypatch.setattr(app, 'person_members_table_client', None)
    monkeypatch.setattr(app, 'get_face_ids_for_filename', lambda uid, name: ['f2'])
    monkeypatch.setattr(app, 'mark_people_dirty',
        lambda uid, ids: dirty_people.setdefault(uid, set()).update(ids))
    # The real production soft-delete path -- not a hand-simulated manifest
    # touch -- is what must mark p2 dirty.
    app._set_faces_source_deleted_for_filenames(user, ['two.jpg'], True)
    assert dirty_people[user] == {'p2'}

    # A concurrent one-person label job for p1 reads the live dirty set,
    # which now also contains p2 from the visibility change above.
    _seed_person(people, user, 'p1', name='Beatrice', faceIds='["f1"]')
    affected = {'p1'} | dirty_people[user]
    snapshot = storage_utils.refresh_user_people_index(user, source_version='v2', affected_person_ids=affected)
    rows = {row['personId']: row for row in snapshot.rows}
    assert rows['p1']['name'] == 'Beatrice'
    assert rows['p2']['faceCount'] == 0


def test_targeted_people_refresh_still_uses_a_verified_clean_base(people_ctx, monkeypatch):
    people, faces, _ = people_ctx
    user = 'clean-targeted-audit'
    _seed_person(people, user, 'p1', name='Alice', faceIds='["f1"]')
    _seed_person(people, user, 'p2', name='Bob', faceIds='[]')
    _seed_face(faces, user, 'f1', filename='one.jpg', personId='p1')
    storage_utils.refresh_user_people_index(user, source_version='v1')
    _seed_person(people, user, 'p1', name='Beatrice', faceIds='["f1"]')

    def no_scan(*args, **kwargs):
        raise AssertionError('A verified clean base supports targeted point reads')

    monkeypatch.setattr(people, 'query_entities', no_scan)
    monkeypatch.setattr(faces, 'query_entities', no_scan)
    monkeypatch.setattr(storage_utils, '_build_user_people_index_snapshot', no_scan)
    snapshot = storage_utils.refresh_user_people_index(user, source_version='v2', affected_person_ids={'p1'})
    assert {row['personId']: row['name'] for row in snapshot.rows} == {'p1': 'Beatrice', 'p2': 'Bob'}
