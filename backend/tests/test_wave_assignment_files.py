"""Local journal parity, partial writes, mount loss, and measured Table savings."""
from copy import deepcopy
from dataclasses import replace

import pytest
pytest.importorskip('faiss')

from execution_lease import execution_scope, OwnershipLost
import wave_assignment as wave
from test_wave_assignment_journal import setup, oracle, comparable
from clustering_reference_oracle import assignment_snapshot


def run(h, snapshots, store, frozen, leases, root, *, local=True, page_size=3, should_yield=lambda: False,
        progress=lambda count: None):
    (root / 'work').mkdir(exist_ok=True)
    h.assigner.config = replace(h.assigner.config, checkpoint_dir=str(root / 'mount'), work_dir=str(root / 'work'))
    ownership = leases.claim('u', frozen['waveId'], 'cluster', frozen['frozenRevision'])
    def check():
        pass  # Native library lease remains checked by the runner.
    try:
        with execution_scope(leases, ownership, store, lambda: leases.check(ownership)):
            return wave.run_frozen_assignment(store, frozen, h.assigner,
                lambda: iter(deepcopy(snapshots)), check, local_journal=local, page_size=page_size,
                should_yield=should_yield, progress=progress)
    finally:
        try:
            leases.release(ownership)
        except OwnershipLost:
            pass


@pytest.mark.parametrize('page_size', [1, 3, 128])
def test_local_journal_matches_oracle_without_per_face_table_journal_rows(tmp_path, page_size):
    expected = oracle()
    h, snapshots, store, frozen, leases = setup()
    receipt = run(h, snapshots, store, frozen, leases, tmp_path, page_size=page_size)
    assert comparable(assignment_snapshot(h)) == expected
    rows = store._page('assignment!')
    assert {r['RowKey'].split('!')[-1] for r in rows} == {'local-v1', 'manifest', 'complete'}
    assert len(receipt) == 64
    journal_file, = (tmp_path / 'mount').rglob('journal.jsonl')
    assert journal_file.stat().st_size > 0
    assert not list((tmp_path / 'mount').rglob('*.sqlite3'))


@pytest.mark.parametrize('table', ['faces', 'people', 'members'])
def test_lost_materialization_reply_resumes_local_decisions(tmp_path, table):
    expected = oracle()
    h, snapshots, store, frozen, leases = setup()
    getattr(h, table).fail = OSError('lost reply after commit')
    with pytest.raises(OSError):
        run(h, snapshots, store, frozen, leases, tmp_path)
    class Poison:
        def __deepcopy__(self, memo):
            pytest.fail('Recaptured inputs after materialization')
    run(h, Poison(), store, frozen, leases, tmp_path)
    assert comparable(assignment_snapshot(h)) == expected


def test_missing_marked_mount_journal_fails_before_writes(tmp_path):
    h, snapshots, store, frozen, leases = setup()
    run(h, snapshots, store, frozen, leases, tmp_path)
    journal_file, = (tmp_path / 'mount').rglob('journal.jsonl')
    journal_file.unlink()
    for table in (h.faces, h.people, h.members):
        table.after_write = lambda row: pytest.fail('Wrote after losing frozen baseline')
    with pytest.raises(ValueError, match='missing'):
        run(h, snapshots, store, frozen, leases, tmp_path)


def test_torn_final_append_is_discarded_without_reapplying_results(tmp_path):
    h, snapshots, store, frozen, leases = setup()
    receipt = run(h, snapshots, store, frozen, leases, tmp_path)
    journal_file, = (tmp_path / 'mount').rglob('journal.jsonl')
    with journal_file.open('ab') as output:
        output.write(b'{"key":"torn')
    for table in (h.faces, h.people, h.members):
        table.after_write = lambda row: pytest.fail('Reapplied completed face')
    assert run(h, snapshots, store, frozen, leases, tmp_path) == receipt
    assert journal_file.read_bytes().endswith(b'\n')


def test_corrupt_marked_journal_fails_closed(tmp_path):
    h, snapshots, store, frozen, leases = setup()
    run(h, snapshots, store, frozen, leases, tmp_path)
    journal_file, = (tmp_path / 'mount').rglob('journal.jsonl')
    journal_file.write_bytes(journal_file.read_bytes().replace(b'"digest":"', b'"digest":"wrong', 1))
    with pytest.raises(ValueError, match='Corrupt'):
        run(h, snapshots, store, frozen, leases, tmp_path)


