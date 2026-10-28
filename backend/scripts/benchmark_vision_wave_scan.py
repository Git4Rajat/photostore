"""Read complete existing wave member ranges with matched page sizes.

Streaming fingerprints verify the same rows; reports exclude entity payloads.
"""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import time

from azure.data.tables import TableClient
from azure.identity import AzureCliCredential
from benchmark_vision_reads import TokenCredential
from upload_waves import digest
import os


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--rounds', type=int, default=3)
    args = parser.parse_args()
    if args.rounds < 2:
        parser.error('Use at least two rounds')
    credential = TokenCredential() if os.environ.get('STORAGE_BENCHMARK_TOKEN') else AzureCliCredential()
    table = TableClient('https://forenklaprep1devmqwhngus.table.core.windows.net', 'photojobs', credential=credential)
    partition = 'upload-wave-shadow-' + digest('owner')
    reports = []
    for wave in ('e6f6d0610479484caddb380f741df945', '42cc98bb4e854c48a03a22593b654d59'):
        expected = None
        for round_number in range(args.rounds):
            for page_size in (20, 200, 1000) if round_number % 2 == 0 else (1000, 200, 20):
                counters = dict(requests=0, response_bytes=0, throttles=0)
                def hook(response):
                    counters['requests'] += 1
                    counters['response_bytes'] += len(response.http_response.body())
                    counters['throttles'] += int(response.http_response.status_code in (429, 503))
                prefix = 'e!' + wave + '!m!'
                query = "PartitionKey eq '" + partition + "' and RowKey ge '" + prefix + "' and RowKey lt '" + prefix + "~'"
                started = time.perf_counter()
                first_chunk = None
                rows = 0
                fingerprint = hashlib.sha256()
                for row in table.query_entities(query, results_per_page=page_size, raw_response_hook=hook):
                    fingerprint.update(json.dumps(dict(row), sort_keys=True, default=str).encode())
                    rows += 1
                    if first_chunk is None and rows == min(20, page_size):
                        first_chunk = time.perf_counter() - started
                output_hash = fingerprint.hexdigest()
                if expected is None:
                    expected = (rows, output_hash)
                if (rows, output_hash) != expected:
                    raise RuntimeError('Wave rows changed during the scan')
                report = dict(wave=wave, page_size=page_size, round=round_number, rows=rows,
                              wall_seconds=time.perf_counter() - started,
                              first_processing_chunk_seconds=first_chunk if first_chunk is not None else time.perf_counter() - started,
                              data_sha256=output_hash, **counters)
                reports.append(report)
                print(json.dumps(report), flush=True)
                args.output.write_text(json.dumps(dict(platform=platform.platform(), reports=reports), indent=2))


if __name__ == '__main__':
    main()
