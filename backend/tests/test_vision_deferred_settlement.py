"""A drained page settles behind the next page's sources; nothing is lost or counted twice."""
import json
import threading
import time

import pytest

import upload_wave_execution as execution
from execution_lease import OwnershipLost
from tests.test_wave_source_batch import world, admit, run, publish_completion  # noqa: F401


def checkpoint_position(world, wave):
    store, _, leases, *_ = world
    saved = leases._get(leases.key(store.library, wave, 'vision'))['progressCheckpoint']
    return json.loads(saved[7:])


def members_of(store, wave):
    return store._page(f'e!{wave}!m!', size=100)


def trace_steps(monkeypatch, active):
    steps = []
    original = execution._PageSettlement.step
    def step(self):
        steps.append(active[0])
        return original(self)
    monkeypatch.setattr(execution._PageSettlement, 'step', step)
    return steps


def tracked_process(store, metadata, active, lock, calls):
    def process(library, filename, revision, renew):
        with lock:
            active[0] += 1
            calls.append(filename)
        try:
            time.sleep(0.01)
            publish_completion(store, metadata, filename=filename)
        finally:
            with lock:
                active[0] -= 1
    return process


def test_page_settles_while_the_next_pages_sources_are_running(world, monkeypatch):
    metadata, wave, key = admit(world, 45)
    store = world[0]
    active, lock, calls = [0], threading.Lock(), []
    steps = trace_steps(monkeypatch, active)
    result = run(world, metadata, wave, key, tracked_process(store, metadata, active, lock, calls),
                 source_workers=2, defer_settlement=True)
    assert result['state'] == 'ready'
    assert len(calls) == len(set(calls)) == 45
    assert any(running for running in steps), 'no member was settled while a source ran'
    assert all(m['outcome'] == 'settled' for m in members_of(store, wave))


def test_switch_off_settles_each_page_before_the_next_begins(world, monkeypatch):
    metadata, wave, key = admit(world, 45)
    store = world[0]
    active, lock, calls = [0], threading.Lock(), []
    steps = trace_steps(monkeypatch, active)
    run(world, metadata, wave, key, tracked_process(store, metadata, active, lock, calls),
        source_workers=2, defer_settlement=False)
    assert steps and not any(steps)
    assert len(calls) == len(set(calls)) == 45


def test_environment_switch_disables_deferral(world, monkeypatch):
    monkeypatch.setenv('IPWORK_VISION_DEFER_SETTLEMENT', 'false')
    metadata, wave, key = admit(world, 45)
    store = world[0]
    active, lock, calls = [0], threading.Lock(), []
    steps = trace_steps(monkeypatch, active)
    run(world, metadata, wave, key, tracked_process(store, metadata, active, lock, calls), source_workers=2)
    assert steps and not any(steps)


def test_a_page_is_committed_before_its_checkpoint_and_only_once(world, monkeypatch):
    import wave_source_batch
    metadata, wave, key = admit(world, 45)
    store, _, leases, *_ = world
    events = []
    commit = wave_source_batch.commit_observations
    def recording_commit(store_, wave_, observations):
        events.append(('commit', [member['RowKey'] for member, _, _ in observations]))
        return commit(store_, wave_, observations)
    monkeypatch.setattr(wave_source_batch, 'commit_observations', recording_commit)
    checkpoint = leases.checkpoint
    def recording_checkpoint(owner, value):
        events.append(('checkpoint', value))
        return checkpoint(owner, value)
    monkeypatch.setattr(leases, 'checkpoint', recording_checkpoint)
    def process(library, filename, revision, renew):
        publish_completion(store, metadata, filename=filename)
    run(world, metadata, wave, key, process, source_workers=2, defer_settlement=True)
    commits = [rows for kind, rows in events if kind == 'commit']
    assert [len(rows) for rows in commits] == [20, 20, 5]
    assert len({row for rows in commits for row in rows}) == 45  # No member settled in two commits.
    positions = [json.loads(value[7:]) for kind, value in events
                 if kind == 'checkpoint' and value.startswith('vision:') and '"sources"' in value]
    assert [position.get('done') for position in positions[:3]] == [20, 40, 45]
    # Each source checkpoint directly follows the commit of the page it covers.
    order = [kind for kind, _ in events if kind == 'commit' or '"sources"' in _]
    assert order[:6] == ['commit', 'checkpoint'] * 3


