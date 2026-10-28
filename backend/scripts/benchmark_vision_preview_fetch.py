"""Private benchmark-asset downloads: serial vs bounded RAM/disk prefetch.

Uses a private JSON list of {url, sha256, bytes}. URLs never enter reports.
No production source downloads or model inference are performed here.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import platform
import tempfile
import time
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--rounds', type=int, default=3)
    parser.add_argument('--transport', choices=['sdk', 'urllib'], default='sdk')
    args = parser.parse_args()
    entries = json.loads(args.manifest.read_text())
    limit = 16 * 1024 * 1024
    if args.rounds < 2 or not entries or any(not 0 < entry['bytes'] <= limit for entry in entries):
        parser.error('Use at least two rounds and valid preview assets bounded by 16 MiB')
    if args.transport == 'sdk':
        import requests
        from azure.core.pipeline.transport import RequestsTransport
        from azure.storage.blob import BlobClient
        session = requests.Session()
        session.mount('https://', requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=8))
        transport = RequestsTransport(session=session, session_owner=False)
    reports = []
    for round_number in range(args.rounds):
        modes = ['serial', 'memory4', 'disk4']
        for mode in modes if round_number % 2 == 0 else reversed(modes):
            with tempfile.TemporaryDirectory(prefix='vision-previews-') as directory:
                def download(index):
                    item = entries[index]
                    if args.transport == 'sdk':
                        client = BlobClient.from_blob_url(item['url'], transport=transport)
                        data = client.download_blob(max_concurrency=1).readall()
                    else:
                        with urllib.request.urlopen(item['url'], timeout=60) as response:
                            data = response.read(limit + 1)
                    if len(data) != item['bytes'] or hashlib.sha256(data).hexdigest() != item['sha256']:
                        raise RuntimeError('Preview download mismatch')
                    if mode == 'disk4':
                        path = Path(directory) / str(index)
                        path.write_bytes(data)
                        return path
                    return data
                started = time.perf_counter()
                peak_buffer_bytes = 0
                consumer_seconds = 0
                with ThreadPoolExecutor(1 if mode == 'serial' else 4) as pool:
                    pending = []
                    next_index = 0
                    completed = 0
                    buffered = 0
                    while completed < len(entries):
                        while next_index < len(entries) and len(pending) < (1 if mode == 'serial' else 4):
                            item_bytes = entries[next_index]['bytes']
                            if buffered + item_bytes > limit:
                                break
                            pending.append((next_index, pool.submit(download, next_index)))
                            buffered += item_bytes
                            next_index += 1
                            peak_buffer_bytes = max(peak_buffer_bytes, buffered)
                        if not pending:
                            raise RuntimeError('Preview buffer made no progress')
                        index, future = pending.pop(0)
                        value = future.result()
                        before = time.perf_counter()
                        if mode == 'disk4':
                            data = value.read_bytes()
                            value.unlink()
                        else:
                            data = value
                        if hashlib.sha256(data).hexdigest() != entries[index]['sha256']:
                            raise RuntimeError('Buffered preview changed')
                        consumer_seconds += time.perf_counter() - before
                        del data, value, future
                        completed += 1
                        buffered -= entries[index]['bytes']
                report = dict(mode=mode, round=round_number, images=completed, wall_seconds=time.perf_counter() - started,
                              bytes=sum(entry['bytes'] for entry in entries), peak_buffer_bytes=peak_buffer_bytes,
                              buffer_limit_bytes=limit, consumer_seconds=consumer_seconds)
                reports.append(report)
                print(json.dumps(report), flush=True)
                args.output.write_text(json.dumps(dict(platform=platform.platform(), transport=args.transport,
                                                       reports=reports), indent=2))


if __name__ == '__main__':
    main()
