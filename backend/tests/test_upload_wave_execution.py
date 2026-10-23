import pytest

from index_work import WorkStore
from upload_waves import ShadowWaves
from upload_wave_execution import advance_wave, dispatch_outbox, execution_enabled, sweep_library
from tests.test_index_work import EtagTable
from tests.test_upload_waves import AtomicTable, close, finish_scan


@pytest.fixture
def world():
    table = AtomicTable()
    work_table = EtagTable()
    return ShadowWaves(table, 'library-A'), WorkStore(work_table), table, work_table


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
    key = WorkStore.key('library-A', f"wave-vision:{row['waveId']}")
    requested = work_table.rows[('index-maintenance-pending', key)]
    assert requested['libraryId'] == 'library-A' and requested['scope'] == f"wave-vision:{row['waveId']}"


def test_dispatch_outbox_is_idempotent_and_does_not_bump_revision(world, monkeypatch):
    store, work, table, work_table = world
    monkeypatch.setenv('UPLOAD_WAVE_EXECUTION_ENABLED', 'true')
    row = store.open_session('p', 0)
    close(store, 'p', row)
    vision_key = store.status(row['waveId'])['visionPendingKey']
    assert dispatch_outbox(work, store, 'library-A', vision_key, 'vision') is True
    key = WorkStore.key('library-A', f"wave-vision:{row['waveId']}")
    first_revision = work_table.rows[('index-maintenance-pending', key)]['revision']
    # Already dispatched: reports nothing new happened, and never touches
    # the durable request again.
    assert dispatch_outbox(work, store, 'library-A', vision_key, 'vision') is False
    assert work_table.rows[('index-maintenance-pending', key)]['revision'] == first_revision


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


def test_advance_wave_promotes_a_waiting_successor_once_predecessor_is_ready(world, monkeypatch):
    store, work, table, work_table = world
    monkeypatch.setenv('UPLOAD_WAVE_EXECUTION_ENABLED', 'true')
    predecessor = store.open_session('p', 0)
    close(store, 'p', predecessor)
    finish_scan(store, predecessor['waveId'])

    successor = store.open_session('s', 0)
    close(store, 's', successor)
    assert store.evaluate(successor['waveId'])['state'] == 'waiting_predecessor'

    wave, dispatched = advance_wave(work, store, 'library-A', successor['waveId'])
    assert wave['state'] == 'processing_pending' and dispatched == ['vision']


def test_advance_wave_leaves_a_waiting_successor_alone_while_predecessor_is_not_ready(world, monkeypatch):
    """A successor can only be admitted once its predecessor has already
    reached 'ready' (open_session reuses the still-collecting wave
    otherwise), so this forces the predecessor back to a non-ready state
    the way a future extra wave state (step 5/6's assignments_committed,
    indexing) would -- not reachable via the public API in this shadow
    module today, but promote_successor's guard must hold regardless."""
    store, work, table, work_table = world
    monkeypatch.setenv('UPLOAD_WAVE_EXECUTION_ENABLED', 'true')
    predecessor = store.open_session('p', 0)
    close(store, 'p', predecessor)
    finish_scan(store, predecessor['waveId'])

    successor = store.open_session('s', 0)
    close(store, 's', successor)
    assert store.evaluate(successor['waveId'])['state'] == 'waiting_predecessor'

    table.rows[store.partition, store.wave_key(predecessor['waveId'])]['state'] = 'processing'

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
    assert states[first['waveId']] == 'ready' and states[second['waveId']] == 'processing_pending'
    assert set(entry['waveId'] for entry in touched if entry['dispatched']) == {first['waveId'], second['waveId']}


def test_sweep_library_is_idempotent_on_repeated_calls(world, monkeypatch):
    store, work, table, work_table = world
    monkeypatch.setenv('UPLOAD_WAVE_EXECUTION_ENABLED', 'true')
    row = store.open_session('p', 0)
    close(store, 'p', row)
    first_pass = sweep_library(work, store, 'library-A')
    assert first_pass
    second_pass = sweep_library(work, store, 'library-A')
    assert second_pass == []
