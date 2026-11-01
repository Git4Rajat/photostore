"""Quiet-library GC on the existing worker recovery sweep, without source scans."""
from contextlib import contextmanager
from datetime import timedelta
import hashlib
import json
import threading
from types import SimpleNamespace

import pytest

import app
import automatic_index_updates as automatic
import index_artifact_cleanup as gc
import index_work
import storage_utils as su
from tests.test_automatic_index_updates import Table
from tests.test_index_artifact_cleanup import NOW, OLD, USER, add, entity, env, setup_scope


@pytest.fixture
def recovery(env, monkeypatch):
    jobs = Table()
    monkeypatch.setattr(app, 'jobs_table_client', jobs)
    monkeypatch.setattr(app, 'search_index_dirty_table_client', env.dirty)
    monkeypatch.setattr(app, 'index_maintenance_queue_client', object())
    monkeypatch.setattr(app, '_INDEX_ARTIFACT_RECOVERY', threading.local())
    monkeypatch.setattr(su, 'DURABLE_INDEX_UPDATES', True)
    monkeypatch.setenv('INDEX_ARTIFACT_CLEANUP_LIBRARIES_PER_RECOVERY', '25')
    env.jobs = jobs
    env.clock = NOW
    env.scans, env.reports, env.sent = [], [], []
    work_init = index_work.WorkStore.__init__

    def clocked_work(self, *args, **kwargs):
        kwargs.setdefault('clock', lambda: env.clock.timestamp())
        work_init(self, *args, **kwargs)

    monkeypatch.setattr(index_work.WorkStore, '__init__', clocked_work)

    def controls(query, **kwargs):
        assert query == "RowKey eq '!coverage'" or query.startswith("RowKey eq '!coverage' and libraryId eq '")
        env.scans.append(query)
        # Stream the already paged recovery survey. No source table is queried.
        yield from env.dirty.rows.values()

    monkeypatch.setattr(env.dirty, 'query_entities', controls)
    monkeypatch.setattr(app, '_send_index_wakeup', lambda *a, **kw: env.sent.append(a))
    monkeypatch.setattr(gc, 'artifact_catalog_serialization',
                        lambda user, **kw: env.service.serialization(user))
    original = gc.cleanup_user_index_artifacts

    def cleanup(user, **kwargs):
        report = original(user, now=env.clock, **kwargs)
        env.reports.append((user, report))
        return report

    monkeypatch.setattr(gc, 'cleanup_user_index_artifacts', cleanup)
    # Any accidental full metadata scan is a failure, not an empty fake result.
    monkeypatch.setattr(app, 'metadata_table_client', SimpleNamespace(
        query_entities=lambda *a, **kw: pytest.fail('source metadata scan')))
    su._CTX['metadata_table_client'] = app.metadata_table_client
    # Other libraries need not have all GC manifests for admission-only tests.
    env.dirty.rows = {key: row for key, row in env.dirty.rows.items() if row['libraryId'] == USER}
    return env


def prepare_all(recovery, *, fresh=False):
    result = []
    for kind in gc.KINDS:
        scope, current, row = setup_scope(recovery, kind)
        orphan = add(recovery, scope, 1)
        previous = add(recovery, scope, 2, modified=OLD + timedelta(hours=1))
        if fresh:
            if kind == 'hash':
                row['Timestamp'] = NOW
            else:
                recovery.service.put(scope.container, scope.manifest, json.dumps(row).encode(), modified=NOW)
        result.append((scope, current, previous, orphan))
    return result


def certified(recovery, user, kind='hash'):
    row = entity(automatic.partition(user, kind), automatic.CONTROL,
        libraryId=user, kind=kind, coverageVersion=automatic.VERSION,
        revision='r', coveredRevision='r', active=0, fullRequired=False, base='base')
    recovery.dirty.rows[row['PartitionKey'], row['RowKey']] = row
    return row


