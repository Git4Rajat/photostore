import pytest

from index_work import WorkStore
from upload_waves import ShadowWaves
from upload_wave_execution import STAGE_PARTITION, advance_wave, dispatch_outbox, execution_enabled, stage_work_store, sweep_library
from tests.test_index_work import EtagTable
from tests.test_upload_waves import AtomicTable, close, finish_scan


@pytest.fixture
def world():
    table = AtomicTable()
    work_table = EtagTable()
    return ShadowWaves(table, 'library-A'), stage_work_store(work_table), table, work_table



def complete_publication(store, wave_id):
    from wave_publication import ObligationLedger, compute_obligations
    wave = store._required(store.wave_key(wave_id))
    ledger = ObligationLedger(EtagTable(), wave_id, library=store.library,
                              frozen_revision=wave['frozenRevision'])
    ids = compute_obligations(store.library, wave_id, wave['frozenRevision'], ['people'])
    frontier = ledger.record_required(ids)
    ledger.record_receipt(ids['people'], output_generation='published-people-v1')
    # Simulate the future stage owner's transition after assignments and before
    # publication; the completion API itself cannot turn a ready wave complete.
    store._commit(store._required('control'), [store._change(wave,
        dict(wave, state='indexing', requiredPublicationId=frontier))])
    return store.complete_indexing(wave_id, ledger)

def test_execution_disabled_by_default(monkeypatch):
    monkeypatch.delenv('UPLOAD_WAVE_EXECUTION_ENABLED', raising=False)
    assert execution_enabled() is False


def test_dispatch_outbox_is_a_noop_while_disabled(world, monkeypatch):
    store, work, table, work_table = world
    monkeypatch.delenv('UPLOAD_WAVE_EXECUTION_ENABLED', raising=False)
    row = store.open_session('p', 0)
    close(store, 'p', row)
    vision_key = store.status(row['waveId'])['visionPendingKey']
    assert dispatch_outbox(work, store, 'library-A', vision_key, 'vision') is False
    assert not work_table.rows
    assert store._get(vision_key)['dispatchable'] is False


def test_dispatch_outbox_requests_work_and_marks_dispatchable(world, monkeypatch):
    store, work, table, work_table = world
    monkeypatch.setenv('UPLOAD_WAVE_EXECUTION_ENABLED', 'true')
    row = store.open_session('p', 0)
    close(store, 'p', row)
    vision_key = store.status(row['waveId'])['visionPendingKey']
    assert dispatch_outbox(work, store, 'library-A', vision_key, 'vision') is True
    assert store._get(vision_key)['dispatchable'] is True
    from upload_waves import digest
    key = WorkStore.key('library-A', f"wave-vision:{row['waveId']}:{digest(vision_key)}")
    requested = work_table.rows[(STAGE_PARTITION, key)]
    assert requested['libraryId'] == 'library-A' and requested['scope'].startswith(f"wave-vision:{row['waveId']}:")


def test_dispatch_outbox_is_idempotent_and_does_not_bump_revision(world, monkeypatch):
    store, work, table, work_table = world
    monkeypatch.setenv('UPLOAD_WAVE_EXECUTION_ENABLED', 'true')
    row = store.open_session('p', 0)
    close(store, 'p', row)
    vision_key = store.status(row['waveId'])['visionPendingKey']
    assert dispatch_outbox(work, store, 'library-A', vision_key, 'vision') is True
    from upload_waves import digest
    key = WorkStore.key('library-A', f"wave-vision:{row['waveId']}:{digest(vision_key)}")
    first_revision = work_table.rows[(STAGE_PARTITION, key)]['revision']
    # Already dispatched: reports nothing new happened, and never touches
    # the durable request again.
    assert dispatch_outbox(work, store, 'library-A', vision_key, 'vision') is False
    assert work_table.rows[(STAGE_PARTITION, key)]['revision'] == first_revision


def test_dispatch_outbox_returns_false_for_a_missing_outbox(world, monkeypatch):
    store, work, table, work_table = world
    monkeypatch.setenv('UPLOAD_WAVE_EXECUTION_ENABLED', 'true')
    assert dispatch_outbox(work, store, 'library-A', 'shadow-cluster!no-such-wave', 'cluster') is False
    assert not work_table.rows


def test_advance_wave_dispatches_vision_once_processing_pending(world, monkeypatch):
    store, work, table, work_table = world
    monkeypatch.setenv('UPLOAD_WAVE_EXECUTION_ENABLED', 'true')
    row = store.open_session('p', 0)
    close(store, 'p', row)
    wave, dispatched = advance_wave(work, store, 'library-A', row['waveId'])
    assert wave['state'] == 'processing_pending' and dispatched == ['vision']


