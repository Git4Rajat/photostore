"""Offline person-read concurrency benchmark; fixed fake latency, no Azure I/O."""
import argparse
import json
from pathlib import Path
import re
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from processing_metrics import accounting
from table_reads import key_batches, read_entities


class Table:
    def __init__(self, missing, delay):
        self.missing, self.delay = missing, delay

    def query_entities(self, query):
        if self.delay:
            time.sleep(self.delay / 1000)
        if self.missing:
            return []
        return [dict(PartitionKey='local', RowKey=key, name=key,
                     repEmbedding='[' + ','.join(['0'] * 512) + ']')
                for key in re.findall("RowKey eq '([^']*)'", query)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = dict(scope='Real read_entities helper, 56 keys, 512D payloads, five alternating trials.',
        limitation='Fixed per-query sleep only; not Azure response times or whole-wave throughput.',
        summaries=[])
    keys = [f'person-{i:04d}' for i in range(56)]
    for latency_ms in (0, 1, 5):
        for missing in (False, True):
            table = Table(missing, latency_ms)
            reference, samples = None, {1: [], 4: []}
            for trial in range(5):
                for workers in ([1, 4] if trial % 2 == 0 else [4, 1]):
                    started = time.perf_counter()
                    with accounting() as counts:
                        rows = {}
                        for batch in (key_batches(keys) if workers == 1 else [keys]):
                            rows.update(read_entities(table, 'local', batch, workers=workers))
                    elapsed = time.perf_counter() - started
                    reference = rows if reference is None else reference
                    assert rows == reference, 'Lookup outputs differ'
                    counters = counts.snapshot()
                    samples[workers].append(dict(seconds=elapsed,
                        queries=counters['batch_queries'], peak_inflight=counters['batch_peak_inflight']))
            serial = statistics.median(row['seconds'] for row in samples[1])
            parallel = statistics.median(row['seconds'] for row in samples[4])
            result['summaries'].append(dict(latency_ms=latency_ms,
                case='missing' if missing else 'existing', serial_median_ms=serial*1000,
                parallel_median_ms=parallel*1000, measured_speedup=serial/parallel,
                output_parity=True, samples=samples))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + '\n')


if __name__ == '__main__':
    main()
