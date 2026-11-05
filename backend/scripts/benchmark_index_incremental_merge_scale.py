#!/usr/bin/env python3
"""Does people/albums incremental-merge cost scale with INDEX SIZE (total
people/albums rows) or with AFFECTED-ID COUNT (how many a CRUD edit touched)?

Answers the question behind inline_index_merge.py's flat _MAX_PREV_ROWS
library-row-count ceiling: that gate reads the library's PHOTO count, but
refresh_user_people_index/refresh_user_albums_index's ID-targeted merge path
(_incremental_people_index_snapshot_by_ids / _incremental_albums_index_snapshot)
never scans photos -- it point-reads only the affected people/albums, then
merges the result into the previous snapshot's row list. If that merge step
is O(index rows) rather than O(affected ids), the right inline-eligibility
budget is index-row-count- or time-based, not a library-photo-count ceiling.

Local synthetic benchmark against the real storage_utils functions, FakeTable/
FakeBlob in-memory doubles (tests/fakes.py pattern) -- no Azure, no HTTP.
"""
import argparse
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Bypass index_ownership.require_catalog_owner's APP_ROLE gate -- this
# benchmarks the merge functions' raw CPU cost, not the role-gating logic
# (which is already covered by tests/test_index_ownership.py).
os.environ.setdefault('APP_ROLE', 'index-worker')

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import storage_utils  # noqa: E402
from tests.fakes import FakeTable  # noqa: E402


class _FakeBlob:
    def __init__(self, store: dict, key: str) -> None:
        self._store = store
        self._key = key

    def upload_blob(self, data, overwrite=True, content_settings=None):
        self._store[self._key] = data

    def download_blob(self):
        if self._key not in self._store:
            raise KeyError(self._key)
        return _Downloaded(self._store[self._key])

    def delete_blob(self):
        self._store.pop(self._key, None)


class _Downloaded:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def readall(self) -> bytes:
        return self._data


class _FakeBlobServiceClient:
    def __init__(self) -> None:
        self.blobs: dict = {}

    def get_blob_client(self, container, blob):
        return _FakeBlob(self.blobs, f'{container}/{blob}')


def summarize(samples_ms):
    ordered = sorted(samples_ms)
    n = len(ordered)
    return {'samples': n, **{
        name: round(ordered[max(0, math.ceil(n * p) - 1)], 3)
        for name, p in (('p50_ms', .5), ('p95_ms', .95), ('max_ms', 1.0))}}


def _seed_people(person_table, face_table, user_id, count):
    for i in range(count):
        pid, fid = f'p{i:07d}', f'f{i:07d}'
        person_table.upsert_entity({'PartitionKey': user_id, 'RowKey': pid,
                                     'name': f'Person {i}', 'faceIds': json.dumps([fid])})
        face_table.upsert_entity({'PartitionKey': user_id, 'RowKey': fid,
                                   'filename': f'img_{i}.jpg', 'personId': pid,
                                   'confidence': 0.9, 'bbox': '{}'})


def _seed_albums(albums_table, user_id, count):
    for i in range(count):
        aid = f'a{i:07d}'
        albums_table.upsert_entity({'PartitionKey': user_id, 'RowKey': aid,
                                     'name': f'Album {i}', 'filenames': json.dumps([f'img_{i}.jpg']),
                                     'updatedAt': '2026-01-01T00:00:00+00:00'})


def _fresh_blob_ctx():
    blob_service = _FakeBlobServiceClient()
    storage_utils._CTX['blob_service_client'] = blob_service
    storage_utils._CTX['blob_lexical_index_container'] = 'lexical-index'


def _people_ctx(user_id, count):
    person_table, face_table = FakeTable(), FakeTable()
    storage_utils._CTX['person_table_client'] = person_table
    storage_utils._CTX['face_table_client'] = face_table
    _fresh_blob_ctx()
    _seed_people(person_table, face_table, user_id, count)
    storage_utils.refresh_user_people_index(user_id, source_version='v0')  # seed the full snapshot/manifest


def _albums_ctx(user_id, count):
    albums_table = FakeTable()
    storage_utils._CTX['albums_table_client'] = albums_table
    _fresh_blob_ctx()
    _seed_albums(albums_table, user_id, count)
    storage_utils.refresh_user_albums_index(user_id, source_version='v0')


def _time_people_merges(user_id, affected_ids, samples):
    timings = []
    for s in range(samples):
        t0 = time.perf_counter()
        snapshot = storage_utils.refresh_user_people_index(
            user_id, source_version=f'v{s + 1}', affected_person_ids=affected_ids)
        timings.append((time.perf_counter() - t0) * 1000)
        assert snapshot is not None
    return timings


