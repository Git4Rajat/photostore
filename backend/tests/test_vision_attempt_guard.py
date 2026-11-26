"""Reserving source attempts reuses the stage's one-second eligibility observation.

Every admission used to read the policy, wave, dispatch and lease rows twice over (eligibility, then the
guard); now it reads the lease row once for its ETag and the policy row once for its own guard. Nothing that
fences a write is cached: ownership, the policy guard and the attempt counters are still conditional writes.
"""
import re
import threading
import time
from collections import Counter

import pytest
from azure.core.exceptions import ResourceNotFoundError

import upload_wave_execution as execution
import wave_source_batch
from execution_lease import OwnershipLost
from upload_waves import Conflict
from tests.test_wave_source_batch import world, admit, run, publish_completion  # noqa: F401

SWITCH = 'IPWORK_VISION_ATTEMPT_GUARD_COALESCE'


def count_reads(world, monkeypatch):
    table, reads = world[3], Counter()
    original = table.get_entity

    def get(partition, row_key, *args, **kwargs):
        reads[row_key.split('!', 1)[0]] += 1
        return original(partition, row_key, *args, **kwargs)
    monkeypatch.setattr(table, 'get_entity', get)
    return reads


def publisher(store, metadata, calls, lock=None):
    lock = lock or threading.Lock()

    def process(library, filename, revision, renew):
        with lock:
            calls.append(filename)
        time.sleep(0.002)
        publish_completion(store, metadata, filename=filename)
    return process


def attempts(store):
    return [row['count'] for row in store._page('source-attempt!', size=100)]


@pytest.mark.parametrize('enabled', ['true', 'false'])
def test_each_photo_is_attempted_once_and_the_wave_finishes(world, monkeypatch, enabled):
    monkeypatch.setenv(SWITCH, enabled)
    metadata, wave, key = admit(world, 45)
    store, calls = world[0], []
    result = run(world, metadata, wave, key, publisher(store, metadata, calls), source_workers=3)
    assert result['state'] == 'ready'
    assert len(calls) == len(set(calls)) == 45
    assert attempts(store) == [1] * 45


MEASURED = {}


@pytest.mark.parametrize('enabled', ['false', 'true'])
def test_benchmark_reads_per_photo(world, monkeypatch, enabled):
    """Offline benchmark, 45 photos, 3 source workers: row reads from the jobs table."""
    monkeypatch.setenv(SWITCH, enabled)
    metadata, wave, key = admit(world, 45)
    reads = count_reads(world, monkeypatch)
    run(world, metadata, wave, key, publisher(world[0], metadata, []), source_workers=3)
    MEASURED[enabled] = reads


def test_coalescing_cuts_the_wave_rows_read_per_photo(capsys):
    off, on = MEASURED['false'], MEASURED['true']
    with capsys.disabled():
        print(f'\nphotojobs reads per photo (45 photos): off={sum(off.values()) / 45:.2f} on={sum(on.values()) / 45:.2f}')
        print('  off', dict(off))
        print('  on ', dict(on))
    assert sum(on.values()) <= sum(off.values()) - 45  # At least one read per photo fewer.
    for row in ('policy', 'wave', 'shadow-vision', 'library-A'):
        assert on[row] < off[row]


@pytest.mark.parametrize('enabled', ['true', 'false'])
def test_deadline_restart_reserves_each_attempt_once(world, monkeypatch, enabled):
    monkeypatch.setenv(SWITCH, enabled)
    metadata, wave, key = admit(world, 45)
    store, clock, calls = world[0], [0.0], []
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])

    def process(library, filename, revision, renew):
        calls.append(filename)
        publish_completion(store, metadata, filename=filename)
        clock[0] += 1
    run(world, metadata, wave, key, process, deadline=25)
    assert 0 < len(calls) < 45
    for _ in range(60):
        if store.status(wave)['state'] == 'ready':
            break
        run(world, metadata, wave, key, process, deadline=clock[0] + 25)
    assert len(calls) == len(set(calls)) == 45
    assert attempts(store) == [1] * 45


