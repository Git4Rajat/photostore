"""Unit tests for image-name anonymization in storage_utils.

Covers the mapping round-trip (real filename <-> anonymous UUID blob name), the
in-memory forward/reverse caches, the SAS-path physical-name resolver, and the
delete cleanup that must drop both the table row and the cache entries.
"""
from __future__ import annotations

import re
import uuid

import pytest

import storage_utils


class _ResourceNotFound(Exception):
    pass


class _FakeImageNamesTable:
    """Fake Azure table supporting the exact queries storage_utils issues:
    the plain ``PartitionKey eq 'X'`` scan and the compound
    ``PartitionKey eq 'X' and original_filename eq 'Y' and kind eq 'Z'`` lookup.
    """

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

    def query_entities(self, filter_str):
        f = filter_str.strip()
        compound = re.match(
            r"PartitionKey eq '(.*?)' and original_filename eq '(.*?)' and kind eq '(.*?)'$",
            f,
        )
        if compound:
            pk, original, kind = compound.groups()
            return [
                dict(v)
                for (p, _), v in self.rows.items()
                if p == pk and v.get('original_filename') == original and v.get('kind') == kind
            ]
        simple = re.match(r"PartitionKey eq '(.*)'$", f)
        if simple:
            pk = simple.group(1)
            return [dict(v) for (p, _), v in self.rows.items() if p == pk]
        raise ValueError(f'Unsupported filter: {filter_str}')


@pytest.fixture
def anon_ctx(monkeypatch):
    """Configure storage_utils with a fake image-names table and clean caches."""
    table = _FakeImageNamesTable()
    monkeypatch.setitem(storage_utils._CTX, 'image_names_table_client', table)
    monkeypatch.setitem(storage_utils._CTX, 'blob_image_container', 'images')
    storage_utils.invalidate_image_names_cache()
    yield table
    storage_utils.invalidate_image_names_cache()


def test_get_anonymous_blob_name_creates_uuid(anon_ctx):
    lib = 'lib-A'
    blob = storage_utils.get_anonymous_blob_name(lib, 'vacation.jpg', 'image', 'images')
    # A UUID, and crucially not the original filename.
    assert blob and blob != 'vacation.jpg'
    uuid.UUID(blob)  # raises if not a valid UUID
    # The table row records the real name behind the anonymous id.
    row = anon_ctx.get_entity(lib, blob)
    assert row['original_filename'] == 'vacation.jpg'
    assert row['kind'] == 'image'


def test_get_anonymous_blob_name_reuses_existing(anon_ctx):
    lib = 'lib-A'
    first = storage_utils.get_anonymous_blob_name(lib, 'vacation.jpg', 'image', 'images')
    second = storage_utils.get_anonymous_blob_name(lib, 'vacation.jpg', 'image', 'images')
    # Re-processing the same file must reuse the id, not mint a duplicate blob.
    assert first == second
    assert len([r for r in anon_ctx.rows.values() if r['original_filename'] == 'vacation.jpg']) == 1


def test_restore_original_filename_round_trip(anon_ctx):
    lib = 'lib-A'
    blob = storage_utils.get_anonymous_blob_name(lib, 'beach day.png', 'image', 'images')
    assert storage_utils.restore_original_filename(lib, blob) == 'beach day.png'


def test_resolve_physical_blob_name_uses_reverse_cache(anon_ctx):
    lib = 'lib-A'
    blob = storage_utils.get_anonymous_blob_name(lib, 'vacation.jpg', 'image', 'images')
    # Known file -> anonymous UUID (the physical blob to mint a SAS against).
    assert storage_utils.resolve_physical_blob_name(lib, 'vacation.jpg', 'image') == blob
    # Unknown / pre-anonymization file -> original name unchanged (safe fallback).
    assert storage_utils.resolve_physical_blob_name(lib, 'legacy.jpg', 'image') == 'legacy.jpg'


def test_per_library_isolation(anon_ctx):
    a = storage_utils.get_anonymous_blob_name('lib-A', 'shared.jpg', 'image', 'images')
    b = storage_utils.get_anonymous_blob_name('lib-B', 'shared.jpg', 'image', 'images')
    # Same filename in two libraries gets distinct ids (no cross-library correlation).
    assert a != b
    assert storage_utils.resolve_physical_blob_name('lib-A', 'shared.jpg', 'image') == a
    assert storage_utils.resolve_physical_blob_name('lib-B', 'shared.jpg', 'image') == b


