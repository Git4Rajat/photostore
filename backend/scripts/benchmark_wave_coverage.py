"""Compare identical frozen assignments with per-write versus page coverage.

Offline, real FAISS and coverage protocol, in-memory ETag-bearing table fakes.
No Azure latency or throughput prediction. Run with --faces 1..1000.
"""
import argparse
from collections import Counter
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import threading
import time

sys.path[:0] = [str(Path(__file__).resolve().parents[1]),
                str(Path(__file__).resolve().parents[1] / 'tests')]

import pytest
import app
import automatic_index_updates as automatic
import processing_metrics as metrics
import processing_diagnostics as diagnostics
import storage_utils as su
import wave_assignment as wave
from clustering_reference_oracle import assignment_snapshot
from execution_lease import execution_scope
from test_wave_assignment_journal import setup, comparable
from tests.test_automatic_index_updates import Table


class BenchmarkTable(Table):
    """Same atomic fake semantics, without copying unrelated ETag history.

    The test fake deep-copies its entire versions map on each transaction.
    Deleted reservation IDs accumulate there, making timings quadratic in N.
    Snapshot only transaction participants; rollback still restores exact rows
    and versions on any failure. This changes the harness, never production.
    """
    def submit_transaction(self, operations, **kwargs):
        operations = list(operations)
        assert len(operations) <= 100
        assert len({op[1]['PartitionKey'] for op in operations}) <= 1
        keys = {(op[1]['PartitionKey'], op[1]['RowKey']) for op in operations}
        previous_rows = {key: deepcopy(self.rows[key]) for key in keys if key in self.rows}
        previous_versions = {key: self.versions[key] for key in keys if key in self.versions}
        self.submit_transaction_calls.append(operations)
        try:
            for operation in operations:
                method, entity = operation[:2]
                options = operation[2] if len(operation) > 2 else {}
                if method == 'delete':
                    self.delete_entity(entity['PartitionKey'], entity['RowKey'], **options)
                elif method in ('update', 'create', 'upsert'):
                    getattr(self, method + '_entity')(entity, **options)
                else:
                    raise AssertionError(method)
        except BaseException:
            for key in keys:
                self.rows.pop(key, None)
                self.versions.pop(key, None)
            self.rows.update(previous_rows)
            self.versions.update(previous_versions)
            raise


