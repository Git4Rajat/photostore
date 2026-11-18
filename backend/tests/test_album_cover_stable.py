"""An album's cover is chosen once, stored on the row, and never recomputed by index builds."""
import json

import pytest

import album_store
import search_db
import storage_utils
from tests.fakes import FakeTable


@pytest.fixture
def library(monkeypatch, tmp_path):
    """A library database whose ratings the test can change between index builds."""
    state = {'rows': []}

    def rebuild(rows):
        state['rows'] = rows
        path = str(tmp_path / 'lib.sqlite')
        search_db.build_database(rows, path)
        return path

    path = rebuild([
        {'RowKey': 'a.jpg', 'rating': 2, 'likes': 0, 'uploadDate': '2026-01-01T00:00:00+00:00'},
        {'RowKey': 'b.jpg', 'rating': 5, 'likes': 1, 'uploadDate': '2026-01-02T00:00:00+00:00'},
        {'RowKey': 'c.jpg', 'rating': 5, 'likes': 3, 'uploadDate': '2026-01-03T00:00:00+00:00'},
    ])
    current = {'path': path}
    monkeypatch.setattr(search_db, 'open_database', lambda uid, **k: search_db.SearchDatabase(current['path']))

    def change(rows):
        current['path'] = rebuild(rows)
    return change


def _entity(**extra):
    return {'PartitionKey': 'u', 'RowKey': 'alb', 'name': 'Trip', **extra}


def test_cover_is_chosen_by_rating_then_likes_when_photos_are_added(library):
    entity = _entity()
    album_store.add(entity, ['a.jpg', 'b.jpg', 'c.jpg'])
    assert entity['coverFilename'] == 'c.jpg'


def test_empty_album_gets_its_cover_when_the_first_photos_arrive_then_keeps_it(library):
    entity = _entity()
    album_store.set_all(entity, [])
    assert album_store.cover_of(entity) == ''
    album_store.add(entity, ['a.jpg'])
    assert entity['coverFilename'] == 'a.jpg'
    album_store.add(entity, ['b.jpg', 'c.jpg'])           # a better photo arrives: the cover stays
    assert entity['coverFilename'] == 'a.jpg'


def test_set_all_picks_the_cover_at_creation(library):
    entity = _entity()
    album_store.set_all(entity, ['a.jpg', 'b.jpg'])
    assert entity['coverFilename'] == 'b.jpg'


def test_removing_the_cover_photo_picks_a_replacement_once(library):
    entity = _entity()
    album_store.set_all(entity, ['a.jpg', 'b.jpg', 'c.jpg'])
    album_store.remove(entity, ['a.jpg'])                 # not the cover: unchanged
    assert entity['coverFilename'] == 'c.jpg'
    album_store.remove(entity, ['c.jpg'])
    assert entity['coverFilename'] == 'b.jpg'
    album_store.remove(entity, ['b.jpg'])
    assert entity['coverFilename'] == ''


def test_unavailable_library_does_not_blank_or_guess_the_cover(monkeypatch):
    monkeypatch.setattr(search_db, 'open_database', lambda uid, **k: None)
    entity = _entity()
    album_store.set_all(entity, ['a.jpg'])
    assert album_store.cover_of(entity) == ''


@pytest.fixture
def albums_ctx(monkeypatch):
    table = FakeTable()
    monkeypatch.setitem(storage_utils._CTX, 'albums_table_client', table)
    return table


def test_index_rebuild_keeps_the_stored_cover_when_ratings_change(albums_ctx, library):
    entity = _entity()
    album_store.set_all(entity, ['a.jpg', 'b.jpg', 'c.jpg'])
    albums_ctx.upsert_entity(entity)
    first = storage_utils._build_user_albums_index_snapshot('u', 'v1').rows[0]['coverFilename']
    library([
        {'RowKey': 'a.jpg', 'rating': 5, 'likes': 99, 'uploadDate': '2026-02-01T00:00:00+00:00'},
        {'RowKey': 'b.jpg', 'rating': 1, 'likes': 0, 'uploadDate': '2026-01-02T00:00:00+00:00'},
        {'RowKey': 'c.jpg', 'rating': 1, 'likes': 0, 'uploadDate': '2026-01-03T00:00:00+00:00'},
    ])
    second = storage_utils._build_user_albums_index_snapshot('u', 'v2').rows[0]['coverFilename']
    assert first == second == 'c.jpg'


def test_legacy_row_without_a_cover_gets_one_persisted_once(albums_ctx, library):
    albums_ctx.upsert_entity(_entity(filenames=json.dumps(['a.jpg', 'b.jpg'])))
    assert storage_utils._build_user_albums_index_snapshot('u', 'v1').rows[0]['coverFilename'] == 'b.jpg'
    stored = albums_ctx.get_entity(partition_key='u', row_key='alb')
    assert stored['coverFilename'] == 'b.jpg' and json.loads(stored['filenames']) == ['a.jpg', 'b.jpg']
    library([{'RowKey': 'a.jpg', 'rating': 5, 'likes': 9, 'uploadDate': '2026-03-01T00:00:00+00:00'}])
    assert storage_utils._build_user_albums_index_snapshot('u', 'v2').rows[0]['coverFilename'] == 'b.jpg'


def test_incremental_rebuild_uses_the_stored_cover(albums_ctx, library):
    entity = _entity(coverFilename='a.jpg')
    album_store.write_filenames(entity, ['a.jpg', 'b.jpg'])
    albums_ctx.upsert_entity(entity)
    snapshot = storage_utils._incremental_albums_index_snapshot('u', 'v2', {'alb'}, [])
    assert snapshot.rows[0]['coverFilename'] == 'a.jpg'


def test_legacy_album_payload_uses_the_stored_cover_not_the_current_ranking(library, monkeypatch):
    import app
    monkeypatch.setattr(app, '_get_metadata_entities', lambda uid, names: {n: {'RowKey': n} for n in names})
    monkeypatch.setattr(app, '_thumbnail_url_from_metadata', lambda row, name: f'https://t/{name}')
    entity = _entity(coverFilename='a.jpg')                # c.jpg now ranks higher, but a.jpg was chosen first
    assert app._album_cover_thumbnail_url('u', ['a.jpg', 'b.jpg', 'c.jpg'], entity) == 'https://t/a.jpg'
    assert app._album_cover_thumbnail_url('u', ['a.jpg', 'b.jpg', 'c.jpg'], entity) == 'https://t/a.jpg'
