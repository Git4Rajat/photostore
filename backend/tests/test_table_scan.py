"""Ordered parallel partition scan: same rows, same order as a sequential scan."""
import re
import threading
import time

import pytest

import table_scan


class _RangeTable:
    """Minimal Table fake: one partition, RowKey-ordered, understands
    "(base) and RowKey ge/gt 'x' and RowKey lt 'y'"."""

    def __init__(self, keys, delay=0.0, fail_on=None):
        self.rows = [{'RowKey': k, 'v': i} for i, k in enumerate(sorted(keys))]
        self.calls = []
        self.delay = delay
        self.fail_on = fail_on
        self.lock = threading.Lock()

    def query_entities(self, filter_str, select=None, **_):
        with self.lock:
            self.calls.append(filter_str)
        if self.fail_on and self.fail_on in filter_str:
            raise RuntimeError('storage down')
        lo = re.search(r"RowKey (ge|gt) '((?:[^']|'')*)'", filter_str)
        hi = re.search(r"RowKey lt '((?:[^']|'')*)'", filter_str)

        def gen():
            for row in self.rows:
                key = row['RowKey']
                if lo and not (key >= lo.group(2).replace("''", "'") if lo.group(1) == 'ge' else key > lo.group(2).replace("''", "'")):
                    continue
                if hi and not key < hi.group(1).replace("''", "'"):
                    continue
                if self.delay:
                    time.sleep(self.delay)
                yield dict(row)
        return gen()


KEYS = (
    [f'IMG_{i:05d}.jpg' for i in range(9000)]            # one very hot prefix
    + [f'DSC{i:04d}.jpg' for i in range(500)]
    + [f'{i:08x}-aaaa-bbbb.jpg' for i in range(700)]      # uuid-like
    + ['zeta.jpg', 'Zed.png', "it's.jpg", '~tilde.jpg', ' space.jpg', 'é.jpg']
)


def _scan(table, **kw):
    return list(table_scan.scan_partition(table.query_entities, "PartitionKey eq 'u'", **kw))


def test_matches_a_sequential_scan_in_order(monkeypatch):
    monkeypatch.setattr(table_scan, 'SPLIT_AFTER_ROWS', 700)
    table = _RangeTable(KEYS)
    parallel = _scan(table, workers=4)
    assert [r['RowKey'] for r in parallel] == sorted(KEYS)
    assert len({r['RowKey'] for r in parallel}) == len(KEYS)          # nothing duplicated or dropped
    assert len(table.calls) > 66                                       # hot ranges were split on the fly


def test_hot_prefix_is_spread_across_workers(monkeypatch):
    monkeypatch.setattr(table_scan, 'SPLIT_AFTER_ROWS', 500)
    table = _RangeTable([f'IMG_{i:05d}.jpg' for i in range(6000)], delay=0.0002)
    start = time.time()
    rows = _scan(table, workers=6)
    parallel_s = time.time() - start
    start = time.time()
    list(_RangeTable([f'IMG_{i:05d}.jpg' for i in range(6000)], delay=0.0002).query_entities("PartitionKey eq 'u'"))
    sequential_s = time.time() - start
    assert len(rows) == 6000
    assert parallel_s < sequential_s * 0.7, (parallel_s, sequential_s)


def test_one_worker_is_the_plain_sequential_scan():
    table = _RangeTable(KEYS[:50])
    assert len(_scan(table, workers=1)) == 50
    assert len(table.calls) == 1


def test_a_worker_error_reaches_the_consumer():
    table = _RangeTable(KEYS, fail_on="'IMG_")
    with pytest.raises(RuntimeError, match='storage down'):
        _scan(table, workers=4)


def test_abandoning_the_iterator_stops_the_workers(monkeypatch):
    monkeypatch.setattr(table_scan, 'QUEUE_ROWS', 10)
    table = _RangeTable(KEYS)
    before = threading.active_count()
    it = table_scan.scan_partition(table.query_entities, "PartitionKey eq 'u'", workers=4)
    for _ in range(25):
        next(it)
    it.close()
    time.sleep(0.5)
    assert threading.active_count() <= before + 1


def test_single_worker_with_tiny_queues_does_not_deadlock(monkeypatch):
    monkeypatch.setattr(table_scan, 'QUEUE_ROWS', 5)
    monkeypatch.setattr(table_scan, 'SPLIT_AFTER_ROWS', 20)
    table = _RangeTable(KEYS[:600])
    assert [r['RowKey'] for r in _scan(table, workers=2)] == sorted(KEYS[:600])


def test_empty_partition():
    assert _scan(_RangeTable([]), workers=4) == []


def test_library_streaming_scan_uses_the_parallel_reader(monkeypatch):
    import storage_utils
    keys = [f'IMG_{i:05d}.jpg' for i in range(3000)] + ['trashed.jpg']
    table = _RangeTable(keys)
    for row in table.rows:
        row['processing_complete'] = True
        row['processing_state'] = 'deleted' if row['RowKey'] == 'trashed.jpg' else 'done'
    monkeypatch.setitem(storage_utils._CTX, 'metadata_table_client', table)
    monkeypatch.setattr(table_scan, 'PARALLELISM', 4)
    monkeypatch.setattr(table_scan, 'SPLIT_AFTER_ROWS', 400)
    names = [r['RowKey'] for r in storage_utils.iter_library_rows('u')]
    assert names == sorted(k for k in keys if k != 'trashed.jpg')
    assert len(table.calls) > 1
