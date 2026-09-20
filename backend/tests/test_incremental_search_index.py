"""Coverage for incremental (dirty-set) search-index rebuilds.

The lexical/vector indexes used to be rebuilt as an all-or-nothing full
re-scan (lexical) / re-embed (vector) of a user's entire library on ANY
single-photo edit -- a rating change on one photo re-embedded the other
35,999. Each edit now marks just its own filename dirty (see
touch_user_search_indexes_state), and a rebuild only re-fetches/merges the
dirty subset into the existing snapshot instead of re-scanning everything.
"""
from __future__ import annotations

import pytest

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


class _CountingTable(FakeTable):
    """Wraps FakeTable to count full-partition scans separately from
    point-reads, so tests can assert an incremental refresh never falls back
    to scanning the whole library."""

    def __init__(self) -> None:
        super().__init__()
        self.scan_count = 0
        self.get_entity_calls: list = []

    def query_entities(self, filter_str, select=None):
        self.scan_count += 1
        return super().query_entities(filter_str, select=select)

    def get_entity(self, partition_key, row_key):
        self.get_entity_calls.append((partition_key, row_key))
        return super().get_entity(partition_key, row_key)


def _seed_row(table, user_id: str, filename: str, **overrides) -> None:
    table.upsert_entity({'PartitionKey': user_id, 'RowKey': filename, **overrides})


@pytest.fixture
def ctx(monkeypatch):
    metadata = _CountingTable()
    dirty = FakeTable()
    blob_service = _FakeBlobServiceClient()
    monkeypatch.setitem(storage_utils._CTX, 'metadata_table_client', metadata)
    monkeypatch.setitem(storage_utils._CTX, 'embeddings_table_client', None)
    monkeypatch.setitem(storage_utils._CTX, 'search_index_dirty_table_client', dirty)
    monkeypatch.setitem(storage_utils._CTX, 'blob_service_client', blob_service)
    monkeypatch.setitem(storage_utils._CTX, 'blob_lexical_index_container', 'lexical-index')
    monkeypatch.setitem(storage_utils._CTX, 'blob_vector_index_container', 'vector-index')
    yield metadata, dirty, blob_service


# --- dirty-set bookkeeping ---------------------------------------------------

def test_touch_marks_both_index_kinds_dirty(ctx):
    _metadata, dirty, _blobs = ctx
    storage_utils.touch_user_search_indexes_state('u1', filenames=['a.jpg', 'b.jpg'])

    assert storage_utils._get_dirty_search_index_filenames('u1', 'lexical') == {'a.jpg', 'b.jpg'}
    assert storage_utils._get_dirty_search_index_filenames('u1', 'vector') == {'a.jpg', 'b.jpg'}


def test_touch_with_single_filename_string_not_list(ctx):
    storage_utils.touch_user_search_indexes_state('u1', filenames='solo.jpg')
    assert storage_utils._get_dirty_search_index_filenames('u1', 'lexical') == {'solo.jpg'}


def test_get_dirty_filenames_returns_none_when_table_unconfigured(monkeypatch):
    monkeypatch.setitem(storage_utils._CTX, 'search_index_dirty_table_client', None)
    assert storage_utils._get_dirty_search_index_filenames('u1', 'lexical') is None


def test_clear_removes_only_named_filenames(ctx):
    _metadata, dirty, _blobs = ctx
    storage_utils.touch_user_search_indexes_state('u1', filenames=['a.jpg', 'b.jpg'])
    storage_utils._clear_dirty_search_index_filenames('u1', 'lexical', {'a.jpg'})

    assert storage_utils._get_dirty_search_index_filenames('u1', 'lexical') == {'b.jpg'}
    # Clearing the lexical partition must not touch the vector one.
    assert storage_utils._get_dirty_search_index_filenames('u1', 'vector') == {'a.jpg', 'b.jpg'}


# --- lexical incremental merge ----------------------------------------------

