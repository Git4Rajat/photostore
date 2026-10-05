"""GET /api/persons?namesOnly=1&covers=1 -- every cluster in one request, with a
cover face id, and no per-page cap (accounts have tens of thousands of clusters)."""
from __future__ import annotations

import json
import time

import app
from routes.people import list_persons


def _world(n_people: int, faces_per_person: int = 2):
    rows, faces = [], {}
    for p in range(n_people):
        pid = f'p{p:06d}'
        face_ids = []
        for f in range(faces_per_person):
            fid = f'{pid}-f{f}'
            face_ids.append(fid)
            faces[fid] = {'personId': pid, 'confidence': 0.5 + f * 0.1}   # last face is the best
        rows.append({'RowKey': pid, 'faceIds': json.dumps(face_ids), 'name': f'Person {p}' if p % 3 == 0 else ''})
    return rows, faces


def _wire(monkeypatch, rows, faces):
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('owner', None))
    monkeypatch.setattr(app, '_people_features_available', lambda: True)
    monkeypatch.setattr(app, '_scan_person_and_face_rows', lambda uid: (rows, faces))
    # a per-person network lookup would be the expensive path this mode must avoid
    monkeypatch.setattr(app, 'face_table_client', None)


def _get(url):
    with app.app.test_request_context(url):
        response = list_persons()
    return response.get_json()


def test_roster_returns_every_cluster_not_a_page_of_200(monkeypatch):
    rows, faces = _world(30000)
    _wire(monkeypatch, rows, faces)
    started = time.monotonic()
    payload = _get('/api/persons?namesOnly=1&covers=1')
    assert payload['total'] == 30000 and len(payload['persons']) == 30000   # no 200 cap, no paging
    assert time.monotonic() - started < 10


def test_roster_carries_the_best_cover_face_and_naming_state(monkeypatch):
    rows, faces = _world(6)
    faces['p000001-f0'].update({'confirmedByUser': True})            # confirmed beats higher confidence
    faces['p000002-f1'].update({'rejected': True})                   # rejected faces are never a cover
    _wire(monkeypatch, rows, faces)
    by_id = {p['personId']: p for p in _get('/api/persons?namesOnly=1&covers=1')['persons']}
    assert by_id['p000000']['coverFaceId'] == 'p000000-f1' and by_id['p000000']['isNamed'] is True
    assert by_id['p000001']['coverFaceId'] == 'p000001-f0' and by_id['p000001']['isNamed'] is False
    assert by_id['p000002']['coverFaceId'] == 'p000002-f0' and by_id['p000002']['faceCount'] == 1
    assert all(p['faceCount'] >= 1 for p in by_id.values())


def test_covers_are_opt_in_so_the_existing_names_only_shape_is_unchanged(monkeypatch):
    rows, faces = _world(3)
    _wire(monkeypatch, rows, faces)
    person = _get('/api/persons?namesOnly=1')['persons'][0]
    assert set(person) == {'personId', 'name', 'faceCount'}


def test_named_clusters_sort_first_and_empty_unnamed_clusters_are_left_out(monkeypatch):
    rows, faces = _world(4)
    rows.append({'RowKey': 'empty', 'faceIds': '[]', 'name': ''})     # nothing in it, never named
    _wire(monkeypatch, rows, faces)
    persons = _get('/api/persons?namesOnly=1&covers=1')['persons']
    assert 'empty' not in {p['personId'] for p in persons}
    named_flags = [p['isNamed'] for p in persons]
    assert named_flags == sorted(named_flags, reverse=True)           # named first


def test_paged_mode_is_unchanged_and_still_honours_limit(monkeypatch):
    rows, faces = _world(40)
    _wire(monkeypatch, rows, faces)
    monkeypatch.setattr(app, '_face_summary_for_person_list', lambda fid, face, uid: {'faceId': fid})
    monkeypatch.setattr(app, '_load_people_name_index', lambda uid: ({}, {}), raising=False)
    payload = _get('/api/persons?offset=0&limit=5')
    assert len(payload['persons']) <= 5 and payload['total'] == 40
