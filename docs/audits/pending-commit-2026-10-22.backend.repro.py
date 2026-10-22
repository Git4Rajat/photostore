"""Audit reproduction: a dirty manifest does not prove that supplied IDs cover every source edit."""
import storage_utils
from tests.test_people_index import people_ctx, _seed_person, _seed_face


def test_targeted_label_refresh_does_not_clear_unrelated_visibility_change(people_ctx):
    people, faces, blobs = people_ctx
    user = 'pending-commit-audit'
    _seed_person(people, user, 'p1', name='Alice', faceIds='["f1"]')
    _seed_person(people, user, 'p2', name='Bob', faceIds='["f2"]')
    _seed_face(faces, user, 'f1', filename='one.jpg', personId='p1')
    _seed_face(faces, user, 'f2', filename='two.jpg', personId='p2')
    storage_utils.refresh_user_people_index(user, source_version='v1')
    # Trash/restore writes mark the People manifest dirty without marking per-person dirty IDs.
    _seed_face(faces, user, 'f2', filename='two.jpg', personId='p2', sourceDeleted=True)
    storage_utils.touch_user_people_index_state(user)
    # A concurrent one-person label job knows only p1.
    _seed_person(people, user, 'p1', name='Beatrice', faceIds='["f1"]')
    snapshot = storage_utils.refresh_user_people_index(user, source_version='v2', affected_person_ids={'p1'})
    rows = {row['personId']: row for row in snapshot.rows}
    assert rows['p1']['name'] == 'Beatrice'
    expected = storage_utils._build_user_people_index_snapshot(user, 'source-check')
    assert {row['personId']: row for row in expected.rows}['p2']['faceCount'] == 0
    assert storage_utils._load_people_index_manifest(user)['dirty'] is False
    assert rows['p2']['faceCount'] == 0
