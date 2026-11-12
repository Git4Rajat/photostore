#!/usr/bin/env python3
"""Backfill the sharp grid-tile image (grid_images.py) for photos uploaded before it existed.

Every photo with a preview gets a ~400 px (short edge) WebP made from that
2048 px preview, never from the original. New uploads get theirs when their
upload wave finishes; this script covers the photos that were already there.

It does not make images itself. A dry run scans each library's metadata
(narrow columns, no blob reads) and reports how many photos still need one.
--apply queues one `grid_images` job per library on the clustering queue; the
clustering worker then works through the library in batches inside Azure,
where reading previews costs no egress, and skips photos that already have a
grid image. Safe to re-run.

Do NOT --apply while photos are uploading to that environment: libraries with
an open upload are refused unless you pass --force. Same action as
POST /api/admin/backfill/grid-images, for operators without an app session.

Auth: DefaultAzureCredential (uses your `az login`). Needs Storage Table Data
Reader on photometadata/photojobs and, for --apply, Storage Queue Data Message
Sender on the clustering queue.

Usage:
  STORAGE_ACCOUNT_NAME=<account> python scripts/backfill_grid_images.py            # dry run, counts only
  STORAGE_ACCOUNT_NAME=<account> python scripts/backfill_grid_images.py --library <id>
  STORAGE_ACCOUNT_NAME=<account> python scripts/backfill_grid_images.py --apply    # queue the work
"""
import argparse
import os
import sys
from collections import Counter

from azure.data.tables import TableServiceClient
from azure.identity import DefaultAzureCredential
from azure.storage.queue import QueueClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import grid_images
from upload_waves import ShadowWaves
from wave_policy import owned

# Typical grid image size (WebP, q72, 400 px short edge), for the storage estimate.
TYPICAL_GRID_BYTES = 30_000


def _escape(value: str) -> str:
    return value.replace("'", "''")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--metadata-table', default=os.getenv('METADATA_TABLE', 'photometadata'))
    parser.add_argument('--jobs-table', default=os.getenv('JOBS_TABLE', 'photojobs'))
    parser.add_argument('--queue', default=os.getenv('CLUSTERING_QUEUE_NAME', 'photostore-clustering'))
    parser.add_argument('--library', action='append', default=[], help='Only this library id (repeatable).')
    parser.add_argument('--apply', action='store_true', help='Queue the work. Without this flag, only counts are reported.')
    parser.add_argument('--force', action='store_true', help='Queue even for a library whose upload looks open.')
    args = parser.parse_args()

    account_name = os.getenv('STORAGE_ACCOUNT_NAME')
    if not account_name:
        print('ERROR: set STORAGE_ACCOUNT_NAME', file=sys.stderr)
        return 2

    credential = DefaultAzureCredential()
    tables = TableServiceClient(endpoint=f'https://{account_name}.table.core.windows.net', credential=credential)
    metadata = tables.get_table_client(args.metadata_table)
    jobs = tables.get_table_client(args.jobs_table)
    queue = QueueClient(f'https://{account_name}.queue.core.windows.net', args.queue, credential=credential)

    query = ' or '.join(f"PartitionKey eq '{_escape(library)}'" for library in args.library)
    rows = metadata.query_entities(query, select=list(grid_images.SOURCE_FIELDS)) if query else \
        metadata.list_entities(select=list(grid_images.SOURCE_FIELDS))
    needed, total = Counter(), Counter()
    for row in rows:
        library = str(row.get('PartitionKey') or '')
        total[library] += 1
        if grid_images.needs_grid(row):
            needed[library] += 1

    queued = refused = 0
    for library in sorted(total):
        line = f'{library}: {needed[library]} of {total[library]} photos need a grid image'
        if args.apply and needed[library]:
            uploading = owned(jobs, library) and bool((ShadowWaves(jobs, library)._get('control') or {}).get('collectingWaveId'))
            if uploading and not args.force:
                refused += 1
                line += ' -- SKIPPED: an upload is open (re-run later, or --force)'
            else:
                queue.send_message(grid_images.job_message(library, reason='script_backfill'))
                queued += 1
                line += ' -- queued'
        print(line)

    remaining = sum(needed.values())
    print(f'\n{remaining} photos need a grid image (~{remaining * TYPICAL_GRID_BYTES / 1e6:.0f} MB at ~{TYPICAL_GRID_BYTES // 1000} KB each).')
    if args.apply:
        print(f'Queued {queued} librar{"y" if queued == 1 else "ies"}; skipped {refused} with open uploads.')
    else:
        print('Dry run: nothing queued. Pass --apply to queue the work.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
