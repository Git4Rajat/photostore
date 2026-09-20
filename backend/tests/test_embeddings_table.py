"""Coverage for the photoembeddings table split: photoEmbedding/semanticEmbedding
used to live inline on the photometadata row; they're now written to their own
table (_extract_and_store_embeddings) and read back by the vector-index
rebuild and the cold-start search fallback (get_photo_embeddings), so the hot
display row never carries these large JSON float-array columns.
"""
from __future__ import annotations

import json

import pytest

import storage_utils


class _FakeTable:
    def __init__(self) -> None:
        self.rows: dict = {}

    def upsert_entity(self, entity):
        self.rows[(entity['PartitionKey'], entity['RowKey'])] = dict(entity)

    def get_entity(self, partition_key, row_key):
        key = (partition_key, row_key)
        if key not in self.rows:
            raise Exception('not found')
        return dict(self.rows[key])

    def delete_entity(self, partition_key, row_key):
        self.rows.pop((partition_key, row_key), None)

    def query_entities(self, filter_str, select=None):
        import re
        m = re.match(r"PartitionKey eq '([^']*)'$", filter_str)
        assert m, f'unexpected filter: {filter_str}'
        pk = m.group(1)
        return [dict(row) for (p, _), row in self.rows.items() if p == pk]


@pytest.fixture
def embeddings_table(monkeypatch):
    table = _FakeTable()
    monkeypatch.setitem(storage_utils._CTX, 'embeddings_table_client', table)
    return table


def test_extract_and_store_embeddings_pops_fields_and_writes_to_new_table(embeddings_table):
    entity = {
        'PartitionKey': 'u1', 'RowKey': 'photo.jpg',
        'tags': '["cat"]',
        'photoEmbedding': '[0.1, 0.2]',
        'photoEmbeddingVersion': 'v1',
        'semanticEmbedding': '[0.3, 0.4]',
        'semanticEmbeddingVersion': 'v2',
    }
    storage_utils._extract_and_store_embeddings('u1', 'photo.jpg', entity)

    # Popped off the entity that will be upserted to metadata_table_client.
    assert 'photoEmbedding' not in entity
    assert 'semanticEmbedding' not in entity
    assert 'photoEmbeddingVersion' not in entity
    assert entity['tags'] == '["cat"]'

    stored = embeddings_table.get_entity('u1', 'photo.jpg')
    assert stored['photoEmbedding'] == '[0.1, 0.2]'
    assert stored['semanticEmbedding'] == '[0.3, 0.4]'


def test_extract_and_store_embeddings_no_op_when_nothing_to_move(embeddings_table):
    entity = {'PartitionKey': 'u1', 'RowKey': 'photo.jpg', 'tags': '[]'}
    storage_utils._extract_and_store_embeddings('u1', 'photo.jpg', entity)
    assert entity == {'PartitionKey': 'u1', 'RowKey': 'photo.jpg', 'tags': '[]'}
    assert ('u1', 'photo.jpg') not in embeddings_table.rows


def test_extract_and_store_embeddings_puts_fields_back_when_table_unconfigured(monkeypatch):
    monkeypatch.setitem(storage_utils._CTX, 'embeddings_table_client', None)
    entity = {'PartitionKey': 'u1', 'RowKey': 'photo.jpg', 'photoEmbedding': '[0.1]'}
    storage_utils._extract_and_store_embeddings('u1', 'photo.jpg', entity)
    # No table configured -- must not silently drop real embedding data.
    assert entity['photoEmbedding'] == '[0.1]'


def test_get_photo_embeddings_round_trips(embeddings_table):
    storage_utils._extract_and_store_embeddings('u1', 'photo.jpg', {
        'PartitionKey': 'u1', 'RowKey': 'photo.jpg',
        'photoEmbedding': '[0.5]', 'photoEmbeddingVersion': 'v1',
    })
    result = storage_utils.get_photo_embeddings('u1', 'photo.jpg')
    assert result['photoEmbedding'] == '[0.5]'
    assert result['photoEmbeddingVersion'] == 'v1'
    assert result['semanticEmbedding'] is None


def test_get_photo_embeddings_missing_row_returns_empty_dict(embeddings_table):
    assert storage_utils.get_photo_embeddings('u1', 'nope.jpg') == {}


def test_delete_embeddings_entry_removes_row(embeddings_table):
    embeddings_table.upsert_entity({'PartitionKey': 'u1', 'RowKey': 'photo.jpg', 'photoEmbedding': '[0.1]'})
    storage_utils.delete_embeddings_entry('u1', 'photo.jpg')
    assert ('u1', 'photo.jpg') not in embeddings_table.rows


def test_vector_index_snapshot_reads_embedding_from_new_table(monkeypatch, embeddings_table):
    """_build_user_vector_index_snapshot must prefer the embeddings table over
    any (legacy) inline column on the metadata row."""
    metadata_table = _FakeTable()
    metadata_table.upsert_entity({'PartitionKey': 'u1', 'RowKey': 'photo.jpg', 'tags': '[]'})
    monkeypatch.setitem(storage_utils._CTX, 'metadata_table_client', metadata_table)

    dim = 4
    monkeypatch.setattr(storage_utils, 'PHOTO_EMBEDDING_DIMENSION', dim)
    monkeypatch.setattr(storage_utils.vision_utils, 'get_text_embedding_dimension', lambda: dim)
    monkeypatch.setattr(storage_utils.vision_utils, 'get_text_embedding_version', lambda: 'textv1')

    embeddings_table.upsert_entity({
        'PartitionKey': 'u1', 'RowKey': 'photo.jpg',
        'photoEmbedding': json.dumps([1.0, 0.0, 0.0, 0.0]),
        'photoEmbeddingVersion': storage_utils.PHOTO_EMBEDDING_MODEL_VERSION,
    })

    snapshot = storage_utils._build_user_vector_index_snapshot('u1', 'source-v1')

    assert snapshot is not None
    assert snapshot.row_keys == ['photo.jpg']
    assert snapshot.embeddings.shape == (1, dim)


def test_vector_index_snapshot_falls_back_to_row_for_pre_migration_data(monkeypatch, embeddings_table):
    """A row written before EMBEDDINGS_TABLE existed still carries its
    embedding inline -- the rebuild must still find it there."""
    metadata_table = _FakeTable()
    dim = 4
    metadata_table.upsert_entity({
        'PartitionKey': 'u1', 'RowKey': 'legacy.jpg', 'tags': '[]',
        'photoEmbedding': json.dumps([0.0, 1.0, 0.0, 0.0]),
        'photoEmbeddingVersion': storage_utils.PHOTO_EMBEDDING_MODEL_VERSION,
    })
    monkeypatch.setitem(storage_utils._CTX, 'metadata_table_client', metadata_table)
    monkeypatch.setattr(storage_utils, 'PHOTO_EMBEDDING_DIMENSION', dim)
    monkeypatch.setattr(storage_utils.vision_utils, 'get_text_embedding_dimension', lambda: dim)
    monkeypatch.setattr(storage_utils.vision_utils, 'get_text_embedding_version', lambda: 'textv1')

    # No corresponding row in embeddings_table -- must fall back to the
    # metadata row's own (legacy, inline) embedding instead of dropping it.
    snapshot = storage_utils._build_user_vector_index_snapshot('u1', 'source-v1')

    assert snapshot is not None
    assert snapshot.row_keys == ['legacy.jpg']
