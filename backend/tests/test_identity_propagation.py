"""Identity propagation ("Find more faces") against the cluster names the
assigners actually write: the FAISS assigner leaves unnamed people with an
empty name, the legacy one wrote "Unnamed N"."""
from __future__ import annotations

import json

import pytest

import app
from fakes import FakeTable

USER = 'lib-A'
PIHU = [1.0, 0.1, 0.0, 0.0]
PIHU_LIKE = [0.98, 0.12, 0.02, 0.0]
TANU = [0.0, 0.0, 1.0, 0.1]


@pytest.fixture(autouse=True)
def tables(monkeypatch):
    faces, people = FakeTable(), FakeTable()
    monkeypatch.setattr(app, 'face_table_client', faces)
    monkeypatch.setattr(app, 'person_table_client', people)
    monkeypatch.setattr(app, 'face_embeddings_table_client', None)
    monkeypatch.setattr(app, '_face_summary_scan_cache', app._UserScanCache(app.PEOPLE_SCAN_CACHE_TTL_SECONDS))
    return faces, people


def _face(faces, face_id, embedding, person_id):
    faces.upsert_entity({
        'PartitionKey': USER, 'RowKey': face_id, 'filename': f'{face_id}.jpg',
        'embedding': json.dumps(embedding), 'alignmentMethod': 'landmark-5pt-mp',
        'embeddingVersion': app.IPWORKER_FACE_CLUSTER_EMBEDDING_VERSION,
        'confidence': '0.9', 'personId': person_id,
    })


def _person(people, person_id, name, face_ids, rep):
    people.upsert_entity({'PartitionKey': USER, 'RowKey': person_id, 'name': name,
                          'faceIds': json.dumps(face_ids), 'repEmbedding': json.dumps(rep)})


@pytest.mark.parametrize('name, unnamed', [('', True), ('   ', True), (None, True), ('Unnamed 12', True),
                                           ('unnamed', True), ('Pihu', False), ('Unnamed Uncle', False)])
def test_empty_names_are_unnamed(name, unnamed):
    assert app._is_unnamed_name(name) is unnamed


def test_find_more_faces_takes_from_empty_named_and_placeholder_clusters_but_not_named_people(tables):
    faces, people = tables
    _person(people, 'pihu', 'Pihu', ['p1', 'p2'], PIHU)
    _face(faces, 'p1', PIHU, 'pihu')
    _face(faces, 'p2', PIHU, 'pihu')
    _person(people, 'faiss-made', '', ['f1'], PIHU_LIKE)  # how the FAISS assigner names a new person
    _face(faces, 'f1', PIHU_LIKE, 'faiss-made')
    _person(people, 'legacy-made', 'Unnamed 3', ['l1'], PIHU_LIKE)
    _face(faces, 'l1', PIHU_LIKE, 'legacy-made')
    _person(people, 'tanu', 'Tanu', ['t1', 't2'], TANU)
    _face(faces, 't1', TANU, 'tanu')
    _face(faces, 't2', TANU, 'tanu')
    _face(faces, 'loose', PIHU_LIKE, '')

    result = app._propagate_person_identity(USER, 'pihu', apply=False, collect_suggestions=False)

    # f1, l1 and the loose face; Tanu's faces are never candidates.
    assert result['autoAssignedCount'] == 3 and result['candidateFaces'] == 3


@pytest.fixture
def writes(tables, monkeypatch):
    """The apply path's side effects, with the membership table the person's photo page reads."""
    faces, people = tables
    members = FakeTable()
    monkeypatch.setattr(app, 'person_members_table_client', members)
    monkeypatch.setattr(app, '_remove_face_from_person', lambda *a, **k: None)
    monkeypatch.setattr(app, '_remove_face_from_other_people', lambda *a, **k: {})
    monkeypatch.setattr(app, '_update_person_rep_embedding', lambda *a, **k: [])
    monkeypatch.setattr(app, '_rebuild_metadata_faces_for_filenames', lambda *a, **k: {})
    monkeypatch.setattr(app, '_resync_person_rule_albums', lambda *a, **k: None)
    monkeypatch.setattr(app, '_update_person_entity',
                        lambda uid, pid, updates: people.upsert_entity({**people.get_entity(uid, pid), **updates}) or True)
    return members


