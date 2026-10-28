"""Read-only Azure Table experiment for real Vision inspection key sets.

Supports Azure CLI credentials or a short-lived STORAGE_BENCHMARK_TOKEN and
STORAGE_BENCHMARK_TOKEN_EXPIRY supplied as job secrets. No credentials, keys,
filenames or entity payloads are written to reports.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
from itertools import groupby
from itertools import islice
import json
import os
from pathlib import Path
import platform
import threading
import time

from azure.core.credentials import AccessToken, AzureSasCredential
from azure.core.exceptions import ResourceNotFoundError
from azure.data.tables import TableClient
from azure.identity import AzureCliCredential

from table_reads import key_batches
from upload_waves import digest


def escaped(value):
    return value.replace("'", "''")


def identity(rows):
    return hashlib.sha256(json.dumps({key: dict(row) if row is not None else None
        for key, row in rows.items()}, sort_keys=True, default=str).encode()).hexdigest()


class TokenCredential:
    def get_token(self, *scopes, **kwargs):
        return AccessToken(os.environ['STORAGE_BENCHMARK_TOKEN'],
                           int(os.environ['STORAGE_BENCHMARK_TOKEN_EXPIRY']))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--account', required=True)
    parser.add_argument('--library', required=True)
    parser.add_argument('--wave', nargs='+', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--rounds', type=int, default=3)
    parser.add_argument('--max-members', type=int, default=200)
    parser.add_argument('--metadata-account', help='Optional private seeded benchmark storage account')
    args = parser.parse_args()
    if args.rounds < 2 or not 1 <= args.max_members <= 1000:
        parser.error('Expected at least two rounds and 1–1000 sampled members')
    credential = TokenCredential() if os.environ.get('STORAGE_BENCHMARK_TOKEN') else AzureCliCredential()
    endpoint = 'https://' + args.account + '.table.core.windows.net'
    tables = {name: TableClient(endpoint, name, credential=credential) for name in ('photojobs', 'photometadata')}
    if args.metadata_account:
        tables['photometadata'] = TableClient('https://' + args.metadata_account + '.table.core.windows.net',
            'benchmarkmetadata', credential=AzureSasCredential(os.environ['BENCHMARK_METADATA_SAS']))
    partition = 'upload-wave-shadow-' + digest(args.library)
    artifact_partition = 'processing-artifacts-' + digest(args.library)
    reports = []
    ranges = []
    summary = []

    def fetch(table, partition, keys, mode, hook=None):
        keys = list(dict.fromkeys(keys))
        def query(batch):
            query_filter = "PartitionKey eq '" + escaped(partition) + "' and (" + ' or '.join(
                "RowKey eq '" + escaped(key) + "'" for key in batch) + ')'
            return [(row['RowKey'], row) for row in table.query_entities(query_filter, raw_response_hook=hook)]
        def point(key):
            try:
                return key, table.get_entity(partition, key, raw_response_hook=hook)
            except ResourceNotFoundError:
                return key, None
        result = dict.fromkeys(keys)
        batches = list(key_batches(keys))
        if mode in ('prefix_query', 'parallel_prefix_query'):
            batches = [batch for _, grouped in groupby(sorted(keys), key=lambda key: key.split('!', 1)[0])
                       for batch in key_batches(list(grouped))]
        if mode in ('query', 'prefix_query'):
            for batch in batches:
                result.update(query(batch))
        else:
            workers = 4 if mode in ('parallel_query', 'parallel_prefix_query', 'point4') else 8
            with ThreadPoolExecutor(workers) as pool:
                if mode in ('parallel_query', 'parallel_prefix_query'):
                    for rows in pool.map(query, batches):
                        result.update(rows)
                else:
                    result.update(pool.map(point, keys))
        if set(result) != set(keys):
            raise RuntimeError('Query returned an unexpected key')
        return result

    for wave in args.wave:
        prefix = 'e!' + wave + '!m!'
        query_filter = "PartitionKey eq '" + partition + "' and RowKey ge '" + prefix + "' and RowKey lt '" + prefix + "~'"
        members = list(islice(tables['photojobs'].query_entities(query_filter, results_per_page=1000), args.max_members))
        wave_row = tables['photojobs'].get_entity(partition, 'wave!' + wave)
        summary.append(dict(wave=wave, member_count=wave_row.get('memberCount'), sampled=len(members)))
        for page_size in (20, 200, 1000):
            for round_number in range(args.rounds):
                calls = [0]
                def range_hook(response):
                    calls[0] += 1
                started = time.perf_counter()
                actual = list(islice(tables['photojobs'].query_entities(query_filter,
                    results_per_page=page_size, raw_response_hook=range_hook), len(members)))
                if identity({row['RowKey']: row for row in actual}) != identity({row['RowKey']: row for row in members}):
                    raise RuntimeError('Member range changed during measurement')
                ranges.append(dict(wave=wave, page_size=page_size, sampled=len(members), round=round_number,
                                   wall_seconds=time.perf_counter() - started, requests=calls[0]))
        for size in sorted({min(20, len(members)), len(members)}):
            sample = members[:size]
            metadata_keys = [row['filename'] for row in sample]
            metadata = fetch(tables['photometadata'], args.library, metadata_keys, 'query')
            proof_keys = [kind + digest([row['photoId'], row['processingRevision']])
                          for row in sample for kind in ('deleted-source!', 'supersession!')]
            inbox_keys = []
            for row in sample:
                completion = (metadata.get(row['filename']) or {}).get('processingCompletionId')
                if not completion and row.get('outcome') == 'settled':
                    completion = row.get('evidenceId')
                if completion:
                    inbox_keys.append('source-completion!' + digest([row['photoId'], row['processingRevision']]) + '!' + completion)
            inboxes = fetch(tables['photojobs'], partition, inbox_keys, 'query')
            manifest_keys = []
            for row in inboxes.values():
                if row and row.get('artifactReceiptQualified'):
                    reference = json.loads(row['payload']).get('artifactReceipt')
                    if isinstance(reference, dict) and isinstance(reference.get('id'), str):
                        manifest_keys.append('manifest!' + reference['id'])
            manifests = fetch(tables['photojobs'], artifact_partition, manifest_keys, 'query')
            manifest_origin = 'wave_completion_references'
            if not manifests:
                manifest_origin = 'retained_library_receipts_sample'
                manifest_rows = list(islice(tables['photojobs'].query_entities("PartitionKey eq '" + artifact_partition +
                    "' and RowKey ge 'manifest!' and RowKey lt 'manifest!~'"), size))
                manifests = {row['RowKey']: row for row in manifest_rows}
                manifest_keys = list(manifests)
            summary.append(dict(wave=wave, size=size, manifest_origin=manifest_origin,
                                metadata_kind='seeded synthetic' if args.metadata_account else 'existing production rows'))
            face_keys = ['face!' + key for row in manifests.values() if row
                         for key in json.loads(row['payload']).get('faceSnapshots', [])]
            workloads = [('metadata', 'photometadata', args.library, metadata_keys),
                         ('proofs_and_inboxes', 'photojobs', partition, proof_keys + inbox_keys),
                         ('manifests', 'photojobs', artifact_partition, manifest_keys),
                         ('face_receipts', 'photojobs', artifact_partition, face_keys)]
            for label, table_name, row_partition, keys in workloads:
                if not keys:
                    summary.append(dict(wave=wave, size=size, workload=label, skipped='no matching keys'))
                    continue
                expected = None
                for round_number in range(args.rounds):
                    modes = ['query', 'parallel_query', 'prefix_query', 'parallel_prefix_query', 'point4', 'point8']
                    for mode in modes if round_number % 2 == 0 else reversed(modes):
                        counters = dict(requests=0, response_bytes=0, throttles=0, errors=0)
                        lock = threading.Lock()
                        def hook(response):
                            status = response.http_response.status_code
                            with lock:
                                counters['requests'] += 1
                                counters['response_bytes'] += len(response.http_response.body())
                                counters['throttles'] += int(status in (429, 503))
                                counters['errors'] += int(status >= 400 and status != 404)
                        started = time.perf_counter()
                        rows = fetch(tables[table_name], row_partition, keys, mode, hook)
                        elapsed = time.perf_counter() - started
                        result_hash = identity(rows)
                        if expected is None:
                            expected = result_hash
                        if result_hash != expected:
                            raise RuntimeError('Read outputs differ or live data changed: ' + label)
                        report = dict(wave=wave, members=size, workload=label, keys=len(rows), mode=mode,
                                      round=round_number, wall_seconds=elapsed, found=sum(row is not None for row in rows.values()),
                                      result_hash=result_hash, **counters)
                        reports.append(report)
                        print(json.dumps(report), flush=True)
                        args.output.write_text(json.dumps(dict(platform=platform.platform(), waves=summary,
                                                               range_reports=ranges, reports=reports), indent=2))


if __name__ == '__main__':
    main()
