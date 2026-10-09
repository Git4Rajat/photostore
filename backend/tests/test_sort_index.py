"""Unit tests for the sort index (storage_utils.get_user_sort_index and
friends), added to let the gallery paginate client-side instead of
materializing and sorting the whole library on every /photos request.

Mirrors test_lexical_index.py's structure closely -- the sort index reuses
the exact same blob + manifest + dirty-Table + incremental-merge +
serve-stale-and-background-rebuild machinery as the lexical index, just with
its own blob pair/manifest/dirty-partition so a rating/likes edit (which
never affects lexical/vector content) doesn't trigger their expensive
OCR/tag/embedding rebuild. See _SORT_INDEX_RELEVANT_FIELDS and
touch_user_sort_index_dirty in storage_utils.py.
"""
from __future__ import annotations

import gzip
import json
import threading
import time

import pytest

import app
from routes.photos import photos_sort_index
import storage_utils
from tests.fakes import FakeTable


class _FakeBlob:
    def __init__(self, store: dict, key: str) -> None:
        self._store = store
        self._key = key

    def upload_blob(self, data, overwrite=True, content_settings=None):
        self._store[self._key] = data

    def download_blob(self):
        if self._key not in self._store:
            raise KeyError(self._key)
        return _Downloaded(self._store[self._key])

    def delete_blob(self):
        self._store.pop(self._key, None)


class _Downloaded:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def readall(self) -> bytes:
        return self._data


class _FakeBlobServiceClient:
    def __init__(self) -> None:
        self.blobs: dict = {}

    def get_blob_client(self, container, blob):
        return _FakeBlob(self.blobs, f'{container}/{blob}')


def _seed_row(table: FakeTable, user_id: str, filename: str, **overrides) -> None:
    row = {'PartitionKey': user_id, 'RowKey': filename, **overrides}
    table.upsert_entity(row)


@pytest.fixture
def sort_ctx(monkeypatch):
    table = FakeTable()
    dirty_table = FakeTable()
    blob_service = _FakeBlobServiceClient()
    monkeypatch.setitem(storage_utils._CTX, 'metadata_table_client', table)
    monkeypatch.setitem(storage_utils._CTX, 'search_index_dirty_table_client', dirty_table)
    monkeypatch.setitem(storage_utils._CTX, 'blob_service_client', blob_service)
    monkeypatch.setitem(storage_utils._CTX, 'blob_lexical_index_container', 'lexical-index')
    yield table, dirty_table, blob_service


# --- _build_user_sort_index_snapshot -----------------------------------------

def test_build_snapshot_projects_only_sort_relevant_fields(sort_ctx):
    table, _, _ = sort_ctx
    _seed_row(
        table, 'lib-A', 'photo.jpg',
        rating=4, likes=2, uploadDate='2026-01-01T00:00:00+00:00',
        tags='["dog"]', ocrText='hello', photoEmbedding='[0.1]',
    )

    snapshot = storage_utils._build_user_sort_index_snapshot('lib-A', 'v1')

    assert snapshot is not None
    assert len(snapshot.rows) == 1
    row = snapshot.rows[0]
    assert set(row.keys()) == {'RowKey', 'captureDate', 'rating', 'likes', 'uploadDate', 'thumb'}
    assert row['rating'] == 4
    assert row['likes'] == 2
    assert row['uploadDate'] == '2026-01-01T00:00:00+00:00'
    assert 'tags' not in row
    assert 'ocrText' not in row
    assert 'photoEmbedding' not in row


def test_build_snapshot_computes_capture_date_from_upload_date_fallback(sort_ctx):
    table, _, _ = sort_ctx
    _seed_row(table, 'lib-A', 'no-exif.jpg', uploadDate='2026-03-15T10:00:00+00:00')

    snapshot = storage_utils._build_user_sort_index_snapshot('lib-A', 'v1')

    assert snapshot.rows[0]['captureDate'] == '2026-03-15T10:00:00+00:00'


