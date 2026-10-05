"""Albums are not limited to one 64 KB table property any more."""
import json

import pytest

import album_store
import app
from routes import albums
from tests.fakes import FakeTable


def test_round_trip_across_many_properties_and_back_to_one():
    entity = {'filenames': '[]'}
    names = [f'IMG_{i:08d}.jpg' for i in range(10000)]
    album_store.write_filenames(entity, names)
    chunk_props = [k for k in entity if k.startswith('filenames')]
    assert len(chunk_props) > 3 and all(len(entity[k].encode('utf-16-le')) <= 65536 for k in chunk_props)
    assert album_store.read_filenames(entity) == names
    album_store.write_filenames(entity, names[:3])                        # shrinking clears the chunks it no longer uses
    assert album_store.read_filenames(entity) == names[:3]
    assert all(entity[k] == '' for k in chunk_props if k != 'filenames')


def test_rows_written_before_this_change_still_read():
    assert album_store.read_filenames({'filenames': json.dumps(['a.jpg', 'b.jpg'])}) == ['a.jpg', 'b.jpg']
    assert album_store.read_filenames({}) == [] and album_store.read_filenames({'filenames': 'not json'}) == []


def test_unicode_names_never_overflow_a_property():
    names = [f'📷{i}-é.jpg' for i in range(20000)]
    entity = {}
    kept, cut = album_store.fit(names)
    album_store.write_filenames(entity, kept)
    assert all(len(v.encode('utf-16-le')) <= 65536 for k, v in entity.items())
    assert album_store.read_filenames(entity) == kept and (cut or len(kept) == len(names))


def test_too_large_raises_and_fit_returns_the_longest_prefix():
    names = [f'a-fairly-long-original-photo-name-{i:07d}.jpg' for i in range(40000)]
    with pytest.raises(album_store.AlbumTooLarge):
        album_store.write_filenames({}, names)
    kept, cut = album_store.fit(names)
    assert cut and 5000 < len(kept) < 40000 and kept == names[:len(kept)]
    album_store.write_filenames({}, kept)                                  # it really fits


def test_adding_beyond_capacity_is_a_clear_413_not_a_500(monkeypatch):
    table, meta = FakeTable(), FakeTable()
    names = [f'a-fairly-long-original-photo-name-{i:07d}.jpg' for i in range(40000)]
    for n in names[-50:]:
        meta.upsert_entity({'PartitionKey': 'u1', 'RowKey': n})
    monkeypatch.setattr(app, 'metadata_table_client', meta)
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('u1', None))
    monkeypatch.setattr(app, '_albums_table_available', lambda: True)
    entity = {'PartitionKey': 'u1', 'RowKey': 'al', 'filenames': '[]'}
    album_store.write_filenames(entity, album_store.fit(names)[0])         # already at capacity
    monkeypatch.setattr(app, '_load_album_entity', lambda uid, aid: entity)
    monkeypatch.setattr(app, '_save_album_entity', lambda e: table.upsert_entity(e))
    with app.app.test_request_context('/api/albums/al/photos/add', method='POST', json={'filenames': names[-50:]}):
        response = albums.add_photos_to_album('al')
    assert response[1] == 413 and response[0].get_json()['code'] == 'album_too_large'


def test_smart_album_larger_than_capacity_is_trimmed_with_a_message(monkeypatch):
    saved = []
    names = [f'a-fairly-long-original-photo-name-{i:07d}.jpg' for i in range(40000)]
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('u1', None))
    monkeypatch.setattr(app, '_albums_table_available', lambda: True)
    monkeypatch.setattr(app, 'albums_table_client', type('T', (), {'query_entities': lambda self, q: []})())
    monkeypatch.setattr(app, '_save_album_entity', lambda e: saved.append(e))
    monkeypatch.setattr(app, '_smart_album_candidates', lambda uid, rule, rows: [{'name': 'Uploaded: Jan 1', 'filenames': names}])
    import search_db
    monkeypatch.setattr(search_db, 'open_database', lambda uid, **k: None)
    monkeypatch.setattr(app, '_iter_metadata_rows_for_user', lambda *a, **k: iter([]))
    with app.app.test_request_context('/api/albums/autocreate', method='POST', json={'rule': 'recent-upload'}):
        payload = albums.autocreate_albums().get_json()
    assert payload['count'] == 1 and 'larger than one album can hold' in payload['message']
    assert 0 < payload['album']['photoCount'] < 40000
    assert len(album_store.read_filenames(saved[0])) == payload['album']['photoCount']


def _album_world(monkeypatch, count=50):
    meta = FakeTable()
    names = [f'p{i:03d}.jpg' for i in range(count)]
    for n in names:
        meta.upsert_entity({'PartitionKey': 'u1', 'RowKey': n, 'processing_state': 'deleted' if n == 'p003.jpg' else 'active'})
    entity = {'PartitionKey': 'u1', 'RowKey': 'al', 'name': 'Trip'}
    album_store.write_filenames(entity, names)
    monkeypatch.setattr(app, 'metadata_table_client', meta)
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('u1', None))
    monkeypatch.setattr(app, '_albums_table_available', lambda: True)
    monkeypatch.setattr(app, '_load_album_entity', lambda uid, aid: entity)
    monkeypatch.setattr(app, '_album_cover_thumbnail_url', lambda uid, names: '')
    monkeypatch.setattr(app, '_load_people_name_index', lambda uid: ({}, {}))
    monkeypatch.setattr(app, '_build_photo_summaries_page', lambda uid, items, pn: [{'filename': n} for n, _ in items])
    return meta, names


def test_album_detail_pages_in_album_order_and_skips_trashed(monkeypatch):
    _album_world(monkeypatch)
    with app.app.test_request_context('/api/albums/al?offset=0&limit=10'):
        page1 = albums.get_album('al').get_json()
    assert [p['filename'] for p in page1['photos']] == [f'p{i:03d}.jpg' for i in range(10) if i != 3]
    assert page1['total'] == 50 and page1['hasMore'] is True
    with app.app.test_request_context('/api/albums/al?offset=40&limit=10'):
        last = albums.get_album('al').get_json()
    assert len(last['photos']) == 10 and last['hasMore'] is False
    with app.app.test_request_context('/api/albums/al'):
        everything = albums.get_album('al').get_json()
    assert len(everything['photos']) == 49 and 'hasMore' not in everything


def test_album_photos_are_read_in_batches_not_one_by_one(monkeypatch):
    meta, names = _album_world(monkeypatch, count=45)
    queries, points = [], []
    real_q, real_g = meta.query_entities, meta.get_entity
    meta.query_entities = lambda f, **k: (queries.append(f), real_q(f, **k))[1]
    meta.get_entity = lambda partition_key, row_key: (points.append(row_key), real_g(partition_key, row_key))[1]
    with app.app.test_request_context('/api/albums/al'):
        albums.get_album('al')
    assert len(queries) == 3 and not points                                  # ceil(45 / 15) round trips
