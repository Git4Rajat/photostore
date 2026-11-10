import json

import person_reconcile as pr


class Members:
    def __init__(self, by_person):
        self.by_person = by_person

    def query_entities(self, query, select=None):
        person = query.split("'")[1]
        return [{'RowKey': face_id} for face_id in self.by_person.get(person, [])]


def run(rows, members, faces, **kwargs):
    added = []
    result = pr.reconcile_people(
        'u', rows, Members(members), lambda ids: {i: faces[i] for i in ids if i in faces},
        lambda face: face.get('rejected') == 'true', lambda pid, extra: added.append((pid, list(extra))), **kwargs)
    return result, added


def person(pid, ids, name=''):
    return {'RowKey': pid, 'name': name, 'faceIds': json.dumps(ids)}


def test_adds_faces_the_row_lacks_but_owns():
    faces = {'f1': {'personId': 'p'}, 'f2': {'personId': 'p'}, 'f3': {'personId': 'p'}}
    result, added = run([person('p', ['f1'], 'Ann')], {'p': ['f1', 'f2', 'f3']}, faces, dry_run=False)
    assert added == [('p', ['f2', 'f3'])]
    assert result['repaired'] == [dict(personId='p', name='Ann', added=2, before=1, after=3)]


def test_dry_run_writes_nothing_but_reports():
    faces = {'f2': {'personId': 'p'}}
    result, added = run([person('p', ['f1'])], {'p': ['f1', 'f2']}, faces)
    assert added == [] and result['repaired'][0]['added'] == 1


def test_ignores_faces_moved_rejected_or_missing():
    faces = {'moved': {'personId': 'other'}, 'bad': {'personId': 'p', 'rejected': 'true'}}
    result, added = run([person('p', [])], {'p': ['moved', 'bad', 'gone']}, faces, dry_run=False)
    assert added == [] and result['repaired'] == []


def test_consistent_person_is_untouched():
    result, added = run([person('p', ['f1'])], {'p': ['f1']}, {'f1': {'personId': 'p'}}, dry_run=False)
    assert added == [] and result['checked'] == 1


def test_overflow_is_reported_not_written():
    ids = [f'{i:036d}' for i in range(900)]                      # ~ the size of a full row already
    extra = {f'x{i:035d}': {'personId': 'p'} for i in range(700)}
    result, added = run([person('p', ids)], {'p': ids + list(extra)}, extra, dry_run=False)
    assert added == [] and result['overflow'] == [dict(personId='p', missing=700)]


def test_stops_and_reports_where_to_resume():
    rows = [person('a', []), person('b', []), person('c', [])]
    seen = []
    result, _ = run(rows, {}, {}, should_stop=lambda: len(seen.append(1) or seen) > 2)
    assert result['complete'] is False and result['lastChecked'] == 'b' and result['checked'] == 2
