#!/usr/bin/env python3
"""One-time backfill for the public-album-share-token index.

_find_public_album_by_token() (app.py) used to run an unscoped
`publicToken eq '...'` scan of every account's albums on every public share
view; it now does an O(1) point read against a small index table instead:

  * ALBUM_TOKEN_INDEX_TABLE (default 'photoalbumtokens')  PartitionKey=publicToken, RowKey='owner' -> (userId, albumId)

New/changed share links keep the index current automatically (share_album,
revoke_album_share, delete_album). This script backfills entries for albums
that were already publicly shared *before* that change shipped, so existing
share links keep resolving via the fast path immediately instead of falling
back to the (still-present but slower) scan.

Note: _find_public_album_by_token also self-heals lazily -- the first time a
pre-existing share link is visited post-deploy, it falls back to the scan and
populates the index itself. This script just avoids paying that first-hit cost
per link.

Idempotent -- safe to re-run (every write is an upsert).

Auth: DefaultAzureCredential (uses your `az login`). Needs Storage Table Data
Contributor (read to scan photoalbums, write to the index table).

Usage:
  STORAGE_ACCOUNT_NAME=ownphotostoreywgttvrae27 \
    python scripts/backfill_album_token_index.py            # dry run, reports counts only
  STORAGE_ACCOUNT_NAME=ownphotostoreywgttvrae27 \
    python scripts/backfill_album_token_index.py --apply    # actually writes
"""
import argparse
import os
import sys

from azure.data.tables import TableServiceClient
from azure.identity import DefaultAzureCredential


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--albums-table", default=os.getenv("ALBUMS_TABLE", "photoalbums"))
    parser.add_argument("--album-token-index-table", default=os.getenv("ALBUM_TOKEN_INDEX_TABLE", "photoalbumtokens"))
    parser.add_argument("--apply", action="store_true", help="Actually write. Without this flag, only counts are reported.")
    args = parser.parse_args()

    account_name = os.getenv("STORAGE_ACCOUNT_NAME")
    if not account_name:
        print("ERROR: set STORAGE_ACCOUNT_NAME", file=sys.stderr)
        return 2

    credential = DefaultAzureCredential()
    svc = TableServiceClient(endpoint=f"https://{account_name}.table.core.windows.net", credential=credential)

    albums_table = svc.get_table_client(args.albums_table)
    album_token_index_table = svc.get_table_client(args.album_token_index_table)

    if args.apply:
        svc.create_table_if_not_exists(table_name=args.album_token_index_table)

    scanned = 0
    indexed = 0
    skipped_no_token = 0

    print(f"Scanning '{args.albums_table}' (single one-time full scan; live traffic no longer does this)...")
    for row in albums_table.list_entities(select=["PartitionKey", "RowKey", "publicToken"]):
        scanned += 1
        if scanned % 1000 == 0:
            print(f"  ...{scanned} rows scanned, {indexed} indexed so far")

        user_id = str(row.get("PartitionKey") or "")
        album_id = str(row.get("RowKey") or "")
        token = str(row.get("publicToken") or "")
        if not user_id or not album_id or not token:
            skipped_no_token += 1
            continue

        indexed += 1
        if args.apply:
            album_token_index_table.upsert_entity({
                "PartitionKey": token,
                "RowKey": "owner",
                "userId": user_id,
                "albumId": album_id,
            })

    print()
    print(f"Scanned:                {scanned}")
    print(f"Indexed:                 {indexed}{'' if args.apply else '  (dry run -- nothing written, re-run with --apply)'}")
    print(f"Skipped (not shared):    {skipped_no_token}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
