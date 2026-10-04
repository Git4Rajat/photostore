"""No request path may materialize the library. These pin the replacements for
the scans that used to load every row (all columns) into the 1Gi backend."""
from __future__ import annotations

import gc
import inspect
import tracemalloc
import types

import pytest

import app
import search_db
from routes import admin, albums, photos, upload


class _RecordingTable:
    """query_entities that records its arguments and yields rows lazily."""

    def __init__(self, make_rows):
        self.make_rows = make_rows
        self.calls = []

    def query_entities(self, query, **kwargs):
        self.calls.append((query, kwargs))
        return self.make_rows()


def _install(monkeypatch, table):
    monkeypatch.setattr(app, 'metadata_table_client', table)


def test_iter_is_lazy_filters_server_side_and_projects_columns(monkeypatch):
    table = _RecordingTable(lambda: iter([{'RowKey': 'a', 'processing_state': 'deleted'}, {'RowKey': 'b'}]))
    _install(monkeypatch, table)
    gen = app._iter_metadata_rows_for_user('u1', select=['RowKey'], extra_filter="x eq 'y'")
    assert isinstance(gen, types.GeneratorType) and table.calls == []   # nothing scanned until consumed
    assert [r['RowKey'] for r in gen] == ['b']                           # trashed rows skipped by default
    query, kwargs = table.calls[0]
    assert query == "PartitionKey eq 'u1' and (x eq 'y')"
    assert 'processing_state' in kwargs['select']                        # added so the skip works


def test_iter_can_include_deleted_rows(monkeypatch):
    _install(monkeypatch, _RecordingTable(lambda: iter([{'RowKey': 'a', 'processing_state': 'deleted'}])))
    assert [r['RowKey'] for r in app._iter_metadata_rows_for_user('u1', include_deleted=True)] == ['a']


def test_iter_enforces_the_row_ceiling(monkeypatch):
    monkeypatch.setattr(app, 'PHOTO_TABLE_SCAN_MAX_ROWS', 3)
    _install(monkeypatch, _RecordingTable(lambda: ({'RowKey': str(i)} for i in range(10))))
    with pytest.raises(RuntimeError, match='exceeded'):
        list(app._iter_metadata_rows_for_user('u1'))


def test_iter_memory_is_flat_for_a_big_library(monkeypatch):
    _install(monkeypatch, _RecordingTable(lambda: ({'RowKey': f'r{i}', 'blob': 'x' * 12000} for i in range(8000))))
    gc.collect()
    tracemalloc.start()
    try:
        seen = sum(1 for _ in app._iter_metadata_rows_for_user('u1'))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert seen == 8000 and peak < 5 * 1024 * 1024   # ~96MB if the rows were retained


def test_no_request_route_uses_the_materializing_helpers():
    """Guard against reintroducing library-sized loads in route code."""
    forbidden = ('_cached_metadata_rows_for_user', '_cached_metadata_list_rows_for_user',
                 '_cached_sorted_metadata_rows_for_user', '_cached_sorted_metadata_list_rows_for_user',
                 'get_user_listing_index', 'get_user_lexical_index', "_query_metadata_rows_for_user(user_id, include_deleted=True")
    for module in (photos, albums, admin, upload):
        source = inspect.getsource(module)
        for name in forbidden:
            assert name not in source, f'{module.__name__} uses {name}'


# --- routes push work to the server / the local database --------------------------

def _call(view, url, method='GET', **kw):
    with app.app.test_request_context(url, method=method, **kw):
        response = view()
    return response.get_json() if hasattr(response, 'get_json') else response[0].get_json()


def test_trash_list_transfers_only_trashed_rows(monkeypatch):
    table = _RecordingTable(lambda: iter([{'RowKey': 'a.jpg', 'processing_state': 'deleted', 'deletedAt': '2026-01-01T00:00:00+00:00'}]))
    _install(monkeypatch, table)
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('owner', None))
    monkeypatch.setattr(app, '_load_people_name_index', lambda uid: ({}, {}))
    payload = _call(photos.list_trashed_photos, '/api/photos/trash')
    assert payload['total'] == 1
    query, kwargs = table.calls[0]
    assert "processing_state eq 'deleted'" in query and 'deletedAt' in kwargs['select'] and 'photoEmbedding' not in kwargs['select']


