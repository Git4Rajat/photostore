#!/usr/bin/env python3
"""One-time backfill for the keyed filename->face-ids index table.

_live_faiss_metadata_update() (app.py) looks up a photo's face IDs via a
point read on FACE_BY_FILENAME_TABLE (default 'photofacebyfilename',
PartitionKey=user_id, RowKey=filename). That table is only dual-written by
_store_client_face_entities() going forward -- faces stored *before* that
write path existed have no row there at all, so every lookup for them
returns None and the caller falls back to a full-partition
'PartitionKey eq ... and filename eq ...' scan over FACE_TABLE
(default 'photofaces'). Confirmed live on microsvcpoc-dev 2026-10-02: that
fallback scan is the dominant cost of the clustering worker's per-photo
metadata projection (~3.8s avg, up to 9.5s, vs ~20ms for an indexed point
read), capping real new-assignment throughput at ~150-175/hr on a single
replica against a ~100k-row face partition.

This script rebuilds FACE_BY_FILENAME_TABLE from the current contents of
FACE_TABLE so every filename gets a valid indexed row and the fallback
scan stops firing for the existing backlog.

Row validity (must match storage_utils._validated_face_filename_ids exactly,
or _live_faiss_metadata_update's lookup keeps treating the row as absent):
  schemaVersion=1 (int), state='complete', generation=<32-char hex>,
  leaseExpiresAt='', faceIds=<JSON array of distinct nonempty strings>

Writes are ETag-conditional (create if absent, update IfNotModified if
present) and skip any row with an active writer lease -- a concurrent
real upload/clustering write always wins over this backfill; on conflict
we just skip and leave it for the next run. Never clobbers a valid
'complete' row with the same ID set (checked before writing, so re-running
after the live system has since produced a fresh row with different IDs
replaces a *stale* backfill row with the current truth, but a mid-flight
Error-only lease check prevents stepping on an in-progress writer).

Idempotent -- safe to re-run.

Auth: DefaultAzureCredential (uses your `az login`). Needs Storage Table
Data Contributor (read FACE_TABLE, read/write FACE_BY_FILENAME_TABLE).

Usage:
  STORAGE_ACCOUNT_NAME=microsvcpocdevxv52hdxp4j \
    python scripts/backfill_face_by_filename_table.py            # dry run, reports counts only
  STORAGE_ACCOUNT_NAME=microsvcpocdevxv52hdxp4j \
    python scripts/backfill_face_by_filename_table.py --apply    # actually writes
"""
import argparse
import json
import os
import re
import sys
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone

from azure.core.exceptions import ResourceExistsError, ResourceModifiedError, ResourceNotFoundError
from azure.data.tables import TableServiceClient, UpdateMode
from azure.core import MatchConditions
from azure.identity import DefaultAzureCredential

_SCHEMA_VERSION = 1


def _lease_active(entity) -> bool:
    value = entity.get('leaseExpiresAt')
    if not value:
        return False
    try:
        expires = datetime.fromisoformat(value)
        return expires > datetime.now(timezone.utc)
    except (TypeError, ValueError):
        return True


