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
    table.upsert_entity({'PartitionKey': user_id, 'RowKey': filename, 'processing_complete': True, **overrides})


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
    # touch_user_search_indexes_state also dirties the sort index (see
    # test_sort_index.py for its independent-dirtying tests) -- this pins
    # that the shared _SEARCH_INDEX_KINDS loop still covers it here too.
    assert storage_utils._get_dirty_search_index_filenames('u1', 'sort') == {'a.jpg', 'b.jpg'}


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


# --- dirty-filename write batching (large-upload write volume) --------------

def test_marking_dirty_below_batch_size_does_not_hit_the_table_yet(ctx):
    """A handful of dirty-marking calls (well under the 100-entry batch
    threshold) should stay purely in memory until something actually reads
    or clears the dirty set -- see _mark_search_index_dirty_filenames."""
    _metadata, dirty, _blobs = ctx
    storage_utils.touch_user_search_indexes_state('u1', filenames=['a.jpg', 'b.jpg'])
    assert dirty.rows == {}
    assert dirty.submit_transaction_calls == []


def test_reading_dirty_filenames_flushes_the_buffer(ctx):
    _metadata, dirty, _blobs = ctx
    storage_utils.touch_user_search_indexes_state('u1', filenames=['a.jpg', 'b.jpg'])

    result = storage_utils._get_dirty_search_index_filenames('u1', 'lexical')

    assert result == {'a.jpg', 'b.jpg'}
    assert dirty.rows == {
        ('u1#lexical', 'a.jpg'): dirty.rows[('u1#lexical', 'a.jpg')],
        ('u1#lexical', 'b.jpg'): dirty.rows[('u1#lexical', 'b.jpg')],
    }
    # Coalesced into one transaction covering both filenames, not one
    # upsert_entity call per filename.
    assert len(dirty.submit_transaction_calls) == 1
    assert len(dirty.submit_transaction_calls[0]) == 2


def test_hitting_the_batch_size_flushes_eagerly_without_a_read(ctx):
    """Once one (user, kind) partition's buffer reaches
    _DIRTY_FILENAME_BATCH_SIZE entries, it flushes immediately rather than
    waiting for a read -- bounds memory and transaction size under a
    sustained burst (e.g. a large upload) with no reads happening at all."""
    _metadata, dirty, _blobs = ctx
    batch_size = storage_utils._DIRTY_FILENAME_BATCH_SIZE
    filenames = [f'photo{i}.jpg' for i in range(batch_size)]

    storage_utils.touch_user_search_indexes_state('u1', filenames=filenames)

    # One flush per index kind (vector/lexical/sort all hit the threshold
    # together, since every filename dirties all three) -- each flush is
    # still a single batched transaction covering the whole partition.
    assert len(dirty.submit_transaction_calls) == 3
    assert all(len(call) == batch_size for call in dirty.submit_transaction_calls)
    assert storage_utils._DIRTY_FILENAME_BUFFER.get(('u1', 'lexical')) is None


def test_dedupes_the_same_filename_marked_dirty_repeatedly(ctx):
    """The same photo going through several processing steps in quick
    succession (e.g. ocr then face then vision, each independently calling
    touch_user_search_indexes_state) should collapse to one buffered entry,
    not one per step."""
    _metadata, dirty, _blobs = ctx
    for _ in range(5):
        storage_utils.touch_user_search_indexes_state('u1', filenames=['a.jpg'])

    assert storage_utils._get_dirty_search_index_filenames('u1', 'lexical') == {'a.jpg'}
    assert len(dirty.submit_transaction_calls[0]) == 1


def test_clearing_flushes_buffered_entries_before_deleting(ctx):
    """A filename can be marked dirty and still be sitting only in the
    in-memory buffer (never yet written to the table) when a clear call for
    it comes in -- the clear must flush first or it silently no-ops and the
    buffered copy resurrects the mark on the next flush."""
    _metadata, dirty, _blobs = ctx
    storage_utils.touch_user_search_indexes_state('u1', filenames=['a.jpg'])
    assert dirty.rows == {}  # still only buffered, not yet in the table

    storage_utils._clear_dirty_search_index_filenames('u1', 'lexical', {'a.jpg'})

    assert storage_utils._get_dirty_search_index_filenames('u1', 'lexical') == set()


