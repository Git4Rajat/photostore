"""Merging people: faces move, source clusters go, and a batch costs one face-table scan, not one per pair."""
import json

import app
from tests.fakes import FakeTable


class _Faces(FakeTable):
    def __init__(self):
        super().__init__()
        self.scans = 0

    def query_entities(self, filter_str, select=None, **kw):
        self.scans += 1
        return super().query_entities(filter_str, select=select, **kw)


def _world(monkeypatch):
    persons, faces, merges = FakeTable(), _Faces(), FakeTable()
    for pid, name, fids in (('A', 'Asha', ['f1']), ('B', '', ['f2', 'f3']), ('C', '', ['f4']), ('D', '', ['f5', 'f6'])):
        persons.upsert_entity({'PartitionKey': 'u1', 'RowKey': pid, 'name': name, 'faceIds': json.dumps(fids)})
        for i, fid in enumerate(fids):
            faces.upsert_entity({'PartitionKey': 'u1', 'RowKey': fid, 'filename': f'{fid}.jpg', 'personId': pid,
                                 'confidence': 0.5, 'rejected': False})
    monkeypatch.setattr(app, 'person_table_client', persons)
    monkeypatch.setattr(app, 'face_table_client', faces)
    monkeypatch.setattr(app, 'merge_table_client', merges)
    monkeypatch.setattr(app, 'person_members_table_client', None)
    monkeypatch.setattr(app, 'upload_file_to_blob', lambda *a, **k: None)
    monkeypatch.setattr(app, '_update_person_rep_embedding', lambda *a, **k: [])
    monkeypatch.setattr(app, '_rebuild_metadata_faces_for_filenames', lambda uid, names, **k: {'files': list(names)})
    monkeypatch.setattr(app, '_update_person_entity',
                        lambda uid, pid, updates: persons.upsert_entity({**persons.get_entity(uid, pid), **updates}) or True)
    app._face_summary_scan_cache.invalidate('u1')
    return persons, faces


def test_merge_moves_faces_and_deletes_the_source_cluster(monkeypatch):
    persons, faces = _world(monkeypatch)
    result = app._merge_persons_core('u1', 'A', ['B'])
    assert result and result['mergeId']
    assert ('u1', 'B') not in persons.rows
    assert {faces.get_entity('u1', f)['personId'] for f in ('f1', 'f2', 'f3')} == {'A'}
    assert set(json.loads(persons.get_entity('u1', 'A')['faceIds'])) == {'f1', 'f2', 'f3'}
    assert faces.get_entity('u1', 'f2')['confirmedByUser'] is True


def test_a_batch_of_merges_scans_the_face_table_once(monkeypatch):
    persons, faces = _world(monkeypatch)
    app._merge_persons_core('u1', 'A', ['B'])
    app._merge_persons_core('u1', 'A', ['C'])
    app._merge_persons_core('u1', 'A', ['D'])
    assert faces.scans == 1          # was one full scan per pair
    assert set(json.loads(persons.get_entity('u1', 'A')['faceIds'])) == {'f1', 'f2', 'f3', 'f4', 'f5', 'f6'}


def test_unknown_base_person_returns_none(monkeypatch):
    _world(monkeypatch)
    assert app._merge_persons_core('u1', 'missing', ['B']) is None


def test_batch_route_passes_one_snapshot_so_cache_invalidation_cannot_force_rescans(monkeypatch):
    from routes import people
    persons, faces = _world(monkeypatch)
    # production wraps the face table so any write drops the cache; emulate that here
    real_upsert = faces.upsert_entity
    faces.upsert_entity = lambda e: (real_upsert(e), app._face_summary_scan_cache.invalidate('u1'))[0]
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('u1', None))
    monkeypatch.setattr(app, '_people_features_available', lambda: True)
    monkeypatch.setattr(app, '_person_is_named', lambda uid, pid: False)
    body = {'merges': [{'targetPersonId': 'A', 'mergeIds': ['B']}, {'targetPersonId': 'A', 'mergeIds': ['C']},
                       {'targetPersonId': 'A', 'mergeIds': ['D']}]}
    with app.app.test_request_context('/api/persons/merge/batch', method='POST', json=body):
        payload = people.merge_persons_batch().get_json()
    assert payload['success'] is True
    assert faces.scans == 1