def test_load_cache_then_resolve_without_table_query(anon_ctx):
    lib = 'lib-A'
    blob = storage_utils.get_anonymous_blob_name(lib, 'vacation.jpg', 'image', 'images')
    storage_utils.invalidate_image_names_cache(lib)
    storage_utils.load_image_names_cache(lib)
    # After a warm load, both directions resolve from cache.
    assert storage_utils.resolve_physical_blob_name(lib, 'vacation.jpg', 'image') == blob
    assert storage_utils.restore_original_filename(lib, blob) == 'vacation.jpg'


def test_delete_mapping_clears_row_and_caches(anon_ctx):
    lib = 'lib-A'
    blob = storage_utils.get_anonymous_blob_name(lib, 'vacation.jpg', 'image', 'images')
    storage_utils.delete_image_name_mapping(lib, blob)
    # Table row gone.
    with pytest.raises(_ResourceNotFound):
        anon_ctx.get_entity(lib, blob)
    # Forward cache no longer resolves the id...
    assert storage_utils.restore_original_filename(lib, blob) is None
    # ...and the reverse cache no longer maps the filename to the dead id.
    assert storage_utils.resolve_physical_blob_name(lib, 'vacation.jpg', 'image') == 'vacation.jpg'


def test_no_table_client_is_safe_noop(monkeypatch):
    # When the image-names table isn't configured (feature not wired up yet),
    # anonymization must degrade gracefully rather than error.
    monkeypatch.setitem(storage_utils._CTX, 'image_names_table_client', None)
    storage_utils.invalidate_image_names_cache()
    assert storage_utils.get_anonymous_blob_name('lib-A', 'vacation.jpg', 'image', 'images') is None
    # Resolver falls back to the original filename so serving/reading still works.
    assert storage_utils.resolve_physical_blob_name('lib-A', 'vacation.jpg', 'image') == 'vacation.jpg'
    assert storage_utils.restore_original_filename('lib-A', 'anything') is None


class _FakeMetadataTable:
    """Metadata-table fake supporting the queries finalize_uploaded_file issues:
    ``PartitionKey eq 'X' and fileHash eq 'Y'`` (dedup) and ``RowKey eq 'X'``
    (cross-tenant filename-clash check), plus point get/upsert/delete.
    """

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
        f = filter_str.strip()
        dedup = re.match(r"PartitionKey eq '(.*?)' and fileHash eq '(.*?)'$", f)
        if dedup:
            pk, fh = dedup.groups()
            return [dict(v) for (p, _), v in self.rows.items()
                    if p == pk and str(v.get('fileHash', '')) == fh]
        by_rowkey = re.match(r"RowKey eq '(.*)'$", f)
        if by_rowkey:
            rk = by_rowkey.group(1)
            return [dict(v) for (_, r), v in self.rows.items() if r == rk]
        by_pk = re.match(r"PartitionKey eq '(.*)'$", f)
        if by_pk:
            pk = by_pk.group(1)
            return [dict(v) for (p, _), v in self.rows.items() if p == pk]
        raise ValueError(f'Unsupported filter: {filter_str}')


@pytest.fixture
def finalize_ctx(monkeypatch):
    """storage_utils wired with fake metadata + image-names tables, as
    configure_storage() does once IMAGE_NAMES_TABLE is provisioned."""
    metadata = _FakeMetadataTable()
    image_names = _FakeImageNamesTable()
    monkeypatch.setitem(storage_utils._CTX, 'metadata_table_client', metadata)
    monkeypatch.setitem(storage_utils._CTX, 'image_names_table_client', image_names)
    monkeypatch.setitem(storage_utils._CTX, 'blob_service_client', object())
    monkeypatch.setitem(storage_utils._CTX, 'blob_image_container', 'images')
    monkeypatch.setitem(storage_utils._CTX, 'face_table_client', None)
    storage_utils.invalidate_image_names_cache()
    yield metadata, image_names
    storage_utils.invalidate_image_names_cache()


def test_finalize_anonymized_upload_is_consistent(finalize_ctx):
    """Drives the real finalize path: an anonymized .jpg (client hash supplied, so
    no blob I/O) must stamp the metadata row and the mapping with the SAME UUID the
    blob was uploaded under — the two bugs fixed in this change."""
    metadata, image_names = finalize_ctx
    user_id = 'lib-A'
    upload_uuid = str(uuid.uuid4())

    duplicates, final_name = storage_utils.finalize_uploaded_file(
        user_id,
        'vacation.jpg',
        'image/jpeg',
        client_sha256='a' * 64,
        anonymous_blob_name=upload_uuid,
    )

    # Metadata RowKey stays the original filename (UI/keys unchanged).
    assert final_name == 'vacation.jpg'
    row = metadata.get_entity(user_id, 'vacation.jpg')
    # anonymousImageId is exactly the UUID the blob lives under — not a fresh one.
    assert row['anonymousImageId'] == upload_uuid
    # The mapping row is keyed by that same UUID and points back to the real name.
    mapping = image_names.get_entity(user_id, upload_uuid)
    assert mapping['original_filename'] == 'vacation.jpg'
    # And the serve-path resolver agrees.
    assert storage_utils.resolve_physical_blob_name(user_id, 'vacation.jpg', 'image') == upload_uuid


