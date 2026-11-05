"""Offline clustering audit: real matcher/wrappers, in-memory storage only.

Run from repository root with .venv-photostore-tools/bin/python <this file>.
Counts outer fake-storage calls; nested implementations of fake transactions
are not additional network calls. Scheduling is recorded, not executed.
"""
from collections import Counter
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'backend'), str(ROOT / 'backend/tests')]

import pytest
import app
import storage_utils as su
import automatic_index_updates as automatic
from tests.test_automatic_index_updates import Table
from test_wave_assignment_journal import setup, comparable
from clustering_reference_oracle import assignment_snapshot
from execution_lease import execution_scope
from wave_policy import begin_source_change, end_source_change
import wave_assignment as wave


def measure(mode, count=128):
    h, _, store, frozen, leases = setup()
    snapshots = []
    for i in range(count):
        row = dict(PartitionKey='u', RowKey=f'incoming-{i:06d}',
                   filename=f'{i:06d}.jpg', alignmentMethod='5pt',
                   embeddingVersion='v1', personId='',
                   embedding=json.dumps([1., 0., 0.]))
        h.faces.create_entity(row)
        snapshots.append(row)
    counts, schedules = Counter(), []
    dirty, admission = Table(), Table()
    depth = [0]
    for label, table in [('faces', h.faces), ('people', h.people),
                         ('members', h.members), ('embeddings', h.embeddings),
                         ('coverage', dirty), ('admission', admission)]:
        for method in ('get_entity', 'create_entity', 'update_entity',
                       'upsert_entity', 'delete_entity', 'query_entities', 'submit_transaction'):
            original = getattr(table, method, None)
            if original is None:
                continue
            def counted(*args, _fn=original, _key=label + '.' + method, **kwargs):
                if not depth[0]:
                    counts[_key] += 1
                depth[0] += 1
                try:
                    return _fn(*args, **kwargs)
                finally:
                    depth[0] -= 1
            setattr(table, method, counted)
    with pytest.MonkeyPatch.context() as patch, tempfile.TemporaryDirectory() as tmp:
        patch.setattr(su, 'DURABLE_INDEX_UPDATES', True)
        patch.setattr(su, '_CTX', {'search_index_dirty_table_client': dirty,
                                  'wave_owned': lambda _: True})
        patch.setattr(app, 'jobs_table_client', admission)
        patch.setattr(automatic, 'schedule', lambda user, kind: schedules.append((user, kind)))
        if mode != 'raw':
            h.assigner.face_table = automatic.SourceTable(h.faces, 'face')
            h.assigner.person_table = automatic.SourceTable(h.people, 'person')
        h.assigner.config = replace(h.assigner.config, checkpoint_dir=tmp + '/mount',
                                   work_dir=tmp + '/work')
        Path(tmp + '/work').mkdir()
        ownership = leases.claim('u', frozen['waveId'], 'cluster', frozen['frozenRevision'])
        token = begin_source_change() if mode == 'tracked_source_scope' else None
        try:
            with execution_scope(leases, ownership, store, lambda: leases.check(ownership)):
                wave.run_frozen_assignment(store, frozen, h.assigner,
                    lambda: iter(deepcopy(snapshots)), lambda: None, local_journal=True)
        finally:
            if token is not None:
                end_source_change(token)
            leases.release(ownership)
        snapshot = comparable(assignment_snapshot(h))
    return dict(mode=mode, faces=count, calls=dict(sorted(counts.items())),
                schedule_calls=len(schedules)), snapshot


def main():
    rows, reference = [], None
    for mode in ('raw', 'tracked_current', 'tracked_source_scope'):
        row, snapshot = measure(mode)
        if reference is None:
            reference = snapshot
        assert snapshot == reference, 'Assignment outputs differ'
        rows.append(row)
    print(json.dumps(dict(results=rows, assignment_output_parity=True,
        limitations=['No network or Azure latency; synthetic hot-person workload.',
                     'Wave-owned policy lookup is a constant callback.',
                     'Scheduling recorded only; real WorkStore/queue overhead excluded.',
                     'Raw mode is a measurement control, not a proposed production bypass.']), indent=2))


if __name__ == '__main__':
    main()