def test_quiet_library_deferred_then_periodic_recovery_after_reader_grace(recovery):
    artifacts = prepare_all(recovery, fresh=True)
    # The successful worker's final cleanup happens inside reader grace.
    initial = app._cleanup_completed_index_artifacts(USER)
    assert initial['deleted'] == 0 and 'reader grace' in initial['reason']
    assert not recovery.jobs.rows  # no future job to drive cleanup
    recovery.clock = NOW + timedelta(hours=24, seconds=1)
    assert app.recover_pending_index_work() == 0
    assert len(recovery.scans) == 1
    assert recovery.reports[-1][0] == USER
    assert recovery.reports[-1][1]['status'] == 'complete'
    assert recovery.reports[-1][1]['deleted'] == len(gc.KINDS)
    assert not recovery.sent and not recovery.jobs.rows
    for scope, current, previous, orphan in artifacts:
        assert (scope.container, orphan) not in recovery.service.data
        assert (scope.container, current) in recovery.service.data
        assert (scope.container, previous) in recovery.service.data


@pytest.mark.parametrize('role', ['core', 'vision', 'cluster', 'recovery', 'upload', ''])
def test_unsafe_role_never_discovers_or_scans_gc(recovery, monkeypatch, role):
    prepare_all(recovery)
    monkeypatch.setenv('APP_ROLE', role)
    monkeypatch.setattr(app, '_cleanup_completed_index_artifacts',
                        lambda *a: pytest.fail('nonworker GC discovery'))
    assert app.recover_pending_index_work() == 0
    assert len(recovery.scans) == 1  # existing recovery scan, not an added GC scan
    assert not recovery.reports and not recovery.service.queries


def test_direct_cleanup_unsafe_role_is_noop_before_any_io(recovery, monkeypatch):
    monkeypatch.setenv('APP_ROLE', 'core')
    monkeypatch.setattr(automatic, '_quiescence_row', lambda *a: pytest.fail('nonworker I/O'))
    assert app._cleanup_completed_index_artifacts(USER) is None
    assert not recovery.service.queries


@pytest.mark.parametrize('field,value', [
    ('libraryId', 'other'), ('libraryId', ''), ('libraryId', ' padded '),
    ('libraryId', None), ('libraryId', 123), ('kind', 'unknown'), ('kind', None),
    ('PartitionKey', automatic.partition('other', 'hash')), ('RowKey', 'id-not-control'),
    ('coverageVersion', True), ('coverageVersion', 999),
])
@pytest.mark.parametrize('covered', [True, False])
def test_malformed_controls_never_cross_library_schedule_or_gc(recovery, monkeypatch, field, value, covered):
    row = recovery.dirty.rows[automatic.partition(USER, 'hash'), automatic.CONTROL]
    recovery.dirty.rows = {(row['PartitionKey'], row['RowKey']): row}
    if not covered:
        row['coveredRevision'] = 'old'
    row[field] = value
    monkeypatch.setattr(app, '_cleanup_completed_index_artifacts', lambda *a: pytest.fail('malformed GC'))
    assert app.recover_pending_index_work() == 0
    assert not recovery.jobs.rows and not recovery.sent


@pytest.mark.parametrize('field,value', [
    ('active', 1), ('active', False), ('active', '0'), ('fullRequired', True),
    ('fullRequired', None), ('revision', ''), ('base', ''),
])
def test_uncertified_hash_control_is_not_gc_candidate(recovery, monkeypatch, field, value):
    row = recovery.dirty.rows[automatic.partition(USER, 'hash'), automatic.CONTROL]
    row[field] = value
    monkeypatch.setattr(app, '_cleanup_completed_index_artifacts', lambda *a: pytest.fail('unsafe GC'))
    assert app.recover_pending_index_work() >= 0
    assert not recovery.service.deleted


