"""Unit tests for the lexical search index (storage_utils.get_user_lexical_index
and friends), added to fix /photos/search timing out on large libraries.

search_photos() used to read every request via _cached_metadata_rows_for_user,
a full unprojected Azure Table scan of the user's whole metadata partition
(measured at 60-75s for a 36,633-row library live in QA -- see
[[universal-preview-tier-and-fr-button]]-adjacent investigation). This mirrors
the existing vector-index blob-cache architecture (get_user_vector_index) for
the lexical/location/people/date half of search: a per-user blob, lazily
rebuilt only when a write marks it dirty, holding every row field except the
two large, redundant, unused-by-search embedding arrays.
"""
from __future__ import annotations

import gzip
import json
import threading

import pytest

import app
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
def lexical_ctx(monkeypatch):
    table = FakeTable()
    blob_service = _FakeBlobServiceClient()
    monkeypatch.setitem(storage_utils._CTX, 'metadata_table_client', table)
    monkeypatch.setitem(storage_utils._CTX, 'blob_service_client', blob_service)
    monkeypatch.setitem(storage_utils._CTX, 'blob_lexical_index_container', 'lexical-index')
    yield table, blob_service


# --- _build_user_lexical_index_snapshot -------------------------------------

def test_build_snapshot_excludes_embedding_fields_keeps_others(lexical_ctx):
    table, _ = lexical_ctx
    _seed_row(
        table, 'lib-A', 'photo.jpg',
        tags='["dog"]', caption='a dog', photoEmbedding='[0.1, 0.2]',
        semanticEmbedding='[0.3, 0.4]', photoEmbeddingVersion='v1',
    )

    snapshot = storage_utils._build_user_lexical_index_snapshot('lib-A', 'v1')

    assert snapshot is not None
    assert len(snapshot.rows) == 1
    row = snapshot.rows[0]
    assert 'photoEmbedding' not in row
    assert 'semanticEmbedding' not in row
    assert row['tags'] == '["dog"]'
    assert row['caption'] == 'a dog'
    assert row['photoEmbeddingVersion'] == 'v1'  # small version string, not the array itself -- kept


def test_build_snapshot_skips_rows_without_a_filename(lexical_ctx):
    table, _ = lexical_ctx
    table.rows[('lib-A', '')] = {'PartitionKey': 'lib-A', 'RowKey': ''}
    _seed_row(table, 'lib-A', 'real.jpg', tags='[]')

    snapshot = storage_utils._build_user_lexical_index_snapshot('lib-A', 'v1')

    assert [row['RowKey'] for row in snapshot.rows] == ['real.jpg']


def test_build_snapshot_returns_none_when_table_unconfigured(monkeypatch):
    monkeypatch.setitem(storage_utils._CTX, 'metadata_table_client', None)
    assert storage_utils._build_user_lexical_index_snapshot('lib-A', 'v1') is None


def test_build_snapshot_returns_none_on_query_exception_not_empty_rows(monkeypatch):
    """A transient query failure must not be persisted as an empty library --
    refresh_user_lexical_index would otherwise happily overwrite a previously
    good index with a dirty:false empty one."""
    class _BoomTable:
        def query_entities(self, filter_str):
            raise RuntimeError('table storage hiccup')

    monkeypatch.setitem(storage_utils._CTX, 'metadata_table_client', _BoomTable())
    assert storage_utils._build_user_lexical_index_snapshot('lib-A', 'v1') is None


# --- serialize / deserialize round trip -------------------------------------

def test_serialize_round_trip_via_gzip_json():
    snapshot = storage_utils.LexicalIndexSnapshot(
        user_id='lib-A', source_version='v1', schema_version='v1',
        updated_at='v1', rows=[{'RowKey': 'a.jpg', 'tags': '["cat"]'}],
    )

    raw = storage_utils._serialize_lexical_index(snapshot)
    parsed = json.loads(gzip.decompress(raw).decode('utf-8'))

    assert parsed['sourceVersion'] == 'v1'
    assert parsed['schemaVersion'] == 'v1'
    assert parsed['rows'] == [{'RowKey': 'a.jpg', 'tags': '["cat"]'}]


