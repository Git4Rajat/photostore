"""Repeatable offline before/after wave benchmark; never contacts Azure.

Runs real FAISS, SQLite and fsync with ETag-bearing test tables. Synthetic
orthogonal 512D vectors match existing people; this isolates persistence cost
and does not represent real face distributions, recall or Azure throughput.
"""
import argparse
import gc
import json
import platform
from pathlib import Path
import statistics
import sys
import time

from benchmark_wave_coverage import measure


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sizes', type=int, nargs='+', default=[1, 128, 500, 1902, 10000])
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--latency-ms', type=float, default=0)
    parser.add_argument('--identities', type=int, default=32)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if (not all(1 <= count <= 100000 for count in args.sizes)
            or not 1 <= args.repeats <= 5 or not 0 <= args.latency_ms <= 10
            or not 1 <= args.identities <= 512):
        parser.error('sizes 1..100000, repeats 1..5, latency 0..10ms, identities 1..512')
    output = dict(environment=dict(python=sys.version, platform=platform.platform(),
        machine=platform.machine(), dimensions=512, identities=args.identities,
        latency_ms_per_fake_call=args.latency_ms, repeats=args.repeats),
        scope='Fresh wave runner, including local journal and instrumented control calls; '
              'excludes upload, Vision, immutable receipt fetching, claim/release, Index and publication.',
        limitations=['Synthetic orthogonal vectors and known identities; not a face-quality benchmark.',
                     'In-memory tables do not enforce Azure request/entity byte limits.',
                     'Fake query implementation scans rows; CPU costs may differ from Azure.',
                     'Fixed sleeps model call latency, not a real transport or throughput forecast.',
                     'No continuation, live curation races or cross-process storage tested here.',
                     'Wave ownership callback is constant; production policy lookup cost excluded.',
                     'Python allocation tracing disabled to avoid distorting timing.'],
        runs=[], summaries=[])
    def save():
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(output, indent=2) + '\n')
    # Unmeasured small warmup loads native code/SQLite paths, using both modes.
    _, before = measure(1, batched=False, identities=args.identities, include_control=True)
    _, after = measure(1, batched=True, identities=args.identities, include_control=True)
    assert before == after, 'Warmup assignment output changed'
    for count in args.sizes:
        samples = {False: [], True: []}
        for trial in range(args.repeats):
            expected = None
            for batched in ([False, True] if trial % 2 == 0 else [True, False]):
                gc.collect()
                started = time.monotonic()
                run, actual = measure(count, batched=batched, latency_ms=args.latency_ms,
                    identities=args.identities, include_control=True)
                if expected is None:
                    expected = actual
                elif expected != actual:
                    raise RuntimeError(f'Assignment output changed at {count} faces trial {trial}')
                run.update(trial=trial, output_parity=True)
                output['runs'].append(run)
                samples[batched].append(run)
                save()
                print(f"faces={count} trial={trial + 1} mode={run['mode']} "
                      f"wall={run['elapsed_seconds']:.3f}s calls={run['total_measured_calls']} "
                      f"harness={time.monotonic()-started:.3f}s", flush=True)
        row = dict(faces=count, output_parity=True)
        for batched, name in [(False, 'before'), (True, 'after')]:
            runs = samples[batched]
            seconds = statistics.median(run['elapsed_seconds'] for run in runs)
            row[name] = dict(median_seconds=seconds, faces_per_second=count/seconds,
                min_seconds=min(run['elapsed_seconds'] for run in runs),
                max_seconds=max(run['elapsed_seconds'] for run in runs),
                median_cpu_seconds=statistics.median(run['cpu_seconds'] for run in runs),
                median_counted_calls=statistics.median(run['total_measured_calls'] for run in runs),
                median_fsyncs=statistics.median(run['journal_fsyncs'] for run in runs))
        row['measured_speedup'] = row['before']['median_seconds'] / row['after']['median_seconds']
        row['counted_call_reduction_percent'] = 100 * (1 - row['after']['median_counted_calls'] /
                                                       row['before']['median_counted_calls'])
        output['summaries'].append(row)
        save()
    output['complete'] = True
    save()


if __name__ == '__main__':
    main()