def test_finalize_without_anonymous_name_leaves_no_mapping(finalize_ctx):
    """Pre-anonymization / unwired uploads keep the original name and add no mapping."""
    metadata, image_names = finalize_ctx
    user_id = 'lib-A'
    _, final_name = storage_utils.finalize_uploaded_file(
        user_id, 'legacy.jpg', 'image/jpeg', client_sha256='b' * 64,
    )
    assert final_name == 'legacy.jpg'
    row = metadata.get_entity(user_id, 'legacy.jpg')
    assert not row.get('anonymousImageId')
    assert image_names.rows == {}


# --- Retry/resume durability (regression guard for the upload-retry bug) -------

def test_reserve_pending_anonymous_blob_is_stable_across_calls(finalize_ctx):
    """A resumed /upload/init must return the SAME reserved blob, or the browser
    would stage the remaining blocks to a different blob than the earlier ones
    (InvalidBlockList on commit -> retry never succeeds)."""
    metadata, _ = finalize_ctx
    first = storage_utils.reserve_pending_anonymous_blob('lib-A', 'vacation.jpg')
    assert first
    uuid.UUID(first)
    second = storage_utils.reserve_pending_anonymous_blob('lib-A', 'vacation.jpg')
    third = storage_utils.reserve_pending_anonymous_blob('lib-A', 'vacation.jpg')
    assert second == first and third == first
    # Persisted durably on the metadata row (not in process memory).
    assert metadata.get_entity('lib-A', 'vacation.jpg')['pendingAnonymousBlob'] == first


def test_reserve_pending_anonymous_blob_ignores_a_different_in_flight_upload(finalize_ctx):
    """Two DISTINCT photos can share a filename while one is still mid-upload
    (e.g. two files both named "IP_image.heic" in one batch). The second
    file's reservation call must not reuse the first's still-pending blob --
    doing so would stage both files' chunks onto the same blob (positional
    block IDs collide) and produce a corrupted blob / "Uploaded blob size
    mismatch" at finalize."""
    first = storage_utils.reserve_pending_anonymous_blob('lib-A', 'IP_image.heic', 'h-first-photo')
    second = storage_utils.reserve_pending_anonymous_blob('lib-A', 'IP_image.heic', 'h-second-photo')
    assert second != first


def test_reserve_pending_anonymous_blob_reuses_for_matching_hash(finalize_ctx):
    """A genuine resume/retry of the exact same content must still reuse the
    original reservation."""
    first = storage_utils.reserve_pending_anonymous_blob('lib-A', 'IP_image.heic', 'h-same-photo')
    second = storage_utils.reserve_pending_anonymous_blob('lib-A', 'IP_image.heic', 'h-same-photo')
    assert second == first


def test_read_pending_anonymous_blob_is_replica_safe(finalize_ctx):
    """finalize can land on a different replica than init; the reservation must be
    readable from the durable row with no shared in-process state."""
    reserved = storage_utils.reserve_pending_anonymous_blob('lib-A', 'vacation.jpg')
    # No in-memory handoff exists any more — this reads straight from the table.
    assert storage_utils.read_pending_anonymous_blob('lib-A', 'vacation.jpg') == reserved
    # Unknown upload -> None (finalize then falls back to the original filename).
    assert storage_utils.read_pending_anonymous_blob('lib-A', 'never-inited.jpg') is None


def test_reserve_then_finalize_roundtrip_promotes_and_clears(finalize_ctx):
    """Full durable handoff: reserve at init, read at finalize, promote to
    anonymousImageId, and clear the transient reservation."""
    metadata, image_names = finalize_ctx
    reserved = storage_utils.reserve_pending_anonymous_blob('lib-A', 'vacation.jpg')
    # finalize_direct_upload reads the reservation from the row (replica-safe)...
    handoff = storage_utils.read_pending_anonymous_blob('lib-A', 'vacation.jpg')
    assert handoff == reserved
    storage_utils.finalize_uploaded_file(
        'lib-A', 'vacation.jpg', 'image/jpeg', client_sha256='c' * 64,
        anonymous_blob_name=handoff,
    )
    row = metadata.get_entity('lib-A', 'vacation.jpg')
    assert row['anonymousImageId'] == reserved
    assert 'pendingAnonymousBlob' not in row  # transient marker cleared
    assert image_names.get_entity('lib-A', reserved)['original_filename'] == 'vacation.jpg'
