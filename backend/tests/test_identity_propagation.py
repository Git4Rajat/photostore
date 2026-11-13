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
