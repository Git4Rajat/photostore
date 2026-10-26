#!/usr/bin/env python3
"""Compare Vision coordination with a committed baseline using synthetic I/O.

Uses the real wave/receipt logic with the test suite's ETag tables. No cloud
writes, decoding, model inference, or 10k/hour production claim. Requires the
backend test dependencies. Run from the repository root, for example:
python backend/scripts/benchmark_wave_source_batch.py --photos 80 --model-ms 100 --storage-ms 2
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
import types

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-ref', default='7e76cff')
    parser.add_argument('--photos', type=int, default=80)
    parser.add_argument('--workers', type=int, default=2)
    parser.add_argument('--storage-ms', type=float, default=1)
    parser.add_argument('--model-ms', type=float, default=5)
    args = parser.parse_args()
    if not 1 <= args.photos <= 1000 or not 1 <= args.workers <= 20:
        parser.error('Expected 1-1000 photos and 1-20 workers')
    if args.storage_ms < 0 or args.model_ms < 0:
        parser.error('Synthetic latencies must be nonnegative')
    import os
    os.environ['UPLOAD_WAVE_EXECUTION_ENABLED'] = 'true'
    from execution_lease import ExecutionLeases
    from upload_waves import ShadowWaves
    from upload_wave_execution import run_vision_release, stage_work_store
    from tests.test_upload_waves import AtomicTable
    from tests.test_index_work import EtagTable
    from tests.test_wave_source_batch import admit, publish_completion

    baseline = types.ModuleType('wave_batch_baseline')
    baseline_commit = subprocess.check_output(['git', 'rev-parse', args.baseline_ref], text=True).strip()
    source = subprocess.check_output(['git', 'show', baseline_commit + ':backend/upload_wave_execution.py'], text=True)
    exec(compile(source, args.baseline_ref + ':backend/upload_wave_execution.py', 'exec'), baseline.__dict__)

    def measure(function, *, workers):
        table = AtomicTable()
        store = ShadowWaves(table, 'library-A')
        work = stage_work_store(EtagTable())
        leases = ExecutionLeases(table, partition=store.partition)
        metadata, wave, key = admit((store, work, leases, table, work.table), args.photos)
        calls = Counter()
        counter_lock = threading.Lock()
        checkpoints = leases.checkpoint
        def checkpoint(*a, **kw):
            with counter_lock:
                calls['checkpoints'] += 1
            return checkpoints(*a, **kw)
        leases.checkpoint = checkpoint
        for target, name in ((table, 'get_entity'), (table, 'query_entities'), (table, 'submit_transaction'),
                             (metadata, 'get_entity'), (metadata, 'query_entities')):
            original = getattr(target, name)
            label = ('wave.' if target is table else 'metadata.') + name
            def delayed(*a, _call=original, _label=label, **kw):
                with counter_lock:
                    calls[_label] += 1
                if _label == 'wave.submit_transaction':
                    operations = list(a[0]); a = (operations,) + a[1:]
                    if any(row['RowKey'].startswith(f'e!{wave}!m!') and row.get('outcome') == 'settled'
                           for _, row, *_ in operations):
                        with counter_lock:
                            calls['observation_transactions'] += 1
                time.sleep(args.storage_ms / 1000)
                return _call(*a, **kw)
            setattr(target, name, delayed)
        def process(library, filename, revision, renew):
            time.sleep(args.model_ms / 1000)
            publish_completion(store, metadata, filename=filename)
        started = time.monotonic()
        extra = {'source_workers': workers} if workers is not None else {}
        result = function(leases, work, store, store.library, wave, key, metadata, None, None,
            lambda *a: None, process_source=process, **extra)
        elapsed = time.monotonic() - started
        assert result['state'] in ('ready', 'indexing')
        members = store._page(f'e!{wave}!m!', size=args.photos)
        assert len(members) == args.photos and all(row['outcome'] == 'settled' for row in members)
        return dict(seconds=round(elapsed, 4), calls=dict(calls), verified_sources=len(members))

    before = measure(baseline.run_vision_release, workers=None)
    after = measure(run_vision_release, workers=args.workers)
    print(json.dumps(dict(kind='synthetic coordination comparison; not production throughput',
        config=vars(args), baseline_commit=baseline_commit, before=before, after=after,
        speedup=round(before['seconds'] / after['seconds'], 2)), indent=2))


if __name__ == '__main__':
    main()
