#!/usr/bin/env python3
"""Compare Table versus mounted-file journals using real FAISS and fake storage.

No Azure operations. Wall times use local disk and zero network latency; they
are not production throughput predictions. Requires backend test dependencies.
"""
import argparse
from collections import Counter
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import time

sys.path[:0] = [str(Path(__file__).resolve().parents[1]),
                str(Path(__file__).resolve().parents[1] / 'tests')]


def measure(count, local, root):
    import wave_assignment as wave
    from test_wave_assignment_journal import setup, comparable
    from clustering_reference_oracle import assignment_snapshot
    from execution_lease import execution_scope
    h, _, store, frozen, leases = setup()
    snapshots = []
    for i in range(count):
        row = dict(PartitionKey='u', RowKey=f'incoming-{i:06d}', filename=f'{i:06d}.jpg',
                   alignmentMethod='5pt', embeddingVersion='v1', personId='',
                   embedding=json.dumps(([1., 0., 0.], [0., 1., 0.], [0., 0., 1.])[i % 3]))
        h.faces.create_entity(row)
        snapshots.append(row)
    calls = Counter()
    for label, table in [('jobs', store.table), ('faces', h.faces), ('people', h.people),
                         ('members', h.members), ('embeddings', h.embeddings)]:
        for method in ('get_entity', 'create_entity', 'update_entity', 'query_entities', 'submit_transaction'):
            original = getattr(table, method, None)
            if original is None:
                continue
            def measured(*args, _original=original, _key=label + '.' + method, **kwargs):
                calls[_key] += 1
                return _original(*args, **kwargs)
            setattr(table, method, measured)
    (root / 'work').mkdir(parents=True)
    h.assigner.config = replace(h.assigner.config, checkpoint_dir=str(root / 'mount'), work_dir=str(root / 'work'))
    ownership = leases.claim('u', frozen['waveId'], 'cluster', frozen['frozenRevision'])
    started = time.monotonic()
    with execution_scope(leases, ownership, store, lambda: leases.check(ownership)):
        receipt = wave.run_frozen_assignment(store, frozen, h.assigner,
            lambda: iter(deepcopy(snapshots)), lambda: None, local_journal=local)
    elapsed = time.monotonic() - started
    leases.release(ownership)
    return dict(faces=count, mode='files' if local else 'tables', seconds=round(elapsed, 3),
                calls=dict(calls), total_calls=sum(calls.values()), receipt=receipt), comparable(assignment_snapshot(h))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--faces', type=int, default=1000)
    args = parser.parse_args()
    if not 1 <= args.faces <= 1000:
        parser.error('Expected 1-1000 faces')
    with tempfile.TemporaryDirectory(prefix='wave-journal-benchmark-') as directory:
        before, reference = measure(args.faces, False, Path(directory) / 'tables')
        after, result = measure(args.faces, True, Path(directory) / 'files')
        if result != reference:
            raise RuntimeError('Assignment outputs changed')
    print(json.dumps(dict(table_journal=before, file_journal=after, output_parity=True,
                         note='Synthetic Table I/O; no Azure latency. Guard-check frequency held constant.'), indent=2))


if __name__ == '__main__':
    main()