def test_build_snapshot_skips_deleted_and_missing_filename_rows(sort_ctx):
    table, _, _ = sort_ctx
    table.rows[('lib-A', '')] = {'PartitionKey': 'lib-A', 'RowKey': ''}
    _seed_row(table, 'lib-A', 'gone.jpg', processing_state='deleted')
    _seed_row(table, 'lib-A', 'real.jpg')

    snapshot = storage_utils._build_user_sort_index_snapshot('lib-A', 'v1')

    assert [row['RowKey'] for row in snapshot.rows] == ['real.jpg']


def test_build_snapshot_returns_none_when_table_unconfigured(monkeypatch):
    monkeypatch.setitem(storage_utils._CTX, 'metadata_table_client', None)
    assert storage_utils._build_user_sort_index_snapshot('lib-A', 'v1') is None


def test_build_snapshot_returns_none_on_query_exception_not_empty_rows(monkeypatch):
    class _BoomTable:
        def query_entities(self, filter_str):
            raise RuntimeError('table storage hiccup')

    monkeypatch.setitem(storage_utils._CTX, 'metadata_table_client', _BoomTable())
    assert storage_utils._build_user_sort_index_snapshot('lib-A', 'v1') is None


# --- serialize / deserialize round trip --------------------------------------

def test_serialize_round_trip_via_gzip_json():
    snapshot = storage_utils.LexicalIndexSnapshot(
        user_id='lib-A', source_version='v1', schema_version='v1',
        updated_at='v1', rows=[{'RowKey': 'a.jpg', 'rating': 3, 'likes': 0, 'uploadDate': None, 'captureDate': None}],
    )

    raw = storage_utils._serialize_sort_index(snapshot)
    parsed = json.loads(gzip.decompress(raw).decode('utf-8'))

    assert parsed['sourceVersion'] == 'v1'
    assert parsed['schemaVersion'] == 'v1'
    assert parsed['rows'][0]['RowKey'] == 'a.jpg'
    assert parsed['rows'][0]['rating'] == 3


def test_refresh_then_get_round_trips_through_the_fake_blob_service(sort_ctx):
    table, _, _ = sort_ctx
    _seed_row(table, 'lib-B', 'a.jpg', rating=5, likes=1, tags='["cat"]')

    refreshed = storage_utils.refresh_user_sort_index('lib-B', source_version='v1')
    assert refreshed is not None
    storage_utils.invalidate_user_sort_index_cache('lib-B')  # force the blob path, not the in-memory cache

    result = storage_utils.get_user_sort_index('lib-B', allow_refresh=False)

    assert result is not None
    assert len(result['rows']) == 1
    assert result['rows'][0]['RowKey'] == 'a.jpg'
    assert result['rows'][0]['rating'] == 5
    assert 'tags' not in result['rows'][0]


def test_get_user_sort_index_returns_none_without_refresh_when_never_built(sort_ctx):
    assert storage_utils.get_user_sort_index('lib-never-built', allow_refresh=False) is None


def test_get_user_sort_index_builds_on_first_call_with_allow_refresh(sort_ctx):
    table, _, _ = sort_ctx
    _seed_row(table, 'lib-C', 'a.jpg')

    result = storage_utils.get_user_sort_index('lib-C', allow_refresh=True)

    assert result is not None
    assert [row['RowKey'] for row in result['rows']] == ['a.jpg']


def test_get_user_sort_index_returned_rows_are_a_copy_not_the_shared_cache(sort_ctx):
    table, _, _ = sort_ctx
    _seed_row(table, 'lib-D', 'a.jpg', rating=1)

    first = storage_utils.get_user_sort_index('lib-D', allow_refresh=True)
    first['rows'][0]['rating'] = 999
    second = storage_utils.get_user_sort_index('lib-D', allow_refresh=True)

    assert second['rows'][0]['rating'] == 1


# --- independence from the lexical/vector indexes ----------------------------
# The entire point of a separate blob/manifest: a rating/likes edit must
# dirty ONLY the sort index, never trigger the lexical index's expensive
# full OCR/tag/embedding rebuild.

