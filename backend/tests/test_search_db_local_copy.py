"""The replica's local copy of the library database: several worker processes share one disk, so a
download or cleanup in one must never damage a file another is using (a 0 MB, table-less database
replaced a good one in production and every /api/photos call failed with 'no such table')."""
import gzip
import multiprocessing
import os
import sqlite3

import pytest

import search_db


def _rows(n=300):
    return [{'RowKey': f'p{i:05d}.jpg', 'uploadDate': f'2020-01-01T00:{i // 60:02d}:{i % 60:02d}+00:00'} for i in range(n)]


@pytest.fixture
def local(tmp_path, monkeypatch):
    src = str(tmp_path / 'src.sqlite')
    search_db.build_database(_rows(), src)
    payload = gzip.compress(open(src, 'rb').read())
    cache = str(tmp_path / 'cache')
    state = {'payload': payload}

    class Blob:
        def download_blob(self):
            class D:
                def readinto(_, fh):
                    fh.write(state['payload'])
            return D()

    monkeypatch.setattr(search_db, 'SEARCH_DB_DIR', cache)
    monkeypatch.setattr(search_db, '_blob_client', lambda name: Blob())
    monkeypatch.setattr(search_db, 'load_manifest', lambda uid: {
        'sourceVersion': 'v1', 'schemaVersion': search_db.SCHEMA_VERSION, 'deltaSeq': 0, 'lineage': 'v1'})
    search_db._OPEN.clear()
    search_db._VERIFIED.clear()
    return state, cache


def test_eviction_only_removes_finished_old_databases(local):
    _, cache = local
    os.makedirs(cache)
    keep = os.path.join(cache, 'u-aaaaaaaaaaaa.sqlite')
    old = os.path.join(cache, 'u-bbbbbbbbbbbb.sqlite')
    other_process_scratch = os.path.join(cache, 'u-aaaaaaaaaaaa.sqlite.123.456.db.tmp')
    for path in (keep, keep + '-wal', keep + '-shm', old, old + '-wal', other_process_scratch):
        open(path, 'w').close()
    search_db._evict_user('u', keep=keep)
    assert sorted(os.listdir(cache)) == sorted(['u-aaaaaaaaaaaa.sqlite', 'u-aaaaaaaaaaaa.sqlite-wal',
                                                'u-aaaaaaaaaaaa.sqlite-shm', 'u-aaaaaaaaaaaa.sqlite.123.456.db.tmp'])


def test_an_empty_download_is_rejected_not_installed(local):
    state, cache = local
    empty = str(os.path.join(os.path.dirname(cache), 'empty.sqlite'))
    sqlite3.connect(empty).close()
    state['payload'] = gzip.compress(open(empty, 'rb').read())
    assert search_db.open_database('u') is None
    assert not [n for n in os.listdir(cache) if n.endswith('.sqlite')]      # nothing installed
    state['payload'] = gzip.compress(open(os.path.join(os.path.dirname(cache), 'src.sqlite'), 'rb').read())
    db = search_db.open_database('u')                                       # the next request recovers
    assert db is not None and db.list_page(sort='capture', limit=5)[1] == 300


def test_a_damaged_local_copy_is_discarded_and_refetched(local):
    _, cache = local
    db = search_db.open_database('u')
    path = db.path
    search_db._OPEN.clear()
    search_db._VERIFIED.clear()
    with open(path, 'wb') as fh:                                            # an empty, table-less file
        fh.write(b'')
    assert search_db.open_database('u') is None and not os.path.exists(path)
    again = search_db.open_database('u')
    assert again is not None and again.list_page(sort='capture', limit=5)[1] == 300


def test_query_failure_on_a_bad_copy_drops_it(local):
    db = search_db.open_database('u')
    search_db.report_failure(db, sqlite3.OperationalError('no such table: rows'))
    assert not os.path.exists(db.path) and db.path not in search_db._OPEN


def _fetch_in_process(cache, payload, out):
    class Blob:
        def download_blob(self):
            class D:
                def readinto(_, fh):
                    fh.write(payload)
            return D()
    search_db.SEARCH_DB_DIR = cache
    search_db._blob_client = lambda name: Blob()
    search_db.load_manifest = lambda uid: {'sourceVersion': 'v1', 'schemaVersion': search_db.SCHEMA_VERSION, 'deltaSeq': 0, 'lineage': 'v1'}
    search_db._OPEN.clear()
    search_db._VERIFIED.clear()
    try:
        db = search_db.open_database('u')
        out.put(db.list_page(sort='capture', limit=5)[1] if db else -1)
    except Exception as exc:                                                # pragma: no cover
        out.put(repr(exc))


def test_two_processes_fetching_at_once_leave_a_good_database(local):
    state, cache = local
    ctx = multiprocessing.get_context('fork')
    out = ctx.Queue()
    procs = [ctx.Process(target=_fetch_in_process, args=(cache, state['payload'], out)) for _ in range(4)]
    [p.start() for p in procs]
    [p.join(60) for p in procs]
    results = [out.get(timeout=5) for _ in procs]
    assert results == [300] * 4
    search_db._OPEN.clear()
    search_db._VERIFIED.clear()
    assert search_db.open_database('u').list_page(sort='capture', limit=5)[1] == 300
