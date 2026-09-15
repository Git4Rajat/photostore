#!/usr/bin/env python3
"""Measure whether ipworker's face embeddings (landmark-5pt-mp, MediaPipe-
aligned) are close enough to the browser's (landmark-5pt, face-api-aligned)
to share a clustering tier, or need their own -- the explicit validation gate
called out in ipwork_face.py before PEOPLE_CLUSTER_ALIGNMENT_TIERS (app.py)
is touched. See backend/scripts/calibrate_face_thresholds.py for the sibling
script this is modeled on (that one calibrates eps from same-tier data; this
one calibrates whether two tiers are the same tier).

What it does (read-only against face rows; writes nothing):
  1. Pulls a sample of a user's existing browser-computed 'landmark-5pt' faces
     (the ones that already cluster today) from Table Storage.
  2. Downloads each face's source photo and re-runs ipwork_face.process_face
     on it locally -- the SAME code path ipworker itself runs -- to get a
     freshly computed, independently-aligned embedding for the same face.
  3. Matches ipworker's detection back to the stored face by bbox IoU, then
     reports the cosine similarity between the two pipelines' embeddings for
     that SAME real face, compared against the same-photo (known-different-
     person) similarity ceiling already established by
     calibrate_face_thresholds.py.
  4. Prints an explicit reuse-tier-vs-new-tier recommendation from that data
     -- this script does not decide for you, it produces the numbers the
     decision needs (per this project's practice of never shipping a
     face-pipeline change on plausibility alone).

Requires the SAME heavy deps as the ipworker image itself (onnxruntime,
opencv, mediapipe) -- not something to run in the plain backend venv. Run
either inside the built ipworker image, or in a venv with
requirements.txt + requirements-ipworker.txt installed.

Auth: DefaultAzureCredential (uses your `az login`), same as
calibrate_face_thresholds.py. Needs Storage Table/Blob Data Reader on the
account.

Usage:
  STORAGE_ACCOUNT_NAME=<account> \\
    python scripts/calibrate_ipworker_face_tier.py owner --limit 25
"""
from __future__ import annotations

import argparse
import json
import os
import sys
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


def _embedding(face: Dict) -> List[float]:
    try:
        emb = json.loads(face.get('embedding', '[]') or '[]')
        return emb if isinstance(emb, list) else []
    except Exception:
        return []


def _bbox(face: Dict) -> Optional[Dict[str, float]]:
    try:
        raw = json.loads(face.get('bbox', '{}') or '{}')
    except Exception:
        return None
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


