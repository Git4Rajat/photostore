"""_load_face_rows_by_ids: a targeted multi-key fetch for a known face_id set, not a
partition scan -- cost (and query count) scales with len(face_ids), not account size."""
import json

import app
from tests.fakes import FakeTable


class _CountingFaces(FakeTable):
    def __init__(self):
        super().__init__()
        self.query_calls = 0

    def query_entities(self, filter_str, select=None, **kw):
        self.query_calls += 1
        return super().query_entities(filter_str, select=select, **kw)


def _faces_with(n):
    faces = _CountingFaces()
    for i in range(n):
        fid = f'f{i:04d}'
        faces.upsert_entity({'PartitionKey': 'u1', 'RowKey': fid, 'filename': f'{fid}.jpg', 'personId': 'P'})
    return faces


def test_returns_only_the_requested_ids_even_with_other_rows_in_the_partition(monkeypatch):
    faces = _faces_with(10)
    faces.upsert_entity({'PartitionKey': 'u1', 'RowKey': 'unrelated', 'filename': 'unrelated.jpg', 'personId': 'Q'})
    monkeypatch.setattr(app, 'face_table_client', faces)
    result = app._load_face_rows_by_ids('u1', ['f0001', 'f0003'])
    assert set(result) == {'f0001', 'f0003'}


def test_chunks_at_100_ids_per_query(monkeypatch):
    faces = _faces_with(150)
    monkeypatch.setattr(app, 'face_table_client', faces)
    ids = [f'f{i:04d}' for i in range(150)]
    result = app._load_face_rows_by_ids('u1', ids)
    assert len(result) == 150
    assert faces.query_calls == 2  # 100 + 50


def test_empty_face_ids_returns_empty_with_no_queries(monkeypatch):
    faces = _faces_with(5)
    monkeypatch.setattr(app, 'face_table_client', faces)
    result = app._load_face_rows_by_ids('u1', [])
    assert result == {}
    assert faces.query_calls == 0


def test_duplicate_ids_are_deduplicated(monkeypatch):
    faces = _faces_with(3)
    monkeypatch.setattr(app, 'face_table_client', faces)
    result = app._load_face_rows_by_ids('u1', ['f0000', 'f0000', 'f0001'])
    assert set(result) == {'f0000', 'f0001'}
    assert faces.query_calls == 1
