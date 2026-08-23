"""Unit tests for the O(1) dedup/collision indexes in storage_utils.

Before this change, detect_duplicates() scanned the uploading library's whole
metadata partition on every /upload/finalize call, and _resolve_filename_for_
upload() scanned the ENTIRE metadata table (every library, every photo) on
every upload. Both scans got more expensive as row counts grew, so a big
upload batch started fast and dragged progressively more with each later
photo. They now do a point lookup against two small index tables (see
_store_hash_index / _store_filename_owner in storage_utils.py) instead.

These tests drive the real finalize_uploaded_file() path plus the lookup
functions directly, and assert (via a metadata fake that raises on any scan)
that the common case never falls back to scanning -- and that a stale index
row can never cause a real upload to be wrongly treated as a duplicate.
"""
from __future__ import annotations

import re

import pytest

import storage_utils


class _ResourceNotFound(Exception):
    pass


class _FakePointTable:
    """Fake for the two new index tables: point get/upsert/delete, plus the
    single ``PartitionKey eq 'X'`` query _query_filename_owners issues."""

    def __init__(self) -> None:
        self.rows: dict = {}

    def upsert_entity(self, entity):
        self.rows[(entity['PartitionKey'], entity['RowKey'])] = dict(entity)

    def get_entity(self, partition_key, row_key):
        key = (partition_key, row_key)
        if key not in self.rows:
            raise _ResourceNotFound(f'{key} not found')
        return dict(self.rows[key])

    def delete_entity(self, partition_key, row_key):
        self.rows.pop((partition_key, row_key), None)

    def query_entities(self, filter_str, **kwargs):
        m = re.match(r"PartitionKey eq '(.*)'$", filter_str.strip())
        if not m:
            raise ValueError(f'Unsupported filter: {filter_str}')
        pk = m.group(1)
        return [dict(v) for (p, _), v in self.rows.items() if p == pk]


class _FakeMetadataTable:
    """Metadata-table fake for the O(1)-path tests. Only point get/upsert/
    delete are needed once the index tables are wired up; query_entities
    raises so any accidental fall-back-to-scan shows up as a test failure
    instead of silently passing."""

    def __init__(self) -> None:
        self.rows: dict = {}

    def upsert_entity(self, entity):
        self.rows[(entity['PartitionKey'], entity['RowKey'])] = dict(entity)

    def get_entity(self, partition_key, row_key):
        key = (partition_key, row_key)
        if key not in self.rows:
            raise _ResourceNotFound(f'{key} not found')
        return dict(self.rows[key])

    def delete_entity(self, partition_key, row_key):
        self.rows.pop((partition_key, row_key), None)

    def query_entities(self, filter_str, **kwargs):
        raise AssertionError(f'unexpected metadata-table scan with indexes wired up: {filter_str}')


@pytest.fixture
def dedup_ctx(monkeypatch):
    """storage_utils wired with the index tables present -- the post-fix,
    steady-state configuration."""
    metadata = _FakeMetadataTable()
    hash_index = _FakePointTable()
    filename_owners = _FakePointTable()
    monkeypatch.setitem(storage_utils._CTX, 'metadata_table_client', metadata)
    monkeypatch.setitem(storage_utils._CTX, 'hash_index_table_client', hash_index)
    monkeypatch.setitem(storage_utils._CTX, 'filename_owners_table_client', filename_owners)
    monkeypatch.setitem(storage_utils._CTX, 'image_names_table_client', None)
    monkeypatch.setitem(storage_utils._CTX, 'blob_service_client', object())
    monkeypatch.setitem(storage_utils._CTX, 'blob_image_container', 'images')
    monkeypatch.setitem(storage_utils._CTX, 'face_table_client', None)
    storage_utils.invalidate_image_names_cache()
    yield metadata, hash_index, filename_owners
    storage_utils.invalidate_image_names_cache()


def test_finalize_writes_both_indexes(dedup_ctx):
    metadata, hash_index, filename_owners = dedup_ctx
    file_hash = 'a' * 64
    storage_utils.finalize_uploaded_file('lib-A', 'vacation.jpg', 'image/jpeg', client_sha256=file_hash)

    assert hash_index.get_entity('lib-A', file_hash)['filename'] == 'vacation.jpg'
    assert filename_owners.get_entity('vacation.jpg', 'lib-A')['fileHash'] == file_hash


