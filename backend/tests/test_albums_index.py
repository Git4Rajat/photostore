"""Unit tests for the albums index (storage_utils.get_user_albums_index and
friends), added so the Albums page can render its list and open an album
without either of the two real inefficiencies this replaces:

  - list_albums's per-album cover computation (_album_cover_thumbnail_url)
    rode the whole-library rating/likes/date sorted scan; this index picks
    each album's cover from the already-built sort index's rows instead.
  - get_album's _load_photos_for_filenames did a sequential, non-batched,
    non-concurrent Table point-read per photo in the album; the index
    carries each album's filenames directly so the frontend can resolve full
    photo data via the existing batched /api/photos/lookup-batch instead.

Mirrors test_sort_index.py's structure, minus the incremental-merge tests:
album count is small relative to library size (the reason the sort/lexical
indexes need point-read-only incremental rebuilds), so the albums index
always does a full rebuild -- see the module comment in storage_utils.py.
"""
from __future__ import annotations

import gzip
import json
import threading
import time

import pytest

import app
from routes.albums import albums_index
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


def _seed_album(table: FakeTable, user_id: str, album_id: str, **overrides) -> None:
    row = {
        'PartitionKey': user_id, 'RowKey': album_id,
        'name': overrides.pop('name', album_id), 'filenames': '[]',
        'updatedAt': '2026-01-01T00:00:00+00:00',
        **overrides,
    }
    table.upsert_entity(row)


def _seed_photo(table: FakeTable, user_id: str, filename: str, **overrides) -> None:
    row = {'PartitionKey': user_id, 'RowKey': filename, **overrides}
    table.upsert_entity(row)


@pytest.fixture
def albums_ctx(monkeypatch):
    albums_table = FakeTable()
    metadata_table = FakeTable()
    blob_service = _FakeBlobServiceClient()
    monkeypatch.setitem(storage_utils._CTX, 'albums_table_client', albums_table)
    monkeypatch.setitem(storage_utils._CTX, 'metadata_table_client', metadata_table)
    monkeypatch.setitem(storage_utils._CTX, 'search_index_dirty_table_client', None)
    monkeypatch.setitem(storage_utils._CTX, 'blob_service_client', blob_service)
    monkeypatch.setitem(storage_utils._CTX, 'blob_lexical_index_container', 'lexical-index')
    yield albums_table, metadata_table, blob_service


# --- _build_user_albums_index_snapshot ---------------------------------------

def test_build_snapshot_projects_expected_fields(albums_ctx):
    albums_table, metadata_table, _ = albums_ctx
    _seed_album(albums_table, 'lib-A', 'alb-1', name='Trip', filenames=json.dumps(['a.jpg', 'b.jpg']))
    _seed_photo(metadata_table, 'lib-A', 'a.jpg', rating=2, likes=0, uploadDate='2026-01-01T00:00:00+00:00')
    _seed_photo(metadata_table, 'lib-A', 'b.jpg', rating=5, likes=1, uploadDate='2026-01-02T00:00:00+00:00')
    storage_utils.refresh_user_sort_index('lib-A', source_version='sv1')  # cover source

    snapshot = storage_utils._build_user_albums_index_snapshot('lib-A', 'v1')

    assert snapshot is not None
    assert len(snapshot.rows) == 1
    row = snapshot.rows[0]
    assert row['albumId'] == 'alb-1'
    assert row['name'] == 'Trip'
    assert row['photoCount'] == 2
    assert row['coverFilename'] == 'b.jpg'  # higher rating wins
    assert row['filenames'] == ['a.jpg', 'b.jpg']


def test_build_snapshot_includes_share_status_fields(albums_ctx, monkeypatch):
    """AlbumsPage.tsx reads album.isPublic/publicUrl/hasAccessCode directly --
    the index must carry these or the sharing UI regresses to always showing
    'Share' instead of 'Sharing on' for an already-shared album."""
    monkeypatch.setenv('EXTRAS_PUBLIC_BASE_URL', 'https://extras.example.invalid')
    albums_table, _, _ = albums_ctx
    _seed_album(
        albums_table, 'lib-A', 'alb-1',
        isPublic=True, publicToken='tok123', accessCode='secret', publicExpiresAt='',
    )

    snapshot = storage_utils._build_user_albums_index_snapshot('lib-A', 'v1')

    row = snapshot.rows[0]
    assert row['isPublic'] is True
    assert row['publicUrl'] == 'https://extras.example.invalid/public/album/tok123'
    assert row['hasAccessCode'] is True
    assert row['isExpired'] is False


