"""Isolated benchmark of the clustering mounted-file journal flush cadence.

Run: PYTHONPATH=backend python scripts/benchmark_cluster_journal.py

Drives the real FileAssignmentJournal decision-loop write pattern (decision!N
then applied!N per face) under two flush cadences:
  - current: _flush() (fsync) after every record  -> 2 fsyncs/face
  - batched: buffer records, fsync once per page, decisions durable before
    their applied markers (preserving the write-ahead invariant) -> 2 fsyncs/page

Local disk fsync is microseconds, so WALL here is NOT Azure Files. This measures
the fsync COUNT and verifies that the durable journal content is byte-identical
between cadences (batching must change only when os.fsync is called, never what
resume replays), then projects Azure wall from the LAW-measured per-fsync cost
(22.9s / 1972 = 11.6ms on the dev mount, wave 5e19459990b8). Crash/torn-journal
recovery is covered by the 49 tests in tests/test_wave_assignment_journal.py and
tests/test_wave_assignment_files.py, not re-derived here.
"""
import json
import os
import tempfile
import time
from pathlib import Path

import wave_assignment_files as waf
from upload_waves import digest

FSYNC_COUNT = [0]
_real_fsync = os.fsync
def counting_fsync(fd):
    FSYNC_COUNT[0] += 1
    return _real_fsync(fd)
os.fsync = counting_fsync


class FakeStore:
    """Minimal store: the decision!/applied! writes only touch the local file +
    SQLite index, never the cloud Table, so no real store/guard is needed."""
    library = 'u'
    def _get(self, key):
        return None
    def _row(self, key, **f):
        return dict(PartitionKey='p', RowKey=key, **f)


def decision_payload(i):
    return dict(faceId=f'face-{i}', personId=f'person-{i % 50}', outcome='assigned',
                position=i, manifestId='m', previousDigest='c' * 64, embeddingTier=1)


def run(n_faces, page_size, batched):
    FSYNC_COUNT[0] = 0
    with tempfile.TemporaryDirectory(prefix='cj-share-') as share, \
         tempfile.TemporaryDirectory(prefix='cj-work-') as work:
        directory = Path(share)  # share holds the durable journal.jsonl
        j = waf.FileAssignmentJournal(FakeStore(), 'wave-1', lambda *a: None, directory, work)
        started = time.perf_counter()
        for i in range(n_faces):
            if batched:
                j.preparing = True  # suppress per-record flush
                j.put(f'decision!{i:012d}', decision_payload(i))
                if (i + 1) % page_size == 0 or i + 1 == n_faces:
                    j._flush()  # decisions durable before any applied marker
                    for k in range(i - (i % page_size), i + 1):
                        j.put(f'applied!{k:012d}', {'decisionId': digest(decision_payload(k))})
                    j._flush()
            else:
                j.preparing = False  # production decision-loop cadence: flush per record
                j.put(f'decision!{i:012d}', decision_payload(i))
                j.put(f'applied!{i:012d}', {'decisionId': digest(decision_payload(i))})
        wall = time.perf_counter() - started
        records = {}
        with open(directory / 'journal.jsonl', 'rb') as f:
            for line in f:
                row = json.loads(line)
                if digest(row['payload']) != row['digest']:
                    raise AssertionError('corrupt record in journal')
                records[row['key']] = row['payload']
        j.close()
        return FSYNC_COUNT[0], wall, records


def main():
    n, page, azure_fsync_ms = 985, 128, 22916.095781 / 1972
    cur_fsyncs, cur_wall, cur_rec = run(n, page, batched=False)
    bat_fsyncs, bat_wall, bat_rec = run(n, page, batched=True)
    parity = cur_rec == bat_rec
    print(f"faces={n} page={page}  azure_per_fsync={azure_fsync_ms:.2f}ms (from LAW 22.9s/1972)")
    print(f"{'mode':<10}{'fsyncs':>8}{'local_wall_s':>14}{'azure_proj_s':>14}")
    print(f"{'current':<10}{cur_fsyncs:>8}{cur_wall:>14.3f}{cur_fsyncs * azure_fsync_ms / 1000:>14.2f}")
    print(f"{'batched':<10}{bat_fsyncs:>8}{bat_wall:>14.3f}{bat_fsyncs * azure_fsync_ms / 1000:>14.2f}")
    print(f"fsync reduction: {cur_fsyncs} -> {bat_fsyncs} ({(1 - bat_fsyncs / cur_fsyncs) * 100:.1f}% fewer)")
    print(f"projected azure journal-fsync wall: {cur_fsyncs * azure_fsync_ms / 1000:.1f}s -> "
          f"{bat_fsyncs * azure_fsync_ms / 1000:.2f}s")
    print(f"durable-record PARITY (current vs batched identical): {parity}  ({len(cur_rec)} records each)")
    if not parity:
        raise SystemExit('PARITY FAILED: batched cadence changed durable journal content')


if __name__ == '__main__':
    main()
