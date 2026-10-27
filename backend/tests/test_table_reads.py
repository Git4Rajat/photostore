import re

import pytest
from test_index_work import EtagTable, Entity
from table_reads import read_entities


class QueryRows(EtagTable):
    def __init__(self):
        super().__init__()
        self.queries = []
        self.reads = 0

    def get_entity(self, *args, **kwargs):
        self.reads += 1
        return super().get_entity(*args, **kwargs)

    def query_entities(self, query):
        self.queries.append(query)
        parts = re.findall(r"(?:PartitionKey|RowKey) eq '((?:[^']|'')*)'", query)
        assert len(parts) <= 15
        partition, *keys = [part.replace("''", "'") for part in parts]
        return [Entity(row, self.versions[key]) for key, row in self.rows.items()
                if key[0] == partition and key[1] in keys]


def test_key_queries_are_bounded_and_preserve_conditional_write_etags():
    table = QueryRows()
    keys = [f"photo'{i}" for i in range(40)]
    for key in keys:
        table.create_entity(dict(PartitionKey="lib'", RowKey=key))
    rows = read_entities(table, "lib'", keys + ['missing'])
    assert len(table.queries) == 3 and table.reads == 0
    assert rows['missing'] is None
    assert all(rows[key].metadata['etag'] == '1' for key in keys)


def test_batch_queries_reject_other_partitions(monkeypatch):
    table = QueryRows()
    monkeypatch.setattr(table, 'query_entities', lambda query: [dict(PartitionKey='foreign', RowKey='key')])
    with pytest.raises(ValueError, match='another partition'):
        read_entities(table, 'lib', ['key'])


def test_long_keys_split_before_the_filter_byte_budget():
    table = QueryRows()
    read_entities(table, 'lib', ['å' * 200 + str(i) for i in range(20)])
    assert len(table.queries) >= 5
