"""Offline metadata/disk buffering capacity experiment using synthetic rows.

100k rows are generated, never read from a user's library. Disk timings include
SQLite ingestion and planning reads, not Azure reads, claims, or model inference.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time


def resident_memory_bytes():
    statm = Path('/proc/self/statm')
    if statm.exists():
        return int(statm.read_text().split()[1]) * os.sysconf('SC_PAGE_SIZE')
    import psutil
    return psutil.Process().memory_info().rss


def row(index):
    return dict(photoId=f'{index:032x}', filename=f'photo-{index:08}.jpg',
                processingRevision='benchmark-revision', previewReady=index % 5 != 0,
                exifPacketReady=index % 7 != 0, width=2048, height=1365,
                fileHash=hashlib.sha256(str(index).encode()).hexdigest(),
                steps=['preview', 'thumbnail', 'exif', 'ocr', 'face', 'ai_vision'],
                statuses={name: 'pending' for name in ('ocr', 'face', 'ai_vision')},
                padding='x' * 1024)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--rounds', type=int, default=3)
    parser.add_argument('--case-mode', choices=['memory', 'sqlite'], help=argparse.SUPPRESS)
    parser.add_argument('--case-size', type=int, help=argparse.SUPPRESS)
    parser.add_argument('--case-round', type=int, default=0, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.rounds < 2 and not args.case_mode:
        parser.error('Use at least two rounds')
    if not args.case_mode:
        reports = []
        with tempfile.TemporaryDirectory(prefix='vision-memory-cases-') as directory:
            for round_number in range(args.rounds):
                for size in (20, 1000, 100000):
                    for mode in (('memory', 'sqlite') if round_number % 2 == 0 else ('sqlite', 'memory')):
                        output = Path(directory) / 'case.json'
                        subprocess.run([sys.executable, str(Path(__file__).resolve()), '--output', str(output),
                            '--rounds', '1', '--case-mode', mode, '--case-size', str(size),
                            '--case-round', str(round_number)], check=True, capture_output=True)
                        report = json.loads(output.read_text())['reports'][0]
                        reports.append(report)
                        print(json.dumps(report), flush=True)
                        args.output.write_text(json.dumps(dict(platform=platform.platform(),
                            kind='synthetic metadata, real local disk, isolated process per case', reports=reports), indent=2))
        for size in (20, 1000, 100000):
            if len({report['data_sha256'] for report in reports if report['rows'] == size}) != 1:
                raise RuntimeError('Planning results differ')
        return
    reports = []
    for round_number in range(args.rounds):
        for size in (args.case_size,):
            for mode in (args.case_mode,):
                memory = [resident_memory_bytes()]
                stop = threading.Event()
                def sample():
                    while not stop.wait(.01):
                        memory.append(resident_memory_bytes())
                sampler = threading.Thread(target=sample, daemon=True)
                sampler.start()
                with tempfile.TemporaryDirectory(prefix='vision-metadata-') as directory:
                    started = time.perf_counter()
                    first_ready = None
                    values = []
                    db = None
                    disk_bytes = 0
                    try:
                        if mode == 'sqlite':
                            db = sqlite3.connect(str(Path(directory) / 'wave.sqlite'))
                            db.execute('pragma journal_mode=off')
                            db.execute('pragma synchronous=off')
                            db.execute('pragma cache_size=-4096')
                            db.execute('create table metadata (id integer primary key, payload text)')
                        for start in range(0, size, 1000):
                            batch = [(index, json.dumps(row(index), separators=(',', ':')))
                                     for index in range(start, min(size, start + 1000))]
                            if db:
                                db.executemany('insert into metadata values (?, ?)', batch)
                                db.commit()
                            else:
                                values.extend(batch)
                            if first_ready is None:
                                first_ready = time.perf_counter() - started
                        load_seconds = time.perf_counter() - started
                        if db:
                            disk_bytes = sum(path.stat().st_size for path in Path(directory).iterdir())
                        planning_started = time.perf_counter()
                        original_required = 0
                        fingerprint = hashlib.sha256()
                        for start in range(0, size, 20):
                            batch = list(db.execute('select id,payload from metadata where id >= ? and id < ? order by id',
                                                    (start, start + 20))) if db else values[start:start + 20]
                            for _, payload in batch:
                                metadata = json.loads(payload)
                                original_required += int(not metadata['previewReady'] or not metadata['exifPacketReady'])
                                fingerprint.update(payload.encode())
                        planning_seconds = time.perf_counter() - planning_started
                        reports.append(dict(round=args.case_round, mode=mode, rows=size, disk_bytes=disk_bytes,
                            first_chunk_ready_seconds=first_ready, load_seconds=load_seconds,
                            planning_seconds=planning_seconds, original_required=original_required,
                            data_sha256=fingerprint.hexdigest(), rss_start_bytes=memory[0],
                            rss_peak_bytes=max(memory + [resident_memory_bytes()])))
                    finally:
                        values.clear()
                        if db:
                            db.close()
                        stop.set()
                        sampler.join()
                print(json.dumps(reports[-1]), flush=True)
                args.output.write_text(json.dumps(dict(platform=platform.platform(), kind='synthetic metadata, real local disk',
                                                       reports=reports), indent=2))


if __name__ == '__main__':
    main()