def test_existing_table_journal_remains_the_authority(tmp_path, monkeypatch):
    h, snapshots, store, frozen, leases = setup()
    receipt = run(h, snapshots, store, frozen, leases, tmp_path, local=False)
    monkeypatch.setattr(wave, '_decide', lambda *a: pytest.fail('Recomputed old journal'))
    assert run(h, snapshots, store, frozen, leases, tmp_path, local=True) == receipt
    assert not (tmp_path / 'mount' / 'wave-journals').exists()


def test_table_journal_transactions_are_constant_for_local_plan(tmp_path):
    counts = {}
    for local in (False, True):
        root = tmp_path / str(local)
        root.mkdir()
        h, snapshots, store, frozen, leases = setup()
        original = store.table.submit_transaction
        writes = []
        def submit(operations):
            operations = list(operations)
            writes.extend(row['RowKey'] for _, row, *_ in operations
                          if row['RowKey'].startswith('assignment!'))
            return original(operations)
        store.table.submit_transaction = submit
        run(h, snapshots, store, frozen, leases, root, local=local)
        counts[local] = len(writes)
    assert counts[True] == 3
    assert counts[False] > 3 * len(snapshots)


def test_compact_manifest_does_not_switch_recovery_to_table_journal(tmp_path):
    h, snapshots, store, frozen, leases = setup()
    receipt = run(h, snapshots, store, frozen, leases, tmp_path)
    assert store._get(f"assignment!{frozen['waveId']}!manifest")
    for table in (h.faces, h.people, h.members):
        table.after_write = lambda row: pytest.fail('Reapplied local decisions')
    assert run(h, snapshots, store, frozen, leases, tmp_path) == receipt
    journal_file, = (tmp_path / 'mount').rglob('journal.jsonl')
    journal_file.unlink()
    with pytest.raises(ValueError, match='missing'):
        run(h, snapshots, store, frozen, leases, tmp_path)
    with pytest.raises(ValueError, match='checkpoint mount'):
        run(h, snapshots, store, frozen, leases, tmp_path, local=False)


def test_completed_old_local_journal_backfills_missing_handoff_header(tmp_path):
    h, snapshots, store, frozen, leases = setup()
    receipt = run(h, snapshots, store, frozen, leases, tmp_path)
    # Reproduce the deployed layout: local marker + complete, no Table header.
    del store.table.rows[store.partition, f"assignment!{frozen['waveId']}!manifest"]
    for table in (h.faces, h.people, h.members):
        table.after_write = lambda row: pytest.fail('Reapplied already completed assignment')
    assert run(h, snapshots, store, frozen, leases, tmp_path) == receipt
    assert store._required(f"assignment!{frozen['waveId']}!manifest")['digest']


def test_interrupted_compact_manifest_publication_resumes_without_reapplying(tmp_path, monkeypatch):
    h, snapshots, store, frozen, leases = setup()
    original = wave.WaveAssignmentJournal.put
    def publish(self, key, payload):
        if type(self) is wave.WaveAssignmentJournal and key == 'manifest':
            raise OSError('handoff header upload failed')
        return original(self, key, payload)
    with monkeypatch.context() as patch:
        patch.setattr(wave.WaveAssignmentJournal, 'put', publish)
        with pytest.raises(OSError, match='header upload'):
            run(h, snapshots, store, frozen, leases, tmp_path)
    assert not store._get(f"assignment!{frozen['waveId']}!complete")
    for table in (h.faces, h.people, h.members):
        table.after_write = lambda row: pytest.fail('Reapplied after header upload failure')
    receipt = run(h, snapshots, store, frozen, leases, tmp_path)
    assert store._get(f"assignment!{frozen['waveId']}!complete")['digest'] == receipt


def test_local_plan_preserves_foreground_curation_after_interrupted_decision(tmp_path, monkeypatch):
    h, snapshots, store, frozen, leases = setup()
    with monkeypatch.context() as patch:
        patch.setattr(wave, '_apply', lambda *a: (_ for _ in ()).throw(OSError('before materialization')))
        with pytest.raises(OSError):
            run(h, snapshots, store, frozen, leases, tmp_path)
    h.faces.rows['u', 'a'].update(reviewStatus='confirmed', personId='alice', rejected=False)
    h.faces.versions['u', 'a'] += 1
    receipt = run(h, snapshots, store, frozen, leases, tmp_path)
    assert h.faces.rows['u', 'a']['reviewStatus'] == 'confirmed'
    assert h.faces.rows['u', 'a']['personId'] == 'alice'
    for table in (h.faces, h.people, h.members):
        table.after_write = lambda row: pytest.fail('Reapplied already witnessed curation')
    assert run(h, snapshots, store, frozen, leases, tmp_path) == receipt