@pytest.mark.parametrize('problem', ['source', 'lease', 'model-lease', 'quiescence', 'wave'])
def test_periodic_recovery_never_deletes_active_or_quiesced_library(recovery, monkeypatch, problem):
    prepare_all(recovery)
    if problem == 'source':
        recovery.dirty.rows[automatic.partition(USER, 'vector'), automatic.CONTROL]['active'] = 1
    elif problem in ('lease', 'model-lease'):
        partition = automatic.TAG_PARTITION if problem == 'model-lease' else index_work.PARTITION
        store = index_work.WorkStore(recovery.jobs, partition=partition)
        key = store.request(USER, 'tag_embedding' if problem == 'model-lease' else 'full')
        assert store.claim(key) is not None
    elif problem == 'quiescence':
        recovery.jobs.upsert_entity(dict(PartitionKey=automatic.QUIESCENCE_PARTITION,
            RowKey=hashlib.sha256(USER.encode()).hexdigest(), libraryId=USER))
    else:
        monkeypatch.setattr(automatic, 'wave_pending', lambda user: True)
    assert app.recover_pending_index_work() == 0
    assert not recovery.service.deleted
    if problem in ('quiescence', 'wave'):
        assert not recovery.reports and not recovery.service.queries
    else:
        assert recovery.reports[-1][1]['status'] == 'deferred'


def test_quiescence_rechecked_after_catalog_acquisition(recovery, monkeypatch):
    prepare_all(recovery)

    @contextmanager
    def fence_on_acquire(user, **kw):
        with recovery.service.serialization(user) as guard:
            recovery.jobs.upsert_entity(dict(PartitionKey=automatic.QUIESCENCE_PARTITION,
                RowKey=hashlib.sha256(user.encode()).hexdigest(), libraryId=user))
            yield guard

    monkeypatch.setattr(gc, 'artifact_catalog_serialization', fence_on_acquire)
    assert app.recover_pending_index_work() == 0
    assert 'operator-quiesced' in recovery.reports[-1][1]['reason']
    assert not recovery.service.queries and not recovery.service.deleted


def test_cap_rotates_fairly_across_unordered_controls_and_wraps(recovery, monkeypatch):
    recovery.dirty.rows.clear()
    users = [f'library-{i}' for i in range(63)]
    for user in reversed(users):
        for kind in automatic.KINDS:
            certified(recovery, user, kind)
    calls = []

    def deferred(user):
        calls.append(user)
        return dict(status='deferred')

    monkeypatch.setattr(app, '_cleanup_completed_index_artifacts', deferred)
    ordered = sorted(users, key=lambda user: automatic.partition(user, 'hash'))
    for sweep in range(4):
        assert app.recover_pending_index_work() == 0
        expected = [ordered[i % len(users)] for i in range(sweep * 25, (sweep + 1) * 25)]
        assert calls[sweep * 25:] == expected
    assert set(calls[:75]) == set(users)
    assert len(recovery.scans) == 4
    assert vars(app._INDEX_ARTIFACT_RECOVERY) == {'cursor': automatic.partition(calls[-1], 'hash')}


def test_rotation_handles_removed_cursor_new_library_and_changed_cap(recovery, monkeypatch):
    recovery.dirty.rows.clear()
    for i in range(7):
        certified(recovery, f'lib-{i}')
    calls = []
    monkeypatch.setattr(app, '_cleanup_completed_index_artifacts', calls.append)
    monkeypatch.setenv('INDEX_ARTIFACT_CLEANUP_LIBRARIES_PER_RECOVERY', '2')
    app.recover_pending_index_work()
    cursor = app._INDEX_ARTIFACT_RECOVERY.cursor
    del recovery.dirty.rows[cursor, automatic.CONTROL]
    certified(recovery, 'new-library')
    monkeypatch.setenv('INDEX_ARTIFACT_CLEANUP_LIBRARIES_PER_RECOVERY', '3')
    remaining = sorted((row['PartitionKey'], row['libraryId']) for row in recovery.dirty.rows.values())
    expected = [user for key, user in remaining if key > cursor]
    expected += [user for key, user in remaining if key <= cursor]
    app.recover_pending_index_work()
    assert calls[2:] == expected[:3]