def test_touch_user_sort_index_dirty_does_not_touch_lexical_or_vector_state(monkeypatch):
    calls = []
    monkeypatch.setattr(storage_utils, 'touch_user_vector_index_state', lambda *a, **k: calls.append('vector'))
    monkeypatch.setattr(storage_utils, 'touch_user_lexical_index_state', lambda *a, **k: calls.append('lexical'))
    monkeypatch.setattr(storage_utils, 'touch_user_tag_embedding_index_state', lambda *a, **k: calls.append('tag'))

    storage_utils.touch_user_sort_index_dirty('lib-E', ['a.jpg'])

    assert calls == []


def test_touch_user_search_indexes_state_now_also_touches_sort(monkeypatch):
    calls = []
    monkeypatch.setattr(storage_utils, 'touch_user_vector_index_state', lambda *a, **k: calls.append('vector'))
    monkeypatch.setattr(storage_utils, 'touch_user_lexical_index_state', lambda *a, **k: calls.append('lexical'))
    monkeypatch.setattr(storage_utils, 'touch_user_tag_embedding_index_state', lambda *a, **k: calls.append('tag'))
    monkeypatch.setattr(storage_utils, 'touch_user_sort_index_state', lambda *a, **k: calls.append('sort'))

    storage_utils.touch_user_search_indexes_state('lib-F')

    assert set(calls) == {'vector', 'lexical', 'tag', 'sort'}


def test_metadata_updates_affect_sort_index_field_set():
    assert storage_utils.metadata_updates_affect_sort_index({'rating': 5}) is True
    assert storage_utils.metadata_updates_affect_sort_index({'likes': 1}) is True
    assert storage_utils.metadata_updates_affect_sort_index({'likedBy': '[]'}) is True
    assert storage_utils.metadata_updates_affect_sort_index({'tags': '[]'}) is False
    assert storage_utils.metadata_updates_affect_sort_index({}) is False


def test_update_metadata_fields_rating_change_dirties_sort_not_search_indexes(sort_ctx, monkeypatch):
    """End-to-end: a plain rating write through _update_metadata_fields must
    mark the sort index dirty and leave the lexical/vector/tag-embedding
    indexes alone."""
    table, _, _ = sort_ctx
    _seed_row(table, 'lib-G', 'a.jpg', rating=1)

    search_calls = []
    sort_calls = []
    monkeypatch.setattr(storage_utils, 'touch_user_search_indexes_state', lambda *a, **k: search_calls.append((a, k)))
    monkeypatch.setattr(storage_utils, 'touch_user_sort_index_dirty', lambda *a, **k: sort_calls.append((a, k)))

    storage_utils._update_metadata_fields('lib-G', 'a.jpg', {'rating': 5})

    assert search_calls == []
    assert sort_calls == [(('lib-G', ['a.jpg']), {})]


def test_update_metadata_fields_tag_change_still_dirties_search_indexes_not_sort(sort_ctx, monkeypatch):
    table, _, _ = sort_ctx
    _seed_row(table, 'lib-G2', 'a.jpg', tags='[]')

    search_calls = []
    sort_calls = []
    monkeypatch.setattr(storage_utils, 'touch_user_search_indexes_state', lambda *a, **k: search_calls.append((a, k)))
    monkeypatch.setattr(storage_utils, 'touch_user_sort_index_dirty', lambda *a, **k: sort_calls.append((a, k)))

    storage_utils._update_metadata_fields('lib-G2', 'a.jpg', {'tags': '["dog"]'})

    assert len(search_calls) == 1
    assert sort_calls == []


# --- serve-stale-while-rebuilding-in-background ------------------------------

