#!/usr/bin/env python3
"""One-time backfill for the upload-dedup/filename-collision indexes.

detect_duplicates() and _resolve_filename_for_upload() (storage_utils.py) used
to run a per-partition or whole-table scan on EVERY /upload/finalize call; that
scan got slower as a library grew, so a big upload batch started fast and
dragged more with every subsequent photo. They now do an O(1) point lookup
against two small index tables instead:

  * HASH_INDEX_TABLE      (default 'photofilehashes')    PartitionKey=library_id, RowKey=fileHash    -> filename
  * FILENAME_OWNERS_TABLE (default 'photofilenameowners') PartitionKey=filename,   RowKey=library_id  -> fileHash

New uploads keep both indexes current automatically (finalize_uploaded_file
writes to them). This script backfills entries for photos that were already
uploaded *before* that change shipped, so dedup/collision detection covers the
existing library immediately instead of only catching duplicates of photos
uploaded after the deploy.

Idempotent -- safe to re-run (every write is an upsert).

Auth: DefaultAzureCredential (uses your `az login`). Needs Storage Table Data
Contributor (read to scan photometadata, write to the two index tables).

Usage:
  STORAGE_ACCOUNT_NAME=ownphotostoreywgttvrae27 \
    python scripts/backfill_dedup_indexes.py            # dry run, reports counts only
  STORAGE_ACCOUNT_NAME=ownphotostoreywgttvrae27 \
    python scripts/backfill_dedup_indexes.py --apply    # actually writes
"""
import argparse
import os
import sys

from azure.data.tables import TableServiceClient
from azure.identity import DefaultAzureCredential


def _coerce_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--metadata-table", default=os.getenv("METADATA_TABLE", "photometadata"))
    parser.add_argument("--hash-index-table", default=os.getenv("HASH_INDEX_TABLE", "photofilehashes"))
    parser.add_argument("--filename-owners-table", default=os.getenv("FILENAME_OWNERS_TABLE", "photofilenameowners"))
    parser.add_argument("--apply", action="store_true", help="Actually write. Without this flag, only counts are reported.")
    args = parser.parse_args()

    account_name = os.getenv("STORAGE_ACCOUNT_NAME")
    if not account_name:
        print("ERROR: set STORAGE_ACCOUNT_NAME", file=sys.stderr)
        return 2

    credential = DefaultAzureCredential()
    svc = TableServiceClient(endpoint=f"https://{account_name}.table.core.windows.net", credential=credential)

    metadata_table = svc.get_table_client(args.metadata_table)
    hash_index_table = svc.get_table_client(args.hash_index_table)
    filename_owners_table = svc.get_table_client(args.filename_owners_table)

    if args.apply:
        svc.create_table_if_not_exists(table_name=args.hash_index_table)
        svc.create_table_if_not_exists(table_name=args.filename_owners_table)

    scanned = 0
    indexed = 0
    skipped_no_hash = 0
    skipped_deleted = 0

    print(f"Scanning '{args.metadata_table}' (single one-time full scan; live traffic no longer does this)...")
    for row in metadata_table.list_entities(select=["PartitionKey", "RowKey", "fileHash", "deleted"]):
        scanned += 1
        if scanned % 1000 == 0:
            print(f"  ...{scanned} rows scanned, {indexed} indexed so far")

        if _coerce_bool(row.get("deleted")):
            skipped_deleted += 1
            continue

        library_id = str(row.get("PartitionKey") or "")
        filename = str(row.get("RowKey") or "")
        file_hash = str(row.get("fileHash") or "")
        if not library_id or not filename or not file_hash:
            skipped_no_hash += 1
            continue

        indexed += 1
        if args.apply:
            hash_index_table.upsert_entity({
                "PartitionKey": library_id,
                "RowKey": file_hash,
                "filename": filename,
            })
            filename_owners_table.upsert_entity({
                "PartitionKey": filename,
                "RowKey": library_id,
                "fileHash": file_hash,
            })

    print()
    print(f"Scanned:              {scanned}")
    print(f"Indexed:               {indexed}{'' if args.apply else '  (dry run -- nothing written, re-run with --apply)'}")
    print(f"Skipped (no fileHash): {skipped_no_hash}  (uploads still in flight / never completed finalize)")
    print(f"Skipped (deleted):     {skipped_deleted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
