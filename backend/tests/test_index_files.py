"""index_files: the disk-backed building blocks that keep index builds out of RAM."""
from __future__ import annotations

import gc
import gzip
import json
import os
import time
import tracemalloc

import pytest

import index_files as ix


def test_rows_file_is_valid_json_and_line_streamable(tmp_path):
    path = str(tmp_path / 'r.json.gz')
    with ix.RowsWriter(path, {'userId': 'u', 'sourceVersion': 'v1', 'schemaVersion': 's'}) as w:
        for i in range(5):
            w.add({'RowKey': f'p{i}', 'n': i, 'text': 'ünï"cøde\nnewline'})
    whole = json.loads(gzip.decompress(open(path, 'rb').read()))      # what a browser / old reader sees
    assert whole['sourceVersion'] == 'v1' and len(whole['rows']) == 5 and whole['rows'][4]['text'] == 'ünï"cøde\nnewline'
    assert [r['n'] for r in ix.iter_rows(path)] == [0, 1, 2, 3, 4]
    assert ix.read_header(path) == {'userId': 'u', 'sourceVersion': 'v1', 'schemaVersion': 's'}


def test_empty_rows_file_and_empty_header(tmp_path):
    path = str(tmp_path / 'e.json.gz')
    with ix.RowsWriter(path, {}) as w:
        pass
    assert json.loads(gzip.decompress(open(path, 'rb').read())) == {'rows': []}
    assert list(ix.iter_rows(path)) == [] and ix.read_header(path) == {}


def test_reader_accepts_legacy_single_line_files(tmp_path):
    path = str(tmp_path / 'old.json.gz')
    with gzip.open(path, 'wb') as fh:
        fh.write(json.dumps({'userId': 'u', 'sourceVersion': 'v0', 'rows': [{'RowKey': 'a'}, {'RowKey': 'b'}]}).encode())
    assert [r['RowKey'] for r in ix.iter_rows(path)] == ['a', 'b']
    assert ix.read_header(path)['sourceVersion'] == 'v0'


def test_abort_leaves_no_unclosed_writer(tmp_path):
    w = ix.RowsWriter(str(tmp_path / 'a.json.gz'), {})
    w.add({'a': 1})
    w.abort()
    w.abort()   # idempotent


def test_streaming_a_big_file_uses_flat_memory(tmp_path):
    path = str(tmp_path / 'big.json.gz')
    with ix.RowsWriter(path, {'v': 1}) as w:
        for i in range(30000):
            w.add({'RowKey': f'p{i}', 'blob': 'x' * 400 + str(i)})
    gc.collect()
    tracemalloc.start()
    try:
        total = sum(1 for _ in ix.iter_rows(path))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert total == 30000 and peak < 3 * 1024 * 1024    # ~12MB of rows if materialized


def test_workspace_is_removed_and_stale_ones_swept(tmp_path, monkeypatch):
    monkeypatch.setattr(ix, 'WORK_DIR', str(tmp_path))
    stale = tmp_path / 'index-build-old'
    stale.mkdir()
    old = time.time() - 10 * 3600
    os.utime(stale, (old, old))
    keep = tmp_path / 'other-dir'
    keep.mkdir()
    with ix.workspace() as ws:
        assert os.path.isdir(ws) and ws.startswith(str(tmp_path))
        open(os.path.join(ws, 'x'), 'w').write('1')
    assert not os.path.exists(ws) and not stale.exists() and keep.exists()


def test_workspace_cleans_up_when_the_build_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(ix, 'WORK_DIR', str(tmp_path))
    with pytest.raises(RuntimeError):
        with ix.workspace() as ws:
            raise RuntimeError('boom')
    assert not os.path.exists(ws)


def test_sqlite_scratch_is_always_local_not_the_share(tmp_path, monkeypatch):
    monkeypatch.setattr(ix, 'WORK_DIR', str(tmp_path / 'share'))
    monkeypatch.setattr(ix, 'SQLITE_DIR', str(tmp_path / 'local'))
    with ix.workspace(sqlite=True) as ws:
        assert ws.startswith(str(tmp_path / 'local'))


def test_diskkv_round_trips_and_batches_lookups(tmp_path):
    kv = ix.DiskKV(str(tmp_path))
    for i in range(12000):
        kv.put(f'f{i}', {'personId': f'p{i % 7}', 'confidence': i / 100})
    assert len(kv) == 12000
    got = kv.get_many([f'f{i}' for i in range(0, 12000, 3)] + ['missing'])
    assert len(got) == 4000 and got['f9']['personId'] == 'p2' and 'missing' not in got
    kv.put('f9', {'personId': 'changed'})
    assert kv.get_many(['f9'])['f9']['personId'] == 'changed'    # put after a read is visible (auto flush)
    kv.close()