def test_corrupted_uploads_filters_on_the_server_and_projects(monkeypatch):
    table = _RecordingTable(lambda: iter([{'RowKey': 'bad.jpg', 'verification_status': 'failed', 'verification_error': 'decode error'}]))
    _install(monkeypatch, table)
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('owner', None))
    payload = _call(upload.list_corrupted_uploads, '/api/uploads/corrupted')
    assert 'bad.jpg' in str(payload)
    query, kwargs = table.calls[0]
    assert "verification_status eq 'failed'" in query and 'corrupted eq true' in query
    assert 'verification_error' in kwargs['select'] and 'ocrText' not in kwargs['select']


def test_smart_albums_stream_a_narrow_projection(monkeypatch):
    rows = [{'RowKey': f'p{i}.jpg', 'locationCity': 'Paris', 'locationCountry': 'France', 'uploadDate': '2020-01-01T00:00:00+00:00'}
            for i in range(5)]
    table = _RecordingTable(lambda: iter(rows))
    _install(monkeypatch, table)
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('owner', None))
    monkeypatch.setattr(app, '_albums_table_available', lambda: True)
    saved = []
    monkeypatch.setattr(app, 'albums_table_client', types.SimpleNamespace(query_entities=lambda q: []))
    monkeypatch.setattr(app, '_save_album_entity', lambda entity: saved.append(entity))
    payload = _call(albums.autocreate_albums, '/api/albums/autocreate', method='POST', json={'rule': 'location'})
    assert payload['count'] == 1 and saved and 'Paris' in saved[0]['name']
    _, kwargs = table.calls[0]
    assert set(app.SMART_ALBUM_SELECT) <= set(kwargs['select']) and 'ocrText' not in kwargs['select']


def test_admin_backfill_streams_two_columns_and_skips_trashed(monkeypatch):
    table = _RecordingTable(lambda: iter([{'RowKey': 'a.jpg'}, {'RowKey': 'b.jpg', 'processing_state': 'deleted'}]))
    _install(monkeypatch, table)
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('owner', None))
    queued = []
    monkeypatch.setattr(app, '_enqueue_processing_steps', lambda uid, name, steps, force=False: queued.append(name))
    monkeypatch.setattr(app, '_queue_ipwork_processing', lambda *a, **k: None)
    monkeypatch.setattr(app, '_invalidate_metadata_scan_cache', lambda uid: None)
    payload = _call(admin.admin_backfill_photos, '/api/admin/backfill/photos', method='POST',
                    json={'repair': True, 'confirm': 'BACKFILL_ALL_PHOTOS'})
    assert payload['queued'] == 1 and payload['skipped'] == 1 and queued == ['a.jpg']
    _, kwargs = table.calls[0]
    assert kwargs['select'] == ['RowKey', 'processing_state']


# --- album covers come from SQL ------------------------------------------------------

def test_album_cover_ranks_by_rating_in_sql_and_reads_only_the_top_few(monkeypatch, tmp_path):
    rows = [{'RowKey': f'p{i}.jpg', 'rating': i % 6, 'likes': 0, 'uploadDate': '2020-01-01T00:00:00+00:00'} for i in range(300)]
    path = str(tmp_path / 'c.sqlite')
    search_db.build_database(rows, path)
    db = search_db.SearchDatabase(path)
    album = [f'p{i}.jpg' for i in range(0, 300, 2)]   # even photos only
    top = db.top_rated(album, limit=12)
    assert len(top) == 12 and all(rows[int(n[1:-4])]['rating'] == 4 for n in top)  # best rating among the album's photos is 4
    assert top == sorted(top)                                                      # ties break by filename

    monkeypatch.setattr(search_db, 'open_database', lambda uid, **k: db)
    read = []
    monkeypatch.setattr(app, '_get_metadata_entities', lambda uid, names: read.extend(names) or {n: {'RowKey': n, 'thumbnail_status': 'done'} for n in names})
    monkeypatch.setattr(app, '_thumbnail_url_from_metadata', lambda row, name: f'https://t/{name}')
    assert app._album_cover_thumbnail_url('owner', album) == f'https://t/{top[0]}'
    assert len(read) == 12   # not the whole album, not the whole library
    monkeypatch.setattr(search_db, 'open_database', lambda uid, **k: None)
    assert app._album_cover_thumbnail_url('owner', album) == '' and app._album_cover_thumbnail_url('owner', []) == ''