def test_list_known_file_hashes_returns_this_librarys_index(dedup_ctx):
    """Backs the frontend's once-per-batch prefetch (GET /upload/known-hashes)
    that lets the browser skip re-uploading a known duplicate before spending
    any transfer bandwidth on it."""
    metadata, hash_index, filename_owners = dedup_ctx
    hash_a = 'n' * 64
    hash_b = 'o' * 64
    storage_utils.finalize_uploaded_file('lib-A', 'vacation.jpg', 'image/jpeg', client_sha256=hash_a)
    storage_utils.finalize_uploaded_file('lib-A', 'beach.jpg', 'image/jpeg', client_sha256=hash_b)
    # A different library's hashes must never leak into lib-A's prefetch.
    storage_utils.finalize_uploaded_file('lib-B', 'other.jpg', 'image/jpeg', client_sha256='p' * 64)

    assert storage_utils.list_known_file_hashes('lib-A') == {
        hash_a: 'vacation.jpg',
        hash_b: 'beach.jpg',
    }


def test_list_known_file_hashes_empty_without_index_table(no_index_ctx):
    # Older-deploy fallback: no crash, just nothing to prefetch client-side --
    # dedup still works via the finalize-time scan fallback either way.
    assert storage_utils.list_known_file_hashes('lib-A') == {}


def test_detect_duplicates_hit_is_point_lookup_not_a_scan(dedup_ctx):
    metadata, hash_index, filename_owners = dedup_ctx
    file_hash = 'b' * 64
    storage_utils.finalize_uploaded_file('lib-A', 'vacation.jpg', 'image/jpeg', client_sha256=file_hash)

    # Re-uploading the same bytes: the metadata fake raises on any
    # query_entities call, so a passing result proves this is a get_entity
    # point lookup, not a partition scan.
    duplicates = storage_utils.detect_duplicates('lib-A', file_hash)
    assert duplicates == [{'filename': 'vacation.jpg', 'type': 'exact', 'hash': file_hash}]


def test_detect_duplicates_no_hit_is_point_lookup_not_a_scan(dedup_ctx):
    # The overwhelming common case (no duplicate) must also never scan.
    duplicates = storage_utils.detect_duplicates('lib-A', 'c' * 64)
    assert duplicates == []


def test_detect_duplicates_self_heals_stale_index(dedup_ctx):
    """A hash-index row can point at a photo that was since deleted through a
    path that didn't clean the index up. That must never cause a live upload
    to be silently skipped as "already have it" -- the point of this test is
    the correctness guarantee, not just the perf one."""
    metadata, hash_index, filename_owners = dedup_ctx
    file_hash = 'd' * 64
    # Simulate a stale index row with no backing metadata row.
    hash_index.upsert_entity({'PartitionKey': 'lib-A', 'RowKey': file_hash, 'filename': 'ghost.jpg'})

    duplicates = storage_utils.detect_duplicates('lib-A', file_hash)
    assert duplicates == []
    # Self-heal: the stale row is removed so future lookups don't re-pay the cost.
    with pytest.raises(_ResourceNotFound):
        hash_index.get_entity('lib-A', file_hash)


def test_resolve_filename_conflict_across_libraries_renames(dedup_ctx):
    metadata, hash_index, filename_owners = dedup_ctx
    storage_utils.finalize_uploaded_file('lib-A', 'vacation.jpg', 'image/jpeg', client_sha256='e' * 64)

    # A different library uploading different content under the same name
    # must be renamed to avoid overwriting lib-A's blob (blob names aren't
    # partitioned per library). Calls the resolver directly rather than the
    # full finalize path, which would also try to move blob bytes to the new
    # name -- unrelated to what this test is verifying.
    final_name = storage_utils._resolve_filename_for_upload('lib-B', 'vacation.jpg', 'f' * 64)
    assert final_name != 'vacation.jpg'
    assert final_name.startswith('vacation-') and final_name.endswith('.jpg')


def test_resolve_filename_identical_content_across_libraries_no_rename(dedup_ctx):
    metadata, hash_index, filename_owners = dedup_ctx
    same_hash = 'g' * 64
    storage_utils.finalize_uploaded_file('lib-A', 'vacation.jpg', 'image/jpeg', client_sha256=same_hash)

    # Same filename AND same content (e.g. a shared-library scenario) is not
    # a collision -- no rename needed.
    _, final_name = storage_utils.finalize_uploaded_file('lib-B', 'vacation.jpg', 'image/jpeg', client_sha256=same_hash)
    assert final_name == 'vacation.jpg'