def test_candidate_memory_bounded_and_scan_complete_before_cleanup(recovery, monkeypatch):
    import bisect
    recovery.dirty.rows.clear()
    for i in range(1000):
        certified(recovery, f'lib-{i}')
    ordered = sorted(row['PartitionKey'] for row in recovery.dirty.rows.values())
    app._INDEX_ARTIFACT_RECOVERY.cursor = ordered[500]
    monkeypatch.setenv('INDEX_ARTIFACT_CLEANUP_LIBRARIES_PER_RECOVERY', '3')
    insert = bisect.insort
    windows, scanned = {}, []

    def bounded_insert(window, item):
        assert len(window) <= 3
        windows[id(window)] = window
        insert(window, item)
        assert len(window) <= 4  # one transient lookahead before pop

    def controls(query):
        for row in recovery.dirty.rows.values():
            scanned.append(row['PartitionKey'])
            yield row

    def cleanup(user):
        assert len(scanned) == 1000
        assert len(windows) == 2
        assert all(len(window) <= 3 for window in windows.values())

    monkeypatch.setattr(bisect, 'insort', bounded_insert)
    monkeypatch.setattr(recovery.dirty, 'query_entities', controls)
    monkeypatch.setattr(app, '_cleanup_completed_index_artifacts', cleanup)
    assert app.recover_pending_index_work() == 0
    assert vars(app._INDEX_ARTIFACT_RECOVERY) == {'cursor': ordered[503]}


@pytest.mark.parametrize('setting,expected', [('0', 0), ('-1', 0), ('2', 2), ('bad', 25), ('100000', 1000)])
def test_configured_gc_cap_is_bounded(recovery, monkeypatch, setting, expected):
    recovery.dirty.rows.clear()
    for i in range(1010):
        certified(recovery, f'lib-{i}')
    calls = []
    monkeypatch.setattr(app, '_cleanup_completed_index_artifacts', calls.append)
    monkeypatch.setenv('INDEX_ARTIFACT_CLEANUP_LIBRARIES_PER_RECOVERY', setting)
    assert app.recover_pending_index_work() == 0
    assert len(calls) == expected


def test_gc_exception_does_not_stop_wakeups_or_later_libraries(recovery, monkeypatch, caplog):
    recovery.dirty.rows.clear()
    for user in ('a', 'b', 'c'):
        certified(recovery, user)
    dirty = certified(recovery, 'pending', 'vector')
    dirty['coveredRevision'] = 'old'
    calls = []

    def broken(user):
        assert recovery.sent[0][:2] == ('pending', 'vector')
        calls.append(user)
        raise OSError('cleanup failed')

    monkeypatch.setattr(app, '_cleanup_completed_index_artifacts', broken)
    assert app.recover_pending_index_work() == 1
    assert len(calls) == 3
    assert 'Periodic index artifact cleanup failed' in caplog.text
    assert index_work.WorkStore(recovery.jobs)._get(index_work.WorkStore.key('pending', 'vector'))


def test_cleanup_policy_error_is_reported_not_raised(recovery, monkeypatch):
    monkeypatch.setenv('INDEX_ARTIFACT_CLEANUP_GRACE_SECONDS', 'invalid')
    report = app._cleanup_completed_index_artifacts(USER)
    assert report['status'] == 'deferred' and report['deleted'] == 0
    assert 'ValueError' in report['reason']
    assert not recovery.service.deleted
    assert app.recover_pending_index_work() == 0


def test_scoped_recovery_never_starts_periodic_gc_or_moves_cursor(recovery, monkeypatch):
    monkeypatch.setattr(app, '_cleanup_completed_index_artifacts', lambda *a: pytest.fail('scoped GC'))
    app._INDEX_ARTIFACT_RECOVERY.cursor = 'saved'
    assert app.recover_pending_index_work(USER) == 0
    assert app._INDEX_ARTIFACT_RECOVERY.cursor == 'saved'


@pytest.mark.parametrize('missing', ['jobs_table_client', 'index_maintenance_queue_client'])
def test_unavailable_recovery_never_scans_gc(recovery, monkeypatch, missing):
    monkeypatch.setattr(app, missing, None)
    assert app.recover_pending_index_work() == 0
    assert not recovery.scans and not recovery.service.queries