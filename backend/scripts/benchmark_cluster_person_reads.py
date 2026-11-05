"""Read-only Azure comparison of bounded baseline-person lookup strategies.

Samples at most 56 existing keys in one library, then compares the actual
read_entities helper against those keys and an equal-sized set of missing IDs.
No source data, policy, queues or artifacts are mutated. Outputs no row payloads.
"""
import argparse
from itertools import islice
import json
from pathlib import Path
import statistics
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from azure.data.tables import TableServiceClient
from azure.identity import AzureCliCredential
import processing_metrics as metrics
from table_reads import read_entities, key_batches


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--account', required=True)
    parser.add_argument('--library', required=True)
    parser.add_argument('--keys', type=int, default=28)
    parser.add_argument('--trials', type=int, default=3)
    args = parser.parse_args()
    if not 1 <= args.keys <= 56 or not 1 <= args.trials <= 3:
        parser.error('Expected 1..56 keys and 1..3 trials')
    table = TableServiceClient(f'https://{args.account}.table.core.windows.net',
        credential=AzureCliCredential()).get_table_client('photopeople')
    query = "PartitionKey eq '" + args.library.replace("'", "''") + "'"
    keys = [row['RowKey'] for row in islice(table.query_entities(query,
        select=['RowKey', 'PartitionKey'], results_per_page=args.keys), args.keys)]
    if not keys:
        raise RuntimeError('No existing people in selected library')
    # Warm credential/session before comparing transport strategies.
    table.get_entity(args.library, keys[0])
    cases = {'existing': keys, 'missing': [str(uuid.uuid5(uuid.NAMESPACE_URL,
        'photostore-read-benchmark/' + args.library + '/' + key)) for key in keys]}
    modes = [('query_serial', 'query', 1), ('query_window_parallel', 'query', 4), ('point_parallel', 'point', 4)]
    result = []
    for case, selected in cases.items():
        baseline, samples = None, {name: [] for name, _, _ in modes}
        calls = {}
        for trial in range(args.trials):
            for name, strategy, workers in modes[trial % len(modes):] + modes[:trial % len(modes)]:
                started = time.monotonic()
                with metrics.accounting() as counter:
                    # Compare current fourteen-key windows with a proposed bounded
                    # 56-key window; each query still contains at most 14 keys.
                    rows = {}
                    for batch in (key_batches(selected) if name == 'query_serial' else [selected]):
                        rows.update(read_entities(table, args.library, batch,
                                                  strategy=strategy, workers=workers))
                samples[name].append((time.monotonic() - started) * 1000)
                canonical = json.dumps({key: dict(row) if row is not None else None
                                        for key, row in rows.items()}, sort_keys=True, default=str)
                if baseline is None:
                    baseline = canonical
                if baseline != canonical:
                    raise RuntimeError('Rows changed or transport results differ; do not use timings')
                counters = counter.snapshot()
                calls[name] = counters['batch_queries'] + counters['batch_point_reads']
        for name, _, _ in modes:
            result.append(dict(case=case, mode=name, keys=len(selected),
                found=sum(value is not None for value in rows.values()),
                median_ms=statistics.median(samples[name]), samples_ms=samples[name],
                helper_requests_per_trial=calls[name]))
    print(json.dumps(dict(results=result, output_parity=True,
        note='Read-only same-library sample; helper request counts exclude SDK retry/page requests. '
             'Does not measure whole-wave throughput or authorize mutable-row caching.'), indent=2))


if __name__ == '__main__':
    main()