def _member_ids(members, person_id):
    return {row['RowKey'] for row in members.query_entities(f"PartitionKey eq '{person_id}'")}


def _tanu(faces, people, members):
    _person(people, 'tanu', 'Tanu', ['t1', 't2'], TANU)
    for fid in ('t1', 't2'):
        _face(faces, fid, TANU, 'tanu')
        members.upsert_entity({'PartitionKey': 'tanu', 'RowKey': fid, 'userId': USER})


def test_moved_faces_get_membership_rows_so_they_show_on_the_person(tables, writes):
    faces, people = tables
    _tanu(faces, people, writes)
    _person(people, 'unnamed', '', ['u1'], TANU)
    _face(faces, 'u1', TANU, 'unnamed')

    result = app._propagate_person_identity(USER, 'tanu', apply=True, collect_suggestions=False)

    assert result['autoAssigned'] == ['u1'] and result['repairedCount'] == 0
    assert faces.get_entity(USER, 'u1')['personId'] == 'tanu'
    assert json.loads(people.get_entity(USER, 'tanu')['faceIds']) == ['t1', 't2', 'u1']
    assert _member_ids(writes, 'tanu') == {'t1', 't2', 'u1'}


def test_a_rerun_finishes_faces_an_interrupted_pass_left_half_moved(tables, writes):
    # The 2026-11-13 state: owner already set to Tanu, but missing from her faceIds
    # list (25 faces) or only missing a membership row (38 faces).
    faces, people = tables
    _tanu(faces, people, writes)
    _face(faces, 'orphan', TANU, 'tanu')
    _face(faces, 'listed-no-row', TANU, 'tanu')
    people.upsert_entity({**people.get_entity(USER, 'tanu'), 'faceIds': json.dumps(['t1', 't2', 'listed-no-row'])})

    result = app._propagate_person_identity(USER, 'tanu', apply=True, collect_suggestions=False)

    assert result['repairedCount'] == 2 and result['autoAssignedCount'] == 2
    assert set(json.loads(people.get_entity(USER, 'tanu')['faceIds'])) == {'t1', 't2', 'listed-no-row', 'orphan'}
    assert _member_ids(writes, 'tanu') == {'t1', 't2', 'listed-no-row', 'orphan'}
    # Nothing left to finish on the next run.
    again = app._propagate_person_identity(USER, 'tanu', apply=True, collect_suggestions=False)
    assert again['repairedCount'] == 0 and again['candidateFaces'] == 0


def test_an_unreadable_membership_table_only_repairs_unlisted_faces(tables, writes, monkeypatch):
    # A failed read used to look like "no rows at all", so every one of the person's
    # faces was rewritten and every photo's metadata rebuilt.
    faces, people = tables
    _tanu(faces, people, writes)
    _face(faces, 'orphan', TANU, 'tanu')

    def unreadable(*a, **k):
        raise RuntimeError('membership table unavailable')
    monkeypatch.setattr(writes, 'query_entities', unreadable)

    assert app._person_member_ids('tanu') is None
    result = app._propagate_person_identity(USER, 'tanu', apply=True, collect_suggestions=False)
    assert result['repairedCount'] == 1 and result['autoAssigned'] == ['orphan']


def test_job_record_keeps_skip_reasons_and_stays_a_find_faces_job():
    skipped = app._propagation_summary({'autoAssignedCount': 0, 'skipped': 'not enough anchor faces'})
    assert skipped == {'autoAssignedFaces': 0, 'repairedFaces': 0, 'searchedFaces': 0, 'skipped': 'not enough anchor faces'}
    row = {'jobType': 'clustering', 'status': 'done', 'result': json.dumps(skipped)}
    humanized = app._humanize_job(row)
    assert humanized['kind'] == 'find_faces'
    assert humanized['message'] == 'This person needs at least 2 faces before more can be found.'
    found = app._humanize_job({'jobType': 'clustering', 'status': 'done',
                               'result': json.dumps(app._propagation_summary({'autoAssignedCount': 63, 'repairedCount': 25, 'candidateFaces': 900}))})
    assert found['kind'] == 'find_faces' and found['message'] == 'Added 63 matching faces.'