# --- manifest dirty-flag debounce (Part 1) -----------------------------------

def test_repeated_touches_only_write_the_manifest_blob_once(ctx):
    """touch_user_lexical_index_state gets called once per relevant metadata
    write -- during a large upload's per-step processing, that's the same
    user's manifest being told "dirty" over and over. Once it's already
    marked dirty in this process, repeat calls should skip the redundant
    blob rewrite (and cache invalidation) entirely until a real rebuild
    clears the flag. See _manifest_already_marked_dirty."""
    _metadata, _dirty, _blobs = ctx
    first = storage_utils.touch_user_lexical_index_state('lib-Z')
    second = storage_utils.touch_user_lexical_index_state('lib-Z')
    third = storage_utils.touch_user_lexical_index_state('lib-Z')

    # A real write returns the sourceVersion it wrote; a skipped (already
    # known dirty) call returns '' -- see touch_user_lexical_index_state.
    assert first != ''
    assert second == ''
    assert third == ''
    assert storage_utils._INDEX_MANIFEST_DIRTY_FLAGS[('lib-Z', 'lexical')] is True


def test_manifest_dirty_flag_clears_after_a_real_rebuild(ctx):
    _metadata, dirty, _blobs = ctx
    storage_utils.touch_user_lexical_index_state('lib-Z')
    assert storage_utils._INDEX_MANIFEST_DIRTY_FLAGS.get(('lib-Z', 'lexical')) is True

    storage_utils.refresh_user_lexical_index('lib-Z', source_version='v1')

    assert ('lib-Z', 'lexical') not in storage_utils._INDEX_MANIFEST_DIRTY_FLAGS
    # And a touch after the rebuild writes a fresh manifest again rather
    # than staying gated by the (now-cleared) flag.
    assert storage_utils.touch_user_lexical_index_state('lib-Z') != ''


# --- background-rebuild cooldown (Part 3) ------------------------------------

def test_rebuild_cooldown_skips_a_second_trigger_right_after_the_first(ctx, monkeypatch):
    """_rebuild_lexical_index_in_background must not spawn a fresh rebuild
    thread again the instant the previous one finishes, even though the
    per-user Lock alone would allow it -- see _index_rebuild_in_cooldown."""
    _metadata, _dirty, _blobs = ctx
    calls = []
    monkeypatch.setattr(
        storage_utils, 'refresh_user_lexical_index',
        lambda key, source_version=None: calls.append(key) or None,
    )
    manifest = {'sourceVersion': 'v1'}

    storage_utils._rebuild_lexical_index_in_background('lib-Y', manifest)
    for thread in list(storage_utils.threading.enumerate()):
        if thread.name == 'lexical-index-rebuild':
            thread.join(timeout=2)
    assert calls == ['lib-Y']

    storage_utils._rebuild_lexical_index_in_background('lib-Y', manifest)
    for thread in list(storage_utils.threading.enumerate()):
        if thread.name == 'lexical-index-rebuild':
            thread.join(timeout=2)
    # Second call landed inside the cooldown window -- no second rebuild.
    assert calls == ['lib-Y']


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
    metadata.upsert_entity({'PartitionKey': 'u1', 'RowKey': 'b.jpg', 'tags': '["dog", "park"]', 'processing_complete': True})
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


def test_lexical_incremental_refresh_drops_still_processing_photos(ctx):
    """A dirty photo that hasn't finished processing is dropped from the
    index like a delete, not merged in half-empty -- it comes back once
    processing_complete flips true on a later dirty pass."""
    metadata, _dirty, _blobs = ctx
    _seed_row(metadata, 'u1', 'a.jpg', tags='[]')
    _seed_row(metadata, 'u1', 'b.jpg', tags='[]')
    storage_utils.refresh_user_lexical_index('u1', source_version='v1')

    metadata.upsert_entity({'PartitionKey': 'u1', 'RowKey': 'b.jpg', 'tags': '[]', 'processing_complete': False})
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
