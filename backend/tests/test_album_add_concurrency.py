import json

import app
from routes import albums


def _entity(names):
    return {'PartitionKey': 'u1', 'RowKey': 'al', 'filenames': json.dumps(names)}


def _setup(monkeypatch, loads, save_results):
    """loads: entities returned by successive loads (None after that); save_results: outcomes of the
    ETag-checked saves. Returns the list that records every successful save's photo list."""
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('u1', None))
    monkeypatch.setattr(app, '_albums_table_available', lambda: True)
    monkeypatch.setattr(albums, '_existing_photos', lambda uid, names: {n: True for n in names})
    monkeypatch.setattr(app, '_album_entity_to_payload', lambda e: {})
    entities = iter(loads)
    monkeypatch.setattr(app, '_load_album_entity', lambda uid, aid: next(entities, None))
    outcomes = iter(save_results)
    saved = []

    def save_if_unchanged(entity):
        ok = next(outcomes)
        if ok:
            saved.append(json.loads(entity['filenames']))
        return ok
    monkeypatch.setattr(app, '_save_album_entity_if_unchanged', save_if_unchanged)
    return saved


def _add(names):
    with app.app.test_request_context('/api/albums/al/photos/add', method='POST', json={'filenames': names}):
        return albums.add_photos_to_album('al')


def test_conflicting_add_reloads_and_keeps_the_other_writers_photos(monkeypatch):
    saved = _setup(monkeypatch, [_entity([]), _entity(['theirs.jpg'])], [False, True])
    assert _add(['mine.jpg']).status_code == 200
    assert saved == [['theirs.jpg', 'mine.jpg']]


def test_add_gives_up_with_409_after_repeated_conflicts(monkeypatch):
    saved = _setup(monkeypatch, [_entity([])] * (albums._ALBUM_ADD_ATTEMPTS + 1), [False] * albums._ALBUM_ADD_ATTEMPTS)
    assert _add(['mine.jpg'])[1] == 409 and saved == []


def test_album_deleted_during_a_conflict_is_a_404(monkeypatch):
    saved = _setup(monkeypatch, [_entity([])], [False])
    assert _add(['mine.jpg'])[1] == 404 and saved == []
