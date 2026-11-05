"""Page reservations retain live source fences and fail-closed certification."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context

import pytest
from azure.core import MatchConditions
from azure.data.tables import UpdateMode

import automatic_index_updates as automatic
import storage_utils as su
from tests.test_automatic_index_updates import runtime
from wave_policy import source_change, source_change_active


@pytest.fixture
def covered(runtime, monkeypatch):
    monkeypatch.setattr(su, '_CTX', {**su._CTX, 'wave_owned': lambda _: True})
    monkeypatch.setenv('WAVE_CLUSTER_COVERAGE_BATCH_ENABLED', 'true')
    monkeypatch.setattr(automatic, 'schedule', lambda *args: pytest.fail('Independent wave scheduling'))
    runtime.source.create_entity(dict(PartitionKey='A', RowKey='person', name='Original'))
    runtime.proxy = automatic.SourceTable(runtime.source, 'person')
    return runtime


@source_change
def update(runtime, key='person', user='A', **fields):
    old = runtime.source.get_entity(user, key)
    runtime.proxy.update_entity(dict(old, **fields), mode=UpdateMode.MERGE,
        etag=old.metadata['etag'], match_condition=MatchConditions.IfNotModified)


def scope(runtime, **kwargs):
    return automatic.wave_assignment_coverage('A', object(), runtime.proxy, lambda: None, **kwargs)


def test_one_page_invalidates_old_capture_and_covers_every_owner(covered):
    control = covered.store.ensure('A', 'people_embedding')
    covered.dirty.update_entity(dict(control, fullRequired=False, base='previously-certified'),
                                **automatic._cas(control))
    captured = covered.store.capture('A', 'people_embedding')
    assert not captured.control['fullRequired']
    with scope(covered):
        for i in range(128):
            update(covered, name=str(i))
            assert covered.store.control('A', 'people_embedding')['active'] == 1
        reservations = covered.store.reservations('A', 'people_embedding')
        assert len(reservations) == 1 and reservations[0]['idsCount'] == 0
        assert reservations[0]['operation'] == 'wave_assignment_page'
        with pytest.raises(automatic.SourceBusy):
            covered.store.capture('A', 'people_embedding')
        with pytest.raises(automatic.SourceBusy):
            covered.store.check('A', 'people_embedding', captured)
    final = covered.store.control('A', 'people_embedding')
    assert final['active'] == 0 and final['fullRequired']
    assert not covered.store.reservations('A', 'people_embedding')
    with pytest.raises(automatic.SourceBusy):
        covered.store.check('A', 'people_embedding', captured)
    assert not automatic._WAVE_ASSIGNMENT_COVERAGE.get()
    assert not source_change_active()


def test_read_only_or_replayed_page_never_begins_coverage(covered):
    with scope(covered):
        covered.proxy.get_entity('A', 'person')
    assert not covered.dirty.rows


def test_unknown_begin_response_leaves_reservation_without_source_effect(covered, monkeypatch):
    begin = automatic.CoverageStore.begin
    def lost(self, *args, **kwargs):
        begin(self, *args, **kwargs)
        raise OSError('begin committed, reply lost')
    monkeypatch.setattr(automatic.CoverageStore, 'begin', lost)
    with pytest.raises(OSError, match='reply lost'), scope(covered):
        update(covered, name='Must not commit')
    assert covered.source.get_entity('A', 'person')['name'] == 'Original'
    assert covered.store.control('A', 'people_embedding')['active'] == 1
    assert len(covered.store.reservations('A', 'people_embedding')) == 1
    with pytest.raises(automatic.SourceBusy):
        covered.store.capture('A', 'people_embedding')
    assert not automatic._WAVE_ASSIGNMENT_COVERAGE.get()


def test_admission_fence_during_begin_prevents_source_effect(covered, monkeypatch):
    begin = automatic.CoverageStore.begin
    def fence(self, *args, **kwargs):
        result = begin(self, *args, **kwargs)
        automatic.operator_fence('A', operator='test', reason='interrupted admission')
        return result
    monkeypatch.setattr(automatic.CoverageStore, 'begin', fence)
    with pytest.raises(automatic.SourceBusy), scope(covered):
        update(covered, name='Must not commit')
    assert covered.source.get_entity('A', 'person')['name'] == 'Original'
    assert covered.store.control('A', 'people_embedding')['active'] == 0


def test_admission_fence_between_writes_is_not_cached_for_the_page(covered):
    with pytest.raises(automatic.SourceBusy), scope(covered):
        update(covered, name='First write')
        automatic.operator_fence('A', operator='test', reason='stop subsequent writes')
        update(covered, name='Must not commit')
    assert covered.source.get_entity('A', 'person')['name'] == 'First write'
    assert covered.store.control('A', 'people_embedding')['active'] == 0


def test_source_failure_keeps_its_identity_when_finish_also_fails(covered, monkeypatch):
    source_error = ValueError('source operation failed')
    def failed(*args, **kwargs):
        raise OSError('finish unavailable')
    monkeypatch.setattr(automatic.CoverageStore, 'finish', failed)
    with pytest.raises(ValueError) as caught:
        with scope(covered):
            update(covered, name='Committed')
            raise source_error
    assert caught.value is source_error
    assert isinstance(source_error.__cause__, ExceptionGroup)
    assert covered.store.control('A', 'people_embedding')['active'] == 1
    assert not automatic._WAVE_ASSIGNMENT_COVERAGE.get()


def test_finish_lost_reply_is_reported_without_double_decrement(covered, monkeypatch):
    finish = automatic.CoverageStore.finish
    def lost(self, *args, **kwargs):
        finish(self, *args, **kwargs)
        raise OSError('finish reply lost')
    with monkeypatch.context() as patch:
        patch.setattr(automatic.CoverageStore, 'finish', lost)
        with pytest.raises(ExceptionGroup), scope(covered):
            update(covered, name='Committed')
    assert covered.store.control('A', 'people_embedding')['active'] == 0
    assert not covered.store.reservations('A', 'people_embedding')
    with scope(covered):
        update(covered, name='Next page')
    assert covered.store.control('A', 'people_embedding')['active'] == 0


def test_unrelated_library_and_unconditional_writes_keep_separate_tracking(covered):
    covered.source.create_entity(dict(PartitionKey='B', RowKey='person', name='Other'))
    with scope(covered):
        update(covered, name='Covered')
        update(covered, user='B', name='Other library')
        assert covered.store.control('B', 'people_embedding')['active'] == 0
        # No genuine CAS observation: must use the ordinary source protocol.
        with_source = source_change(lambda: covered.proxy.upsert_entity(
            dict(PartitionKey='A', RowKey='unconditional', name='Ordinary')))
        with_source()
        assert covered.store.control('A', 'people_embedding')['active'] == 1
    assert covered.store.control('A', 'people_embedding')['active'] == 0


@pytest.mark.parametrize('wave_writer', [True, False])
def test_paused_page_and_other_thread_do_not_share_reservations(covered, monkeypatch, wave_writer):
    scheduled = []
    monkeypatch.setattr(automatic, 'schedule', lambda *args: scheduled.append(args))
    with scope(covered):
        update(covered, name='Page')
        # An inherited mutable context on another thread must still fall back
        # to its own ordinary reservation, never join the paused page.
        context = copy_context()
        writer = update if wave_writer else update.__wrapped__
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(context.run, writer, covered, name='Independent writer').result()
        assert covered.store.control('A', 'people_embedding')['active'] == 1
        assert len(covered.store.reservations('A', 'people_embedding')) == 1
        with pytest.raises(automatic.SourceBusy):
            covered.store.capture('A', 'people_embedding')
    assert covered.store.control('A', 'people_embedding')['active'] == 0
    assert len(scheduled) == (0 if wave_writer else 2)


def test_page_write_limit_prevents_unbounded_reservation(covered):
    with pytest.raises(RuntimeError, match='write bound'), scope(covered, max_faces=1):
        update(covered, name='One')
        update(covered, name='Two')
        update(covered, name='Must not commit')
    assert covered.source.get_entity('A', 'person')['name'] == 'Two'
    assert covered.store.control('A', 'people_embedding')['active'] == 0


@pytest.mark.parametrize('size', [0, 129, True])
def test_invalid_page_size_cannot_create_coverage(covered, size):
    with pytest.raises(ValueError), scope(covered, max_faces=size):
        pytest.fail('Entered invalid page')
    assert not covered.dirty.rows
