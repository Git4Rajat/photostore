#!/usr/bin/env python3
"""Local synthetic benchmark of production SQLite code; no cloud access.

Run with the backend dependencies installed. Temporary database is removed on
exit. This measures one library, not HTTP or distributed user concurrency.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import gzip
import json
import math
import os
from pathlib import Path
import platform
import resource
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import search_db
import search_utils


def summarize(samples):
    ordered = sorted(samples)
    return {"samples": len(samples), **{
        name: round(ordered[max(0, math.ceil(len(ordered) * p) - 1)] * 1000, 3)
        for name, p in (("p50_ms", .5), ("p95_ms", .95), ("max_ms", 1))}}


def rows(count, workdir):
    start = datetime(2015, 1, 1, tzinfo=timezone.utc)
    subjects = ("beach", "dog", "city", "mountain", "birthday", "forest", "car", "cat", "food", "bird")
    for i in range(count):
        if i % 10000 == 0 and shutil.disk_usage(workdir).free < 600 * 1024**2:
            raise RuntimeError("Stopped to preserve 600 MiB free disk")
        stamp = (start + timedelta(days=(i * 7919) % 3650, seconds=i % 86400)).isoformat()
        yield {"RowKey": f"IMG_{i:09d}.jpg", "uploadDate": stamp,
               "tags": json.dumps([subjects[i % 10], "travel", "outdoor"]),
               "caption": f"A {subjects[i % 10]} on a holiday",
               "rating": i % 6, "likes": int(i % 7 == 0),
               "peopleIds": json.dumps([f"p{i % 2000}"]) if i % 3 == 0 else "[]",
               "locationCity": ("Stockholm", "Paris", "London", "Tokyo")[i % 4]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--photos", type=int, default=1_000_000)
    parser.add_argument("--samples", type=int, default=12)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.photos < 1 or args.samples < 1:
        parser.error("photos and samples must be positive")
    result = {"started_utc": datetime.now(timezone.utc).isoformat(),
              "scope": "one synthetic library, production DatabaseBuilder/SearchDatabase, local disk; no HTTP/Azure/AI",
              "photos": args.photos, "python": sys.version, "platform": platform.platform(),
              "sqlite": sqlite3.sqlite_version, "cpu_count": os.cpu_count(),
              "git_revision": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
              "fixture": "10 subjects; travel/outdoor on every row; four cities; 10 years; ratings 0-5; 1/3 person membership; no OCR, media or embeddings"}
    with tempfile.TemporaryDirectory(prefix="keepsake-scale-") as workdir:
        if shutil.disk_usage(workdir).free < 1500 * 1024**2:
            raise RuntimeError("Need 1500 MiB free for this benchmark")
        path = str(Path(workdir) / "library.sqlite")
        begin = time.perf_counter()
        count = search_db.build_database(rows(args.photos, workdir), path)
        assert count == args.photos
        result["build_seconds"] = round(time.perf_counter() - begin, 3)
        result["database_bytes"] = os.path.getsize(path)
        print(json.dumps({k: result[k] for k in ("photos", "build_seconds", "database_bytes")}), flush=True)
        # Stream gzip to a counting sink so compression needs no second file.
        class Sink:
            count = 0
            def write(self, data):
                self.count += len(data)
                return len(data)
        sink = Sink()
        begin = time.perf_counter()
        with open(path, "rb") as source, gzip.GzipFile(fileobj=sink, mode="wb", compresslevel=6, mtime=0) as target:
            shutil.copyfileobj(source, target)
        result["gzip_bytes"] = sink.count
        result["gzip_seconds"] = round(time.perf_counter() - begin, 3)
        db = search_db.SearchDatabase(path)
        tokens = search_utils.parse_search_query("beach")
        terms = search_db.match_terms(tokens)
        def search():
            candidates = db.candidates(terms)
            scores = [search_utils.lexical_search_score(tokens, name, row, {}) for name, row in candidates]
            assert candidates and max(scores) > 0
            assert db.count_matches(terms) == (args.photos + 9) // 10
        def gallery(**kw):
            names, total = db.list_page(limit=100, **kw)
            assert total > 0 and names
        operations = {
            "gallery_capture_first": lambda: gallery(sort="capture"),
            "gallery_capture_middle": lambda: gallery(sort="capture", offset=args.photos // 2),
            "gallery_capture_tail": lambda: gallery(sort="capture", offset=max(0, args.photos - 100)),
            "gallery_name_first": lambda: gallery(sort="name"),
            "gallery_rating_first": lambda: gallery(sort="rating"),
            "gallery_filtered_first": lambda: gallery(sort="capture", min_rating=4, min_likes=1),
            "search_beach_candidates_score_count": search,
            "search_common_candidates_count": lambda: (db.candidates(["travel"]), db.count_matches(["travel"])),
        }
        result["serial"] = {}
        for name, operation in operations.items():
            begin = time.perf_counter()
            operation()
            first = time.perf_counter() - begin
            samples = []
            for _ in range(args.samples):
                begin = time.perf_counter()
                operation()
                samples.append(time.perf_counter() - begin)
            result["serial"][name] = {"first_call_ms": round(first * 1000, 3), **summarize(samples)}
            print(name, json.dumps(result["serial"][name]), flush=True)
        # Closed-loop pressure on one shared library, not distinct tenants or VUs.
        result["concurrent"] = []
        for workers in (1, 4, 16):
            def task(i):
                begin = time.perf_counter()
                if i % 10 == 0:
                    search()
                else:
                    gallery(sort="capture", offset=(i * 7919) % max(1, args.photos - 100))
                return time.perf_counter() - begin
            begin = time.perf_counter()
            with ThreadPoolExecutor(max_workers=workers) as pool:
                samples = list(pool.map(task, range(80)))
            elapsed = time.perf_counter() - begin
            row = {"threads": workers, "operations": len(samples), "wall_seconds": round(elapsed, 3),
                   "ops_per_second": round(len(samples) / elapsed, 3), **summarize(samples)}
            result["concurrent"].append(row)
            print("concurrent", json.dumps(row), flush=True)
        result["peak_process_rss_bytes"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == "darwin" else 1024)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print("Results:", args.output, flush=True)


if __name__ == "__main__":
    main()
