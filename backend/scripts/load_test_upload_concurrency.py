#!/usr/bin/env python3
"""Controlled concurrent-upload load test against a live backend deployment
(intended target: photostore-test, the same environment used for the earlier
GUNICORN_THREADS 4->12 experiment -- see deploy/resources.bicep:470).

Purpose: answer "what does the real upload concurrency curve look like after
the dd2a1b1 CPU-bound-embedding-index fix + the finalize read/write removal
deploy" -- not a synthetic benchmark number. Drives the real HTTP path a
browser uses: POST /upload/init-batch -> PUT bytes straight to the returned
blob SAS URL -> POST /upload/finalize-batch, at increasing concurrency levels
against a FIXED, reproducible file set (same account, same photos, same
batch size across every level -- only worker concurrency varies).

This script measures CLIENT-OBSERVED latency/throughput/errors only. Pair it
with the server-side phase timing this same change adds to
init_upload_batch/finalize_upload_batch/upload_client_processing_results in
backend/app.py (look for 'init-batch timings' / 'finalize-batch timings' /
'client-processing timings' log lines) to see which phase inside a request
actually grows under load -- this script's summary tells you WHEN latency
grew, the server log lines tell you WHERE. Correlate by the wall-clock
window a --level printed at the end of its run; do not tail server logs with
`az containerapp logs --follow` while this runs (see
docs/az-containerapp-logs-follow-stop-incident equivalent -- --follow has a
history of triggering real stop actions against unrelated apps). Use
`az containerapp logs show` (no --follow) for a bounded time window instead.

KNOWN LIMITATION: batches are sent with no `clientProcessing` payload, so
the finalize-batch 'client_processing' phase (which is where dd2a1b1's fix
actually lives -- _apply_client_processing_results -> the embedding-match
path) will read ~0ms in every run. This script proves out the control-plane
cost (SAS minting, blob property checks, metadata read/write, queue enqueue)
under concurrency, which is real and was the source of the 05f43ee convoy
stall -- it does NOT reproduce the embedding-index hotspot end-to-end. That
would need a real browser-shaped clientProcessing.face payload (matching
_apply_client_processing_results's expected report/embedding shape in
storage_utils.py), which is a separate, larger follow-up if this run's
numbers don't already explain what we're chasing.

Auth: reads a session token from UPLOAD_LOADTEST_TOKEN if set (mint one
yourself via POST /auth/login and paste it into your shell env -- do not
pass a password on the CLI, it ends up in shell history and process list).
Otherwise falls back to UPLOAD_LOADTEST_EMAIL + UPLOAD_LOADTEST_PASSWORD env
vars and logs in itself.

Usage:
  export UPLOAD_LOADTEST_TOKEN=eyJ...
  python backend/scripts/load_test_upload_concurrency.py \\
      --base-url https://<photostore-test-backend-fqdn> \\
      --photo-dir ~/Downloads/export \\
      --count 40 --batch-size 12 --concurrency 1,2,4,8

  # See what it would do without making any network calls:
  python backend/scripts/load_test_upload_concurrency.py --photo-dir ~/Downloads/export --dry-run
"""
from __future__ import annotations

import argparse
import hashlib
import mimetypes
import os
import random
import statistics
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

# Small valid 1x1 JPEG, reused as a placeholder thumbnail PUT so the request
# shape matches a real client's two-blob-per-file upload (image + thumbnail)
# without needing real thumbnail generation -- this script only exists to
# load the backend's HTTP/table path, not to produce a real gallery.
_PLACEHOLDER_THUMBNAIL_JPEG = bytes.fromhex(
    'ffd8ffe000104a46494600010100000100010000ffdb004300030202020202'
    '03020202030303030406040404040408060605070907080a0a090809090a0c'
    '0f0c0a0b0e0b09090d110d0e0f101011100a0c12131210130f101010ffc900'
    '0b080001000101011100ffcc0006001000010005ffda0008010100003f00d2'
    'cf20ffd9'
)

SUPPORTED_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.heic', '.heif', '.cr3', '.dng', '.tif', '.tiff'}


@dataclass
class SampleFile:
    path: Path
    filename: str
    size: int
    sha256: str
    content_type: str


@dataclass
class BatchResult:
    init_ms: Optional[float] = None
    blob_put_ms: Optional[float] = None
    finalize_ms: Optional[float] = None
    file_count: int = 0
    errors: List[str] = field(default_factory=list)


def _log(msg: str) -> None:
    print(f'[{datetime.now(timezone.utc).isoformat(timespec="seconds")}] {msg}', flush=True)


def _pick_sample(photo_dir: Path, count: int, seed: int) -> List[Path]:
    candidates = [
        p for p in photo_dir.rglob('*')
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
    ]
    if not candidates:
        raise SystemExit(f'No files with a supported extension found under {photo_dir}')
    candidates.sort()  # deterministic before shuffling, so --seed reproduces the same set
    rng = random.Random(seed)
    rng.shuffle(candidates)
    return candidates[:count]


def _hash_and_read(path: Path) -> SampleFile:
    data = path.read_bytes()
    content_type = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
    return SampleFile(
        path=path,
        filename=path.name,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        content_type=content_type,
    )