def test_advance_wave_dispatches_cluster_once_ready(world, monkeypatch):
    store, work, table, work_table = world
    monkeypatch.setenv('UPLOAD_WAVE_EXECUTION_ENABLED', 'true')
    row = store.open_session('p', 0)
    close(store, 'p', row)
    finish_scan(store, row['waveId'])
    wave, dispatched = advance_wave(work, store, 'library-A', row['waveId'])
    assert wave['state'] == 'ready' and dispatched == ['cluster']


def test_advance_wave_promotes_a_waiting_successor_once_predecessor_publication_is_complete(world, monkeypatch):
    store, work, table, work_table = world
    monkeypatch.setenv('UPLOAD_WAVE_EXECUTION_ENABLED', 'true')
    predecessor = store.open_session('p', 0)
    close(store, 'p', predecessor)
    finish_scan(store, predecessor['waveId'])
    complete_publication(store, predecessor['waveId'])

    successor = store.open_session('s', 0)
    close(store, 's', successor)
    assert store.evaluate(successor['waveId'])['state'] == 'waiting_predecessor'

    wave, dispatched = advance_wave(work, store, 'library-A', successor['waveId'])
    assert wave['state'] == 'processing_pending' and dispatched == ['vision']


def test_advance_wave_leaves_a_waiting_successor_alone_while_predecessor_is_not_ready(world, monkeypatch):
    """Ready is a clustering barrier, not indexing completion."""
    store, work, table, work_table = world
    monkeypatch.setenv('UPLOAD_WAVE_EXECUTION_ENABLED', 'true')
    predecessor = store.open_session('p', 0)
    close(store, 'p', predecessor)
    finish_scan(store, predecessor['waveId'])

    successor = store.open_session('s', 0)
    close(store, 's', successor)
    assert store.evaluate(successor['waveId'])['state'] == 'waiting_predecessor'


    wave, dispatched = advance_wave(work, store, 'library-A', successor['waveId'])
    assert wave['state'] == 'waiting_predecessor' and dispatched == []


def test_promotion_happens_even_while_dispatch_is_disabled(world, monkeypatch):
    """Promotion is a pure protocol transition, independent of real-dispatch
    gating -- only the WorkStore request/dispatchable flip is opt-in."""
    store, work, table, work_table = world
    monkeypatch.delenv('UPLOAD_WAVE_EXECUTION_ENABLED', raising=False)
    predecessor = store.open_session('p', 0)
    close(store, 'p', predecessor)
    finish_scan(store, predecessor['waveId'])
    complete_publication(store, predecessor['waveId'])
    successor = store.open_session('s', 0)
    close(store, 's', successor)
    store.evaluate(successor['waveId'])

    wave, dispatched = advance_wave(work, store, 'library-A', successor['waveId'])
    assert wave['state'] == 'processing_pending' and dispatched == []
    assert not work_table.rows


def test_sweep_library_advances_every_wave_it_finds(world, monkeypatch):
    store, work, table, work_table = world
    monkeypatch.setenv('UPLOAD_WAVE_EXECUTION_ENABLED', 'true')
    first = store.open_session('a', 0)
    close(store, 'a', first)
    finish_scan(store, first['waveId'])
    second = store.open_session('b', 0)
    close(store, 'b', second)

    touched = sweep_library(work, store, 'library-A')
    states = {entry['waveId']: entry['state'] for entry in touched}
    assert states[first['waveId']] == 'ready'
    assert second['waveId'] not in states
    assert store.status(second['waveId'])['state'] == 'waiting_predecessor'
    assert set(entry['waveId'] for entry in touched if entry['dispatched']) == {first['waveId']}


def test_sweep_library_is_idempotent_on_repeated_calls(world, monkeypatch):
    store, work, table, work_table = world
    monkeypatch.setenv('UPLOAD_WAVE_EXECUTION_ENABLED', 'true')
    row = store.open_session('p', 0)
    close(store, 'p', row)
    first_pass = sweep_library(work, store, 'library-A')
    assert first_pass
    second_pass = sweep_library(work, store, 'library-A')
    assert second_pass == []


def test_indexer_recovery_cannot_see_wave_stage_intents():
    table = EtagTable()
    stage = stage_work_store(table)
    stage.request('library-A', 'wave-vision:wave-1:release-1')
    index = WorkStore(table)
    sent = []
    assert index.recover(lambda *args: sent.append(args)) == 0
    assert not sent
    index.request('library-A', 'people')
    assert index.recover(lambda *args: sent.append(args)) == 1
    assert sent[0][1] == 'people'
    claim = stage.claim(stage.key('library-A', 'wave-vision:wave-1:release-1'))
    stage.renew(claim)
    assert not stage.finish(claim)
    assert index._get(index.key('library-A', 'people')) is not None