def test_touch_marks_dirty_and_serves_stale_while_rebuilding_in_background(sort_ctx):
    table, _, _ = sort_ctx
    _seed_row(table, 'lib-H', 'a.jpg', rating=1)
    storage_utils.get_user_sort_index('lib-H', allow_refresh=True)

    _seed_row(table, 'lib-H', 'a.jpg', rating=9)  # rating changed
    storage_utils.touch_user_sort_index_dirty('lib-H', ['a.jpg'])

    immediate = storage_utils.get_user_sort_index('lib-H', allow_refresh=True)
    assert immediate['rows'][0]['rating'] == 1  # stale snapshot, served immediately

    lock = storage_utils._SORT_INDEX_REBUILD_LOCKS.lock_for('lib-H')
    for _ in range(50):
        if lock.acquire(blocking=False):
            lock.release()
            break
        time.sleep(0.05)
    else:
        pytest.fail('background sort index rebuild never completed')

    result = storage_utils.get_user_sort_index('lib-H', allow_refresh=True)
    assert result['rows'][0]['rating'] == 9


def test_get_user_sort_index_never_blocks_once_a_stale_snapshot_exists(sort_ctx, monkeypatch):
    table, _, _ = sort_ctx
    _seed_row(table, 'lib-I', 'a.jpg')
    storage_utils.get_user_sort_index('lib-I', allow_refresh=True)  # cold-start build, synchronous
    storage_utils.touch_user_sort_index_state('lib-I')
    # No filenames marked dirty in the dirty-filenames table below, so without
    # this, refresh_user_sort_index's incremental-merge branch would take an
    # empty dirty set as "nothing to refetch" and return instantly without
    # ever calling the (slow, monkeypatched) full builder this test exercises
    # -- forcing the full-rebuild fallback path, same as a real dirty-table
    # outage would.
    monkeypatch.setitem(storage_utils._CTX, 'search_index_dirty_table_client', None)

    entered = threading.Event()
    release = threading.Event()

    real_query = table.query_entities

    def slow_query(*args, **kwargs):
        entered.set()
        release.wait(timeout=5)
        return real_query(*args, **kwargs)

    table.query_entities = slow_query

    start = time.monotonic()
    result = storage_utils.get_user_sort_index('lib-I', allow_refresh=True)
    elapsed = time.monotonic() - start

    assert elapsed < 1.0, f'get_user_sort_index blocked for {elapsed:.2f}s on a warm-but-dirty index'
    assert [row['RowKey'] for row in result['rows']] == ['a.jpg']  # stale snapshot, served immediately
    assert entered.wait(timeout=5), 'background rebuild never started'

    release.set()
    lock = storage_utils._SORT_INDEX_REBUILD_LOCKS.lock_for('lib-I')
    for _ in range(50):
        if lock.acquire(blocking=False):
            lock.release()
            break
        time.sleep(0.05)
    else:
        pytest.fail('background sort index rebuild never completed')


# --- incremental merge: only dirty filenames are re-read ---------------------

def test_refresh_incremental_merge_only_refetches_dirty_filenames(sort_ctx):
    table, dirty_table, _ = sort_ctx
    _seed_row(table, 'lib-J', 'a.jpg', rating=1)
    _seed_row(table, 'lib-J', 'b.jpg', rating=2)
    storage_utils.refresh_user_sort_index('lib-J', source_version='v1')

    _seed_row(table, 'lib-J', 'b.jpg', rating=99)
    storage_utils.touch_user_sort_index_dirty('lib-J', ['b.jpg'])

    fetched = []
    real_get_entity = table.get_entity

    def spying_get_entity(partition_key, row_key):
        fetched.append(row_key)
        return real_get_entity(partition_key, row_key)

    table.get_entity = spying_get_entity

    refreshed = storage_utils.refresh_user_sort_index('lib-J', source_version='v2')

    assert fetched == ['b.jpg']  # a.jpg was carried over from the existing snapshot, not re-fetched
    assert refreshed.rows == []  # rows live in the blob, never in the returned snapshot
    by_name = {row['RowKey']: row for row in storage_utils._load_sort_index_blob('lib-J').rows}
    assert by_name['a.jpg']['rating'] == 1
    assert by_name['b.jpg']['rating'] == 99


# --- cleanup -------------------------------------------------------------