def test_deadline_flushes_the_page_awaiting_settlement(world, monkeypatch):
    metadata, wave, key = admit(world, 45)
    store = world[0]
    clock = [0.0]
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])
    calls = []
    def process(library, filename, revision, renew):
        calls.append(filename)
        publish_completion(store, metadata, filename=filename)
        clock[0] += 1
    run(world, metadata, wave, key, process, deadline=25, defer_settlement=True)
    first = len(calls)
    assert 20 < first < 45
    position = checkpoint_position(world, wave)
    assert position['done'] == first  # The deferred page was settled, committed and counted at the deadline.
    settled = [m for m in members_of(store, wave) if m['outcome'] == 'settled']
    assert len(settled) == first
    for _ in range(60):
        if store.status(wave)['state'] == 'ready':
            break
        run(world, metadata, wave, key, process, deadline=clock[0] + 25, defer_settlement=True)
    assert len(calls) == len(set(calls)) == 45
    assert store.status(wave)['state'] == 'ready'
    assert [row['count'] for row in store._page('source-attempt!', size=100)] == [1] * 45


def test_ownership_loss_with_a_page_awaiting_settlement_commits_nothing_more(world):
    metadata, wave, key = admit(world, 45)
    store, _, leases, *_ = world
    lock, calls = threading.Lock(), []
    def process(library, filename, revision, renew):
        with lock:
            calls.append(filename)
            lost = len(calls) == 21  # First source of the second page: page one waits for settlement.
        publish_completion(store, metadata, filename=filename)
        if lost:
            row = leases._get(leases.key(store.library, wave, 'vision'))
            leases._replace(row, dict(row, owner='replacement-owner'))
            renew(force=True)
    with pytest.raises(OwnershipLost):
        run(world, metadata, wave, key, process, source_workers=1, defer_settlement=True)
    assert len(calls) == 21
    assert not [m for m in members_of(store, wave) if m['outcome'] == 'settled']
    assert leases._get(leases.key(store.library, wave, 'vision'))['owner'] == 'replacement-owner'


def test_retry_after_crash_replays_unsettled_pages_without_new_model_runs(world, monkeypatch):
    import wave_source_batch
    metadata, wave, key = admit(world, 45)
    store = world[0]
    calls = []
    def process(library, filename, revision, renew):
        calls.append(filename)
        publish_completion(store, metadata, filename=filename)
    commit = wave_source_batch.commit_observations
    committed = []
    def crashing(store_, wave_, observations):
        if committed:
            raise RuntimeError('crash while settling the second page')
        committed.append(len(observations))
        return commit(store_, wave_, observations)
    monkeypatch.setattr(wave_source_batch, 'commit_observations', crashing)
    with pytest.raises(RuntimeError, match='crash while settling'):
        run(world, metadata, wave, key, process, source_workers=2, defer_settlement=True)
    assert committed == [20]
    first = len(calls)
    monkeypatch.setattr(wave_source_batch, 'commit_observations', commit)
    result = run(world, metadata, wave, key, process, source_workers=2, defer_settlement=True)
    assert result['state'] == 'ready'
    assert len(calls) == len(set(calls)) == 45
    assert len(calls) >= first
    assert [row['count'] for row in store._page('source-attempt!', size=100)] == [1] * 45
    assert all(m['outcome'] == 'settled' for m in members_of(store, wave))


def test_failure_between_pages_keeps_the_settled_page(world, monkeypatch):
    """Ownership intact: an error selecting the third page still commits the two pages that finished."""
    import wave_source_batch
    metadata, wave, key = admit(world, 45)
    store = world[0]
    original = wave_source_batch.InspectionBatch
    seen = []
    def inspect(store_, members, metadata_, receipts):
        seen.append(len(members))
        if len(seen) == 4:  # Selection of page three, after page two's sources.
            raise RuntimeError('page three selection failed')
        return original(store_, members, metadata_, receipts)
    monkeypatch.setattr(wave_source_batch, 'InspectionBatch', inspect)
    calls = []
    def process(library, filename, revision, renew):
        calls.append(filename)
        publish_completion(store, metadata, filename=filename)
    with pytest.raises(RuntimeError, match='page three selection'):
        run(world, metadata, wave, key, process, defer_settlement=True)
    settled = [m for m in members_of(store, wave) if m['outcome'] == 'settled']
    assert len(settled) == 40  # Page two finished its sources, so the error path still commits it.
    assert checkpoint_position(world, wave)['done'] == 40
