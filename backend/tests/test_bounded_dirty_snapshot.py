"""Bounded dirty capture, lookahead, and version-safe acknowledgement."""
from types import SimpleNamespace

import pytest

import index_work
import search_db
import storage_utils as su
from tests.test_search_db_delta import world, _change, _row  # noqa: F401


def _rows(count, seen):
    for number in range(count):
        seen.append(number)
        yield {'PartitionKey': 'lib:sort', 'RowKey': str(number)}


@pytest.mark.parametrize('count,cap,truncated', [(0, 2, False), (2, 2, False), (3, 2, True), (1000000, 2, True), (1, 0, True)])
def test_snapshot_consumes_at_most_cap_plus_one(count, cap, truncated):
    seen = []
    snapshot = index_work.DirtySnapshot(_rows(count, seen), max_rows=cap)
    assert len(seen) == min(count, cap + 1)
    assert len(snapshot) == min(count, cap)
    assert snapshot.truncated is snapshot.has_more is truncated
    subset = snapshot.subset({'0'})
    assert subset.has_more is truncated
    if subset:
        assert subset.rows['0'] is snapshot.rows['0']


def test_legacy_snapshot_keeps_all_rows():
    assert len(index_work.DirtySnapshot(_rows(101, []))) == 101


def test_bounded_ack_retains_remarked_and_uncaptured_versions():
    from tests.fakes import FakeTable
    table = FakeTable()
    for name in ('changed', 'unchanged', 'uncaptured'):
        table.upsert_entity({'PartitionKey': 'A', 'RowKey': name})
    snapshot = index_work.DirtySnapshot(table.query_entities("PartitionKey eq 'A'"), max_rows=2)
    table.upsert_entity({'PartitionKey': 'A', 'RowKey': 'changed'})
    index_work.acknowledge_dirty(table, snapshot)
    assert set(table.rows) == {('A', 'changed'), ('A', 'uncaptured')}


def test_bounded_storage_reader_does_not_materialize_partition(monkeypatch):
    seen = []
    table = SimpleNamespace(query_entities=lambda query: _rows(1000000, seen))
    monkeypatch.setitem(su._CTX, 'search_index_dirty_table_client', table)
    monkeypatch.setattr(su, '_flush_dirty_filename_buffer', lambda *a: None)
    snapshot = su._get_dirty_search_index_filenames('lib', 'sort', max_rows=2)
    assert snapshot == {'0', '1'} and snapshot.has_more
    assert seen == [0, 1, 2]


def test_bounded_storage_read_failure_is_not_empty(monkeypatch):
    def rows(query):
        yield {'RowKey': 'a'}
        raise RuntimeError('read failed')
    monkeypatch.setitem(su._CTX, 'search_index_dirty_table_client', SimpleNamespace(query_entities=rows))
    monkeypatch.setattr(su, '_flush_dirty_filename_buffer', lambda *a: None)
    assert su._get_dirty_search_index_filenames('lib', 'sort', max_rows=2) is None


def test_signature_adapter_supports_legacy_but_does_not_hide_internal_typeerror():
    assert index_work.read_bounded_dirty(lambda uid, kind: {uid}, 'lib', 'sort', max_rows=1) == {'lib'}
    calls = []
    def broken(uid, kind, *, max_rows=None):
        calls.append(max_rows)
        raise TypeError('inside reader')
    with pytest.raises(TypeError, match='inside reader'):
        index_work.read_bounded_dirty(broken, 'lib', 'sort', max_rows=1)
    assert calls == [1]


def test_bounded_legacy_adapter_marks_oversize_capture_incomplete():
    result = index_work.read_bounded_dirty(lambda uid: {'a', 'b'}, 'lib', max_rows=1)
    assert len(result) == 1 and result.has_more and result.truncated


def test_empty_truncated_capture_stays_incomplete_through_adapter_and_subset():
    capture = index_work.DirtySnapshot(truncated=True)
    result = index_work.read_bounded_dirty(lambda uid, **kw: capture, 'lib', max_rows=1)
    assert not result and result.has_more and result.subset(set()).truncated


def test_copying_snapshot_preserves_incomplete_capture_and_row_versions():
    capture = index_work.DirtySnapshot(_rows(3, []), max_rows=2)
    copied = index_work.DirtySnapshot(capture)
    assert copied == capture and copied.has_more
    assert copied.rows['0'] is capture.rows['0']


def test_boolean_capture_cap_is_invalid():
    with pytest.raises(ValueError):
        index_work.DirtySnapshot(max_rows=True)


def test_search_reports_more_and_retains_uncaptured_rows(world, monkeypatch):
    _, meta, _ = world
    monkeypatch.setattr(su, 'SEARCH_DB_DELTA_MAX_NAMES', 1)
    for name in ('x.jpg', 'y.jpg', 'z.jpg'):
        _change(meta, _row(name, ['dog']))
    outcome = su.refresh_user_search_db_incremental('lib')
    assert outcome['status'] == 'more' and outcome['has_more']
    assert outcome['remaining'] >= 1  # lower bound, not a full-partition count
    assert len(su._get_dirty_search_index_filenames('lib', 'lexical')) == 2
    assert su.refresh_user_search_db_incremental('lib')['status'] == 'more'
    assert su.refresh_user_search_db_incremental('lib')['status'] == 'delta'
    assert su.refresh_user_search_db_incremental('lib')['status'] == 'noop'


def test_search_publication_failure_retains_captured_and_uncaptured(world, monkeypatch):
    _, meta, _ = world
    monkeypatch.setattr(su, 'SEARCH_DB_DELTA_MAX_NAMES', 1)
    for name in ('x.jpg', 'y.jpg'):
        _change(meta, _row(name, ['dog']))
    monkeypatch.setattr(search_db, 'publish_delta', lambda *a: None)
    assert su.refresh_user_search_db_incremental('lib')['status'] == 'conflict'
    assert su._get_dirty_search_index_filenames('lib', 'lexical') == {'x.jpg', 'y.jpg'}