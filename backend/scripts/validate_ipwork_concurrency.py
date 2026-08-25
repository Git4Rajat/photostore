#!/usr/bin/env python3
"""Tier 2 differential validation for the ipworker intra-replica
concurrency change: proves that running the face (YOLO + MediaPipe +
AdaFace) and CLIP vision pipelines concurrently across several real photos
produces the SAME output as running them one at a time -- i.e. that
_FACE_LANDMARKER_LOCK (ipwork_face.py) and _MODEL_LOCK (vision_utils.py)
actually prevent the cross-thread corruption they're there to prevent.
Required before raising IPWORKER_CONCURRENCY above 1 anywhere, per this
project's practice of never shipping a face-pipeline change on plausibility
alone (see validate-ml-changes-with-real-embeddings).

What it does (read-only against blobs/tables; writes nothing):
  1. Pulls a sample of a user's real photos from Table Storage.
  2. Downloads each photo's bytes once (shared between both passes below,
     so any difference in output is attributable to concurrency, not to
     re-downloading a different rendition of the file).
  3. Runs ipwork_face.process_face + vision_utils.encode_image_embedding on
     every photo SERIALLY (one at a time, main thread) -- the baseline.
  4. Runs the identical work again through a real ThreadPoolExecutor at
     --concurrency workers -- genuinely concurrent, not simulated.
  5. Diffs face embeddings (matched serial-to-concurrent by bbox IoU, since
     multi-face detection order isn't guaranteed stable) and CLIP image
     embeddings per photo. Unlike calibrate_ipworker_face_tier.py (which
     compares two DIFFERENT pipelines and expects genuine-but-imperfect
     agreement), this compares the SAME deterministic pipeline run twice,
     so the bar is near-exact match (cosine > 0.9999), not "reasonably
     similar" -- anything short of that means a lock is scoped wrong or
     two threads' inputs got crossed.
  6. Separately reports (informationally only -- this script never writes
     to the person/face tables) whether any sampled photos' *existing*
     browser-computed faces already share a person, since that's the
     scenario the person-entity race fix (_update_person_entity_with_retry,
     _remove_face_from_person_with_retry) protects -- that fix's actual
     correctness proof is the real-thread unit tests in
     tests/test_person_entity_concurrency.py, not this script; this script
     only proves the model-inference side stays clean under concurrency.

Requires the SAME heavy deps as the ipworker image itself (onnxruntime,
opencv, mediapipe, torch, open_clip) -- not something to run in the plain
backend venv. Run either inside the built ipworker image, or in a venv with
requirements.txt + requirements-ipworker.txt installed.

Auth: DefaultAzureCredential (uses your `az login`), same as
calibrate_ipworker_face_tier.py. Needs Storage Table/Blob Data Reader.

Usage:
  STORAGE_ACCOUNT_NAME=<account> \\
    python scripts/validate_ipwork_concurrency.py owner --limit 24 --concurrency 2
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Tuple

try:
    import numpy as np
except Exception:  # pragma: no cover
    print('ERROR: numpy is required (pip install numpy)', file=sys.stderr)
    raise SystemExit(2)

from azure.data.tables import TableServiceClient
from azure.storage.blob import BlobServiceClient
from azure.identity import DefaultAzureCredential

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ipwork_face  # noqa: E402
import vision_utils  # noqa: E402


def _bbox(face: Dict) -> Optional[Dict[str, float]]:
    raw = face.get('bbox')
    if not isinstance(raw, dict):
        return None
    try:
        return {k: float(raw.get(k, 0) or 0) for k in ('left', 'top', 'width', 'height')}
    except Exception:
        return None


def _iou(a: Dict[str, float], b: Dict[str, float]) -> float:
    ax2, ay2 = a['left'] + a['width'], a['top'] + a['height']
    bx2, by2 = b['left'] + b['width'], b['top'] + b['height']
    ix1, iy1 = max(a['left'], b['left']), max(a['top'], b['top'])
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    union = a['width'] * a['height'] + b['width'] * b['height'] - inter
    return inter / union if union > 0 else 0.0


def _cosine(a: List[float], b: List[float]) -> Optional[float]:
    if not a or not b or len(a) != len(b):
        return None
    va, vb = np.asarray(a, dtype=np.float64), np.asarray(b, dtype=np.float64)
    na, nb = np.linalg.norm(va), np.linalg.norm(vb)
    if na <= 0 or nb <= 0:
        return None
    return float(np.dot(va, vb) / (na * nb))


def _process_photo(user_id: str, filename: str, image_bytes: bytes) -> Dict:
    face_result = ipwork_face.process_face(user_id, filename, image_bytes)
    faces = face_result.get('faces') or [] if isinstance(face_result, dict) else []
    clip_embedding = vision_utils.encode_image_embedding(image_bytes)
    return {'faces': faces, 'clip_embedding': clip_embedding}


def _match_faces(serial_faces: List[Dict], concurrent_faces: List[Dict], min_iou: float) -> List[Tuple[Dict, Optional[Dict], float]]:
    """Pairs each serial-run face with its best-IoU concurrent-run
    counterpart (detection order across two separate runs isn't guaranteed
    stable when a photo has multiple faces)."""
    used = set()
    pairs = []
    for sface in serial_faces:
        sbbox = _bbox(sface)
        best_iou, best_cface, best_idx = 0.0, None, None
        if sbbox is not None:
            for idx, cface in enumerate(concurrent_faces):
                if idx in used:
                    continue
                cbbox = _bbox(cface)
                if cbbox is None:
                    continue
                iou = _iou(sbbox, cbbox)
                if iou > best_iou:
                    best_iou, best_cface, best_idx = iou, cface, idx
        if best_cface is not None and best_iou >= min_iou:
            used.add(best_idx)
        pairs.append((sface, best_cface if best_iou >= min_iou else None, best_iou))
    return pairs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('user_id', help="library / user partition key (single-owner deploys use 'owner')")
    ap.add_argument('--account', default=os.getenv('STORAGE_ACCOUNT_NAME', ''), help='storage account name')
    ap.add_argument('--face-table', default=os.getenv('FACE_TABLE', 'photofaces'))
    ap.add_argument('--metadata-table', default=os.getenv('METADATA_TABLE', 'photometadata'))
    ap.add_argument('--image-container', default=os.getenv('BLOB_IMAGE_CONTAINER', 'images'))
    ap.add_argument('--limit', type=int, default=24, help='max photos to sample (each costs 2x a full inference pass); ignored if --filenames is set')
    ap.add_argument('--filenames', default='', help='comma-separated exact filenames to validate instead of sampling the first --limit metadata rows -- use this to target known multi-face photos (cross-face state corruption is more likely to show up there than in single-face photos)')
    ap.add_argument('--concurrency', type=int, default=int(os.getenv('IPWORKER_CONCURRENCY', '2')), help='worker count for the concurrent pass')
    ap.add_argument('--min-iou', type=float, default=0.5, help='min IoU to consider a serial/concurrent detection the same face')
    args = ap.parse_args()

    if not args.account:
        print('ERROR: pass --account or set STORAGE_ACCOUNT_NAME', file=sys.stderr)
        return 2
    if args.concurrency < 2:
        print('ERROR: --concurrency must be >= 2 (this validates concurrent execution)', file=sys.stderr)
        return 2

    cred = DefaultAzureCredential()
    table_svc = TableServiceClient(endpoint=f'https://{args.account}.table.core.windows.net', credential=cred)
    blob_svc = BlobServiceClient(account_url=f'https://{args.account}.blob.core.windows.net', credential=cred)
    metadata_table = table_svc.get_table_client(args.metadata_table)
    face_table = table_svc.get_table_client(args.face_table)
    container = blob_svc.get_container_client(args.image_container)

    pk = args.user_id.replace("'", "''")
    requested_filenames = [f.strip() for f in args.filenames.split(',') if f.strip()]
    if requested_filenames:
        metadata_rows = []
        for filename in requested_filenames:
            try:
                metadata_rows.append(metadata_table.get_entity(partition_key=args.user_id, row_key=filename))
            except Exception as exc:
                print(f'  [skip] {filename}: metadata row not found ({exc})')
        print(f"Targeting {len(metadata_rows)}/{len(requested_filenames)} requested photos for user '{args.user_id}'.")
    else:
        metadata_rows = list(metadata_table.query_entities(
            f"PartitionKey eq '{pk}'", select=['RowKey', 'anonymousImageId'],
        ))[: args.limit]
        print(f"Found {len(metadata_rows)} photos to sample for user '{args.user_id}'.")
    if not metadata_rows:
        print('Nothing to validate. Wrong account/user, or this library has no photos yet.')
        return 1

    print('Downloading photo bytes (once each, shared between both passes)...')
    photos: Dict[str, bytes] = {}
    for row in metadata_rows:
        filename = str(row.get('RowKey') or '')
        physical_name = str(row.get('anonymousImageId') or '').strip() or filename
        if not filename:
            continue
        try:
            photos[filename] = container.download_blob(physical_name).readall()
        except Exception as exc:
            print(f'  [skip] {filename}: blob download failed ({exc})')
    if not photos:
        print('No photo bytes downloaded -- nothing to validate.')
        return 1
    print(f'Downloaded {len(photos)} photos.\n')

    # Trigger the same lazy-init path run_ipworker() takes before starting
    # its thread pool -- idempotent if these already warmed during import.
    try:
        import app
        app._prewarm_ipwork_models()
    except Exception as exc:
        print(f'  [warn] _prewarm_ipwork_models failed, continuing anyway: {exc}')

    print(f'Pass 1/2: serial baseline ({len(photos)} photos, one at a time)...')
    serial_results: Dict[str, Dict] = {}
    for filename, image_bytes in photos.items():
        try:
            serial_results[filename] = _process_photo(args.user_id, filename, image_bytes)
        except Exception as exc:
            print(f'  [error] {filename}: serial pass raised {exc}')

    print(f'Pass 2/2: concurrent run ({len(photos)} photos, {args.concurrency} workers)...')
    concurrent_results: Dict[str, Dict] = {}
    concurrent_errors: List[Tuple[str, str]] = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = {
            executor.submit(_process_photo, args.user_id, filename, image_bytes): filename
            for filename, image_bytes in photos.items()
        }
        for future, filename in futures.items():
            try:
                concurrent_results[filename] = future.result()
            except Exception as exc:
                concurrent_errors.append((filename, str(exc)))
    print()

    if concurrent_errors:
        print(f'CRASHES under concurrency ({len(concurrent_errors)}) -- these did NOT happen serially:')
        for filename, err in concurrent_errors:
            print(f'  [crash] {filename}: {err}')
        print()

    face_sims: List[float] = []
    face_mismatches: List[str] = []
    clip_sims: List[float] = []
    clip_mismatches: List[str] = []
    for filename in photos:
        serial = serial_results.get(filename)
        concurrent = concurrent_results.get(filename)
        if serial is None or concurrent is None:
            continue

        sfaces, cfaces = serial['faces'], concurrent['faces']
        if len(sfaces) != len(cfaces):
            face_mismatches.append(f'{filename}: serial found {len(sfaces)} face(s), concurrent found {len(cfaces)}')
        for sface, cface, iou in _match_faces(sfaces, cfaces, args.min_iou):
            if cface is None:
                face_mismatches.append(f'{filename}: a serial-pass face had no concurrent-pass match (best IoU={iou:.2f})')
                continue
            sim = _cosine(sface.get('embedding') or [], cface.get('embedding') or [])
            if sim is None:
                face_mismatches.append(f'{filename}: could not compute cosine similarity for a matched face')
                continue
            face_sims.append(sim)
            if sim < 0.9999:
                face_mismatches.append(f'{filename}: matched face cosine similarity={sim:.6f} (expected ~1.0)')

        sim = _cosine(serial.get('clip_embedding') or [], concurrent.get('clip_embedding') or [])
        if sim is not None:
            clip_sims.append(sim)
            if sim < 0.9999:
                clip_mismatches.append(f'{filename}: CLIP cosine similarity={sim:.6f} (expected ~1.0)')

    print('=' * 78)
    print('RESULTS')
    print('=' * 78)
    print(f'Photos sampled: {len(photos)}')
    print(f'Concurrent-pass crashes: {len(concurrent_errors)}')
    if face_sims:
        arr = np.asarray(face_sims, dtype=np.float64)
        print(f'Matched faces: {len(face_sims)}  min={arr.min():.6f}  median={np.median(arr):.6f}')
    else:
        print('Matched faces: 0 (no faces detected in this sample, or none matched)')
    if clip_sims:
        arr = np.asarray(clip_sims, dtype=np.float64)
        print(f'CLIP embeddings compared: {len(clip_sims)}  min={arr.min():.6f}  median={np.median(arr):.6f}')
    print()

    # Informational only -- proves nothing on its own and writes nothing;
    # the actual concurrency-safety proof for shared persons is the
    # real-thread unit tests in tests/test_person_entity_concurrency.py.
    try:
        face_rows = list(face_table.query_entities(f"PartitionKey eq '{pk}'", select=['filename', 'personId']))
        person_to_files: Dict[str, set] = {}
        for row in face_rows:
            filename = str(row.get('filename') or '')
            person_id = str(row.get('personId') or '')
            if filename in photos and person_id:
                person_to_files.setdefault(person_id, set()).add(filename)
        shared = {pid: files for pid, files in person_to_files.items() if len(files) > 1}
        if shared:
            print(f'Note: {len(shared)} existing person(s) already have >1 sampled photo assigned')
            print('(this is the scenario _update_person_entity_with_retry protects; see')
            print('tests/test_person_entity_concurrency.py for the actual write-safety proof).')
        else:
            print('Note: no sampled photos currently share an existing person -- the')
            print('person-entity race fix is validated separately by the unit tests, not this run.')
    except Exception:
        pass

    print()
    print('=' * 78)
    print('VERDICT')
    print('=' * 78)
    ok = not concurrent_errors and not face_mismatches and not clip_mismatches
    if ok:
        print('PASS: concurrent execution matched the serial baseline for every sampled')
        print(f'photo at concurrency={args.concurrency}. Safe to proceed with the benchmark rollout.')
        return 0

    print('FAIL: concurrent execution diverged from the serial baseline.')
    for line in face_mismatches:
        print(f'  [face mismatch] {line}')
    for line in clip_mismatches:
        print(f'  [clip mismatch] {line}')
    print()
    print('Do NOT raise IPWORKER_CONCURRENCY in production until this is understood --')
    print('a lock is likely scoped around the wrong call, or missing entirely.')
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