def test_refresh_then_get_round_trips_through_the_fake_blob_service(lexical_ctx):
    table, _ = lexical_ctx
    _seed_row(table, 'lib-B', 'a.jpg', tags='["cat"]', photoEmbedding='[1.0]')

    refreshed = storage_utils.refresh_user_lexical_index('lib-B', source_version='v1')
    assert refreshed is not None
    storage_utils.invalidate_user_lexical_index_cache('lib-B')  # force the blob path, not the in-memory cache

    result = storage_utils.get_user_lexical_index('lib-B', allow_refresh=False)

    assert result is not None
    assert len(result['rows']) == 1
    assert result['rows'][0]['RowKey'] == 'a.jpg'
    assert 'photoEmbedding' not in result['rows'][0]


def test_get_user_lexical_index_returns_none_without_refresh_when_never_built(lexical_ctx):
    assert storage_utils.get_user_lexical_index('lib-never-built', allow_refresh=False) is None


def test_get_user_lexical_index_builds_on_first_call_with_allow_refresh(lexical_ctx):
    table, _ = lexical_ctx
    _seed_row(table, 'lib-C', 'a.jpg', tags='[]')

    result = storage_utils.get_user_lexical_index('lib-C', allow_refresh=True)

    assert result is not None
    assert [row['RowKey'] for row in result['rows']] == ['a.jpg']


def test_get_user_lexical_index_returned_rows_are_a_copy_not_the_shared_cache(lexical_ctx):
    table, _ = lexical_ctx
    _seed_row(table, 'lib-D', 'a.jpg', tags='[]')

    first = storage_utils.get_user_lexical_index('lib-D', allow_refresh=True)
    first['rows'][0]['tags'] = 'mutated'
    second = storage_utils.get_user_lexical_index('lib-D', allow_refresh=True)

    assert second['rows'][0]['tags'] == '[]'


def test_touch_marks_dirty_and_forces_a_rebuild_on_next_read(lexical_ctx):
    table, _ = lexical_ctx
    _seed_row(table, 'lib-E', 'a.jpg', tags='[]')
    storage_utils.get_user_lexical_index('lib-E', allow_refresh=True)

    _seed_row(table, 'lib-E', 'b.jpg', tags='[]')  # library changed
    storage_utils.touch_user_lexical_index_state('lib-E')
    result = storage_utils.get_user_lexical_index('lib-E', allow_refresh=True)

    assert sorted(row['RowKey'] for row in result['rows']) == ['a.jpg', 'b.jpg']


def test_touch_user_lexical_index_state_is_a_safe_noop_without_blob_client(monkeypatch):
    monkeypatch.setitem(storage_utils._CTX, 'blob_service_client', object())
    # Must not raise even though the dummy object() has no get_blob_client.
    version = storage_utils.touch_user_lexical_index_state('lib-F')
    assert version  # still returns a source_version string


# --- touch_user_search_indexes_state consolidation --------------------------

def test_touch_user_search_indexes_state_touches_both_indexes(monkeypatch):
    calls = []
    monkeypatch.setattr(storage_utils, 'touch_user_vector_index_state', lambda *a, **k: calls.append(('vector', a, k)))
    monkeypatch.setattr(storage_utils, 'touch_user_lexical_index_state', lambda *a, **k: calls.append(('lexical', a, k)))

    storage_utils.touch_user_search_indexes_state('lib-G')

    kinds = {c[0] for c in calls}
    assert kinds == {'vector', 'lexical'}


def test_metadata_updates_affect_search_indexes_includes_processing_metadata():
    """Regression pin: the two hand-copied field lists (storage_utils vs. the
    old inline copy in app.py's _update_metadata_entity_fields) had already
    drifted -- app.py's copy was missing 'processing_metadata'. Now there is
    exactly one canonical gate; this pins its actual field set."""
    assert storage_utils.metadata_updates_affect_search_indexes({'processing_metadata': '{}'}) is True
    assert storage_utils.metadata_updates_affect_search_indexes({'rating': 5}) is False
    assert storage_utils.metadata_updates_affect_search_indexes({}) is False


