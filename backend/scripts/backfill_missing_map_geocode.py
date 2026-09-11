#!/usr/bin/env python3
"""One-time backfill for photos stuck with GPS coords but no city/country.

_apply_server_exif_fallback (storage_utils.py) is the server-side reactive
exiftool re-extraction path -- it kicks in when the browser's own client-side
EXIF/GPS parser reports the exif step as unsupported/failed/timeout, which is
common for CR3/RAW files (their ISO-BMFF-style container trips up the
bespoke browser parser far more than the real exiftool binary the server
uses). A bug in that branch marked map_detection 'done' immediately after
finding lat/lon, without ever calling the reverse geocoder -- so latitude/
longitude reached the UI but locationCity/locationCountry/address never did.
Because map_detection was already 'done', no later retry (browser's own
attempt, or ipworker's independently-queued map_detection step) could ever
fill it in; _step_locked_done treats 'done' as terminal regardless of whether
it actually did the geocode work.

The code path is now fixed (storage_utils.py's _apply_server_exif_fallback
calls _reverse_geocode_fallback before marking the step done, matching its
sibling in _apply_client_processing_results). This script repairs photos that
already got stuck in the broken state before that fix shipped: any row with
latitude/longitude set but both locationCity and locationCountry empty gets
reverse-geocoded now using the same offline GeoNames lookup (maps_utils.py) --
free, no rate limit, no network call.

Idempotent -- safe to re-run (only touches rows still missing city/country;
already-repaired rows are skipped on subsequent runs).

Auth: DefaultAzureCredential (uses your `az login`). Needs Storage Table Data
Contributor (read+write on photometadata).

Usage:
  STORAGE_ACCOUNT_NAME=ownphotostoreywgttvrae27 \
    python scripts/backfill_missing_map_geocode.py            # dry run, reports counts only
  STORAGE_ACCOUNT_NAME=ownphotostoreywgttvrae27 \
    python scripts/backfill_missing_map_geocode.py --apply    # actually writes
"""
import argparse
import json
import os
import sys

from azure.data.tables import TableServiceClient
from azure.identity import DefaultAzureCredential

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import maps_utils
from search_utils import build_semantic_layers, build_semantic_text


def _coerce_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _json_compact(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--metadata-table", default=os.getenv("METADATA_TABLE", "photometadata"))
    parser.add_argument("--apply", action="store_true", help="Actually write. Without this flag, only counts are reported.")
    args = parser.parse_args()

    account_name = os.getenv("STORAGE_ACCOUNT_NAME")
    if not account_name:
        print("ERROR: set STORAGE_ACCOUNT_NAME", file=sys.stderr)
        return 2

    credential = DefaultAzureCredential()
    svc = TableServiceClient(endpoint=f"https://{account_name}.table.core.windows.net", credential=credential)
    metadata_table = svc.get_table_client(args.metadata_table)

    maps_utils.prewarm_offline_geocoder()

    scanned = 0
    candidates = 0
    repaired = 0
    geocode_empty = 0

    print(f"Scanning '{args.metadata_table}' for rows with GPS but no city/country...")
    for row in metadata_table.list_entities(select=[
        "PartitionKey", "RowKey", "latitude", "longitude",
        "locationCity", "locationCountry", "address",
        "deleted", "map_detection_status", "processing_metadata",
    ]):
        scanned += 1
        if scanned % 1000 == 0:
            print(f"  ...{scanned} rows scanned, {candidates} candidates, {repaired} repaired so far")

        if _coerce_bool(row.get("deleted")):
            continue

        lat = str(row.get("latitude") or "").strip()
        lon = str(row.get("longitude") or "").strip()
        if not lat or not lon:
            continue
        if str(row.get("locationCity") or "").strip() or str(row.get("locationCountry") or "").strip():
            continue

        candidates += 1
        library_id = str(row.get("PartitionKey") or "")
        filename = str(row.get("RowKey") or "")
        print(f"  candidate: {library_id}/{filename}  ({lat}, {lon})")

        place = maps_utils.reverse_geocode(lat, lon)
        if not place or not (place.get("city") or place.get("country")):
            geocode_empty += 1
            print(f"    -> offline geocoder returned nothing usable, skipping")
            continue

        repaired += 1
        if not args.apply:
            print(f"    -> would set city={place.get('city')!r} country={place.get('country')!r}")
            continue

        entity = metadata_table.get_entity(partition_key=library_id, row_key=filename)
        entity["locationCity"] = place.get("city", "")
        entity["locationCountry"] = place.get("country", "")
        if place.get("address"):
            entity["address"] = place["address"]
        entity["semanticText"] = build_semantic_text(filename, entity)
        entity["semanticLayers"] = _json_compact(build_semantic_layers(filename, entity))

        processing = json.loads(entity.get("processing_metadata") or "{}")
        map_result = processing.get("map_detection") if isinstance(processing.get("map_detection"), dict) else {}
        map_result.update({"source": "server", "latitude": lat, "longitude": lon, "backfilled": True})
        processing["map_detection"] = map_result
        entity["processing_metadata"] = _json_compact(processing)

        metadata_table.upsert_entity(entity)
        print(f"    -> set city={place.get('city')!r} country={place.get('country')!r}")

    print()
    print(f"Scanned:                  {scanned}")
    print(f"Candidates (GPS, no loc): {candidates}")
    print(f"Repaired:                 {repaired}{'' if args.apply else '  (dry run -- nothing written, re-run with --apply)'}")
    print(f"Geocoder returned empty:  {geocode_empty}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
