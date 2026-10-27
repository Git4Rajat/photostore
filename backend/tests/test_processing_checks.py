from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context

import pytest

from processing_checks import CoalescedCheck
from processing_cleanup import check_source_policy, source_policy_scope, source_policy_boundary
from test_wave_activation import active
from test_wave_recovery import RuntimeTable


def test_check_coalesces_refreshes_and_forces_boundaries():
    now, reads = [0.0], []
    guard = CoalescedCheck(lambda: reads.append(now[0]), clock=lambda: now[0])
    for _ in range(1000): guard()
    assert reads == [0.0]
    now[0] = 1.0
    guard()
    guard(force=True)
    assert reads == [0.0, 1.0, 1.0]
    assert guard.metrics()['cached_checks'] == 999


def test_slow_refresh_cannot_extend_cache_deadline():
    now, reads = [0.0], []
    def read():
        reads.append(now[0])
        now[0] += 2.0
    guard = CoalescedCheck(read, clock=lambda: now[0])
    guard(); guard()
    assert reads == [0.0, 2.0]


def test_forced_failure_retires_previous_approval():
    fail = [False]
    def read():
        if fail[0]: raise RuntimeError('cleanup active')
    guard = CoalescedCheck(read, clock=lambda: 0.0)
    guard()
    fail[0] = True
    with pytest.raises(RuntimeError, match='cleanup'): guard(force=True)
    fail[0] = False
    with pytest.raises(RuntimeError, match='cleanup'): guard()


def test_local_lease_failure_is_observed_even_with_fresh_cache():
    valid, reads = [True], []
    def local():
        if not valid[0]: raise RuntimeError('lease expired')
    guard = CoalescedCheck(lambda: reads.append(1), local=local, clock=lambda: 0.0)
    guard()
    valid[0] = False
    with pytest.raises(RuntimeError, match='expired'): guard()
    assert len(reads) == 1


def test_policy_cache_is_shared_across_source_contexts_and_is_attempt_scoped(monkeypatch):
    table = RuntimeTable()
    active(table)
    read = table.get_entity
    reads = []
    def counted(*args, **kwargs):
        reads.append(args)
        return read(*args, **kwargs)
    monkeypatch.setattr(table, 'get_entity', counted)
    with source_policy_scope(table, 'u') as guard:
        guard.clock = lambda: 0.0
        guard.deadline = 1.0
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(copy_context().run, check_source_policy, table, 'u') for _ in range(100)]
            for future in futures: future.result()
        assert len(reads) == 1
        with source_policy_boundary(): check_source_policy(table, 'u')
        assert len(reads) == 2
        check_source_policy(table, 'other-library')
        assert len(reads) == 3
    check_source_policy(table, 'u')
    assert len(reads) == 4


def test_policy_boundary_detects_cleanup_inside_observation_window():
    table = RuntimeTable()
    store = active(table)
    with source_policy_scope(table, 'u'):
        row = table.get_entity(store.partition, 'policy')
        table.update_entity(dict(row, cleanupState='cleaning'), etag=row.metadata['etag'], match_condition='IfNotModified')
        with pytest.raises(RuntimeError, match='cleanup'):
            with source_policy_boundary(): check_source_policy(table, 'u')
        with pytest.raises(RuntimeError, match='cleanup'): check_source_policy(table, 'u')