@pytest.mark.parametrize('name', [
    '_cached_metadata_rows_for_user', '_cached_sorted_metadata_rows_for_user',
    '_cached_metadata_list_rows_for_user', '_cached_sorted_metadata_list_rows_for_user',
])
def test_whole_library_loaders_fail_loudly_instead_of_loading_everything(name):
    with pytest.raises(RuntimeError, match='OOM-ed the 1Gi backend'):
        getattr(app, name)('owner', purpose='x')
    app._invalidate_metadata_scan_cache('owner')  # write paths still call this; it is a harmless no-op


# --- access-batch lookup ---------------------------------------------------------------

def test_access_lookup_builds_one_compact_map_per_version_and_copies_nothing(monkeypatch):
    import storage_utils
    storage_utils._ACCESS_LOOKUP_CACHE.clear()
    loads = []
    version = {'v': 'v1'}
    rows = [{'RowKey': f'p{i}.jpg', 'blobName': f'uuid{i}', 'thumbnailStatus': 'done', 'previewStatus': ''} for i in range(1000)]
    monkeypatch.setattr(storage_utils, '_load_access_index_manifest', lambda uid: {'sourceVersion': version['v'], 'dirty': False})
    monkeypatch.setattr(storage_utils, '_load_access_index_blob',
                        lambda uid: loads.append(version['v']) or types.SimpleNamespace(rows=list(rows)))

    got = storage_utils.lookup_access_entries('lib', ['p3.jpg', 'p999.jpg', 'missing.jpg'])
    assert got == {
        'p3.jpg': {'anonymousImageId': 'uuid3', 'thumbnail_status': 'done', 'preview_status': ''},
        'p999.jpg': {'anonymousImageId': 'uuid999', 'thumbnail_status': 'done', 'preview_status': ''},
    }
    for _ in range(5):                      # repeated page loads: no reload, no copy
        storage_utils.lookup_access_entries('lib', ['p1.jpg'])
    assert loads == ['v1']
    version['v'] = 'v2'                      # a new build -> reloaded exactly once
    storage_utils.lookup_access_entries('lib', ['p1.jpg'])
    storage_utils.lookup_access_entries('lib', ['p2.jpg'])
    assert loads == ['v1', 'v2']


def test_access_lookup_is_none_without_an_index_and_never_builds_one(monkeypatch):
    import storage_utils
    storage_utils._ACCESS_LOOKUP_CACHE.clear()
    monkeypatch.setattr(storage_utils, '_load_access_index_manifest', lambda uid: {})
    boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError('the backend must never build the access index'))
    monkeypatch.setattr(storage_utils, 'refresh_user_access_index', boom)
    monkeypatch.setattr(storage_utils, '_build_user_access_index_snapshot', boom)
    assert storage_utils.lookup_access_entries('lib', ['a.jpg']) is None
    assert storage_utils.lookup_access_entries('', ['a.jpg']) is None
    monkeypatch.setattr(storage_utils, '_load_access_index_manifest', lambda uid: {'sourceVersion': 'v1'})
    monkeypatch.setattr(storage_utils, '_load_access_index_blob', lambda uid: None)
    assert storage_utils.lookup_access_entries('lib', ['a.jpg']) is None