def test_unpublished_partial_snapshot_is_safe_to_rebuild(tmp_path):
    expected = oracle()
    h, snapshots, store, frozen, leases = setup()
    class Interrupted:
        def __deepcopy__(self, memo):
            def stream():
                yield snapshots[0]
                raise OSError('interrupted capture')
            return stream()
    with pytest.raises(OSError):
        run(h, Interrupted(), store, frozen, leases, tmp_path)
    assert not store._page('assignment!')
    run(h, snapshots, store, frozen, leases, tmp_path)
    assert comparable(assignment_snapshot(h)) == expected


def test_flush_metrics_measure_mount_sync_separately_from_local_commit(monkeypatch):
    from types import SimpleNamespace
    import processing_metrics as metrics
    import wave_assignment_files as files
    journal = object.__new__(files.FileAssignmentJournal)
    calls = []
    journal.file = SimpleNamespace(flush=lambda: calls.append('flush'), fileno=lambda: 1)
    journal.db = SimpleNamespace(commit=lambda: calls.append('commit'))
    monkeypatch.setattr(files.os, 'fsync', lambda descriptor: calls.append('fsync'))
    with metrics.accounting() as counter:
        journal._flush()
    values = counter.snapshot()
    assert calls == ['flush', 'fsync', 'commit']
    assert values['journal_flushes'] == values['journal_fsyncs'] == 1
    assert values['journal_flush_ms'] >= values['journal_fsync_ms'] >= 0


@pytest.mark.parametrize('local', [True, False])
def test_bounded_slices_resume_without_reapplying_or_starving_on_replay(tmp_path, monkeypatch, local):
    expected = oracle()
    h, snapshots, store, frozen, leases = setup()
    original = wave._apply
    applied = []
    def apply(*args, **kwargs):
        face = args[2]
        assert face['RowKey'] not in applied
        result = original(*args, **kwargs)
        applied.append(face['RowKey'])
        return result
    monkeypatch.setattr(wave, '_apply', apply)
    # Even an already-expired budget must make new durable progress after
    # replaying the previous prefix. The final slice completes normally.
    for _ in range(len(snapshots) + 1):
        before = len(applied)
        try:
            receipt = run(h, snapshots, store, frozen, leases, tmp_path,
                local=local, page_size=1, should_yield=lambda: True)
        except wave.AssignmentYield:
            assert len(applied) == before + 1
            assert store._get(f'assignment!{frozen["waveId"]}!complete') is None
        else:
            assert len(receipt) == 64
            break
    else:
        pytest.fail('assignment did not finish across bounded slices')
    assert len(applied) == len(snapshots)
    assert comparable(assignment_snapshot(h)) == expected


def test_resume_snapshot_skips_prefix_preparation_build_and_replay(tmp_path, monkeypatch):
    import processing_metrics as metrics
    expected = oracle()
    h, snapshots, store, frozen, leases = setup()
    with pytest.raises(wave.AssignmentYield):
        run(h, snapshots, store, frozen, leases, tmp_path, page_size=1, should_yield=lambda: True)
    replayed = []
    original = wave._replay
    def replay(workspace, runtime, decision, guard):
        replayed.append(decision['faceId'])
        return original(workspace, runtime, decision, guard)
    monkeypatch.setattr(wave, '_replay', replay)
    monkeypatch.setattr(wave.WaveAssignmentJournal, 'prepare', lambda *a, **kw: pytest.fail('Recaptured snapshot'))
    monkeypatch.setattr(wave.LiveFaceIndex, 'build', lambda *a, **kw: pytest.fail('Rebuilt native index'))
    with metrics.accounting() as counter:
        run(h, snapshots, store, frozen, leases, tmp_path)
    assert len(replayed) == len(snapshots) - 1
    assert comparable(assignment_snapshot(h)) == expected
    assert counter.snapshot()['cluster_resume_hits'] == 1
    assert counter.snapshot()['cluster_resume_misses'] == 0
    assert counter.snapshot()['cluster_faces_replayed'] == 0
    assert counter.snapshot()['cluster_faces_newly_resolved'] == len(snapshots) - 1


@pytest.mark.parametrize('damage', ['workspace', 'runtime', 'missing'])
def test_optional_resume_snapshot_damage_falls_back_to_journal(tmp_path, damage):
    expected = oracle()
    h, snapshots, store, frozen, leases = setup()
    with pytest.raises(wave.AssignmentYield):
        run(h, snapshots, store, frozen, leases, tmp_path, page_size=1, should_yield=lambda: True)
    path, = (tmp_path / 'mount').rglob('workspace.snapshot')
    if damage == 'workspace':path.write_bytes(b'invalid snapshot')
    elif damage == 'missing':path.unlink()
    else:
        pointer, = (tmp_path / 'mount').rglob('CURRENT')
        pointer.write_text('{}')
    run(h, snapshots, store, frozen, leases, tmp_path)
    assert comparable(assignment_snapshot(h)) == expected


def test_interrupted_resume_marker_publication_replays_authoritative_journal(tmp_path, monkeypatch):
    import wave_assignment_files as files
    expected = oracle()
    h, snapshots, store, frozen, leases = setup()
    original = files.FileAssignmentJournal.put
    def put(self, key, payload):
        if key.startswith('resume!'):raise OSError('snapshot marker not published')
        return original(self, key, payload)
    with monkeypatch.context() as patch:
        patch.setattr(files.FileAssignmentJournal, 'put', put)
        with pytest.raises(OSError, match='not published'):
            run(h, snapshots, store, frozen, leases, tmp_path, page_size=1, should_yield=lambda: True)
    run(h, snapshots, store, frozen, leases, tmp_path)
    assert comparable(assignment_snapshot(h)) == expected


@pytest.mark.parametrize('edit', ['curation', 'source'])
def test_resume_preserves_curation_and_rejects_source_replacement(tmp_path, edit):
    from clustering_wave import canonical_sort_key
    h,snapshots,store,frozen,leases=setup()
    ordered=sorted(snapshots,key=lambda row:canonical_sort_key(row['RowKey'],row))
    with pytest.raises(wave.AssignmentYield):
        run(h,snapshots,store,frozen,leases,tmp_path,page_size=1,should_yield=lambda:True)
    face_id=ordered[1]['RowKey']
    live=h.faces.rows['u',face_id]
    if edit=='curation':live.update(reviewStatus='confirmed',personId='alice',rejected=False)
    else:live['processingRevision']='replacement'
    h.faces.versions['u',face_id]+=1
    if edit=='source':
        with pytest.raises(ValueError,match='source or curation changed'):
            run(h,snapshots,store,frozen,leases,tmp_path)
        assert live['processingRevision']=='replacement'
    else:
        run(h,snapshots,store,frozen,leases,tmp_path)
        assert h.faces.rows['u',face_id]['personId']=='alice'
        assert h.faces.rows['u',face_id]['reviewStatus']=='confirmed'


def test_resume_does_not_swallow_guard_io_failure_after_loading_runtime(tmp_path,monkeypatch):
    import wave_assignment_files as files
    h,snapshots,store,frozen,leases=setup()
    with pytest.raises(wave.AssignmentYield):
        run(h,snapshots,store,frozen,leases,tmp_path,page_size=1,should_yield=lambda:True)
    original=files.FileAssignmentJournal.__init__
    def init(self,*args,**kwargs):
        original(self,*args,**kwargs)
        def unavailable():raise FileNotFoundError('guard storage disappeared')
        self.check=unavailable
    monkeypatch.setattr(files.FileAssignmentJournal,'__init__',init)
    with pytest.raises(FileNotFoundError,match='guard storage disappeared'):
        run(h,snapshots,store,frozen,leases,tmp_path)


def test_failed_new_resume_publication_invalidates_old_snapshot_safely(tmp_path,monkeypatch):
    import wave_assignment_files as files
    h,snapshots,store,frozen,leases=setup()
    expected=oracle()
    with pytest.raises(wave.AssignmentYield):
        run(h,snapshots,store,frozen,leases,tmp_path,page_size=1,should_yield=lambda:True)
    original=files.FileAssignmentJournal.put
    def put(self,key,payload):
        if key.startswith('resume!'):raise OSError('new snapshot marker interrupted')
        return original(self,key,payload)
    with monkeypatch.context() as patch:
        patch.setattr(files.FileAssignmentJournal,'put',put)
        with pytest.raises(OSError,match='marker interrupted'):
            run(h,snapshots,store,frozen,leases,tmp_path,page_size=1,should_yield=lambda:True)
    run(h,snapshots,store,frozen,leases,tmp_path)
    assert comparable(assignment_snapshot(h))==expected


@pytest.mark.parametrize('wave_owned', [True, False])
@pytest.mark.parametrize('lost_reply', [False, True])
def test_wave_materialization_keeps_coverage_without_leaking_source_scope(
        tmp_path, monkeypatch, wave_owned, lost_reply):
    import app
    import automatic_index_updates as automatic
    import storage_utils as su
    from tests.test_automatic_index_updates import Table
    from wave_policy import source_change_active

    expected = oracle()
    h, snapshots, store, frozen, leases = setup()
    dirty = Table()
    monkeypatch.setattr(su, 'DURABLE_INDEX_UPDATES', True)
    monkeypatch.setattr(su, '_CTX', {'search_index_dirty_table_client': dirty,
                                   'wave_owned': lambda _: wave_owned})
    monkeypatch.setattr(app, 'jobs_table_client', Table())
    scheduled = []
    monkeypatch.setattr(automatic, 'schedule',
                        lambda *args: scheduled.append(source_change_active()))
    coverage = automatic.CoverageStore(dirty)
    writes = []
    def observe(row):
        assert source_change_active()
        assert coverage.control('u', 'people_embedding')['active'] == 1
        writes.append(row['RowKey'])
    h.faces.after_write = h.people.after_write = observe
    h.assigner.face_table = automatic.SourceTable(h.faces, 'face')
    h.assigner.person_table = automatic.SourceTable(h.people, 'person')

    assert not source_change_active()
    if lost_reply:
        h.faces.fail = OSError('lost face write reply')
        with pytest.raises(OSError, match='lost face write reply'):
            run(h, snapshots, store, frozen, leases, tmp_path)
        assert not source_change_active()
        assert coverage.control('u', 'people_embedding')['active'] == 0
    run(h, snapshots, store, frozen, leases, tmp_path)
    assert comparable(assignment_snapshot(h)) == expected
    assert writes
    assert not source_change_active()
    assert coverage.control('u', 'people_embedding')['active'] == 0
    assert not coverage.reservations('u', 'people_embedding')
    assert scheduled == ([] if wave_owned else [True] * len(scheduled))
    if not wave_owned:
        assert scheduled

    # A subsequent ordinary foreground mutation must still schedule coverage.
    h.people.after_write = None
    scheduled.clear()
    h.assigner.person_table.upsert_entity(dict(PartitionKey='u', RowKey='manual', name='Manual'))
    assert scheduled == [False, False]
    assert coverage.control('u', 'people_embedding')['active'] == 0


def test_fallback_replay_only_checkpoints_newly_resolved_pages(tmp_path):
    import processing_metrics as metrics
    h, snapshots, store, frozen, leases = setup()
    initial = []
    with pytest.raises(wave.AssignmentYield):
        run(h, snapshots, store, frozen, leases, tmp_path, page_size=1,
            should_yield=lambda: True, progress=initial.append)
    assert initial == [1]
    # Lose only the optional acceleration snapshot, not the assignment journal.
    path, = (tmp_path / 'mount').rglob('workspace.snapshot')
    path.unlink()
    checkpoints = []
    with metrics.accounting() as counter:
        run(h, snapshots, store, frozen, leases, tmp_path, page_size=1,
            progress=checkpoints.append)
    assert checkpoints == list(range(2, len(snapshots) + 1))
    assert counter.snapshot()['cluster_faces_replayed'] == 1
    assert counter.snapshot()['cluster_faces_newly_resolved'] == len(snapshots) - 1
    assert counter.snapshot()['cluster_resume_misses'] == 1
    # A completed duplicate delivery neither rewinds progress nor resolves again.
    checkpoints.clear()
    with metrics.accounting() as counter:
        run(h, snapshots, store, frozen, leases, tmp_path, progress=checkpoints.append)
    assert not checkpoints
    assert counter.snapshot()['cluster_faces_newly_resolved'] == 0
    assert counter.snapshot()['cluster_faces_replayed'] == len(snapshots)
