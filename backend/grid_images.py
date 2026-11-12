"""Mid-size grid images: the sharp tile the photo grid offers next to the thumbnail.

A grid tile is ~170 CSS px on a phone and ~130 on desktop, so at 2x the 120 px
thumbnail is stretched about 3x. This module makes one more image per photo,
``image_utils.GRID_SHORT_EDGE`` px on the short edge, as WebP, from the photo's
existing 2048 px preview -- never the original, which would cost a full
original download per photo. It is stored at ``grid/<physical>.webp`` in the
thumbnail container (the same media token reads thumbnails and previews) and
recorded on the metadata row as ``gridImage``. The sort index and photo
summaries hand that blob name to the browser, which lists it next to the
thumbnail in ``srcset``. A photo without a record simply keeps its thumbnail.

The image bakes in only EXIF orientation, like the preview; the grid applies
the user's rotation as CSS, as the viewer does. Rotating a photo therefore
never invalidates it: the image depends only on the preview.

Work runs as a ``grid_images`` queue job on the clustering worker, in bounded
batches that re-enqueue their own continuation. Upload-wave completion queues
one for the photos that wave touched; ``POST /api/admin/backfill/grid-images``
and ``scripts/backfill_grid_images.py`` queue one for a whole library. Photos
that already have a grid image are skipped, so every trigger is safe to repeat.
Nothing here talks to Azure or Flask: callers pass the row source and the
per-photo work, so the batching rules are unit-testable.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable, Dict, Iterable, Optional

from catalog_metadata import CATALOG_ADMISSION_FIELDS, is_upload_reservation_only
from image_utils import is_video_file

FIELD = 'gridImage'
PREFIX = 'grid/'
SCHEMA = 1
JOB_TYPE = 'grid_images'

# Read in projected scans; the admission fields keep reservations
# distinguishable from finished uploads (see catalog_metadata).
SOURCE_FIELDS = tuple(sorted({
    'PartitionKey', 'RowKey', 'anonymousImageId', 'processing_state', 'preview_status', 'previewArtifact',
    'processing_lease_owner', 'processing_lease_expires_at', 'last_processing_update', FIELD,
    *CATALOG_ADMISSION_FIELDS,
}))

_SINCE_RE = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z')


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, '') or default))
    except ValueError:
        return default


# Photos made per queue message, and the wall-clock budget for one message
# (scan + work). Kept well inside the clustering worker's renewed visibility
# window; a library larger than one batch continues in the next message.
BATCH_SIZE = _int_env('GRID_IMAGE_BATCH_SIZE', 100)
TIME_BUDGET_SECONDS = _int_env('GRID_IMAGE_TIME_BUDGET_SECONDS', 90)
# Each photo is a preview download, ~20 ms of CPU and an upload.
WORKERS = _int_env('GRID_IMAGE_WORKERS', 4)


def enabled() -> bool:
    return os.getenv('GRID_IMAGES_ENABLED', 'true').strip().lower() in ('1', 'true', 'yes')


def blob_name(physical: str) -> str:
    return f'{PREFIX}{physical}.webp'


def _physical(row: Dict) -> str:
    return str(row.get('anonymousImageId') or '').strip() or str(row.get('RowKey') or '')


def served_blob(row: Optional[Dict]) -> str:
    """The row's grid image blob name, or '' when it has none (or a record that
    doesn't belong to this photo, which is never served)."""
    raw = (row or {}).get(FIELD)
    if not raw:
        return ''
    try:
        record = json.loads(raw)
    except (TypeError, ValueError):
        return ''
    physical = _physical(row)
    if not isinstance(record, dict) or record.get('v') != SCHEMA or not physical:
        return ''
    return record['blob'] if record.get('blob') == blob_name(physical) else ''


def record(blob: str, width: int, height: int, size: int, source: str) -> str:
    return json.dumps({'v': SCHEMA, 'blob': blob, 'w': width, 'h': height, 'bytes': size, 'src': source},
                      sort_keys=True, separators=(',', ':'))