def measure(count, *, batched, latency_ms=0, identities=None, dimensions=512,
            include_control=False):
    h, _, store, frozen, leases = setup()
    if identities is not None:
        # Balanced known identities allow larger runs without exceeding the
        # genuine legacy per-person membership limit. No new-person randomness.
        for table in (h.faces, h.people, h.embeddings, h.members):
            table.rows.clear()
            table.versions.clear()
        vectors = [[float(i == identity) for i in range(dimensions)]
                   for identity in range(identities)]
        for identity, vector in enumerate(vectors):
            person, face = f'person-{identity:04d}', f'anchor-{identity:04d}'
            h.people.create_entity(dict(PartitionKey='u', RowKey=person,
                faceIds=json.dumps([face]), repEmbedding=json.dumps(vector), name=person))
            h.faces.create_entity(dict(PartitionKey='u', RowKey=face, filename=face+'.jpg',
                personId=person, alignmentMethod='5pt', embeddingVersion='v1',
                embedding=json.dumps(vector)))
    snapshots = []
    for i in range(count):
        row = dict(PartitionKey='u', RowKey=f'incoming-{i:06d}', filename=f'{i:06d}.jpg',
                   alignmentMethod='5pt', embeddingVersion='v1', personId='',
                   embedding=json.dumps(vectors[i % identities]) if identities is not None else '[1,0,0]')
        h.faces.create_entity(row)
        snapshots.append(row)
    dirty, admission = BenchmarkTable(), BenchmarkTable()
    calls, depth, call_lock = Counter(), threading.local(), threading.Lock()
    tables = [('faces', h.faces), ('people', h.people), ('members', h.members),
              ('embeddings', h.embeddings), ('coverage', dirty), ('admission', admission)]
    if include_control:
        tables.append(('jobs', store.table))
    for label, table in tables:
        for name in ('get_entity', 'create_entity', 'update_entity', 'upsert_entity',
                     'delete_entity', 'query_entities', 'submit_transaction'):
            original = getattr(table, name, None)
            if original is None:
                continue
            def counted(*args, _fn=original, _key=label + '.' + name, **kwargs):
                nesting = getattr(depth, 'value', 0)
                if nesting == 0:
                    with call_lock:
                        calls[_key] += 1
                    if latency_ms:
                        time.sleep(latency_ms / 1000)
                depth.value = nesting + 1
                try:
                    return _fn(*args, **kwargs)
                finally:
                    depth.value = nesting
            setattr(table, name, counted)
    with pytest.MonkeyPatch.context() as patch, tempfile.TemporaryDirectory() as root:
        patch.setenv('WAVE_CLUSTER_COVERAGE_BATCH_ENABLED', str(batched).lower())
        patch.setenv('WAVE_CLUSTER_PERSON_READ_BATCH_ENABLED', str(batched).lower())
        patch.setattr(su, 'DURABLE_INDEX_UPDATES', True)
        patch.setattr(su, '_CTX', {'search_index_dirty_table_client': dirty,
                                  'wave_owned': lambda _: True})
        patch.setattr(app, 'jobs_table_client', admission)
        patch.setattr(automatic, 'schedule', lambda *args: (_ for _ in ()).throw(
            AssertionError('Standalone maintenance from wave assignment')))
        h.assigner.face_table = automatic.SourceTable(h.faces, 'face')
        h.assigner.person_table = automatic.SourceTable(h.people, 'person')
        Path(root, 'work').mkdir()
        h.assigner.config = replace(h.assigner.config, work_dir=root + '/work',
                                   checkpoint_dir=root + '/mount')
        owner = leases.claim('u', frozen['waveId'], 'cluster', frozen['frozenRevision'])
        calls.clear()  # Stage claim/setup are outside the timed runner.
        phases = diagnostics.WaveMetrics('u', frozen['waveId'])
        phase_token = diagnostics._WAVE.set(phases)
        started, cpu_started = time.perf_counter(), time.process_time()
        try:
            with metrics.accounting() as counter, execution_scope(
                    leases, owner, store, lambda: leases.check(owner)):
                wave.run_frozen_assignment(store, frozen, h.assigner,
                    lambda: iter(deepcopy(snapshots)), lambda: None, local_journal=True)
        finally:
            elapsed, cpu = time.perf_counter() - started, time.process_time() - cpu_started
            diagnostics._WAVE.reset(phase_token)
            measured = dict(calls)
            leases.release(owner)
        counts = counter.snapshot()
        # Snapshot comparisons are outside the measured call window.
        result = comparable(assignment_snapshot(h))
        if identities is not None:
            assert all(result['owners'][f'incoming-{i:06d}'] == f'person-{i % identities:04d}'
                       for i in range(count)), 'Synthetic identity fixture matched an unexpected person'
    return dict(mode='page' if batched else 'per_write', faces=count,
                calls=measured, total_measured_calls=sum(measured.values()),
                coverage_pages=counts['cluster_coverage_pages'],
                scoped_writes=counts['cluster_coverage_writes'],
                elapsed_seconds=elapsed, cpu_seconds=cpu, faces_per_second=count / elapsed,
                journal_fsyncs=counts['journal_fsyncs'],
                phases=phases.phases, latency_ms_per_fake_call=latency_ms,
                includes_control_calls=include_control, identities=identities,
                dimensions=dimensions if identities is not None else 3), result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--faces', type=int, default=500)
    args = parser.parse_args()
    if not 1 <= args.faces <= 1000:
        parser.error('Expected 1..1000 faces')
    before, expected = measure(args.faces, batched=False)
    after, actual = measure(args.faces, batched=True)
    assert expected == actual, 'Assignment output changed'
    print(json.dumps(dict(before=before, after=after, output_parity=True,
        limitations=['Fake storage, no service latency.',
                     'Counts exclude journal/control storage, immutable receipts and network bytes.',
                     'Wave ownership callback is constant; production policy lookup cost excluded.',
                     'Nested fake transaction implementation calls counted only once.']), indent=2))


if __name__ == '__main__':
    main()