def _login(base_url: str, email: str, password: str, timeout: float) -> str:
    resp = requests.post(
        f'{base_url}/auth/login', json={'email': email, 'password': password}, timeout=timeout,
    )
    resp.raise_for_status()
    token = resp.json().get('token')
    if not token:
        raise SystemExit('Login succeeded but response had no token -- unexpected response shape')
    return token


def _run_batch(
    session: requests.Session,
    base_url: str,
    files: List[SampleFile],
    file_bytes: Dict[str, bytes],
    batch_size: int,
    with_thumbnail_put: bool,
    timeout: float,
) -> List[BatchResult]:
    results: List[BatchResult] = []
    for start in range(0, len(files), batch_size):
        chunk = files[start:start + batch_size]
        result = BatchResult(file_count=len(chunk))

        t0 = time.perf_counter()
        try:
            init_resp = session.post(
                f'{base_url}/upload/init-batch',
                json={'files': [
                    {'filename': f.filename, 'totalSize': f.size, 'sha256': f.sha256} for f in chunk
                ]},
                timeout=timeout,
            )
            init_resp.raise_for_status()
            init_results = init_resp.json().get('results', [])
        except Exception as exc:
            result.errors.append(f'init-batch request failed: {exc}')
            results.append(result)
            continue
        result.init_ms = (time.perf_counter() - t0) * 1000

        by_filename = {f.filename: f for f in chunk}
        finalize_items = []
        t1 = time.perf_counter()
        for entry in init_results:
            filename = entry.get('filename')
            sf = by_filename.get(filename)
            if sf is None:
                continue
            if entry.get('error'):
                result.errors.append(f'{filename}: init-batch returned {entry["error"]}')
                continue
            blob_url = entry.get('blobUrl')
            if not blob_url:
                result.errors.append(f'{filename}: no blobUrl in init-batch response')
                continue
            try:
                put_resp = requests.put(
                    blob_url,
                    data=file_bytes[filename],
                    headers={'x-ms-blob-type': 'BlockBlob', 'Content-Type': sf.content_type},
                    timeout=timeout,
                )
                put_resp.raise_for_status()
                if with_thumbnail_put and entry.get('thumbnailBlobUrl'):
                    thumb_resp = requests.put(
                        entry['thumbnailBlobUrl'],
                        data=_PLACEHOLDER_THUMBNAIL_JPEG,
                        headers={'x-ms-blob-type': 'BlockBlob', 'Content-Type': 'image/jpeg'},
                        timeout=timeout,
                    )
                    thumb_resp.raise_for_status()
            except Exception as exc:
                result.errors.append(f'{filename}: blob PUT failed: {exc}')
                continue
            finalize_items.append({
                'filename': filename,
                'totalSize': sf.size,
                'contentType': sf.content_type,
                'sha256': sf.sha256,
                'uploadId': entry.get('uploadId') or str(uuid.uuid4()),
                'clientAssetId': entry.get('uploadId') or '',
            })
        result.blob_put_ms = (time.perf_counter() - t1) * 1000

        if not finalize_items:
            results.append(result)
            continue

        t2 = time.perf_counter()
        try:
            finalize_resp = session.post(
                f'{base_url}/upload/finalize-batch', json={'files': finalize_items}, timeout=timeout,
            )
            finalize_resp.raise_for_status()
            for entry in finalize_resp.json().get('results', []):
                if entry.get('error'):
                    result.errors.append(f'{entry.get("filename")}: finalize-batch returned {entry["error"]}')
        except Exception as exc:
            result.errors.append(f'finalize-batch request failed: {exc}')
        result.finalize_ms = (time.perf_counter() - t2) * 1000

        results.append(result)
    return results


def _percentile(values: List[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round(pct / 100 * (len(ordered) - 1))))
    return ordered[idx]


