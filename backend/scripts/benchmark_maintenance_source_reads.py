"""Compare real incremental merge source stages with simulated storage latency.

No Azure or blob writes. This isolates source-read latency, not whole-job speed.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import statistics
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lexical_incremental
import people_embedding_incremental


class Table:
    def __init__(self, rows, delay):
        self.rows, self.delay = rows, delay
        self.lock = threading.Lock()
        self.calls = self.active = self.peak = 0

    def get_entity(self, partition_key, row_key):
        with self.lock:
            self.calls += 1
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            time.sleep(self.delay)
            return dict(self.rows[row_key])
        finally:
            with self.lock:
                self.active -= 1


def measure(kind, workers, delay, count):
    os.environ['INDEX_SOURCE_READ_CONCURRENCY'] = str(workers)
    if kind == 'lexical':
        table = Table({str(i): dict(PartitionKey='u', RowKey=str(i),
            processing_complete=True, caption='caption') for i in range(count)}, delay)
        started = time.perf_counter()
        rows = lexical_incremental._affected_rows(table, 'u', list(table.rows), set())
        elapsed = time.perf_counter() - started
        output = json.dumps(rows, sort_keys=True)
        tables = [table]
    else:
        people = Table({str(i): dict(PartitionKey='u', RowKey=str(i), name='Person',
            faceIds=json.dumps([f'{i}-{f}' for f in range(8)]),
            repEmbedding='[1,2,3]') for i in range(count)}, delay)
        faces = Table({f'{i}-{f}': dict(PartitionKey='u', RowKey=f'{i}-{f}', personId=str(i))
                       for i in range(count) for f in range(8)}, delay)
        with sqlite3.connect(':memory:') as db:
            db.execute('CREATE TABLE rows (pid TEXT PRIMARY KEY, position INTEGER, meta TEXT, vector BLOB, width INTEGER)')
            started = time.perf_counter()
            people_embedding_incremental._apply(db, people, faces, 'u', list(people.rows))
            elapsed = time.perf_counter() - started
            output = repr(db.execute('SELECT * FROM rows ORDER BY position').fetchall())
        tables = [people, faces]
    return dict(seconds=elapsed, source_calls=sum(t.calls for t in tables),
                peak_table_reads=max(t.peak for t in tables),
                output_sha256=hashlib.sha256(output.encode()).hexdigest())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--delay-ms', type=float, default=5)
    parser.add_argument('--samples', type=int, default=3)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.samples < 1 or args.delay_ms < 0:
        parser.error('samples must be positive and delay-ms must be nonnegative')
    result = dict(scope='real source stages; synthetic tables; no Azure; no publication',
                  delay_ms=args.delay_ms, samples=args.samples, cases={})
    for kind, count in [('lexical', 256), ('people_embedding', 32)]:
        case = {}
        for workers in [1, 4]:
            samples = [measure(kind, workers, args.delay_ms / 1000, count) for _ in range(args.samples)]
            case[str(workers)] = dict(median_seconds=statistics.median(s['seconds'] for s in samples),
                                     runs=samples)
        assert len({s['output_sha256'] for c in case.values() for s in c['runs']}) == 1
        assert len({s['source_calls'] for c in case.values() for s in c['runs']}) == 1
        case['speedup'] = case['1']['median_seconds'] / case['4']['median_seconds']
        result['cases'][kind] = case
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({kind: {k: v for k, v in case.items() if k == 'speedup' or k in ('1', '4')}
                      for kind, case in result['cases'].items()}, indent=2))


if __name__ == '__main__':
    main()