def _is_valid_complete(entity) -> bool:
    """Any already-valid 'complete' row is authoritative and must not be
    touched, even if its IDs differ from our scan -- the live writer's CAS
    protocol is the only thing allowed to supersede it; a backfill race
    (upload/curation mutating this filename between our scan and our write)
    must never let a stale scan clobber fresher live data."""
    if (type(entity.get('schemaVersion')) is not int
            or entity['schemaVersion'] != _SCHEMA_VERSION
            or entity.get('state') != 'complete'
            or not isinstance(entity.get('generation'), str)
            or not re.fullmatch(r'[0-9a-f]{32}', entity['generation'])
            or entity.get('leaseExpiresAt') != ''):
        return False
    try:
        ids = json.loads(entity['faceIds'])
    except (KeyError, TypeError, ValueError):
        return False
    return isinstance(ids, list) and all(isinstance(fid, str) and fid.strip() for fid in ids)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--face-table", default=os.getenv("FACE_TABLE", "photofaces"))
    parser.add_argument("--face-by-filename-table", default=os.getenv("FACE_BY_FILENAME_TABLE", "photofacebyfilename"))
    parser.add_argument("--apply", action="store_true", help="Actually write. Without this flag, only counts are reported.")
    args = parser.parse_args()

    account_name = os.getenv("STORAGE_ACCOUNT_NAME")
    if not account_name:
        print("ERROR: set STORAGE_ACCOUNT_NAME", file=sys.stderr)
        return 2

    credential = DefaultAzureCredential()
    svc = TableServiceClient(endpoint=f"https://{account_name}.table.core.windows.net", credential=credential)

    face_table = svc.get_table_client(args.face_table)
    index_table = svc.get_table_client(args.face_by_filename_table)

    if args.apply:
        svc.create_table_if_not_exists(table_name=args.face_by_filename_table)

    print(f"Scanning '{args.face_table}' (single one-time full scan)...", flush=True)
    by_key = defaultdict(set)
    scanned = 0
    skipped_no_filename = 0
    for row in face_table.list_entities(select=["PartitionKey", "RowKey", "filename"]):
        scanned += 1
        if scanned % 1000 == 0:
            print(f"  ...{scanned} face rows scanned, {len(by_key)} distinct (user, filename) pairs so far", flush=True)
        user_id = str(row.get("PartitionKey") or "")
        face_id = str(row.get("RowKey") or "")
        filename = row.get("filename")
        if not user_id or not face_id or not filename:
            skipped_no_filename += 1
            continue
        by_key[(user_id, str(filename))].add(face_id)

    print(f"Scanned {scanned} face rows -> {len(by_key)} distinct (user, filename) pairs "
          f"({skipped_no_filename} face rows skipped: missing filename)", flush=True)

    already_valid = 0
    active_writer_skipped = 0
    written = 0
    conflict_skipped = 0
    errors = 0

    for i, ((user_id, filename), face_ids) in enumerate(by_key.items(), 1):
        if i % 1000 == 0:
            print(f"  ...{i}/{len(by_key)} filenames processed, {written} written so far", flush=True)

        expected_ids = sorted(face_ids)
        current = None
        read_failed = False
        for attempt in range(3):
            try:
                current = index_table.get_entity(partition_key=user_id, row_key=filename)
                break
            except ResourceNotFoundError:
                current = None
                break
            except Exception as exc:
                if attempt == 2:
                    read_failed = True
                    print(f"  ERROR reading ({user_id}, {filename}) after 3 attempts: {exc}", file=sys.stderr)
                else:
                    time.sleep(2 ** attempt)
        if read_failed:
            errors += 1
            continue

        if current is not None:
            if _lease_active(current):
                active_writer_skipped += 1
                continue
            if _is_valid_complete(current):
                already_valid += 1
                continue

        entity = {
            "PartitionKey": user_id, "RowKey": filename,
            "schemaVersion": _SCHEMA_VERSION, "generation": uuid.uuid4().hex,
            "state": "complete", "faceIds": json.dumps(expected_ids), "leaseExpiresAt": "",
        }

        if not args.apply:
            written += 1
            continue

        try:
            if current is None:
                index_table.create_entity(entity)
            else:
                index_table.update_entity(entity, mode=UpdateMode.REPLACE,
                                           etag=current.metadata['etag'],
                                           match_condition=MatchConditions.IfNotModified)
            written += 1
        except (ResourceExistsError, ResourceModifiedError):
            # A concurrent real writer touched this row between our read and
            # write; it wins. Safe to leave for the next run.
            conflict_skipped += 1
        except Exception as exc:
            errors += 1
            print(f"  ERROR writing ({user_id}, {filename}): {exc}", file=sys.stderr)

    print()
    print(f"Distinct (user, filename) pairs: {len(by_key)}")
    print(f"Already valid, skipped:          {already_valid}")
    print(f"Active writer lease, skipped:     {active_writer_skipped}")
    print(f"Write conflict, skipped:          {conflict_skipped}")
    print(f"Errors:                           {errors}")
    print(f"Written:                          {written}{'' if args.apply else '  (dry run -- nothing written, re-run with --apply)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