def test_resolve_filename_same_library_reupload_renames(dedup_ctx):
    metadata, hash_index, filename_owners = dedup_ctx
    storage_utils.finalize_uploaded_file('lib-A', 'vacation.jpg', 'image/jpeg', client_sha256='h' * 64)

    # The SAME library uploading different bytes under a filename it already
    # owns must NOT silently replace the earlier photo -- filename collisions
    # happen within one library too (e.g. Apple Photos exporting many distinct
    # edited photos all as "FullSizeRender.heic"), and (user_id, filename) is
    # the metadata table's row key, so without a rename the second upload's
    # finalize would overwrite the first photo's row. Calls the resolver
    # directly rather than the full finalize path, same as
    # test_resolve_filename_conflict_across_libraries_renames above.
    final_name = storage_utils._resolve_filename_for_upload('lib-A', 'vacation.jpg', 'i' * 64)
    assert final_name != 'vacation.jpg'
    assert final_name.startswith('vacation-') and final_name.endswith('.jpg')


def test_delete_helpers_remove_index_rows(dedup_ctx):
    metadata, hash_index, filename_owners = dedup_ctx
    file_hash = 'j' * 64
    storage_utils.finalize_uploaded_file('lib-A', 'vacation.jpg', 'image/jpeg', client_sha256=file_hash)

    storage_utils.delete_hash_index_entry('lib-A', file_hash)
    storage_utils.delete_filename_owner_entry('lib-A', 'vacation.jpg')

    with pytest.raises(_ResourceNotFound):
        hash_index.get_entity('lib-A', file_hash)
    with pytest.raises(_ResourceNotFound):
        filename_owners.get_entity('vacation.jpg', 'lib-A')


# --- Fallback path: index tables not (yet) configured, e.g. the window right
# after a deploy before the backfill script has run. Dedup/collision checks
# must keep working via the pre-fix scan rather than silently going blind. ---

class _FakeScanningMetadataTable(_FakeMetadataTable):
    """Adds the two pre-fix scan queries back, for the fallback-path test."""

    def query_entities(self, filter_str, **kwargs):
        f = filter_str.strip()
        dedup = re.match(r"PartitionKey eq '(.*?)' and fileHash eq '(.*?)'$", f)
        if dedup:
            pk, fh = dedup.groups()
            return [dict(v) for (p, _), v in self.rows.items() if p == pk and str(v.get('fileHash', '')) == fh]
        by_rowkey = re.match(r"RowKey eq '(.*)'$", f)
        if by_rowkey:
            rk = by_rowkey.group(1)
            return [dict(v) for (_, r), v in self.rows.items() if r == rk]
        raise ValueError(f'Unsupported filter: {filter_str}')


@pytest.fixture
def no_index_ctx(monkeypatch):
    metadata = _FakeScanningMetadataTable()
    monkeypatch.setitem(storage_utils._CTX, 'metadata_table_client', metadata)
    monkeypatch.setitem(storage_utils._CTX, 'hash_index_table_client', None)
    monkeypatch.setitem(storage_utils._CTX, 'filename_owners_table_client', None)
    monkeypatch.setitem(storage_utils._CTX, 'image_names_table_client', None)
    monkeypatch.setitem(storage_utils._CTX, 'blob_service_client', object())
    monkeypatch.setitem(storage_utils._CTX, 'blob_image_container', 'images')
    monkeypatch.setitem(storage_utils._CTX, 'face_table_client', None)
    yield metadata


def test_fallback_dedup_still_works_without_index_table(no_index_ctx):
    file_hash = 'k' * 64
    storage_utils.finalize_uploaded_file('lib-A', 'vacation.jpg', 'image/jpeg', client_sha256=file_hash)
    duplicates = storage_utils.detect_duplicates('lib-A', file_hash)
    assert duplicates == [{'filename': 'vacation.jpg', 'type': 'exact', 'hash': file_hash}]


def test_fallback_filename_collision_still_works_without_index_table(no_index_ctx):
    storage_utils.finalize_uploaded_file('lib-A', 'vacation.jpg', 'image/jpeg', client_sha256='l' * 64)
    final_name = storage_utils._resolve_filename_for_upload('lib-B', 'vacation.jpg', 'm' * 64)
    assert final_name != 'vacation.jpg'