def _time_albums_merges(user_id, affected_ids, samples):
    timings = []
    for s in range(samples):
        t0 = time.perf_counter()
        snapshot = storage_utils.refresh_user_albums_index(
            user_id, source_version=f'v{s + 1}', affected_album_ids=affected_ids)
        timings.append((time.perf_counter() - t0) * 1000)
        assert snapshot is not None
    return timings


def bench_people_by_index_size(sizes, affected, samples):
    results = []
    for size in sizes:
        user_id = f'lib-people-size-{size}'
        _people_ctx(user_id, size)
        affected_ids = {f'p{i:07d}' for i in range(min(affected, size))}
        timings = _time_people_merges(user_id, affected_ids, samples)
        results.append({'index_rows': size, 'affected_ids': len(affected_ids), **summarize(timings)})
    return results


def bench_people_by_affected_count(index_size, affected_counts, samples):
    results = []
    for affected in affected_counts:
        user_id = f'lib-people-affected-{affected}'
        _people_ctx(user_id, index_size)
        affected_ids = {f'p{i:07d}' for i in range(min(affected, index_size))}
        timings = _time_people_merges(user_id, affected_ids, samples)
        results.append({'index_rows': index_size, 'affected_ids': len(affected_ids), **summarize(timings)})
    return results


def bench_albums_by_index_size(sizes, affected, samples):
    results = []
    for size in sizes:
        user_id = f'lib-albums-size-{size}'
        _albums_ctx(user_id, size)
        affected_ids = {f'a{i:07d}' for i in range(min(affected, size))}
        timings = _time_albums_merges(user_id, affected_ids, samples)
        results.append({'index_rows': size, 'affected_ids': len(affected_ids), **summarize(timings)})
    return results


def linear_fit(xs, ys):
    """Least-squares slope/intercept for ys ~= slope * xs + intercept."""
    n = len(xs)
    mean_x, mean_y = sum(xs) / n, sum(ys) / n
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    den = sum((x - mean_x) ** 2 for x in xs) or 1.0
    slope = num / den
    return slope, mean_y - slope * mean_x


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sizes', type=int, nargs='+', default=[100, 1000, 5000, 20000, 50000, 100000])
    parser.add_argument('--affected-counts', type=int, nargs='+', default=[1, 5, 20, 50, 100])
    parser.add_argument('--fixed-affected', type=int, default=5)
    parser.add_argument('--fixed-index-size', type=int, default=50000)
    parser.add_argument('--samples', type=int, default=5)
    parser.add_argument('--output', type=Path, default=None)
    args = parser.parse_args()

    result = {'started_utc': datetime.now(timezone.utc).isoformat(),
              'scope': 'in-process storage_utils merge functions, FakeTable/FakeBlob, no Azure/HTTP'}

    print(f'--- People: merge cost vs INDEX SIZE (fixed affected={args.fixed_affected}) ---')
    people_by_size = bench_people_by_index_size(args.sizes, args.fixed_affected, args.samples)
    for row in people_by_size:
        print(row)
    result['people_by_index_size'] = people_by_size

    print(f'\n--- People: merge cost vs AFFECTED COUNT (fixed index_rows={args.fixed_index_size}) ---')
    people_by_affected = bench_people_by_affected_count(args.fixed_index_size, args.affected_counts, args.samples)
    for row in people_by_affected:
        print(row)
    result['people_by_affected_count'] = people_by_affected

    print(f'\n--- Albums: merge cost vs INDEX SIZE (fixed affected={args.fixed_affected}) ---')
    albums_by_size = bench_albums_by_index_size(args.sizes, args.fixed_affected, args.samples)
    for row in albums_by_size:
        print(row)
    result['albums_by_index_size'] = albums_by_size

    p_slope, p_intercept = linear_fit([r['index_rows'] for r in people_by_size],
                                       [r['p50_ms'] for r in people_by_size])
    print(f'\nPeople merge: ~{p_slope * 1000:.4f} us per index row (p50 fit), intercept {p_intercept:.3f}ms')
    result['people_ms_per_index_row'] = p_slope

    a_slope, a_intercept = linear_fit([r['index_rows'] for r in albums_by_size],
                                       [r['p50_ms'] for r in albums_by_size])
    print(f'Albums merge: ~{a_slope * 1000:.4f} us per index row (p50 fit), intercept {a_intercept:.3f}ms')
    result['albums_ms_per_index_row'] = a_slope

    if args.output:
        args.output.write_text(json.dumps(result, indent=2))
        print(f'\nWrote {args.output}')


if __name__ == '__main__':
    main()
