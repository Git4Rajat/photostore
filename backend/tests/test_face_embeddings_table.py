"""Coverage for the photofaceembeddings table split: a face's 'embedding'
column used to live inline on the photofaces row; it's now written to its
own table (_extract_and_store_face_embedding) and read back by clustering
code (get_face_embedding/get_face_embeddings_batch), so the hot face row
never carries this large JSON float-array column. Mirrors
test_embeddings_table.py's shape for the photo-level equivalent.
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
        match = re.fullmatch(r"PartitionKey eq '([^']*)' and filename eq '([^']*)'", filter_str)
        if match:
            return [dict(row) for (pk, _), row in self.rows.items()
                    if pk == match[1] and row.get('filename') == match[2]]
        m = re.match(r"PartitionKey eq '([^']*)' and \((.*)\)$", filter_str)
        if m:
            pk = m.group(1)
            clause_str = m.group(2)
            row_keys = set(re.findall(r"RowKey eq '([^']*)'", clause_str))
            return [
                dict(row) for (p, rk), row in self.rows.items()
                if p == pk and rk in row_keys
            ]
        m = re.match(r"PartitionKey eq '([^']*)'$", filter_str)
        assert m, f'unexpected filter: {filter_str}'
        pk = m.group(1)
        return [dict(row) for (p, _), row in self.rows.items() if p == pk]


@pytest.fixture
def face_embeddings_table(monkeypatch):
    table = _FakeTable()
    monkeypatch.setitem(storage_utils._CTX, 'face_embeddings_table_client', table)
    return table


def test_extract_and_store_face_embedding_pops_field_and_writes_to_new_table(face_embeddings_table):
    entity = {
        'PartitionKey': 'u1', 'RowKey': 'face-1',
        'filename': 'photo.jpg',
        'confidence': 0.9,
        'embedding': '[0.1, 0.2]',
        'embeddingVersion': 'v1',
    }
    storage_utils._extract_and_store_face_embedding('u1', 'face-1', entity)

    # Popped off the entity that will be upserted to face_table_client.
    assert 'embedding' not in entity
    # embeddingVersion deliberately stays on the row -- small scalar, read
    # directly by clustering version-gating logic, not the row-bloat source.
    assert entity['embeddingVersion'] == 'v1'
    assert entity['filename'] == 'photo.jpg'

    stored = face_embeddings_table.get_entity('u1', 'face-1')
    assert stored['embedding'] == '[0.1, 0.2]'


def test_extract_and_store_face_embedding_no_op_when_nothing_to_move(face_embeddings_table):
    entity = {'PartitionKey': 'u1', 'RowKey': 'face-1', 'filename': 'photo.jpg'}
    storage_utils._extract_and_store_face_embedding('u1', 'face-1', entity)
    assert entity == {'PartitionKey': 'u1', 'RowKey': 'face-1', 'filename': 'photo.jpg'}
    assert ('u1', 'face-1') not in face_embeddings_table.rows


def test_extract_and_store_face_embedding_puts_field_back_when_table_unconfigured(monkeypatch):
    monkeypatch.setitem(storage_utils._CTX, 'face_embeddings_table_client', None)
    entity = {'PartitionKey': 'u1', 'RowKey': 'face-1', 'embedding': '[0.1]'}
    storage_utils._extract_and_store_face_embedding('u1', 'face-1', entity)
    # No table configured -- must not silently drop real embedding data.
    assert entity['embedding'] == '[0.1]'


def test_get_face_embedding_round_trips(face_embeddings_table):
    storage_utils._extract_and_store_face_embedding('u1', 'face-1', {
        'PartitionKey': 'u1', 'RowKey': 'face-1', 'embedding': '[0.5, 0.25]',
    })
    assert storage_utils.get_face_embedding('u1', 'face-1') == [0.5, 0.25]


def test_get_face_embedding_missing_row_returns_empty_list(face_embeddings_table):
    assert storage_utils.get_face_embedding('u1', 'nope') == []


def test_get_face_embeddings_batch_returns_only_requested_ids(face_embeddings_table):
    for face_id, emb in (('face-1', [1.0]), ('face-2', [2.0]), ('face-3', [3.0])):
        storage_utils._extract_and_store_face_embedding('u1', face_id, {
            'PartitionKey': 'u1', 'RowKey': face_id, 'embedding': json.dumps(emb),
        })
    result = storage_utils.get_face_embeddings_batch('u1', ['face-1', 'face-3', 'missing'])
    assert result == {'face-1': [1.0], 'face-3': [3.0]}


def test_get_face_embeddings_batch_empty_input_returns_empty_dict(face_embeddings_table):
    assert storage_utils.get_face_embeddings_batch('u1', []) == {}


def test_get_face_embeddings_batch_chunks_large_id_lists(face_embeddings_table, monkeypatch):
    """A person with more faces than the chunk size must still get all of
    them back, via multiple chunked queries rather than one unbounded one."""
    ids = [f'face-{i}' for i in range(120)]
    for face_id in ids:
        storage_utils._extract_and_store_face_embedding('u1', face_id, {
            'PartitionKey': 'u1', 'RowKey': face_id, 'embedding': json.dumps([float(face_id[5:])]),
        })
    result = storage_utils.get_face_embeddings_batch('u1', ids)
    assert len(result) == 120
    assert result['face-0'] == [0.0]
    assert result['face-119'] == [119.0]


def test_delete_face_embeddings_entry_removes_row(face_embeddings_table):
    face_embeddings_table.upsert_entity({'PartitionKey': 'u1', 'RowKey': 'face-1', 'embedding': '[0.1]'})
    storage_utils.delete_face_embeddings_entry('u1', 'face-1')
    assert ('u1', 'face-1') not in face_embeddings_table.rows


def test_store_client_face_entities_writes_embedding_to_new_table(face_embeddings_table, monkeypatch):
    """_store_client_face_entities (the real write path, not just the
    low-level helper) must also route through the split."""
    face_table = _FakeTable()
    monkeypatch.setitem(storage_utils._CTX, 'face_table_client', face_table)
    monkeypatch.setitem(storage_utils._CTX, 'face_summary_lookup', None)

    storage_utils._store_client_face_entities('u1', 'photo.jpg', [{
        'embedding': [0.1, 0.2, 0.3],
        'confidence': 0.95,
        'bbox': {'left': 0, 'top': 0, 'width': 10, 'height': 10},
        'imageWidth': 100, 'imageHeight': 100,
    }])

    stored_faces = [row for (_, _), row in face_table.rows.items()]
    assert len(stored_faces) == 1
    assert 'embedding' not in stored_faces[0]

    stored_embeddings = [row for (_, _), row in face_embeddings_table.rows.items()]
    assert len(stored_embeddings) == 1
    assert json.loads(stored_embeddings[0]['embedding']) == [0.1, 0.2, 0.3]
