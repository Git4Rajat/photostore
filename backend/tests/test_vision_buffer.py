import os
import sqlite3

from test_index_work import Entity
from vision_buffer import LocalRows, MemberWindow
import processing_metrics as metrics


def test_spilled_rows_preserve_etags_and_clear_private_files():
    with metrics.accounting() as counter, LocalRows(prefix='receipt_cache', memory_bytes=0, disk_bytes=100000) as rows:
        row = Entity(dict(PartitionKey='private', RowKey='manifest!x', payload='{}'), 27)
        assert rows.put(row['RowKey'], row)
        rows.flush()
        directory = rows.directory.name
        assert os.stat(directory).st_mode & 0o777 == 0o700
        actual = rows.get(row['RowKey'])
        assert actual == row and actual.metadata['etag'] == '27'
        assert rows.disk_bytes > 0 and rows.memory_bytes == 0
    assert not os.path.exists(directory)
    assert counter.snapshot()['receipt_cache_disk_hits'] == 1


def test_buffer_refuses_over_capacity_without_dropping_prior_rows():
    with LocalRows(prefix='member_buffer', memory_bytes=180, disk_bytes=0) as rows:
        assert rows.put('a', dict(RowKey='a', payload='x' * 20))
        assert not rows.put('b', dict(RowKey='b', payload='x' * 1000))
        assert rows.get('a')['RowKey'] == 'a'
        assert rows.memory_bytes <= 180


def test_disk_failure_disables_spill_and_preserves_memory(monkeypatch):
    with LocalRows(prefix='receipt_cache', memory_bytes=180, disk_bytes=100000) as rows:
        assert rows.put('a', dict(RowKey='a'))
        monkeypatch.setattr(rows, '_database', lambda: (_ for _ in ()).throw(sqlite3.OperationalError('full')))
        assert not rows.put('big', dict(RowKey='big', payload='x' * 1000))
        assert rows.disk_limit == 0 and rows.get('a')['RowKey'] == 'a'


class Store:
    def __init__(self, count):
        self.rows = [Entity(dict(RowKey=f'{index:05}', value=index), 1) for index in range(count)]
        self.calls = []

    def _page(self, prefix, after, *, size):
        self.calls.append((after, size))
        return [row for row in self.rows if row['RowKey'] > after][:size]


def test_large_fetches_feed_small_groups_without_skipping_members():
    store = Store(2037)
    observed = []
    with MemberWindow(store, 'wave') as window:
        cursor = ''
        while True:
            page = window.page(cursor)
            assert len(page) <= 20
            observed.extend(row['value'] for row in page)
            if page:
                cursor = page[-1]['RowKey']
            if window.done:
                break
    assert observed == list(range(2037))
    assert len(store.calls) == 3


def test_deadline_resume_uses_last_processed_cursor_not_prefetched_tail():
    store = Store(80)
    with MemberWindow(store, 'wave') as window:
        first = window.page('')
        cursor = first[4]['RowKey']
    with MemberWindow(store, 'wave') as resumed:
        assert resumed.page(cursor)[0]['value'] == 5


def test_tiny_capacity_does_not_mistake_a_partial_buffer_for_end_of_wave():
    store = Store(41)
    observed = []
    with MemberWindow(store, 'wave', memory_bytes=180, disk_bytes=0) as window:
        cursor = ''
        while True:
            page = window.page(cursor)
            observed.extend(row['value'] for row in page)
            if page:
                cursor = page[-1]['RowKey']
            if window.done:
                break
    assert observed == list(range(41))
