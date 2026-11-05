"""People routes: merge, suggestions, separate/not-face/delete-faces glue, merge history."""
import json

import pytest

import app
from tests.fakes import FakeTable


@pytest.fixture
def http(monkeypatch):
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('u1', None))
    monkeypatch.setattr(app, '_people_features_available', lambda: True)
    app.app.config['PROPAGATE_EXCEPTIONS'] = True
    return app.app.test_client()


@pytest.fixture
def merge_side_effects(monkeypatch):
    log = {'dirty': [], 'touch': 0, 'rebuild': [], 'propagate_jobs': [], 'inline': []}
    monkeypatch.setattr(app, 'mark_people_dirty', lambda user, ids: log['dirty'].append((user, list(ids))))
    monkeypatch.setattr(app, 'touch_user_people_index_state', lambda user: log.__setitem__('touch', log['touch'] + 1))
    monkeypatch.setattr(app, '_trigger_indexer_rebuild', lambda user, reason, scope: log['rebuild'].append((reason, scope)))
    monkeypatch.setattr(app, '_enqueue_propagate_job', lambda user, pid: log['propagate_jobs'].append(pid) or {'status': 'queued', 'jobId': 'job-1'})
    monkeypatch.setattr(app, '_propagate_person_identity', lambda *a, **k: log['inline'].append(a) or {'autoAssignedCount': 4})
    return log


def test_merge_rejects_non_list_ids_and_missing_base(http, monkeypatch, merge_side_effects):
    assert http.post('/api/persons/p1/merge', json={'mergeIds': 'p2'}).status_code == 400
    monkeypatch.setattr(app, '_merge_persons_core', lambda user, base, ids: None)
    assert http.post('/api/persons/p1/merge', json={'mergeIds': ['p2']}).status_code == 404
    assert merge_side_effects['dirty'] == []


def test_merge_marks_dirty_queues_rebuild_and_propagates_for_named_target(http, monkeypatch, merge_side_effects):
    monkeypatch.setattr(app, '_merge_persons_core', lambda user, base, ids: {'mergeId': 'm1'})
    monkeypatch.setattr(app, '_person_is_named', lambda user, pid: True)
    body = http.post('/api/persons/p1/merge', json={'mergeIds': ['p2', 'p3']}).json
    assert body == {'success': True, 'personId': 'p1', 'mergeId': 'm1', 'autoAssignedFaces': 0, 'propagateJobId': 'job-1'}
    assert merge_side_effects['dirty'] == [('u1', ['p1', 'p2', 'p3'])]
    assert merge_side_effects['rebuild'] == [('people-merge', 'people')]
    assert merge_side_effects['propagate_jobs'] == ['p1'] and not merge_side_effects['inline']


def test_merge_into_unnamed_person_skips_propagation(http, monkeypatch, merge_side_effects):
    monkeypatch.setattr(app, '_merge_persons_core', lambda user, base, ids: {'mergeId': 'm1'})
    monkeypatch.setattr(app, '_person_is_named', lambda user, pid: False)
    body = http.post('/api/persons/p1/merge', json={'mergeIds': ['p2']}).json
    assert body['propagateJobId'] is None and body['autoAssignedFaces'] == 0
    assert not merge_side_effects['propagate_jobs'] and not merge_side_effects['inline']


def test_merge_propagates_inline_when_the_queue_is_unavailable(http, monkeypatch, merge_side_effects):
    monkeypatch.setattr(app, '_merge_persons_core', lambda user, base, ids: {'mergeId': 'm1'})
    monkeypatch.setattr(app, '_person_is_named', lambda user, pid: True)
    monkeypatch.setattr(app, '_enqueue_propagate_job', lambda user, pid: {'status': 'unavailable'})
    body = http.post('/api/persons/p1/merge', json={'mergeIds': ['p2']}).json
    assert body['autoAssignedFaces'] == 4 and body['propagateJobId'] is None


def test_merge_still_succeeds_when_index_bookkeeping_fails(http, monkeypatch, merge_side_effects):
    monkeypatch.setattr(app, '_merge_persons_core', lambda user, base, ids: {'mergeId': 'm1'})
    monkeypatch.setattr(app, '_person_is_named', lambda user, pid: False)
    def boom(*a, **k):
        raise RuntimeError('queue down')
    monkeypatch.setattr(app, 'touch_user_people_index_state', boom)
    monkeypatch.setattr(app, '_trigger_indexer_rebuild', boom)
    assert http.post('/api/persons/p1/merge', json={'mergeIds': ['p2']}).json['success'] is True


@pytest.mark.parametrize('url,key,core', [
    ('/api/persons/p1/separate', 'faceId', '_split_face_into_new_person'),
    ('/api/persons/p1/not-face', 'faceId', '_mark_face_not_a_face'),
])
def test_face_actions_require_a_face_id_and_pass_the_core_status_through(http, monkeypatch, url, key, core):
    assert http.post(url, json={}).status_code == 400
    monkeypatch.setattr(app, core, lambda user, person, face: {'status': 404, 'error': 'face not found'})
    missing = http.post(url, json={key: 'f1'})
    assert missing.status_code == 404 and 'status' not in missing.json
    monkeypatch.setattr(app, core, lambda user, person, face: {'ok': True, 'face': face})
    assert http.post(url, json={key: ' f1 '}).json['ok'] is True