def test_delete_cluster_releases_every_face_and_reports_filenames(monkeypatch):
    persons, faces = _world(monkeypatch)
    result = app._delete_person_cluster('u1', 'B', rebuild_metadata=False)
    assert result['deleted'] is True and result['facesUpdated'] == 2
    assert result['filenames'] == ['f2.jpg', 'f3.jpg']
    assert ('u1', 'B') not in persons.rows
    for fid in ('f2', 'f3'):
        row = faces.get_entity('u1', fid)
        assert row['rejected'] is True and row['rejectedReason'] == 'person_cluster_deleted'


def test_bulk_delete_route_handles_many_clusters_and_unknown_ids(monkeypatch):
    from routes import people
    persons, faces = _world(monkeypatch)
    rebuilds = []
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('u1', None))
    monkeypatch.setattr(app, '_people_features_available', lambda: True)
    monkeypatch.setattr(app, '_trigger_tools_index_rebuild', lambda *args, **kwargs: rebuilds.append((args, kwargs)))
    with app.app.test_request_context('/api/persons/delete', method='POST', json={'personIds': ['B', 'C', 'B', 'nope']}):
        payload = people.delete_person_clusters().get_json()
    assert sorted(payload['deletedPersonIds'] if 'deletedPersonIds' in payload else payload.get('deleted', [])) == ['B', 'C']
    assert [e['personId'] for e in payload.get('errors', [])] == ['nope']
    assert rebuilds == [(('u1',), {'reason': 'people-delete', 'scope': 'people'})]


def test_single_delete_route_triggers_people_index_rebuild(monkeypatch):
    from routes import people
    _world(monkeypatch)
    rebuilds = []
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('u1', None))
    monkeypatch.setattr(app, '_people_features_available', lambda: True)
    monkeypatch.setattr(app, '_trigger_tools_index_rebuild', lambda *args, **kwargs: rebuilds.append((args, kwargs)))

    with app.app.test_request_context('/api/persons/B/delete', method='POST'):
        response = people.delete_person_cluster('B')

    assert response.get_json()['success'] is True
    assert rebuilds == [(('u1',), {'reason': 'person-delete', 'scope': 'people'})]


def test_person_detail_pages_faces_best_first_without_per_face_reads(monkeypatch):
    from routes import people
    persons, faces = FakeTable(), _Faces()
    ids = [f'f{i:03d}' for i in range(250)]
    persons.upsert_entity({'PartitionKey': 'u1', 'RowKey': 'P', 'name': 'Asha', 'faceIds': json.dumps(ids)})
    for i, fid in enumerate(ids):
        faces.upsert_entity({'PartitionKey': 'u1', 'RowKey': fid, 'filename': f'{fid}.jpg', 'personId': 'P',
                             'confidence': i / 1000, 'bbox': '{"x":1,"y":2,"width":3,"height":4}', 'imageWidth': 10, 'imageHeight': 10})
    faces.upsert_entity({'PartitionKey': 'u1', 'RowKey': 'rej', 'filename': 'rej.jpg', 'personId': 'P', 'rejected': True, 'confidence': 0.99})
    monkeypatch.setattr(app, 'person_table_client', persons)
    monkeypatch.setattr(app, 'face_table_client', faces)
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('u1', None))
    monkeypatch.setattr(app, '_people_features_available', lambda: True)
    monkeypatch.setattr(app, '_face_thumbnail_url', lambda name, uid='': '')
    app._face_summary_scan_cache.invalidate('u1')
    points = []
    real_get = faces.get_entity
    faces.get_entity = lambda partition_key, row_key: (points.append(row_key), real_get(partition_key, row_key))[1]

    def page(offset, limit):
        with app.app.test_request_context(f'/api/persons/P?offset={offset}&limit={limit}'):
            return people.get_person('P').get_json()

    first, second, last = page(0, 100), page(100, 100), page(200, 100)
    assert first['total'] == 250 and first['hasMore'] is True and last['hasMore'] is False
    got = [f['faceId'] for f in first['faces'] + second['faces'] + last['faces']]
    assert len(got) == 250 and len(set(got)) == 250 and 'rej' not in got
    assert first['faces'][0]['faceId'] == 'f249'                               # highest-confidence first
    assert points == []                                                          # no per-face point reads
    with app.app.test_request_context('/api/persons/P'):
        everything = people.get_person('P').get_json()
    assert len(everything['faces']) == 250 and 'hasMore' not in everything