# --- coalescing: concurrent rebuilds for the same user share one build -----

def test_get_user_lexical_index_coalesces_concurrent_rebuilds(monkeypatch, lexical_ctx):
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def fake_build(user_id, source_version):
        calls.append(source_version)
        entered.set()
        release.wait(timeout=5)
        return storage_utils.LexicalIndexSnapshot(
            user_id=user_id, source_version=source_version,
            schema_version=storage_utils._LEXICAL_INDEX_SCHEMA_VERSION,
            updated_at=source_version, rows=[],
        )

    monkeypatch.setattr(storage_utils, '_build_user_lexical_index_snapshot', fake_build)

    results = []

    def call():
        results.append(storage_utils.get_user_lexical_index('lib-H', allow_refresh=True))

    t1 = threading.Thread(target=call)
    t1.start()
    assert entered.wait(timeout=5), 't1 never entered the expensive build'

    t2 = threading.Thread(target=call)
    t2.start()
    t2.join(timeout=0.2)  # t2 should be blocked on the per-user lock, not finished
    assert t2.is_alive()

    release.set()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert len(calls) == 1
    assert all(r is not None for r in results)


# --- search_photos fallback --------------------------------------------------

def _fallback_row(filename: str) -> dict:
    return {
        'RowKey': filename,
        'PartitionKey': 'owner',
        'tags': '[]',
        'objects': '[]',
        'peopleIds': '[]',
        'peopleNames': '[]',
        'exifData': '{}',
        'processing_metadata': '{}',
    }


@pytest.fixture
def search_route_ctx(monkeypatch):
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('owner', None))
    monkeypatch.setattr(app, 'person_table_client', None)  # _load_people_name_index short-circuits
    monkeypatch.setattr(app.vision_utils, 'encode_text_embedding', lambda text: [])
    monkeypatch.setattr(app, 'vector_search_candidates', lambda *a, **k: [])


def test_search_falls_back_to_full_scan_when_lexical_index_unavailable(monkeypatch, search_route_ctx):
    monkeypatch.setattr(app, 'get_user_lexical_index', lambda *a, **k: None)
    monkeypatch.setattr(app, '_cached_metadata_rows_for_user', lambda *a, **k: [_fallback_row('vacation.jpg')])

    with app.app.test_request_context('/photos/search?q=vacation'):
        response = app.search_photos()

    payload = response.get_json() if hasattr(response, 'get_json') else response[0].get_json()
    filenames = [p['filename'] for p in payload['photos']]
    assert 'vacation.jpg' in filenames


def test_search_503s_when_lexical_index_and_fallback_scan_both_fail(monkeypatch, search_route_ctx):
    monkeypatch.setattr(app, 'get_user_lexical_index', lambda *a, **k: None)

    def _boom(*a, **k):
        raise RuntimeError('table storage hiccup')

    monkeypatch.setattr(app, '_cached_metadata_rows_for_user', _boom)

    with app.app.test_request_context('/photos/search?q=vacation'):
        response = app.search_photos()

    assert response[1] == 503


def test_search_uses_lexical_index_rows_when_available(monkeypatch, search_route_ctx):
    monkeypatch.setattr(app, 'get_user_lexical_index', lambda *a, **k: {'rows': [_fallback_row('fromindex.jpg')]})

    def _boom(*a, **k):
        raise AssertionError('should not fall back to the full scan when the lexical index is available')

    monkeypatch.setattr(app, '_cached_metadata_rows_for_user', _boom)

    with app.app.test_request_context('/photos/search?q=fromindex'):
        response = app.search_photos()

    payload = response.get_json() if hasattr(response, 'get_json') else response[0].get_json()
    filenames = [p['filename'] for p in payload['photos']]
    assert 'fromindex.jpg' in filenames
