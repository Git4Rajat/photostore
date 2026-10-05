#!/usr/bin/env python3
"""Second-step cleanup for the face-embeddings table split: strip the old
inline 'embedding' column off photofaces rows once it's confirmed migrated.

Run this ONLY after, in order:
  1. backfill_face_embeddings_table.py --apply has run and its "Migrated"
     count matches this scan's row count.
  2. The dual-read code (every _face_embedding_from_entity call site that
     needs it merges in FACE_EMBEDDINGS_TABLE via _ensure_face_embedding_present
     / get_face_embeddings_batch / the cluster_user_faces-style bulk join) is
     deployed and confirmed working -- a real clustering pass (recluster,
     incremental-assign, propagate) against live traffic with no regression.
  3. _store_client_face_entities is writing new faces straight to
     FACE_EMBEDDINGS_TABLE (so nothing is re-adding the inline field while
     this runs).

Unlike the photo-embeddings migration (which left its old inline columns in
place indefinitely -- photo rows get touched repeatedly by later pipeline
steps anyway, so they naturally age out), face rows are rarely rewritten
once clustered. Skipping this step would leave the field in place forever
and keep every un-select-projected scan paying its row-bloat cost.

Safety: for each row, re-reads FACE_EMBEDDINGS_TABLE directly and only
strips the inline field if a matching entry is confirmed present there --
never trusts "the backfill ran" on faith. A row with no match in the new
table is left untouched (and counted as "skipped, not yet migrated") rather
than silently losing its only copy of the embedding.

Idempotent -- safe to re-run (rows with no inline 'embedding' left are a
no-op).

Auth: DefaultAzureCredential (uses your `az login`). Needs Storage Table
Data Contributor (read both tables, write back to photofaces).

Usage:
  STORAGE_ACCOUNT_NAME=microsvcpocdevxv52hdxp4j \
    python scripts/strip_face_embeddings_field.py            # dry run, reports counts only
  STORAGE_ACCOUNT_NAME=microsvcpocdevxv52hdxp4j \
    python scripts/strip_face_embeddings_field.py --apply    # actually writes
"""
import argparse
import os
import sys

from azure.data.tables import TableServiceClient, UpdateMode
from azure.identity import DefaultAzureCredential


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--face-table", default=os.getenv("FACE_TABLE", "photofaces"))
    parser.add_argument("--face-embeddings-table", default=os.getenv("FACE_EMBEDDINGS_TABLE", "photofaceembeddings"))
    parser.add_argument("--apply", action="store_true", help="Actually write. Without this flag, only counts are reported.")
    args = parser.parse_args()

    account_name = os.getenv("STORAGE_ACCOUNT_NAME")
    if not account_name:
        print("ERROR: set STORAGE_ACCOUNT_NAME", file=sys.stderr)
        return 2

    credential = DefaultAzureCredential()
    svc = TableServiceClient(endpoint=f"https://{account_name}.table.core.windows.net", credential=credential)

    face_table = svc.get_table_client(args.face_table)
    face_embeddings_table = svc.get_table_client(args.face_embeddings_table)

    scanned = 0
    stripped = 0
    skipped_no_inline_embedding = 0
    skipped_not_yet_migrated = 0

    select_fields = ["PartitionKey", "RowKey", "embedding"]
    print(f"Scanning '{args.face_table}' (single one-time full scan)...")
    for row in face_table.list_entities(select=select_fields):
        scanned += 1
        if scanned % 1000 == 0:
            print(f"  ...{scanned} rows scanned, {stripped} stripped so far")

        user_id = str(row.get("PartitionKey") or "")
        face_id = str(row.get("RowKey") or "")
        if not row.get("embedding"):
            skipped_no_inline_embedding += 1
            continue

        # Safety check: only strip if the new table actually has this face's
        # embedding -- never trust the backfill ran without verifying.
        try:
            face_embeddings_table.get_entity(partition_key=user_id, row_key=face_id)
        except Exception:
            skipped_not_yet_migrated += 1
            continue

        stripped += 1
        if args.apply:
            # MERGE with embedding explicitly set to None removes the property
            # (azure-data-tables drops None-valued fields from a merge) without
            # touching anything else on the row.
            face_table.update_entity(
                {"PartitionKey": user_id, "RowKey": face_id, "embedding": None},
                mode=UpdateMode.MERGE,
            )

    print()
    print(f"Scanned:                      {scanned}")
    print(f"Stripped:                      {stripped}{'' if args.apply else '  (dry run -- nothing written, re-run with --apply)'}")
    print(f"Skipped (no inline embedding): {skipped_no_inline_embedding}")
    print(f"Skipped (not yet migrated):   {skipped_not_yet_migrated}")
    if skipped_not_yet_migrated and not args.apply:
        print()
        print(f"WARNING: {skipped_not_yet_migrated} rows have an inline embedding but no")
        print("matching row in the embeddings table yet. Re-run backfill_face_embeddings_table.py")
        print("--apply first, or these rows will be left untouched (safe, but the row-bloat")
        print("problem persists for them).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