def test_delete_user_sort_index_data_removes_blobs_and_cache(sort_ctx):
    table, _, blob_service = sort_ctx
    _seed_row(table, 'lib-K', 'a.jpg')
    storage_utils.get_user_sort_index('lib-K', allow_refresh=True)
    assert storage_utils._SORT_INDEX_CACHE.get('lib-K') is not None

    storage_utils.delete_user_sort_index_data('lib-K')

    assert storage_utils._SORT_INDEX_CACHE.get('lib-K') is None
    container, blob_name = storage_utils.get_sort_index_blob_location('lib-K')
    assert f'{container}/{blob_name}' not in blob_service.blobs


# --- GET /api/photos/sort-index route ----------------------------------------
# The "not available" case deliberately returns 200 (not 503): a 503 here
# would hit httpClient.ts's cold-start retry loop (any 503 is treated as "the
# ingress rejected before reaching the app, safe to retry" -- see
# isRetriableColdStart), stalling the gallery for up to ~90s of retries before
# fetchPhotos's own fallback-to-legacy-endpoint path ever runs. This endpoint
# gates the primary gallery load, unlike photos_search_index (which 503s on
# its own "not available" case, fine there since it only gates a bonus search
# feature) -- so it needs the frontend to react immediately instead.

@pytest.fixture
def sort_index_route_ctx(monkeypatch):
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('owner', None))


def test_photos_sort_index_returns_200_available_false_when_index_none(monkeypatch, sort_index_route_ctx):
    monkeypatch.setattr(app, 'get_index_manifest_summary', lambda uid, kind: None)

    with app.app.test_request_context('/api/photos/sort-index'):
        response = photos_sort_index()

    assert not isinstance(response, tuple)  # no explicit status -> Flask's default 200
    assert response.status_code == 200
    assert response.get_json() == {'available': False}


def test_photos_sort_index_returns_200_available_false_on_sas_mint_failure(monkeypatch, sort_index_route_ctx):
    monkeypatch.setattr(app, 'get_index_manifest_summary', lambda uid, kind: {'source_version': 'v1', 'updated_at': 'v1', 'dirty': False})

    def _boom(*a, **k):
        raise RuntimeError('storage account not configured')

    monkeypatch.setattr(app, 'get_sort_index_blob_location', _boom)

    with app.app.test_request_context('/api/photos/sort-index'):
        response = photos_sort_index()

    assert response.status_code == 200
    assert response.get_json() == {'available': False}


def test_photos_sort_index_returns_available_true_with_sas_url(monkeypatch, sort_index_route_ctx):
    monkeypatch.setattr(app, 'get_index_manifest_summary', lambda uid, kind: {'source_version': 'v2', 'updated_at': '2026-01-01', 'dirty': False})
    monkeypatch.setattr(app, 'get_sort_index_blob_location', lambda *a, **k: ('lexical-index', 'abc-sort.json.gz'))
    monkeypatch.setattr(app, '_create_stable_read_sas_url', lambda *a, **k: ('https://example.invalid/abc-sort.json.gz?sas', '2026-01-02'))

    with app.app.test_request_context('/api/photos/sort-index'):
        response = photos_sort_index()

    assert response.status_code == 200
    payload = response.get_json()
    assert payload['available'] is True
    assert payload['indexUrl'] == 'https://example.invalid/abc-sort.json.gz?sas'
    assert payload['sourceVersion'] == 'v2'
    assert payload['updatedAt'] == '2026-01-01'


def test_photos_sort_index_route_never_loads_the_data_blob(monkeypatch, sort_index_route_ctx):
    """Regression pin for the 2026-09-30 OOM: the gallery-load endpoint must
    mint the URL from the manifest only (get_index_manifest_summary), never
    get_user_sort_index -- that loads+caches the whole sort blob into this 1Gi
    process, the memory pattern that OOM-crash-looped backend."""
    monkeypatch.setattr(app, 'get_index_manifest_summary', lambda uid, kind: {'source_version': 'v3', 'updated_at': 'v3', 'dirty': False})
    monkeypatch.setattr(app, 'get_sort_index_blob_location', lambda *a, **k: ('lexical-index', 'abc-sort.json.gz'))
    monkeypatch.setattr(app, '_create_stable_read_sas_url', lambda *a, **k: ('https://example.invalid/abc?sas', 'exp'))

    def _boom(*a, **k):
        raise AssertionError('sort-index must not call get_user_sort_index (loads the blob)')

    monkeypatch.setattr(app, 'get_user_sort_index', _boom)

    with app.app.test_request_context('/api/photos/sort-index'):
        response = photos_sort_index()

    assert response.get_json()['available'] is True


