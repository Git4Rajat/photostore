"""A user-approved wipe signs off the library's index quiescence itself.

Live incident (2026-11-13, photostore-rg): every Clean library failed a minute
in with the generic "Library clean failed". _execute_library_clean's
@quiesced_cleanup requires an operator's all-writers-stopped proof, which only
an admin could record and no deployment had. self_quiesced fences, drains and
attests the library for the wipe, then lifts its own fence.
"""
import time
from contextlib import contextmanager

import pytest

import app
import automatic_index_updates as automatic
import storage_utils as su
from tests.test_automatic_index_updates import Table, runtime  # noqa: F401 (fixtures)
from tests.test_automatic_runtime_integration import leased, prove  # noqa: F401 (fixtures)


@automatic.quiesced_cleanup
def wipe(user, seen):
    # Stands in for _execute_library_clean / _purge_library_data, behind the same guard.
    row = automatic._quiescence_row(user)
    seen.append(dict(proven=row.get('proven'), operator=row.get('operator'),
                     quiesced=automatic.current_cleanup_check(user) is not None))
    return 'wiped'


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def run(user='A', *, on_sleep=lambda: None, wait=30, seen=None):
    clock, naps = Clock(), []

    def sleep(seconds):
        naps.append(seconds)
        clock.now += seconds
        on_sleep()

    seen = [] if seen is None else seen
    with automatic.self_quiesced(user, operation='library_clean', reason='libclean:A:job', wait_seconds=wait,
                                 poll_seconds=5, sleep=sleep, clock=clock):
        result = wipe(user, seen)
    return result, seen, naps


def test_clean_proves_itself_runs_quiesced_and_lifts_its_fence(leased):
    result, seen, naps = run()
    assert result == 'wiped' and naps == []
    assert seen == [dict(proven=True, operator='system:library_clean', quiesced=True)]
    assert automatic._quiescence_row('A') is None
    automatic.check_source_admission('A')  # Uploads are admitted again.


def test_waits_for_a_writer_mid_write_and_fences_new_ones_meanwhile(leased):
    runtime, _ = leased
    reservation = runtime.store.begin('A', 'vector', ['IMG_1.jpg'])
    blocked = []

    def writer_finishes():
        with pytest.raises(automatic.SourceBusy):
            automatic.check_source_admission('A')
        blocked.append(True)
        runtime.store.finish('A', 'vector', reservation_id=reservation)

    result, seen, naps = run(on_sleep=writer_finishes)
    assert result == 'wiped' and blocked == [True] and naps == [5]
    assert seen[0]['proven'] is True
    assert automatic._quiescence_row('A') is None


def test_gives_up_without_deleting_when_writers_stay_busy_and_reopens_the_library(leased):
    runtime, _ = leased
    runtime.store.begin('A', 'people_embedding', ['p1'])
    seen = []
    with pytest.raises(automatic.QuiescenceTimeout, match='people_embedding source writes still in flight'):
        run(wait=12, seen=seen)
    assert seen == []
    assert automatic._quiescence_row('A') is None


def test_an_index_build_holding_a_claim_counts_as_busy(leased, monkeypatch):
    calls = []

    def check(user):
        calls.append(user)
        if len(calls) == 1:
            raise automatic.SourceBusy('Live maintenance claim prevents quiesced recovery')
    monkeypatch.setattr(automatic, '_check_publication_claims', check)
    result, _seen, naps = run()
    assert result == 'wiped' and naps == [5]


def test_never_takes_over_an_administrators_unproven_fence(leased):
    fence = automatic.operator_fence('A', operator='admin', reason='repairing')['fenceId']
    seen = []
    with pytest.raises(automatic.OperatorFenced):
        run(seen=seen)
    assert seen == []
    row = automatic._quiescence_row('A')
    assert row['fenceId'] == fence and row['operator'] == 'admin' and row['proven'] is False


def test_uses_an_administrators_proven_fence_and_leaves_it_up(leased):
    fence = prove()
    result, seen, _ = run()
    assert result == 'wiped' and seen[0]['operator'] == 'admin'
    assert automatic._quiescence_row('A')['fenceId'] == fence


def test_a_retry_adopts_the_fence_a_dead_worker_left_and_lifts_it(leased):
    app.jobs_table_client.create_entity(dict(
        PartitionKey=automatic.QUIESCENCE_PARTITION, RowKey=automatic.hashlib.sha256(b'A').hexdigest(),
        libraryId='A', fenceId='dead', operator='system:library_clean', reason='libclean:A:old',
        proven=False, fencedAt=time.time()))
    result, seen, _ = run()
    assert result == 'wiped' and seen[0]['proven'] is True
    assert automatic._quiescence_row('A') is None


def test_writers_lift_a_system_fence_abandoned_past_its_limit_but_never_an_admins(leased, monkeypatch):
    monkeypatch.setattr(automatic, 'SYSTEM_FENCE_MAX_SECONDS', 60)
    key = automatic.hashlib.sha256(b'A').hexdigest()
    app.jobs_table_client.create_entity(dict(
        PartitionKey=automatic.QUIESCENCE_PARTITION, RowKey=key, libraryId='A', fenceId='dead',
        operator='system:library_clean', reason='x', proven=True, fencedAt=time.time() - 30))
    with pytest.raises(automatic.SourceBusy):
        automatic.check_source_admission('A')
    row = automatic._quiescence_row('A')
    app.jobs_table_client.upsert_entity(dict(row, fencedAt=time.time() - 120))
    automatic.check_source_admission('A')
    assert automatic._quiescence_row('A') is None

    automatic.operator_fence('A', operator='admin', reason='old but deliberate')
    row = automatic._quiescence_row('A')
    app.jobs_table_client.upsert_entity(dict(row, fencedAt=time.time() - 120))
    with pytest.raises(automatic.SourceBusy):
        automatic.check_source_admission('A')


