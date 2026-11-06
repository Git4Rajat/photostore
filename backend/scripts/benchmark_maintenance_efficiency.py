"""Measure the worker SQLite page-cache change in isolated local processes.

Uses the actual SearchDatabase connection path and a synthetic 48 MiB table.
This measures resident native SQLite cache, not container allocation or Azure.
"""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def probe(path, cache_kb):
    os.environ['APP_ROLE'] = 'index-worker'
    os.environ['SEARCH_DB_SQLITE_CACHE_KB'] = str(cache_kb)
    import psutil
    import search_db
    process = psutil.Process()
    before = process.memory_info().rss
    db = search_db.SearchDatabase(path)
    started, cpu = time.perf_counter(), time.process_time()
    for _ in range(2):
        assert db._conn().execute('SELECT sum(length(payload)) FROM probe').fetchone()[0] == 24000 * 2048
    return dict(cache_kb=cache_kb, elapsed_seconds=time.perf_counter() - started,
                cpu_seconds=time.process_time() - cpu, rss_increase_bytes=process.memory_info().rss - before,
                pragma_cache_size=db._conn().execute('PRAGMA cache_size').fetchone()[0])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--sqlite-probe')
    parser.add_argument('--cache-kb', type=int)
    args = parser.parse_args()
    if args.sqlite_probe:
        print(json.dumps(probe(args.sqlite_probe, args.cache_kb)))
        return
    if not args.output:
        parser.error('--output is required')
    with tempfile.TemporaryDirectory(prefix='maintenance-efficiency-') as directory:
        path = str(Path(directory) / 'probe.sqlite')
        with sqlite3.connect(path) as conn:
            conn.execute('CREATE TABLE probe (id INTEGER PRIMARY KEY, payload TEXT)')
            conn.executemany('INSERT INTO probe VALUES (?,?)', ((i, 'x' * 2048) for i in range(24000)))
        cases = {str(kb): [json.loads(subprocess.check_output([sys.executable, __file__, '--sqlite-probe', path,
                    '--cache-kb', str(kb)], text=True)) for _ in range(3)] for kb in [16000, 4096]}
    result = dict(scope='native SQLite cache RSS, synthetic local DB, three fresh processes per setting', cases=cases)
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