def test_lexical_incremental_refresh_only_point_reads_the_dirty_filename(ctx):
    metadata, _dirty, _blobs = ctx
    _seed_row(metadata, 'u1', 'a.jpg', tags='["cat"]')
    _seed_row(metadata, 'u1', 'b.jpg', tags='["dog"]')
    _seed_row(metadata, 'u1', 'c.jpg', tags='["bird"]')

    first = storage_utils.refresh_user_lexical_index('u1', source_version='v1')
    assert first is not None
    assert metadata.scan_count == 1  # the initial full build

    # Edit just one photo and mark only it dirty.
    metadata.upsert_entity({'PartitionKey': 'u1', 'RowKey': 'b.jpg', 'tags': '["dog", "park"]'})
    storage_utils.touch_user_search_indexes_state('u1', filenames='b.jpg')

    second = storage_utils.refresh_user_lexical_index('u1', source_version='v2')

    assert second is not None
    assert metadata.scan_count == 1  # no additional full-partition scan
    assert metadata.get_entity_calls == [('u1', 'b.jpg')]  # exactly one point-read
    rows_by_name = {row['RowKey']: row for row in second.rows}
    assert set(rows_by_name) == {'a.jpg', 'b.jpg', 'c.jpg'}
    assert rows_by_name['b.jpg']['tags'] == '["dog", "park"]'
    assert rows_by_name['a.jpg']['tags'] == '["cat"]'  # untouched, carried over from the old snapshot


def test_lexical_incremental_refresh_drops_deleted_photos(ctx):
    metadata, _dirty, _blobs = ctx
    _seed_row(metadata, 'u1', 'a.jpg', tags='[]')
    _seed_row(metadata, 'u1', 'b.jpg', tags='[]')
    storage_utils.refresh_user_lexical_index('u1', source_version='v1')

    metadata.upsert_entity({'PartitionKey': 'u1', 'RowKey': 'b.jpg', 'tags': '[]', 'processing_state': 'deleted'})
    storage_utils.touch_user_search_indexes_state('u1', filenames='b.jpg')

    second = storage_utils.refresh_user_lexical_index('u1', source_version='v2')

    assert {row['RowKey'] for row in second.rows} == {'a.jpg'}


def test_lexical_incremental_refresh_picks_up_new_photo(ctx):
    metadata, _dirty, _blobs = ctx
    _seed_row(metadata, 'u1', 'a.jpg', tags='[]')
    storage_utils.refresh_user_lexical_index('u1', source_version='v1')

    _seed_row(metadata, 'u1', 'new.jpg', tags='["new"]')
    storage_utils.touch_user_search_indexes_state('u1', filenames='new.jpg')

    second = storage_utils.refresh_user_lexical_index('u1', source_version='v2')

    assert {row['RowKey'] for row in second.rows} == {'a.jpg', 'new.jpg'}
    assert metadata.scan_count == 1


def test_lexical_refresh_falls_back_to_full_scan_when_schema_version_changes(ctx, monkeypatch):
    metadata, _dirty, _blobs = ctx
    _seed_row(metadata, 'u1', 'a.jpg', tags='[]')
    storage_utils.refresh_user_lexical_index('u1', source_version='v1')
    assert metadata.scan_count == 1

    storage_utils.touch_user_search_indexes_state('u1', filenames='a.jpg')
    monkeypatch.setattr(storage_utils, '_LEXICAL_INDEX_SCHEMA_VERSION', 'v2-new-schema')

    second = storage_utils.refresh_user_lexical_index('u1', source_version='v2')

    assert second is not None
    assert metadata.scan_count == 2  # forced a real full re-scan, not an incremental merge


def test_lexical_refresh_force_full_ignores_dirty_set(ctx):
    metadata, _dirty, _blobs = ctx
    _seed_row(metadata, 'u1', 'a.jpg', tags='[]')
    _seed_row(metadata, 'u1', 'b.jpg', tags='[]')
    storage_utils.refresh_user_lexical_index('u1', source_version='v1')
    storage_utils.touch_user_search_indexes_state('u1', filenames='a.jpg')

    second = storage_utils.refresh_user_lexical_index('u1', source_version='v2', force_full=True)

    assert metadata.scan_count == 2
    assert {row['RowKey'] for row in second.rows} == {'a.jpg', 'b.jpg'}
    # A full rebuild incorporates everything -- its own dirty set is cleared too.
    assert storage_utils._get_dirty_search_index_filenames('u1', 'lexical') == set()


def test_lexical_refresh_clears_dirty_set_after_full_rebuild_fallback(ctx):
    """First-ever build has no existing snapshot to merge onto -- it must
    still clear any dirty markers so a later incremental merge doesn't
    needlessly re-fetch photos already covered by this fresh snapshot."""
    metadata, _dirty, _blobs = ctx
    _seed_row(metadata, 'u1', 'a.jpg', tags='[]')
    storage_utils.touch_user_search_indexes_state('u1', filenames='a.jpg')

    storage_utils.refresh_user_lexical_index('u1', source_version='v1')

    assert storage_utils._get_dirty_search_index_filenames('u1', 'lexical') == set()


# --- vector incremental merge ------------------------------------------------

def test_vector_incremental_refresh_only_point_reads_the_dirty_filename(ctx, monkeypatch):
    metadata, _dirty, _blobs = ctx
    monkeypatch.setattr(storage_utils, 'PHOTO_EMBEDDING_DIMENSION', 2)
    monkeypatch.setattr(storage_utils.vision_utils, 'get_text_embedding_dimension', lambda: 2)
    monkeypatch.setattr(storage_utils.vision_utils, 'get_text_embedding_version', lambda: 'textv1')
    monkeypatch.setattr(storage_utils.vision_utils, 'encode_text_embedding', lambda text: [1.0, 0.0])

    _seed_row(metadata, 'u1', 'a.jpg', tags='[]')
    _seed_row(metadata, 'u1', 'b.jpg', tags='[]')

    first = storage_utils.refresh_user_vector_index('u1', source_version='v1')
    assert first is not None
    assert set(first.row_keys) == {'a.jpg', 'b.jpg'}
    assert metadata.scan_count == 1

    storage_utils.touch_user_search_indexes_state('u1', filenames='b.jpg')
    second = storage_utils.refresh_user_vector_index('u1', source_version='v2')

    assert second is not None
    assert metadata.scan_count == 1  # still just the one full scan from the initial build
    assert metadata.get_entity_calls == [('u1', 'b.jpg')]
    assert set(second.row_keys) == {'a.jpg', 'b.jpg'}


def test_vector_refresh_falls_back_to_full_build_when_embedding_version_changes(ctx, monkeypatch):
    metadata, _dirty, _blobs = ctx
    monkeypatch.setattr(storage_utils, 'PHOTO_EMBEDDING_DIMENSION', 2)
    monkeypatch.setattr(storage_utils.vision_utils, 'get_text_embedding_dimension', lambda: 2)
    monkeypatch.setattr(storage_utils.vision_utils, 'get_text_embedding_version', lambda: 'textv1')
    monkeypatch.setattr(storage_utils.vision_utils, 'encode_text_embedding', lambda text: [1.0, 0.0])
    _seed_row(metadata, 'u1', 'a.jpg', tags='[]')
    storage_utils.refresh_user_vector_index('u1', source_version='v1')
    assert metadata.scan_count == 1

    storage_utils.touch_user_search_indexes_state('u1', filenames='a.jpg')
    monkeypatch.setattr(storage_utils.vision_utils, 'get_text_embedding_version', lambda: 'textv2-upgraded')

    second = storage_utils.refresh_user_vector_index('u1', source_version='v2')

    assert second is not None
    assert second.embedding_version == 'textv2-upgraded'
    assert metadata.scan_count == 2  # embedding-space change forces a real full re-embed