def test_build_snapshot_expired_share_is_not_public(albums_ctx, monkeypatch):
    monkeypatch.setenv('EXTRAS_PUBLIC_BASE_URL', 'https://extras.example.invalid')
    albums_table, _, _ = albums_ctx
    _seed_album(
        albums_table, 'lib-A', 'alb-1',
        isPublic=True, publicToken='tok123', publicExpiresAt='2020-01-01T00:00:00+00:00',
    )

    snapshot = storage_utils._build_user_albums_index_snapshot('lib-A', 'v1')

    row = snapshot.rows[0]
    assert row['isExpired'] is True
    assert row['isPublic'] is False
    assert row['publicUrl'] == ''


def test_build_snapshot_skips_deleted_albums(albums_ctx):
    albums_table, _, _ = albums_ctx
    _seed_album(albums_table, 'lib-A', 'alb-gone', deleted='true')
    _seed_album(albums_table, 'lib-A', 'alb-real')

    snapshot = storage_utils._build_user_albums_index_snapshot('lib-A', 'v1')

    assert [row['albumId'] for row in snapshot.rows] == ['alb-real']


def test_build_snapshot_returns_none_when_table_unconfigured(monkeypatch):
    monkeypatch.setitem(storage_utils._CTX, 'albums_table_client', None)
    assert storage_utils._build_user_albums_index_snapshot('lib-A', 'v1') is None


def test_build_snapshot_returns_none_on_query_exception_not_empty(monkeypatch):
    class _BoomTable:
        def query_entities(self, filter_str):
            raise RuntimeError('table storage hiccup')

    monkeypatch.setitem(storage_utils._CTX, 'albums_table_client', _BoomTable())
    assert storage_utils._build_user_albums_index_snapshot('lib-A', 'v1') is None


def test_build_snapshot_cover_falls_back_to_first_filename_when_sort_index_cold(albums_ctx):
    """If the sort index has no data for an album's photos (cold/never
    built), cover selection must not crash or block -- just fall back to the
    album's first filename in stored order."""
    albums_table, _, _ = albums_ctx
    _seed_album(albums_table, 'lib-A', 'alb-1', filenames=json.dumps(['x.jpg', 'y.jpg']))
    # Deliberately do NOT build a sort index for lib-A.

    snapshot = storage_utils._build_user_albums_index_snapshot('lib-A', 'v1')

    assert snapshot.rows[0]['coverFilename'] == 'x.jpg'


def test_build_snapshot_never_blocks_on_a_cold_sort_index(albums_ctx, monkeypatch):
    """The sort-index lookup inside the albums-index build must pass
    allow_sync_build=False -- building an albums index must never trigger (or
    wait on) a ~60s+ cold sort-index build."""
    albums_table, _, _ = albums_ctx
    _seed_album(albums_table, 'lib-A', 'alb-1', filenames=json.dumps(['x.jpg']))

    captured = {}

    def _spy(user_id, **kwargs):
        captured.update(kwargs)
        return None

    monkeypatch.setattr(storage_utils, 'get_user_sort_index', _spy)

    storage_utils._build_user_albums_index_snapshot('lib-A', 'v1')

    assert captured.get('allow_sync_build') is False


# --- serialize / round trip ---------------------------------------------------

def test_serialize_round_trip_via_gzip_json():
    snapshot = storage_utils.LexicalIndexSnapshot(
        user_id='lib-A', source_version='v1', schema_version='v1', updated_at='v1',
        rows=[{'albumId': 'a1', 'name': 'Trip', 'photoCount': 1, 'coverFilename': 'a.jpg', 'updatedAt': 'v1', 'filenames': ['a.jpg']}],
    )

    raw = storage_utils._serialize_albums_index(snapshot)
    parsed = json.loads(gzip.decompress(raw).decode('utf-8'))

    assert parsed['sourceVersion'] == 'v1'
    assert parsed['rows'][0]['albumId'] == 'a1'


def test_refresh_then_get_round_trips_through_the_fake_blob_service(albums_ctx):
    albums_table, _, _ = albums_ctx
    _seed_album(albums_table, 'lib-B', 'alb-1', name='Trip', filenames=json.dumps(['a.jpg']))

    refreshed = storage_utils.refresh_user_albums_index('lib-B', source_version='v1')
    assert refreshed is not None
    storage_utils.invalidate_user_albums_index_cache('lib-B')  # force the blob path, not the in-memory cache

    result = storage_utils.get_user_albums_index('lib-B', allow_refresh=False)

    assert result is not None
    assert len(result['rows']) == 1
    assert result['rows'][0]['albumId'] == 'alb-1'


def test_get_user_albums_index_returns_none_without_refresh_when_never_built(albums_ctx):
    assert storage_utils.get_user_albums_index('lib-never-built', allow_refresh=False) is None