def test_does_nothing_without_durable_index_updates(monkeypatch):
    monkeypatch.setattr(su, 'DURABLE_INDEX_UPDATES', False)
    monkeypatch.setattr(app, 'jobs_table_client', None)
    with automatic.self_quiesced('A', operation='library_clean', reason='job'):
        pass


class _Library:
    def __init__(self):
        self.failed = []

    def set_cleanup_failed(self, library_id, reason):
        self.failed.append(reason)

    def set_cleanup_completed(self, *args):
        raise AssertionError('must not complete')


@pytest.mark.parametrize('error, reason', [
    (automatic.QuiescenceTimeout('busy'), 'still updating its indexes. Nothing was deleted'),
    (automatic.OperatorFenced('paused'), 'administrator has paused index updates'),
    (RuntimeError('boom'), 'Library clean failed'),
])
def test_the_worker_records_a_reason_a_user_can_act_on(monkeypatch, error, reason):
    library, statuses = _Library(), []
    monkeypatch.setattr(app, 'library_store', library)
    monkeypatch.setattr(app, '_upsert_job_status', lambda *a, **kw: statuses.append((a, kw)))

    @contextmanager
    def refuse(*args, **kwargs):
        raise error
        yield
    monkeypatch.setattr(automatic, 'self_quiesced', refuse)
    monkeypatch.setattr(app, '_execute_library_clean', lambda library_id: pytest.fail('must not run'))
    app._handle_clustering_queue_payload({'libraryId': 'lib1'}, 'libclean:lib1:job', 'u1', 'library_clean')
    assert reason in library.failed[0]
    failed = [kw for a, kw in statuses if a[3] == 'failed']
    assert reason in failed[0]['error']


def test_the_worker_runs_the_clean_behind_the_sign_off(monkeypatch):
    entered = []

    @contextmanager
    def sign_off(library_id, *, operation, reason):
        entered.append((library_id, operation, reason))
        yield
    monkeypatch.setattr(automatic, 'self_quiesced', sign_off)
    monkeypatch.setattr(app, '_execute_library_clean', lambda library_id: {'photosDeleted': 2, 'blobsDeleted': 2})
    monkeypatch.setattr(app, 'library_store', None)
    monkeypatch.setattr(app, '_notify_cleanup_completed', lambda *a: None)
    monkeypatch.setattr(app, '_upsert_job_status', lambda *a, **kw: None)
    app._handle_clustering_queue_payload({'libraryId': 'lib1'}, 'libclean:lib1:job', 'u1', 'library_clean')
    assert entered == [('lib1', 'library_clean', 'libclean:lib1:job')]


def test_a_real_clean_runs_end_to_end_behind_its_own_sign_off(leased, monkeypatch):
    import processing_cleanup
    for name in list(vars(app)):
        if name.endswith('_table_client') and name != 'jobs_table_client':
            monkeypatch.setattr(app, name, None)
    source = Table()
    for row in (('A', 'a.jpg'), ('A', 'b.heic'), ('B', 'keep.jpg')):
        source.upsert_entity({'PartitionKey': row[0], 'RowKey': row[1]})
    # Tracked like the live metadata client, so each delete passes the fence only under the proof.
    monkeypatch.setattr(app, 'metadata_table_client', su.track_index_source_table(source, 'metadata'))
    events = []
    monkeypatch.setattr(processing_cleanup, 'begin_library_cleanup', lambda *a: events.append('begin'))
    monkeypatch.setattr(processing_cleanup, 'collect_library_artifacts', lambda *a, **kw: events.append('collect'))
    monkeypatch.setattr(processing_cleanup, 'finish_library_cleanup', lambda *a, **kw: events.append('finish'))

    def artifact_purge(user):
        assert automatic._quiescence_row(user)['operator'] == 'system:library_clean'
        automatic.current_cleanup_check(user)()
        events.append('artifacts')
    monkeypatch.setattr(app, 'quiesced_user_index_artifact_purge', artifact_purge)
    for name in ('invalidate_image_names_cache', '_delete_cover_blobs_for_library', 'delete_user_vector_index_data',
                 'delete_user_lexical_index_data', 'delete_user_tag_embedding_index_data', 'delete_user_sort_index_data',
                 'delete_user_access_index_data', 'delete_user_albums_index_data', 'delete_user_people_index_data',
                 '_invalidate_metadata_scan_cache', '_delete_upload_temp_files_for_filename',
                 '_cleanup_library_shadow_rows'):
        monkeypatch.setattr(app, name, lambda *a, **kw: None)
    monkeypatch.setattr(su, 'delete_user_people_embedding_index_data', lambda *a: None)
    monkeypatch.setattr(app, '_delete_photo_blobs_if_present', lambda physical, extra=None: [])
    monkeypatch.setattr(app, '_shared_names_in_batch', lambda names, user: set())

    summary = app._run_library_clean('A', 'libclean:A:job')

    assert summary['photosDeleted'] == 2
    assert set(source.rows) == {('B', 'keep.jpg')}
    assert events == ['begin', 'collect', 'artifacts', 'finish']
    assert automatic._quiescence_row('A') is None