def _summarize(level: int, wall_s: float, batch_results: List[BatchResult]) -> Dict:
    init_times = [r.init_ms for r in batch_results if r.init_ms is not None]
    finalize_times = [r.finalize_ms for r in batch_results if r.finalize_ms is not None]
    total_files = sum(r.file_count for r in batch_results)
    total_errors = sum(len(r.errors) for r in batch_results)
    return {
        'concurrency': level,
        'batches': len(batch_results),
        'files': total_files,
        'wall_s': round(wall_s, 2),
        'throughput_files_per_s': round(total_files / wall_s, 2) if wall_s > 0 else 0.0,
        'init_p50_ms': round(_percentile(init_times, 50), 1),
        'init_p90_ms': round(_percentile(init_times, 90), 1),
        'finalize_p50_ms': round(_percentile(finalize_times, 50), 1),
        'finalize_p90_ms': round(_percentile(finalize_times, 90), 1),
        'errors': total_errors,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--base-url', default=os.getenv('UPLOAD_LOADTEST_BASE_URL', ''),
                     help='backend base URL, e.g. https://photostore-test-backend.<region>.azurecontainerapps.io (no trailing slash)')
    ap.add_argument('--photo-dir', required=True, help='directory of real photos to sample from (recurses)')
    ap.add_argument('--count', type=int, default=40, help='total distinct files to sample -- same set is reused at every concurrency level')
    ap.add_argument('--batch-size', type=int, default=12, help='files per init-batch/finalize-batch call (server max is 20)')
    ap.add_argument('--concurrency', default='1,2,4,8', help='comma-separated list of concurrency levels to sweep, in order')
    ap.add_argument('--seed', type=int, default=42, help='sample selection seed, for a reproducible file set across runs')
    ap.add_argument('--timeout', type=float, default=120.0, help='per-HTTP-call timeout in seconds')
    ap.add_argument('--no-thumbnail-put', action='store_true', help='skip the placeholder thumbnail PUT (real clients always send one)')
    ap.add_argument('--dry-run', action='store_true', help='sample + hash files and print the plan, make no network calls')
    args = ap.parse_args()

    photo_dir = Path(args.photo_dir).expanduser()
    if not photo_dir.is_dir():
        print(f'ERROR: {photo_dir} is not a directory', file=sys.stderr)
        return 2
    levels = [int(x) for x in args.concurrency.split(',') if x.strip()]

    _log(f'Sampling {args.count} files from {photo_dir} (seed={args.seed})...')
    paths = _pick_sample(photo_dir, args.count, args.seed)
    _log(f'Reading + hashing {len(paths)} files once (reused, unmodified, at every concurrency level)...')
    files = [_hash_and_read(p) for p in paths]
    file_bytes = {f.filename: f.path.read_bytes() for f in files}
    total_mb = sum(f.size for f in files) / (1024 * 1024)
    _log(f'Sample ready: {len(files)} files, {total_mb:.1f} MB total.')

    if args.dry_run:
        for f in files[:10]:
            _log(f'  {f.filename}  {f.size / 1024:.0f} KB  {f.content_type}  sha256={f.sha256[:12]}...')
        if len(files) > 10:
            _log(f'  ... and {len(files) - 10} more')
        _log(f'Would sweep concurrency levels {levels} in batches of {args.batch_size} against {args.base_url or "(no --base-url given)"}.')
        return 0

    if not args.base_url:
        print('ERROR: --base-url or UPLOAD_LOADTEST_BASE_URL is required for a real run', file=sys.stderr)
        return 2

    token = os.getenv('UPLOAD_LOADTEST_TOKEN', '')
    if not token:
        email = os.getenv('UPLOAD_LOADTEST_EMAIL', '')
        password = os.getenv('UPLOAD_LOADTEST_PASSWORD', '')
        if not email or not password:
            print(
                'ERROR: set UPLOAD_LOADTEST_TOKEN, or UPLOAD_LOADTEST_EMAIL + UPLOAD_LOADTEST_PASSWORD, in the environment.',
                file=sys.stderr,
            )
            return 2
        _log(f'Logging in as {email}...')
        token = _login(args.base_url, email, password, args.timeout)

    session = requests.Session()
    session.headers['Authorization'] = f'Bearer {token}'

    all_summaries = []
    for level in levels:
        _log(f'=== concurrency={level}: starting ({len(files)} files, batch_size={args.batch_size}) ===')
        window_start = datetime.now(timezone.utc).isoformat(timespec='seconds')
        batches = [files[i:i + args.batch_size] for i in range(0, len(files), args.batch_size)]
        wall_start = time.perf_counter()
        batch_results: List[BatchResult] = []
        with ThreadPoolExecutor(max_workers=level) as executor:
            futures = [
                executor.submit(_run_batch, session, args.base_url, batch, file_bytes, args.batch_size,
                                 not args.no_thumbnail_put, args.timeout)
                for batch in batches
            ]
            for future in as_completed(futures):
                batch_results.extend(future.result())
        wall_s = time.perf_counter() - wall_start
        window_end = datetime.now(timezone.utc).isoformat(timespec='seconds')

        summary = _summarize(level, wall_s, batch_results)
        all_summaries.append(summary)
        _log(f'=== concurrency={level}: done in {wall_s:.1f}s. Server-log window: {window_start} .. {window_end} (UTC) ===')
        for r in batch_results:
            for err in r.errors:
                _log(f'  [error] {err}')

    print()
    print('=' * 100)
    print('CONCURRENCY CURVE')
    print('=' * 100)
    header = f'{"C":>4} {"files":>6} {"wall_s":>8} {"files/s":>8} {"init p50":>9} {"init p90":>9} {"final p50":>10} {"final p90":>10} {"errors":>7}'
    print(header)
    for s in all_summaries:
        print(
            f'{s["concurrency"]:>4} {s["files"]:>6} {s["wall_s"]:>8} {s["throughput_files_per_s"]:>8} '
            f'{s["init_p50_ms"]:>9} {s["init_p90_ms"]:>9} {s["finalize_p50_ms"]:>10} {s["finalize_p90_ms"]:>10} {s["errors"]:>7}'
        )
    print()
    print('Cross-reference the printed server-log windows above against this level\'s')
    print('"init-batch timings" / "finalize-batch timings" log lines (az containerapp')
    print('logs show, NOT --follow) to see which phase grew, not just that latency did.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
