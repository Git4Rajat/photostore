import re

import pytest
from test_index_work import EtagTable, Entity
from table_reads import read_entities
from table_reads import query_batches
from concurrent.futures import ThreadPoolExecutor
import threading


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


def test_known_namespaces_are_sorted_and_separated_but_filenames_share_batches():
    keys = [prefix + str(index) for index in range(20, -1, -1)
            for prefix in ('deleted-source!', 'supersession!', 'source-completion!')]
    batches = list(query_batches(keys, group_namespaces=True))
    assert all(len({key.split('!')[0] for key in batch}) == 1 and batch == sorted(batch) for batch in batches)
    assert len(batches) == 6
    assert len(list(query_batches(['a!holiday.jpg', 'b!holiday.jpg'], group_namespaces=True))) == 1


def test_parallel_reads_propagate_only_metrics_and_drain_on_error(monkeypatch):
    import processing_metrics as metrics
    from processing_fence import current_fence
    table = QueryRows()
    barrier = threading.Barrier(4)
    finished = []
    def query(filter_text):
        assert current_fence() is None
        barrier.wait(timeout=5)
        finished.append(1)
        metrics.record_io('table', 'GET', '/photojobs()', 200, 1, 10)
        if "RowKey eq '0'" in filter_text:
            raise ValueError('read failed')
        return []
    monkeypatch.setattr(table, 'query_entities', query)
    with metrics.accounting() as counter, pytest.raises(ValueError, match='read failed'):
        read_entities(table, 'lib', [str(index) for index in range(56)], workers=4)
    assert len(finished) == 4
    assert counter.snapshot()['job_read_requests'] == 4