def _pct(arr, ps):
    if len(arr) == 0:
        return {p: float('nan') for p in ps}
    return {p: float(np.percentile(arr, p)) for p in ps}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('user_id', help="library / user partition key (single-owner deploys use 'owner')")
    ap.add_argument('--account', default=os.getenv('STORAGE_ACCOUNT_NAME', ''), help='storage account name')
    ap.add_argument('--face-table', default=os.getenv('FACE_TABLE', 'photofaces'))
    ap.add_argument('--metadata-table', default=os.getenv('METADATA_TABLE', 'photometadata'))
    ap.add_argument('--image-container', default=os.getenv('BLOB_IMAGE_CONTAINER', 'images'))
    ap.add_argument('--source-tier', default='landmark-5pt', help='browser alignment tier to compare against')
    ap.add_argument('--limit', type=int, default=25, help='max faces to sample (each costs a download + full inference pass)')
    ap.add_argument('--min-iou', type=float, default=0.5, help='min IoU to consider ipworker/browser detections the same face')
    args = ap.parse_args()

    if not args.account:
        print('ERROR: pass --account or set STORAGE_ACCOUNT_NAME', file=sys.stderr)
        return 2

    cred = DefaultAzureCredential()
    table_svc = TableServiceClient(endpoint=f'https://{args.account}.table.core.windows.net', credential=cred)
    blob_svc = BlobServiceClient(account_url=f'https://{args.account}.blob.core.windows.net', credential=cred)
    face_table = table_svc.get_table_client(args.face_table)
    metadata_table = table_svc.get_table_client(args.metadata_table)
    container = blob_svc.get_container_client(args.image_container)

    pk = args.user_id.replace("'", "''")
    select = ['RowKey', 'filename', 'bbox', 'embedding', 'alignmentMethod', 'confidence', 'personId']
    rows = [
        r for r in face_table.query_entities(f"PartitionKey eq '{pk}'", select=select)
        if str(r.get('alignmentMethod') or '') == args.source_tier and _embedding(r)
    ]
    print(f"Found {len(rows)} '{args.source_tier}' faces with embeddings for user '{args.user_id}'.")
    if not rows:
        print('Nothing to compare. Wrong account/user, or no browser-computed faces at this tier yet.')
        return 1

    sample = rows[: args.limit]
    print(f'Sampling {len(sample)} faces (each downloads its photo + runs the full ipworker face pipeline).\n')

    same_face_similarities: List[float] = []
    unmatched = 0
    for row in sample:
        filename = str(row.get('filename') or '')
        stored_bbox = _bbox(row)
        stored_embedding = _embedding(row)
        if not filename or not stored_bbox:
            continue
        try:
            metadata = metadata_table.get_entity(partition_key=args.user_id, row_key=filename)
        except Exception:
            print(f'  [skip] {filename}: metadata row not found')
            continue
        physical_name = str(metadata.get('anonymousImageId') or '').strip() or filename
        try:
            image_bytes = container.download_blob(physical_name).readall()
        except Exception as exc:
            print(f'  [skip] {filename}: blob download failed ({exc})')
            continue

        result = ipwork_face.process_face(args.user_id, filename, image_bytes)
        candidates = result.get('faces') or []
        best_iou, best_face = 0.0, None
        for face in candidates:
            iou = _iou(stored_bbox, face['bbox'])
            if iou > best_iou:
                best_iou, best_face = iou, face
        if best_face is None or best_iou < args.min_iou:
            unmatched += 1
            print(f'  [no match] {filename}: ipworker found {len(candidates)} face(s), best IoU={best_iou:.2f}')
            continue

        sim = _cosine(stored_embedding, best_face['embedding'])
        if sim is None:
            continue
        same_face_similarities.append(sim)
        print(f'  {filename}: IoU={best_iou:.2f}  cross-pipeline same-face cosine similarity={sim:+.4f}')

    print()
    print('=' * 78)
    print(f'RESULT: {len(same_face_similarities)} matched faces, {unmatched} unmatched (ipworker detection missed or moved)')
    print('=' * 78)
    if not same_face_similarities:
        print('No matched faces -- cannot calibrate. Check that ipworker detects faces on')
        print('this library at all (bbox/detection mismatch would also explain unmatched=N).')
        return 1

    arr = np.asarray(same_face_similarities, dtype=np.float64)
    ps = [1, 5, 25, 50, 75, 95, 99]
    q = _pct(arr, ps)
    print('Cross-pipeline SAME-FACE cosine similarity percentiles:')
    print('  ' + '  '.join(f'p{p}={q[p]:+.3f}' for p in ps))
    print()

    print('=' * 78)
    print('RECOMMENDATION')
    print('=' * 78)
    median = q[50]
    p5 = q[5]
    if p5 >= 0.5:
        print(f'  p5={p5:+.3f}, median={median:+.3f}: consistently high cross-pipeline agreement.')
        print("  -> Reasonable to REUSE the 'landmark-5pt' tier for 'landmark-5pt-mp' faces:")
        print("     add 'landmark-5pt-mp' to PEOPLE_CLUSTER_ALIGNMENT_TIERS (app.py) and let it")
        print('     share PEOPLE_CLUSTER_EPS -- no separate DBSCAN pass needed. Still worth a small')
        print('     real-world recluster + spot-check before trusting this at full scale.')
    elif median >= 0.4:
        print(f'  p5={p5:+.3f}, median={median:+.3f}: moderate agreement, not clean enough to blindly')
        print("     merge into 'landmark-5pt'. Recommend giving 'landmark-5pt-mp' its OWN tier with")
        print('     its own eps, the same way landmark-2pt got PEOPLE_CLUSTER_EPS_2PT -- run')
        print("     calibrate_face_thresholds.py once enough 'landmark-5pt-mp' faces exist in the")
        print('     library to derive that eps from same-tier data instead of this cross-tier sample.')
    else:
        print(f'  p5={p5:+.3f}, median={median:+.3f}: low/inconsistent agreement -- the two alignment')
        print('     pipelines are not producing comparable embeddings for the same face.')
        print("     Do NOT wire 'landmark-5pt-mp' into clustering yet. Likely causes to check:")
        print('     the MediaPipe landmark index mapping in ipwork_face.py (_RIGHT_EYE_INDICES etc.),')
        print('     or a systematic preprocessing difference (color channel order, crop padding).')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