def test_wave_dispatch_rejects_indexer_work_store_and_wrong_identity(world, monkeypatch):
    store, work, table, work_table = world
    monkeypatch.setenv('UPLOAD_WAVE_EXECUTION_ENABLED', 'true')
    row = store.open_session('p', 0)
    close(store, 'p', row)
    key = store.status(row['waveId'])['visionPendingKey']
    with pytest.raises(ValueError, match='isolated'):
        dispatch_outbox(WorkStore(work_table), store, 'library-A', key, 'vision')
    with pytest.raises(ValueError, match='library identity'):
        dispatch_outbox(work, store, 'library-B', key, 'vision')
    with pytest.raises(ValueError, match='destination'):
        dispatch_outbox(work, store, 'library-A', key, 'cluster')
    assert not work_table.rows


def test_reopening_intake_invalidates_old_release_and_allocates_new_intent(world, monkeypatch):
    store, work, _, work_table = world
    monkeypatch.setenv('UPLOAD_WAVE_EXECUTION_ENABLED', 'true')
    parent = store.open_session('p', 0)
    close(store, 'p', parent)
    old_key = store.status(parent['waveId'])['visionPendingKey']
    later = store.open_session('later', 0)
    assert not dispatch_outbox(work, store, 'library-A', old_key, 'vision')
    close(store, 'later', later)
    new_key = store.status(parent['waveId'])['visionPendingKey']
    assert old_key != new_key
    assert dispatch_outbox(work, store, 'library-A', new_key, 'vision')
    assert len(work_table.rows) == 1


def test_reopen_racing_intent_preparation_cannot_mark_obsolete_outbox_dispatchable(world, monkeypatch):
    from upload_waves import Conflict
    store, work, table, work_table = world
    monkeypatch.setenv('UPLOAD_WAVE_EXECUTION_ENABLED', 'true')
    parent = store.open_session('p', 0)
    close(store, 'p', parent)
    old_key = store.status(parent['waveId'])['visionPendingKey']
    request = work.request
    def reopen(*args, **kwargs):
        result = request(*args, **kwargs)
        store.open_session('racing', 0)
        return result
    monkeypatch.setattr(work, 'request', reopen)
    with pytest.raises(Conflict, match='no longer eligible'):
        dispatch_outbox(work, store, 'library-A', old_key, 'vision')
    assert not store._get(old_key)['dispatchable']
    # A stranded preparation is isolated: index recovery still cannot wake it.
    assert WorkStore(work_table).recover(lambda *a: pytest.fail('index recovery woke Vision')) == 0


def test_publication_completion_and_receipt_are_atomic_and_replayable(world):
    from azure.core.exceptions import AzureError
    from wave_publication import ObligationLedger
    store, _, table, _ = world
    parent = store.open_session('p', 0)
    close(store, 'p', parent)
    finish_scan(store, parent['waveId'])
    wave = store._required(store.wave_key(parent['waveId']))
    ledger = ObligationLedger(EtagTable(), wave['waveId'], library=store.library,
                              frozen_revision=wave['frozenRevision'])
    frontier = ledger.record_required({})
    store._commit(store._required('control'), [store._change(wave,
        dict(wave, state='indexing', requiredPublicationId=frontier))])
    def crash():
        raise AzureError('before completion commit')
    table.before_commit = crash
    with pytest.raises(AzureError):
        store.complete_indexing(wave['waveId'], ledger)
    assert store.status(wave['waveId'])['state'] == 'indexing'
    assert store._get('completion!' + wave['waveId']) is None
    result = store.complete_indexing(wave['waveId'], ledger)
    assert result['state'] == 'complete'
    assert store.complete_indexing(wave['waveId'], ledger)['completionReceiptId'] == result['completionReceiptId']


def test_ready_or_wrong_frontier_cannot_be_declared_complete(world):
    from upload_waves import Conflict
    from wave_publication import ObligationLedger
    store, _, _, _ = world
    parent = store.open_session('p', 0)
    close(store, 'p', parent)
    finish_scan(store, parent['waveId'])
    wave = store._required(store.wave_key(parent['waveId']))
    ledger = ObligationLedger(EtagTable(), wave['waveId'], library=store.library,
                              frozen_revision=wave['frozenRevision'])
    frontier = ledger.record_required({})
    with pytest.raises(Conflict, match='frontier'):
        store.complete_indexing(wave['waveId'], ledger)
    store._commit(store._required('control'), [store._change(wave, dict(wave, requiredPublicationId=frontier))])
    with pytest.raises(Conflict, match='Only the indexing'):
        store.complete_indexing(wave['waveId'], ledger)


def test_successor_does_not_promote_from_a_complete_flag_without_coverage_receipt(world):
    store, work, table, _ = world
    parent = store.open_session('p', 0)
    close(store, 'p', parent)
    finish_scan(store, parent['waveId'])
    successor = store.open_session('s', 0)
    close(store, 's', successor)
    table.rows[store.partition, store.wave_key(parent['waveId'])]['state'] = 'complete'
    assert store.promote_successor(successor['waveId'])['state'] == 'waiting_predecessor'