def _lease_live(row: Dict, now: datetime) -> bool:
    if not str(row.get('processing_lease_owner') or '').strip():
        return False
    expires = str(row.get('processing_lease_expires_at') or '').strip()
    try:
        return datetime.fromisoformat(expires.replace('Z', '+00:00')) > now
    except ValueError:
        return False


def needs_grid(row: Dict, *, now: Optional[datetime] = None) -> bool:
    """A finished photo with a preview and no grid image yet.

    Photos a worker or browser holds a live processing lease on are left for a
    later run, so this job doesn't race their metadata writes. Videos have no
    preview and keep their poster-frame thumbnail."""
    filename = str(row.get('RowKey') or '')
    if (not filename or is_video_file(filename) or is_upload_reservation_only(row)
            or str(row.get('processing_state') or '').strip().lower() == 'deleted'
            or str(row.get('preview_status') or '').strip().lower() != 'done'
            or served_blob(row)):
        return False
    return not _lease_live(row, now or datetime.now(timezone.utc))


def stamp_updates(entity: Dict, grid_record: str) -> Dict[str, str]:
    """Fields to merge onto the freshly read row.

    Any metadata write rewrites last_processing_update, which is the upload
    date of last resort for legacy rows that have neither uploadDate nor
    upload_started_at (ordering_utils.metadata_upload_datetime). Pin it first
    so a backfill can't reshuffle "Recently uploaded"."""
    updates = {FIELD: grid_record}
    if not entity.get('uploadDate') and not entity.get('upload_started_at') and entity.get('last_processing_update'):
        updates['uploadDate'] = str(entity['last_processing_update'])
    return updates


def since_from_epoch(seconds) -> str:
    """OData datetime literal body for a row-Timestamp lower bound (5 min of slack)."""
    try:
        value = float(seconds) - 300
    except (TypeError, ValueError):
        return ''
    return datetime.fromtimestamp(max(0.0, value), tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def valid_since(value) -> str:
    return value if isinstance(value, str) and _SINCE_RE.fullmatch(value) else ''


def job_message(library: str, *, cursor: str = '', since: str = '', reason: str = '') -> str:
    job_id = f'grid-images:{uuid.uuid4().hex}'
    return json.dumps({'jobId': job_id, 'correlationId': job_id, 'type': JOB_TYPE, 'user_id': library,
                       'cursor': cursor, 'since': valid_since(since), 'reason': reason}, separators=(',', ':'))


def run_batch(rows: Iterable[Dict], process: Callable[[Dict], bool], *, limit: int = BATCH_SIZE,
              budget_seconds: float = TIME_BUDGET_SECONDS, workers: int = WORKERS,
              clock: Callable[[], float] = time.monotonic, now: Optional[datetime] = None) -> Dict:
    """Make grid images for up to ``limit`` rows that need one, in RowKey order.

    ``rows`` must be in RowKey order. Stops scanning at ``limit`` candidates or
    when the budget runs out, then processes what it collected. ``cursor`` is
    the last RowKey scanned when it stopped early ('' once the scan finished):
    pass it back to continue. A photo that fails is not retried in this pass;
    the next trigger picks it up again."""
    deadline = clock() + budget_seconds
    candidates, scanned, cursor = [], 0, ''
    complete = True
    for row in rows:
        scanned += 1
        cursor = str(row.get('RowKey') or '')
        if needs_grid(row, now=now):
            candidates.append(row)
        if len(candidates) >= limit or clock() >= deadline:
            complete = False
            break

    def attempt(row):
        try:
            return bool(process(row))
        except Exception:
            return False

    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(candidates) or 1))) as pool:
        outcomes = list(pool.map(attempt, candidates))
    made = sum(outcomes)
    return {'scanned': scanned, 'made': made, 'failed': len(outcomes) - made,
            'cursor': '' if complete else cursor}
