#!/usr/bin/env python3
"""One-time backfill for the per-photo embeddings table.

photoEmbedding/semanticEmbedding used to live as JSON float-array columns on
the photometadata row itself -- large columns read only by the vector-index
rebuild (and a rare cold-start search fallback), never by gallery/lexical
browsing, so keeping them on the display row bloated every full-row read for
no benefit. They now live in their own table:

  * EMBEDDINGS_TABLE (default 'photoembeddings')  PartitionKey=user_id, RowKey=filename
    -> {photoEmbedding, photoEmbeddingVersion, semanticEmbedding, semanticEmbeddingVersion}

New processing writes go straight to the new table (apply_client_processing_results,
_finalize_server_side_exif both call _extract_and_store_embeddings before their
metadata_table_client.upsert_entity). This script copies embeddings for photos
that were already processed *before* that change shipped, so the vector-index
rebuild's embeddings_table_client lookup covers the existing library
immediately instead of falling back to the (still-present) inline columns on
each old row one rebuild at a time.

Does NOT clear the old columns off photometadata -- that's not required for
correctness (the vector-index rebuild and search fallback both still check
the row first), just a future cleanup that's safe to defer or skip.

Idempotent -- safe to re-run (every write is an upsert).

Auth: DefaultAzureCredential (uses your `az login`). Needs Storage Table Data
Contributor (read to scan photometadata, write to the embeddings table).

Usage:
  STORAGE_ACCOUNT_NAME=ownphotostoreywgttvrae27 \
    python scripts/backfill_embeddings_table.py            # dry run, reports counts only
  STORAGE_ACCOUNT_NAME=ownphotostoreywgttvrae27 \
    python scripts/backfill_embeddings_table.py --apply    # actually writes
"""
import argparse
import os
import sys

from azure.data.tables import TableServiceClient
from azure.identity import DefaultAzureCredential

_EMBEDDING_FIELDS = ("photoEmbedding", "photoEmbeddingVersion", "semanticEmbedding", "semanticEmbeddingVersion")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--metadata-table", default=os.getenv("METADATA_TABLE", "photometadata"))
    parser.add_argument("--embeddings-table", default=os.getenv("EMBEDDINGS_TABLE", "photoembeddings"))
    parser.add_argument("--apply", action="store_true", help="Actually write. Without this flag, only counts are reported.")
    args = parser.parse_args()

    account_name = os.getenv("STORAGE_ACCOUNT_NAME")
    if not account_name:
        print("ERROR: set STORAGE_ACCOUNT_NAME", file=sys.stderr)
        return 2

    credential = DefaultAzureCredential()
    svc = TableServiceClient(endpoint=f"https://{account_name}.table.core.windows.net", credential=credential)

    metadata_table = svc.get_table_client(args.metadata_table)
    embeddings_table = svc.get_table_client(args.embeddings_table)

    if args.apply:
        svc.create_table_if_not_exists(table_name=args.embeddings_table)

    scanned = 0
    migrated = 0
    skipped_no_embedding = 0

    select_fields = ["PartitionKey", "RowKey", *_EMBEDDING_FIELDS]
    print(f"Scanning '{args.metadata_table}' (single one-time full scan; live traffic no longer does this)...")
    for row in metadata_table.list_entities(select=select_fields):
        scanned += 1
        if scanned % 1000 == 0:
            print(f"  ...{scanned} rows scanned, {migrated} migrated so far")

        user_id = str(row.get("PartitionKey") or "")
        filename = str(row.get("RowKey") or "")
        values = {field: row.get(field) for field in _EMBEDDING_FIELDS}
        if not user_id or not filename or not any(v is not None for v in values.values()):
            skipped_no_embedding += 1
            continue

        migrated += 1
        if args.apply:
            embeddings_table.upsert_entity({
                "PartitionKey": user_id,
                "RowKey": filename,
                **{k: v for k, v in values.items() if v is not None},
            })

    print()
    print(f"Scanned:                 {scanned}")
    print(f"Migrated:                 {migrated}{'' if args.apply else '  (dry run -- nothing written, re-run with --apply)'}")
    print(f"Skipped (no embedding):   {skipped_no_embedding}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
