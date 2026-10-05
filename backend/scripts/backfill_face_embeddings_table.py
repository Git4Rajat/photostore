#!/usr/bin/env python3
"""One-time backfill for the per-face embeddings table.

Face recognition embeddings used to live as a JSON float-array column
(embedding) on the photofaces row itself -- a large column (several KB of
text per row) read only by clustering/matching code, never by People/Faces
page browsing, so keeping it on the display row bloated every
full-partition scan for no benefit. Table Storage has no secondary index
on filename, and every scan's effective page size shrinks well below its
1000-row cap once rows carry this much text -- confirmed live 2026-10-01
on microsvcpoc-dev as the dominant cost behind three separate
uncached-scan bugs, at a 99k+ row face partition. The embedding now lives
in its own table:

  * FACE_EMBEDDINGS_TABLE (default 'photofaceembeddings')
    PartitionKey=user_id, RowKey=face_id
    -> {embedding}

(embeddingVersion/modelTaxonomyVersion deliberately stay on photofaces --
small scalar strings, not the source of row bloat, and read directly off
face rows by clustering version-gating logic.)

New face writes go straight to the new table (_store_client_face_entities
calls _extract_and_store_face_embedding before its own
face_table_client.upsert_entity). This script copies embeddings for faces
that were already stored *before* that change shipped, so clustering code
reading from FACE_EMBEDDINGS_TABLE covers the existing library immediately
instead of falling back to the (still-present) inline field on each old
row one lookup at a time.

Does NOT clear the old 'embedding' field off photofaces -- that is a
separate, deliberate second step (strip_face_embeddings_field.py), run
only after this backfill is verified and the dual-read code is deployed
and confirmed working. Unlike the photo-embeddings migration (which left
its old inline columns in place indefinitely, since photo rows get
touched repeatedly by later pipeline steps anyway), face rows are rarely
rewritten once clustered -- skipping the strip step would leave the field
in place forever.

Idempotent -- safe to re-run (every write is an upsert).

Auth: DefaultAzureCredential (uses your `az login`). Needs Storage Table
Data Contributor (read to scan photofaces, write to the embeddings table).

Usage:
  STORAGE_ACCOUNT_NAME=microsvcpocdevxv52hdxp4j \
    python scripts/backfill_face_embeddings_table.py            # dry run, reports counts only
  STORAGE_ACCOUNT_NAME=microsvcpocdevxv52hdxp4j \
    python scripts/backfill_face_embeddings_table.py --apply    # actually writes
"""
import argparse
import os
import sys

from azure.data.tables import TableServiceClient
from azure.identity import DefaultAzureCredential

_FACE_EMBEDDING_FIELDS = ("embedding",)


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

    if args.apply:
        svc.create_table_if_not_exists(table_name=args.face_embeddings_table)

    scanned = 0
    migrated = 0
    skipped_no_embedding = 0

    select_fields = ["PartitionKey", "RowKey", *_FACE_EMBEDDING_FIELDS]
    print(f"Scanning '{args.face_table}' (single one-time full scan; live traffic no longer does this)...")
    for row in face_table.list_entities(select=select_fields):
        scanned += 1
        if scanned % 1000 == 0:
            print(f"  ...{scanned} rows scanned, {migrated} migrated so far")

        user_id = str(row.get("PartitionKey") or "")
        face_id = str(row.get("RowKey") or "")
        values = {field: row.get(field) for field in _FACE_EMBEDDING_FIELDS}
        if not user_id or not face_id or not any(v is not None for v in values.values()):
            skipped_no_embedding += 1
            continue

        migrated += 1
        if args.apply:
            face_embeddings_table.upsert_entity({
                "PartitionKey": user_id,
                "RowKey": face_id,
                **{k: v for k, v in values.items() if v is not None},
            })

    print()
    print(f"Scanned:                 {scanned}")
    print(f"Migrated:                 {migrated}{'' if args.apply else '  (dry run -- nothing written, re-run with --apply)'}")
    print(f"Skipped (no embedding):   {skipped_no_embedding}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