def test_photos_sort_index_dirty_manifest_triggers_tools_rebuild(monkeypatch, sort_index_route_ctx):
    monkeypatch.setattr(app, 'get_index_manifest_summary', lambda uid, kind: {'source_version': 'v4', 'updated_at': 'v4', 'dirty': True})
    monkeypatch.setattr(app, 'get_sort_index_blob_location', lambda *a, **k: ('lexical-index', 'abc-sort.json.gz'))
    monkeypatch.setattr(app, '_create_stable_read_sas_url', lambda *a, **k: ('https://example.invalid/abc?sas', 'exp'))
    triggered = []
    monkeypatch.setattr(app, '_trigger_indexer_rebuild', lambda uid, **kw: triggered.append(uid))

    with app.app.test_request_context('/api/photos/sort-index'):
        response = photos_sort_index()

    assert triggered == ['owner']
    assert response.get_json()['available'] is True


# --- cold-build is non-blocking + column projection --------------------------

def test_build_snapshot_selects_only_narrow_columns(sort_ctx, monkeypatch):
    """Regression pin for the memory fix: the full-library build must SELECT a
    narrow column set (no photoEmbedding/semanticEmbedding/ocrText/etc.), or a
    cold build re-creates the very O(library-size) memory spike this index is
    meant to relieve."""
    table, _, _ = sort_ctx
    _seed_row(table, 'lib-sel', 'a.jpg', rating=1)

    captured = {}
    real_query = table.query_entities

    def spying_query(filter_str, select=None):
        captured['select'] = select
        return real_query(filter_str, select=select)

    monkeypatch.setattr(table, 'query_entities', spying_query)

    storage_utils._build_user_sort_index_snapshot('lib-sel', 'v1')

    assert captured['select'] is not None, 'build must pass a select= projection, not scan all columns'
    assert 'photoEmbedding' not in captured['select']
    assert 'semanticEmbedding' not in captured['select']
    assert 'ocrText' not in captured['select']
    # ...but must keep everything captureDate derivation + the deleted filter need.
    for required in ('RowKey', 'processing_state', 'rating', 'likes', 'uploadDate', 'exifData', 'clientLastModified'):
        assert required in captured['select'], f'{required} missing from sort-index select'


def test_get_user_sort_index_cold_non_blocking_returns_none_then_builds_in_background(sort_ctx):
    """With allow_sync_build=False, a cold library (no snapshot ever built)
    returns None immediately WITHOUT running the full build inline, and kicks
    the build off-thread so the next read is fast. If it had built
    synchronously, the first call would have returned rows, not None."""
    table, _, _ = sort_ctx
    _seed_row(table, 'lib-cold', 'a.jpg', rating=3)

    result = storage_utils.get_user_sort_index('lib-cold', allow_refresh=True, allow_sync_build=False)
    assert result is None  # did NOT block on a synchronous build

    # The background rebuild it kicked off populates the blob; wait for the
    # rebuild lock to free, then a non-refreshing read should serve it.
    lock = storage_utils._SORT_INDEX_REBUILD_LOCKS.lock_for('lib-cold')
    for _ in range(50):
        if lock.acquire(blocking=False):
            lock.release()
            break
        time.sleep(0.05)
    else:
        pytest.fail('background sort index build never completed')

    built = storage_utils.get_user_sort_index('lib-cold', allow_refresh=False)
    assert built is not None
    assert [row['RowKey'] for row in built['rows']] == ['a.jpg']


