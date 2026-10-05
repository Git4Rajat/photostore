"""Disk-backed building blocks for index builds.

Index builds must be bounded by DISK, not RAM: a 130k-photo library has hundreds
of thousands of rows (and embeddings), and a build that gathers them in Python
lists/dicts before serializing is what OOM-killed replicas. These helpers let a
builder stream rows from the table straight into files, keep lookup tables in
SQLite on disk, and publish the finished file -- holding O(1) rows in memory.

Where the bytes go:
* ``INDEX_BUILD_WORK_DIR`` -- scratch + finished artifacts for plain files
  (gzip JSON, raw vectors). Point it at the shared Azure Files volume and builds
  run "on the file share"; defaults to the container's temp dir.
* ``INDEX_BUILD_SQLITE_DIR`` -- scratch for SQLite files. SQLite needs a real
  local filesystem (locking, random I/O), so this is always local ephemeral disk,
  never the SMB share.
"""
from __future__ import annotations

import gzip
import json
import os
import shutil
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

WORK_DIR = os.getenv('INDEX_BUILD_WORK_DIR', '').strip()
SQLITE_DIR = os.getenv('INDEX_BUILD_SQLITE_DIR', '').strip()
# Leftover workspaces from crashed builds older than this are removed on startup of a new one.
STALE_WORKSPACE_SECONDS = float(os.getenv('INDEX_BUILD_STALE_SECONDS', '7200'))
_PREFIX = 'index-build-'


def _base_dir(sqlite: bool = False) -> str:
    chosen = (SQLITE_DIR if sqlite else WORK_DIR) or os.path.join(tempfile.gettempdir(), 'photostore-index-build')
    os.makedirs(chosen, exist_ok=True)
    return chosen


def _sweep_stale(base: str) -> None:
    now = time.time()
    try:
        for name in os.listdir(base):
            path = os.path.join(base, name)
            if name.startswith(_PREFIX) and now - os.path.getmtime(path) > STALE_WORKSPACE_SECONDS:
                shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


@contextmanager
def workspace(*, sqlite: bool = False):
    """A scratch directory for one build, removed afterwards (and stale ones from
    crashed builds swept first)."""
    base = _base_dir(sqlite)
    _sweep_stale(base)
    path = tempfile.mkdtemp(prefix=_PREFIX, dir=base)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


# --- rows file: valid JSON *and* line-streamable -------------------------------------
# {"userId":...,"sourceVersion":...,"rows":[
# {row},
# {row}
# ]}
# Browsers/readers that json.loads the whole file get the usual {"rows": [...]};
# builders stream it line by line with O(1) memory.

class RowsWriter:
    """Streams rows into a gzip JSON file, one row per line."""

    def __init__(self, path: str, header: Dict[str, object], *, compresslevel: int = 5) -> None:
        self.path = path
        self.count = 0
        self._fh = gzip.open(path, 'wb', compresslevel=compresslevel)
        head = json.dumps(header, ensure_ascii=False, separators=(',', ':'), default=str)
        self._fh.write((head[:-1] + (',' if head != '{}' else '') + '"rows":[\n').encode('utf-8'))

    def add(self, row: Dict) -> None:
        if self.count:
            self._fh.write(b',\n')
        self._fh.write(json.dumps(row, ensure_ascii=False, separators=(',', ':'), default=str).encode('utf-8'))
        self.count += 1

    def close(self) -> int:
        if self._fh is not None:
            self._fh.write(b'\n]}')
            self._fh.close()
            self._fh = None
        return self.count

    def abort(self) -> None:
        try:
            if self._fh is not None:
                self._fh.close()
        finally:
            self._fh = None

    def __enter__(self) -> 'RowsWriter':
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.close()
        else:
            self.abort()


def iter_rows(path: str) -> Iterator[Dict]:
    """Stream the rows of a file written by RowsWriter (or any older single-line
    ``{"rows": [...]}`` gzip JSON, which falls back to a whole-file parse)."""
    with gzip.open(path, 'rb') as fh:
        first = fh.readline()
        if not first.rstrip().endswith(b'"rows":['):
            # legacy single-document file
            doc = json.loads(first + fh.read())
            yield from doc.get('rows') or []
            return
        for line in fh:
            line = line.strip()
            if line in (b']}', b']', b''):
                continue
            if line.endswith(b','):
                line = line[:-1]
            yield json.loads(line)


def read_header(path: str) -> Dict[str, object]:
    with gzip.open(path, 'rb') as fh:
        first = fh.readline().rstrip()
        if first.endswith(b'"rows":['):
            return json.loads(first[:-len(b',"rows":[')] + b'}') if first.endswith(b',"rows":[') else {}
        doc = json.loads(first + fh.read())
        doc.pop('rows', None)
        return doc


# --- disk-backed lookup table ----------------------------------------------------------

class DiskKV:
    """A string -> JSON dict map held in a local SQLite file instead of a Python
    dict. ``put_many`` for bulk loads, ``get_many`` for batched lookups."""

    def __init__(self, directory: str, name: str = 'kv.sqlite') -> None:
        self.path = os.path.join(directory, name)
        self._conn = sqlite3.connect(self.path)
        self._conn.executescript(
            'PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA cache_size=-8000;'
            'CREATE TABLE kv(k TEXT PRIMARY KEY, v TEXT NOT NULL) WITHOUT ROWID;'
        )
        self._pending: List[Tuple[str, str]] = []

    def put(self, key: str, value: Dict) -> None:
        self._pending.append((key, json.dumps(value, separators=(',', ':'), default=str)))
        if len(self._pending) >= 5000:
            self.flush()

    def flush(self) -> None:
        if self._pending:
            self._conn.executemany('INSERT OR REPLACE INTO kv(k, v) VALUES(?, ?)', self._pending)
            self._conn.commit()
            self._pending = []

    def get_many(self, keys: Sequence[str]) -> Dict[str, Dict]:
        self.flush()
        out: Dict[str, Dict] = {}
        unique = list(dict.fromkeys(keys))
        for start in range(0, len(unique), 500):
            chunk = unique[start:start + 500]
            marks = ','.join('?' * len(chunk))
            for key, value in self._conn.execute(f'SELECT k, v FROM kv WHERE k IN ({marks})', chunk):
                out[key] = json.loads(value)
        return out

    def __len__(self) -> int:
        self.flush()
        return int(self._conn.execute('SELECT COUNT(*) FROM kv').fetchone()[0])

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass
