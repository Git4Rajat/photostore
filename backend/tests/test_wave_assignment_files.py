"""Local journal parity, partial writes, mount loss, and measured Table savings."""
from copy import deepcopy
from dataclasses import replace

import pytest
pytest.importorskip('faiss')

from execution_lease import execution_scope, OwnershipLost
import wave_assignment as wave
from test_wave_assignment_journal import setup, oracle, comparable
from clustering_reference_oracle import assignment_snapshot


def run(h, snapshots, store, frozen, leases, root, *, local=True, page_size=3):
    (root / 'work').mkdir(exist_ok=True)
    h.assigner.config = replace(h.assigner.config, checkpoint_dir=str(root / 'mount'), work_dir=str(root / 'work'))
    ownership = leases.claim('u', frozen['waveId'], 'cluster', frozen['frozenRevision'])
    def check():
        pass  # Native library lease remains checked by the runner.
    try:
        with execution_scope(leases, ownership, store, lambda: leases.check(ownership)):
            return wave.run_frozen_assignment(store, frozen, h.assigner,
                lambda: iter(deepcopy(snapshots)), check, local_journal=local, page_size=page_size)
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