@pytest.mark.parametrize('enabled', ['true', 'false'])
def test_ownership_lost_with_work_in_flight_reserves_nothing_more(world, monkeypatch, enabled):
    """The lease row is read fresh and guards the counters' transaction whatever the switch says."""
    monkeypatch.setenv(SWITCH, enabled)
    metadata, wave, key = admit(world, 45)
    store, _, leases, *_ = world
    lock, calls = threading.Lock(), []

    def process(library, filename, revision, renew):
        with lock:
            calls.append(filename)
            lost = len(calls) == 3
        publish_completion(store, metadata, filename=filename)
        if lost:  # Another worker takes the lease without this process's keepalive noticing yet.
            row = leases._get(leases.key(store.library, wave, 'vision'))
            leases._replace(row, dict(row, owner='replacement-owner'))
    with pytest.raises(OwnershipLost):
        run(world, metadata, wave, key, process, source_workers=2)
    reserved = len(attempts(store))
    assert reserved < 45
    assert len(calls) <= reserved
    assert leases._get(leases.key(store.library, wave, 'vision'))['owner'] == 'replacement-owner'
    time.sleep(0.05)
    assert len(attempts(store)) == reserved


@pytest.mark.parametrize('enabled', ['true', 'false'])
def test_retry_after_a_crash_does_not_recount_reserved_attempts(world, monkeypatch, enabled):
    monkeypatch.setenv(SWITCH, enabled)
    metadata, wave, key = admit(world, 45)
    store, calls = world[0], []
    process = publisher(store, metadata, calls)
    commit = wave_source_batch.commit_observations
    crashed = []

    def crashing(store_, wave_, observations):
        if not crashed:
            crashed.append(True)
            raise RuntimeError('crash while settling')
        return commit(store_, wave_, observations)
    monkeypatch.setattr(wave_source_batch, 'commit_observations', crashing)
    with pytest.raises(RuntimeError, match='crash while settling'):
        run(world, metadata, wave, key, process, source_workers=2)
    result = run(world, metadata, wave, key, process, source_workers=2)
    assert result['state'] == 'ready'
    assert max(attempts(store)) == 1 or len(calls) > 45  # A replay re-reserves only what it re-runs.
    assert set(calls) == {f'photo-{i}.jpg' for i in range(45)}


@pytest.mark.parametrize('enabled', ['true', 'false'])
def test_stale_policy_still_fences_reservations(world, monkeypatch, enabled):
    """The policy row is read inside the transaction that writes the counters, never from the observation."""
    monkeypatch.setenv(SWITCH, enabled)
    metadata, wave, key = admit(world, 45)
    store, calls = world[0], []
    process = publisher(store, metadata, calls)
    store.table.submit_transaction([('create', store._row('policy', protocol='shadow', cleanupState=''))])

    def process_then_clean(library, filename, revision, renew):
        process(library, filename, revision, renew)
        if len(calls) == 3:
            policy = store._get('policy')
            store.table.submit_transaction([store._change(policy, dict(policy, cleanupState='running'))])
    with pytest.raises((Conflict, OwnershipLost)):
        run(world, metadata, wave, key, process_then_clean, source_workers=1)
    assert len(calls) < 45


def test_a_failed_observation_is_not_reused_by_reservations(world, monkeypatch):
    """Once the shared observation has failed (wave cancelled, cleanup, ownership), reserving fails with it."""
    monkeypatch.setenv(SWITCH, 'true')
    metadata, wave, key = admit(world, 45)
    store, calls = world[0], []
    process = publisher(store, metadata, calls)
    original = execution._execution_check
    state = {'fail': False}

    def check(*args, **kwargs):
        if state['fail']:
            raise OwnershipLost('Stage release is no longer current')
        return original(*args, **kwargs)
    monkeypatch.setattr(execution, '_execution_check', check)

    def process_then_cancel(library, filename, revision, renew):
        process(library, filename, revision, renew)
        if len(calls) == 3:
            state['fail'] = True
    with pytest.raises(OwnershipLost):
        run(world, metadata, wave, key, process_then_cancel, source_workers=1)
    assert len(attempts(store)) < 45