def test_get_user_albums_index_builds_on_first_call_with_allow_refresh(albums_ctx):
    albums_table, _, _ = albums_ctx
    _seed_album(albums_table, 'lib-C', 'alb-1')

    result = storage_utils.get_user_albums_index('lib-C', allow_refresh=True)

    assert result is not None
    assert [row['albumId'] for row in result['rows']] == ['alb-1']


def test_get_user_albums_index_returned_rows_are_a_copy_not_the_shared_cache(albums_ctx):
    albums_table, _, _ = albums_ctx
    _seed_album(albums_table, 'lib-D', 'alb-1', name='Original')

    first = storage_utils.get_user_albums_index('lib-D', allow_refresh=True)
    first['rows'][0]['name'] = 'mutated'
    second = storage_utils.get_user_albums_index('lib-D', allow_refresh=True)

    assert second['rows'][0]['name'] == 'Original'


# --- cold + non-blocking (allow_sync_build=False) -----------------------------

def test_get_user_albums_index_cold_non_blocking_returns_none_then_builds_in_background(albums_ctx):
    albums_table, _, _ = albums_ctx
    _seed_album(albums_table, 'lib-cold', 'alb-1')

    result = storage_utils.get_user_albums_index('lib-cold', allow_refresh=True, allow_sync_build=False)
    assert result is None  # did NOT block on a synchronous build

    lock = storage_utils._ALBUMS_INDEX_REBUILD_LOCKS.lock_for('lib-cold')
    for _ in range(50):
        if lock.acquire(blocking=False):
            lock.release()
            break
        time.sleep(0.05)
    else:
        pytest.fail('background albums index build never completed')

    built = storage_utils.get_user_albums_index('lib-cold', allow_refresh=False)
    assert built is not None
    assert [row['albumId'] for row in built['rows']] == ['alb-1']


def test_get_user_albums_index_cold_sync_build_default_still_builds_inline(albums_ctx):
    albums_table, _, _ = albums_ctx
    _seed_album(albums_table, 'lib-sync', 'alb-1')

    result = storage_utils.get_user_albums_index('lib-sync', allow_refresh=True)
    assert result is not None


def test_get_user_albums_index_never_blocks_once_a_stale_snapshot_exists(albums_ctx, monkeypatch):
    albums_table, _, _ = albums_ctx
    _seed_album(albums_table, 'lib-I', 'alb-1')
    storage_utils.get_user_albums_index('lib-I', allow_refresh=True)  # cold-start build, synchronous
    storage_utils.touch_user_albums_index_state('lib-I')

    entered = threading.Event()
    release = threading.Event()

    def slow_build(user_id, source_version):
        entered.set()
        release.wait(timeout=5)
        return storage_utils.LexicalIndexSnapshot(
            user_id=user_id, source_version=source_version,
            schema_version=storage_utils._ALBUMS_INDEX_SCHEMA_VERSION,
            updated_at=source_version, rows=[{'albumId': 'alb-2', 'name': 'B', 'photoCount': 0, 'coverFilename': '', 'updatedAt': source_version, 'filenames': []}],
        )

    monkeypatch.setattr(storage_utils, '_build_user_albums_index_snapshot', slow_build)

    start = time.monotonic()
    result = storage_utils.get_user_albums_index('lib-I', allow_refresh=True)
    elapsed = time.monotonic() - start

    assert elapsed < 1.0, f'get_user_albums_index blocked for {elapsed:.2f}s on a warm-but-dirty index'
    assert [row['albumId'] for row in result['rows']] == ['alb-1']  # stale snapshot, served immediately
    assert entered.wait(timeout=5), 'background rebuild never started'

    release.set()
    lock = storage_utils._ALBUMS_INDEX_REBUILD_LOCKS.lock_for('lib-I')
    for _ in range(50):
        if lock.acquire(blocking=False):
            lock.release()
            break
        time.sleep(0.05)
    else:
        pytest.fail('background albums index rebuild never completed')


# --- dirty-marking wiring ------------------------------------------------------

def test_save_album_entity_marks_albums_index_dirty(monkeypatch, albums_ctx):
    albums_table, _, _ = albums_ctx
    # app.py's own _save_album_entity reads its own module-level
    # albums_table_client global, separate from storage_utils._CTX -- the
    # albums_ctx fixture only patches the latter, so this needs its own patch
    # (same pattern as search_route_ctx patching app.person_table_client).
    monkeypatch.setattr(app, 'albums_table_client', albums_table)
    calls = []
    monkeypatch.setattr(app, 'touch_user_albums_index_state', lambda user_id: calls.append(user_id))

    app._save_album_entity({'PartitionKey': 'lib-E', 'RowKey': 'alb-1', 'name': 'x', 'filenames': '[]'})

    assert calls == ['lib-E']