def test_get_user_sort_index_cold_sync_build_default_still_builds_inline(sort_ctx):
    """Backward-compat pin: internal callers keep the default
    allow_sync_build=True and get a synchronous cold build (result in hand on
    the first call)."""
    table, _, _ = sort_ctx
    _seed_row(table, 'lib-sync', 'a.jpg', rating=3)

    result = storage_utils.get_user_sort_index('lib-sync', allow_refresh=True)
    assert result is not None
    assert [row['RowKey'] for row in result['rows']] == ['a.jpg']


def test_streaming_refresh_holds_no_rows_in_memory(sort_ctx):
    import tracemalloc
    table, _, _ = sort_ctx
    for i in range(3000):
        _seed_row(table, 'lib-M', f'p{i}.jpg', rating=i % 5, uploadDate=f'2026-01-01T00:00:{i % 60:02d}+00:00', note='x' * 50 + str(i))
    tracemalloc.start()
    snapshot = storage_utils.refresh_user_sort_index('lib-M', source_version='v1')
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert snapshot.rows == []
    loaded = storage_utils._load_sort_index_blob('lib-M')
    assert len(loaded.rows) == 3000
    assert peak < 3 * 1024 * 1024, f'peak {peak / 1048576:.1f} MB'


def test_streaming_merge_drops_deleted_and_appends_new_rows(sort_ctx):
    table, _, _ = sort_ctx
    _seed_row(table, 'lib-N', 'a.jpg', rating=1)
    _seed_row(table, 'lib-N', 'b.jpg', rating=2)
    storage_utils.refresh_user_sort_index('lib-N', source_version='v1')
    _seed_row(table, 'lib-N', 'b.jpg', rating=2, processing_state='deleted')
    _seed_row(table, 'lib-N', 'c.jpg', rating=3)
    storage_utils.touch_user_sort_index_dirty('lib-N', ['b.jpg', 'c.jpg'])
    storage_utils.refresh_user_sort_index('lib-N', source_version='v2')
    rows = storage_utils._load_sort_index_blob('lib-N').rows
    assert [r['RowKey'] for r in rows] == ['a.jpg', 'c.jpg']


def test_sort_index_route_tells_huge_libraries_to_page_from_the_server(monkeypatch):
    from routes import photos
    import app as app_module
    monkeypatch.setattr(app_module, '_require_user_id', lambda *a, **k: ('u1', None))
    monkeypatch.setattr(app_module, 'get_index_manifest_summary',
                        lambda uid, kind: {'source_version': 'v1', 'updated_at': 'v1', 'dirty': False, 'row_count': photos.SORT_INDEX_CLIENT_MAX_ROWS + 1})
    monkeypatch.setattr(app_module, 'get_sort_index_blob_location', lambda uid: (_ for _ in ()).throw(AssertionError('no SAS for a huge library')))
    with app_module.app.test_request_context('/api/photos/sort-index'):
        payload = photos.photos_sort_index().get_json()
    assert payload == {'available': False, 'reason': 'library_too_large', 'rowCount': photos.SORT_INDEX_CLIENT_MAX_ROWS + 1}


def test_sort_index_route_still_serves_libraries_under_the_limit(monkeypatch):
    from routes import photos
    import app as app_module
    monkeypatch.setattr(app_module, '_require_user_id', lambda *a, **k: ('u1', None))
    monkeypatch.setattr(app_module, 'get_index_manifest_summary',
                        lambda uid, kind: {'source_version': 'v1', 'updated_at': 'v1', 'dirty': False, 'row_count': 5000})
    monkeypatch.setattr(app_module, 'get_sort_index_blob_location', lambda uid: ('c', 'b'))
    monkeypatch.setattr(app_module, '_create_stable_read_sas_url', lambda c, b: ('https://x/b?sig=1', 'later'))
    with app_module.app.test_request_context('/api/photos/sort-index'):
        payload = photos.photos_sort_index().get_json()
    assert payload['available'] is True and payload['indexUrl'].startswith('https://x/')