def test_delete_faces_validates_and_reports_success_from_errors(http, monkeypatch):
    assert http.post('/api/faces/delete', json={'faceIds': 'f1'}).status_code == 400
    seen = []
    monkeypatch.setattr(app, '_delete_faces_bulk', lambda user, ids: seen.append(ids) or {'deleted': 2, 'errors': []})
    ok = http.post('/api/faces/delete', json={'faceIds': ['f1', None, 'f2']})
    assert ok.json == {'success': True, 'deleted': 2, 'errors': []} and seen == [['f1', '', 'f2']]
    monkeypatch.setattr(app, '_delete_faces_bulk', lambda user, ids: {'status': 207, 'deleted': 1, 'errors': ['f2']})
    partial = http.post('/api/faces/delete', json={'faceIds': ['f1', 'f2']})
    assert partial.status_code == 207 and partial.json['success'] is False


def test_decline_suggestions_validates_and_reports_count(http, monkeypatch):
    assert http.post('/api/persons/p1/suggested-faces/decline', json={'faceIds': 'x'}).status_code == 400
    monkeypatch.setattr(app, '_add_declined_face_suggestions', lambda user, pid, ids: len(ids))
    body = http.post('/api/persons/p1/suggested-faces/decline', json={'faceIds': ['a', 'b']}).json
    assert body == {'success': True, 'personId': 'p1', 'declinedFaces': 2}


@pytest.fixture
def people_tables(monkeypatch):
    persons, faces = FakeTable(), FakeTable()
    persons.upsert_entity({'PartitionKey': 'u1', 'RowKey': 'p1', 'name': 'Ada'})
    monkeypatch.setattr(app, 'person_table_client', persons)
    monkeypatch.setattr(app, 'face_table_client', faces)
    moves = {'removed': [], 'added': [], 'others': [], 'rep': [], 'rebuilt': []}
    monkeypatch.setattr(app, '_remove_face_from_person', lambda user, pid, fid: moves['removed'].append((pid, fid)))
    monkeypatch.setattr(app, '_remove_face_from_other_people', lambda user, fid, keep: moves['others'].append((fid, keep)))
    monkeypatch.setattr(app, '_add_face_to_person', lambda user, pid, fid: moves['added'].append((pid, fid)))
    monkeypatch.setattr(app, '_update_person_rep_embedding', lambda user, pid: moves['rep'].append(pid))
    monkeypatch.setattr(app, '_rebuild_metadata_faces_for_filenames', lambda user, names: moves['rebuilt'].append(set(names)))
    return faces, moves


def test_accept_suggestions_requires_list_and_existing_person(http, people_tables):
    assert http.post('/api/persons/p1/suggested-faces/accept', json={'faceIds': 'x'}).status_code == 400
    assert http.post('/api/persons/ghost/suggested-faces/accept', json={'faceIds': ['f1']}).status_code == 404


def test_accept_suggestions_confirms_faces_and_moves_them_from_other_people(http, people_tables):
    faces, moves = people_tables
    faces.upsert_entity({'PartitionKey': 'u1', 'RowKey': 'f1', 'personId': 'p9', 'filename': 'a.jpg', 'confidence': 0.4,
                         'suspiciousReason': 'blur', 'rejected': True})
    faces.upsert_entity({'PartitionKey': 'u1', 'RowKey': 'f2', 'personId': 'p1', 'filename': 'b.jpg', 'confidence': 1.5})
    body = http.post('/api/persons/p1/suggested-faces/accept', json={'faceIds': ['f1', ' ', 'f2', 'missing']}).json
    assert body['acceptedFaces'] == 2 and set(body['accepted']) == {'f1', 'f2'}
    saved = faces.get_entity('u1', 'f1')
    assert saved['personId'] == 'p1' and saved['confirmedByUser'] is True and saved['reviewStatus'] == 'confirmed'
    assert saved['rejected'] is False and 'suspiciousReason' not in saved and saved['confidence'] == 1.0
    assert faces.get_entity('u1', 'f2')['confidence'] == 1.5
    assert moves['removed'] == [('p9', 'f1')]
    assert moves['rep'] == ['p1'] and moves['rebuilt'] == [{'a.jpg', 'b.jpg'}]


def test_accept_suggestions_with_nothing_valid_does_no_rebuild(http, people_tables):
    _, moves = people_tables
    body = http.post('/api/persons/p1/suggested-faces/accept', json={'faceIds': ['ghost']}).json
    assert body['acceptedFaces'] == 0 and not moves['rep'] and not moves['rebuilt']


def test_merge_history_hides_repair_snapshots_and_tolerates_bad_json(http, monkeypatch):
    merges = FakeTable()
    rows = [
        {'RowKey': 'm1', 'targetPersonId': 'p1', 'mergedIds': json.dumps(['p2']), 'targetName': 'Ada', 'mergedNames': json.dumps(['X']), 'createdAt': 'T1'},
        {'RowKey': 'm2', 'targetPersonId': 'p1', 'mergedIds': '{broken', 'mergedNames': '{broken', 'createdAt': 'T2'},
        {'RowKey': 's1', 'kind': 'recluster_snapshot', 'createdAt': 'T3'},
        {'RowKey': 's2', 'kind': 'face_membership_snapshot', 'createdAt': 'T4'},
    ]
    monkeypatch.setattr(app, 'merge_table_client', type('T', (), {'query_entities': lambda self, f, select=None: iter(rows)})())
    body = http.get('/api/persons/merges').json['merges']
    assert [m['mergeId'] for m in body] == ['m1', 'm2']
    assert body[0]['mergedIds'] == ['p2'] and body[0]['mergedNames'] == ['X']
    assert body[1]['mergedIds'] == [] and body[1]['mergedNames'] == []


def test_merge_history_is_empty_when_the_query_fails(http, monkeypatch):
    def boom(self, f, select=None):
        raise RuntimeError('table down')
    monkeypatch.setattr(app, 'merge_table_client', type('T', (), {'query_entities': boom})())
    assert http.get('/api/persons/merges').json == {'merges': []}
