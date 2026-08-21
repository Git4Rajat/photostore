"""reset_upload_tracking_and_reserve_blobs_batch: the batched replacement for
calling reset_received_ranges + reserve_pending_anonymous_blob once per file.
Backs /upload/init-batch, which registers a whole chunk of new uploads with
one query + one transactional write instead of a read+write pair per file.
"""
from __future__ import annotations

import re

import pytest

import storage_utils


class _ResourceNotFound(Exception):
    pass


class _FakeMetadataTable:
    """Adds submit_transaction (atomic batch upsert) on top of the point
    get/upsert/query fake already used by the dedup-index tests."""

    def __init__(self) -> None:
        self.rows: dict = {}
        self.transaction_calls: list = []

    def upsert_entity(self, entity):
        self.rows[(entity['PartitionKey'], entity['RowKey'])] = dict(entity)

    def get_entity(self, partition_key, row_key):
        key = (partition_key, row_key)
        if key not in self.rows:
            raise _ResourceNotFound(f'{key} not found')
        return dict(self.rows[key])

    def query_entities(self, filter_str, **kwargs):
        # Real Azure Table filters are 'PartitionKey eq X and (RowKey eq A or RowKey eq B ...)'.
        m = re.match(r"PartitionKey eq '(.*)' and \((.*)\)$", filter_str.strip())
        if not m:
            raise ValueError(f'Unsupported filter: {filter_str}')
        pk = m.group(1)
        row_keys = set(re.findall(r"RowKey eq '([^']*)'", m.group(2)))
        return [dict(v) for (p, rk), v in self.rows.items() if p == pk and rk in row_keys]

    def submit_transaction(self, operations):
        ops = list(operations)
        self.transaction_calls.append(len(ops))
        for op, entity, *_rest in ops:
            assert op == 'upsert'
            self.rows[(entity['PartitionKey'], entity['RowKey'])] = dict(entity)
        return [{} for _ in ops]


@pytest.fixture
def batch_ctx(monkeypatch):
    metadata = _FakeMetadataTable()
    monkeypatch.setitem(storage_utils._CTX, 'metadata_table_client', metadata)
    monkeypatch.setitem(storage_utils._CTX, 'blob_service_client', object())
    yield metadata


def test_batch_reserves_distinct_blob_names_in_one_transaction(batch_ctx):
    result = storage_utils.reset_upload_tracking_and_reserve_blobs_batch('lib-A', [
        {'filename': 'a.jpg', 'total_size': 100, 'expected_hash': 'h1', 'is_fresh': True},
        {'filename': 'b.jpg', 'total_size': 200, 'expected_hash': 'h2', 'is_fresh': True},
        {'filename': 'c.jpg', 'total_size': 300, 'expected_hash': None, 'is_fresh': True},
    ])
    assert set(result.keys()) == {'a.jpg', 'b.jpg', 'c.jpg'}
    # Every file gets its own distinct anonymous blob name.
    assert len(set(result.values())) == 3
    # Exactly one transaction call for the whole chunk of 3 files.
    assert batch_ctx.transaction_calls == [3]

    stored_a = batch_ctx.get_entity('lib-A', 'a.jpg')
    assert stored_a['upload_total_size'] == 100
    assert stored_a['upload_sha256_expected'] == 'h1'
    assert stored_a['pendingAnonymousBlob'] == result['a.jpg']
    assert stored_a['received_ranges'] == '[]'


def test_batch_reuses_existing_pending_reservation(batch_ctx):
    # Simulate a row already reserved by a prior init call for this filename.
    batch_ctx.upsert_entity({'PartitionKey': 'lib-A', 'RowKey': 'a.jpg', 'pendingAnonymousBlob': 'existing-uuid'})
    result = storage_utils.reset_upload_tracking_and_reserve_blobs_batch('lib-A', [
        {'filename': 'a.jpg', 'total_size': 100, 'expected_hash': None, 'is_fresh': True},
    ])
    assert result['a.jpg'] == 'existing-uuid'


def test_batch_scopes_query_to_requested_filenames_and_partition(batch_ctx):
    # A different library's row with the same filename must never leak in.
    batch_ctx.upsert_entity({'PartitionKey': 'lib-B', 'RowKey': 'shared.jpg', 'pendingAnonymousBlob': 'other-library-uuid'})
    result = storage_utils.reset_upload_tracking_and_reserve_blobs_batch('lib-A', [
        {'filename': 'shared.jpg', 'total_size': 50, 'expected_hash': None, 'is_fresh': True},
    ])
    assert result['shared.jpg'] != 'other-library-uuid'