def test_hard_delete_album_now_marks_albums_index_dirty(monkeypatch, albums_ctx):
    albums_table, _, _ = albums_ctx
    monkeypatch.setattr(app, 'albums_table_client', albums_table)
    _seed_album(albums_table, 'lib-F', 'alb-1')
    calls = []
    monkeypatch.setattr(app, 'touch_user_albums_index_state', lambda user_id: calls.append(user_id))

    ok = app._hard_delete_album_now('lib-F', 'alb-1')

    assert ok is True
    assert calls == ['lib-F']


def test_touch_user_sort_index_state_also_touches_albums_index(monkeypatch):
    """Rating/likes changes (and anything else that dirties the sort index)
    must also dirty the albums index, since cover selection is derived from
    sort-index data."""
    calls = []
    monkeypatch.setattr(storage_utils, 'touch_user_albums_index_state', lambda user_id: calls.append(user_id))

    storage_utils.touch_user_sort_index_state('lib-G')

    assert calls == ['lib-G']


def test_rating_change_dirties_albums_index_via_sort_index_hook(albums_ctx, monkeypatch):
    """End-to-end: a plain rating write must transitively dirty the albums
    index (rating change -> touch_user_sort_index_dirty -> touch_user_sort_index_state
    -> touch_user_albums_index_state)."""
    albums_table, metadata_table, _ = albums_ctx
    _seed_photo(metadata_table, 'lib-H', 'a.jpg', rating=1)

    calls = []
    monkeypatch.setattr(storage_utils, 'touch_user_albums_index_state', lambda user_id: calls.append(user_id))

    storage_utils._update_metadata_fields('lib-H', 'a.jpg', {'rating': 5})

    assert calls == ['lib-H']


# --- cleanup -------------------------------------------------------------

def test_delete_user_albums_index_data_removes_blobs_and_cache(albums_ctx):
    albums_table, _, blob_service = albums_ctx
    _seed_album(albums_table, 'lib-K', 'alb-1')
    storage_utils.get_user_albums_index('lib-K', allow_refresh=True)
    assert storage_utils._ALBUMS_INDEX_CACHE.get('lib-K') is not None

    storage_utils.delete_user_albums_index_data('lib-K')

    assert storage_utils._ALBUMS_INDEX_CACHE.get('lib-K') is None
    container, blob_name = storage_utils.get_albums_index_blob_location('lib-K')
    assert f'{container}/{blob_name}' not in blob_service.blobs


# --- GET /api/albums/index route ----------------------------------------------

@pytest.fixture
def albums_index_route_ctx(monkeypatch):
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('owner', None))


def test_albums_index_returns_200_available_false_when_index_none(monkeypatch, albums_index_route_ctx):
    monkeypatch.setattr(app, 'get_user_albums_index', lambda *a, **k: None)

    with app.app.test_request_context('/api/albums/index'):
        response = albums_index()

    assert not isinstance(response, tuple)
    assert response.status_code == 200
    assert response.get_json() == {'available': False}


def test_albums_index_passes_allow_sync_build_false(monkeypatch, albums_index_route_ctx):
    captured = {}

    def _spy(user_id, **kwargs):
        captured.update(kwargs)
        return None

    monkeypatch.setattr(app, 'get_user_albums_index', _spy)

    with app.app.test_request_context('/api/albums/index'):
        albums_index()

    assert captured.get('allow_sync_build') is False


def test_albums_index_returns_200_available_false_on_sas_mint_failure(monkeypatch, albums_index_route_ctx):
    monkeypatch.setattr(app, 'get_user_albums_index', lambda *a, **k: {'source_version': 'v1', 'updated_at': 'v1', 'rows': []})

    def _boom(*a, **k):
        raise RuntimeError('storage account not configured')

    monkeypatch.setattr(app, 'get_albums_index_blob_location', _boom)

    with app.app.test_request_context('/api/albums/index'):
        response = albums_index()

    assert response.status_code == 200
    assert response.get_json() == {'available': False}


def test_albums_index_returns_available_true_with_sas_url(monkeypatch, albums_index_route_ctx):
    monkeypatch.setattr(app, 'get_user_albums_index', lambda *a, **k: {'source_version': 'v2', 'updated_at': '2026-01-01', 'rows': []})
    monkeypatch.setattr(app, 'get_albums_index_blob_location', lambda *a, **k: ('lexical-index', 'abc-albums.json.gz'))
    monkeypatch.setattr(app, '_create_stable_read_sas_url', lambda *a, **k: ('https://example.invalid/abc-albums.json.gz?sas', '2026-01-02'))

    with app.app.test_request_context('/api/albums/index'):
        response = albums_index()

    assert response.status_code == 200
    payload = response.get_json()
    assert payload['available'] is True
    assert payload['indexUrl'] == 'https://example.invalid/abc-albums.json.gz?sas'
    assert payload['sourceVersion'] == 'v2'
