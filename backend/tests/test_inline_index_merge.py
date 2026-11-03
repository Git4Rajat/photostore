"""Unit tests for inline_index_merge.py and its supporting pieces: the
storage_utils merge-eligibility predicates, the index_ownership escape
hatch, and the app.py dispatch/capture/cancel wiring."""
import queue as queue_module
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from azure.core.exceptions import ResourceNotFoundError

import app
import index_ownership
import index_work
import inline_index_merge
import storage_utils as su
from test_index_work import EtagTable
from tests.test_people_index import _seed_face, _seed_person, people_ctx  # noqa: F401
from tests.test_albums_index import _seed_album, albums_ctx  # noqa: F401
from tests.test_incremental_search_index import ctx as rows_ctx, _seed_row  # noqa: F401


@pytest.fixture(autouse=True)
def _reset_inline_merge_state(monkeypatch):
    monkeypatch.setattr(inline_index_merge, '_queue', None)
    monkeypatch.setattr(inline_index_merge, '_ENABLED', True)
    monkeypatch.setenv('APP_ROLE', 'core')
    yield
    monkeypatch.setattr(inline_index_merge, '_queue', None)


# --- index_ownership.allow_inline_merge --------------------------------------

def test_allow_inline_merge_rejects_heavy_scopes():
    with pytest.raises(ValueError, match='only permitted for'):
        with index_ownership.allow_inline_merge('full'):
            pass
    for bad in ('vector', 'compact', 'tag_embedding', 'people_embedding', 'reconcile'):
        with pytest.raises(ValueError):
            with index_ownership.allow_inline_merge(bad):
                pass


def test_allow_inline_merge_grants_a_narrow_exception_then_releases_it(monkeypatch):
    monkeypatch.setenv('APP_ROLE', 'core')
    monkeypatch.setattr(su, '_ROLE_MAY_BUILD_INDEXES', False)
    with pytest.raises(index_ownership.IndexOwnershipError):
        index_ownership.require_catalog_owner()
    with index_ownership.allow_inline_merge('people'):
        index_ownership.require_catalog_owner()  # must not raise
    with pytest.raises(index_ownership.IndexOwnershipError):
        index_ownership.require_catalog_owner()  # reverted after the context exits


def test_allow_inline_merge_rejects_a_scope_whose_kinds_widen_beyond_the_bounded_set(monkeypatch):
    # If SCOPE_KINDS[scope] is ever widened to include a kind outside the
    # bounded inline-merge set (e.g. 'lexical'/'vector'), allow_inline_merge
    # must refuse the exception rather than silently granting it -- this is
    # the one place that can actually check which kinds the caller is about
    # to touch, since require_catalog_owner itself has no scope parameter.
    import index_updates
    monkeypatch.setitem(index_updates.SCOPE_KINDS, 'people', ('people', 'vector'))
    with pytest.raises(ValueError, match='not entirely within the bounded'):
        with index_ownership.allow_inline_merge('people'):
            pass


def test_configured_build_roles_unaffected_by_the_new_escape_hatch(monkeypatch):
    monkeypatch.setenv('INDEX_BUILD_ROLES', 'core')
    with pytest.raises(ValueError, match='only contain index-worker'):
        index_ownership.configured_build_roles()


# --- storage_utils merge-eligibility predicates ------------------------------

def test_people_merge_eligible_happy_path(people_ctx):
    people, faces, _ = people_ctx
    _seed_person(people, 'lib', 'p1', name='A', faceIds='["f1"]')
    _seed_face(faces, 'lib', 'f1', filename='a.jpg', personId='p1')
    su.refresh_user_people_index('lib', source_version='v1')
    # Returns the validated previous snapshot (not just a bool), reused
    # directly by the caller instead of a second, racy fetch.
    snapshot = su._people_merge_eligible('lib', {'p1'})
    assert snapshot is not None and snapshot.source_version == 'v1'


def test_people_merge_eligible_rejects_over_cap(people_ctx):
    assert su._people_merge_eligible('lib', set(f'p{i}' for i in range(101))) is None


@pytest.mark.parametrize('ids', [None, set()])
def test_people_merge_eligible_rejects_empty(people_ctx, ids):
    assert su._people_merge_eligible('lib', ids) is None


def test_people_merge_eligible_rejects_missing_previous_snapshot(people_ctx):
    assert su._people_merge_eligible('never-built', {'p1'}) is None


def test_people_merge_eligible_rejects_source_version_mismatch(people_ctx, monkeypatch):
    people, faces, blobs = people_ctx
    _seed_person(people, 'lib', 'p1', name='A', faceIds='["f1"]')
    _seed_face(faces, 'lib', 'f1', filename='a.jpg', personId='p1')
    su.refresh_user_people_index('lib', source_version='v1')
    manifest_key = 'lexical-index/' + su._people_index_manifest_blob_name('lib')
    import json
    manifest = json.loads(blobs.blobs[manifest_key])
    blobs.blobs[manifest_key] = json.dumps({**manifest, 'sourceVersion': 'different'}).encode()
    assert su._people_merge_eligible('lib', {'p1'}) is None


def test_albums_merge_eligible_happy_path(albums_ctx):
    albums, _, _ = albums_ctx
    _seed_album(albums, 'lib', 'a1', name='Trip')
    su.refresh_user_albums_index('lib', source_version='v1')
    snapshot = su._albums_merge_eligible('lib', {'a1'})
    assert snapshot is not None and snapshot.source_version == 'v1'


def test_albums_merge_eligible_rejects_over_cap_and_missing_base(albums_ctx):
    assert su._albums_merge_eligible('lib', set(f'a{i}' for i in range(101))) is None
    assert su._albums_merge_eligible('never-built', {'a1'}) is None


def test_rows_merge_eligible_rejects_unknown_kind():
    assert su._rows_merge_eligible('lib', 'people') is False


def test_rows_merge_eligible_rejects_when_nothing_dirty(monkeypatch):
    monkeypatch.setattr(su, '_get_dirty_search_index_filenames', lambda uid, kind: set())
    assert su._rows_merge_eligible('lib', 'sort') is False


def test_rows_merge_eligible_rejects_when_too_large_to_merge(monkeypatch):
    monkeypatch.setattr(su, '_get_dirty_search_index_filenames', lambda uid, kind: {'a.jpg'})
    monkeypatch.setattr(su, '_dirty_set_too_large_to_merge', lambda uid, n: True)
    assert su._rows_merge_eligible('lib', 'sort') is False


def test_rows_merge_eligible_happy_path(monkeypatch):
    monkeypatch.setattr(su, '_get_dirty_search_index_filenames', lambda uid, kind: {'a.jpg'})
    monkeypatch.setattr(su, '_dirty_set_too_large_to_merge', lambda uid, n: False)
    monkeypatch.setattr(su, '_load_sort_index_manifest',
                         lambda uid: {'sourceVersion': 'v1', 'schemaVersion': su._SORT_INDEX_SCHEMA_VERSION})
    assert su._rows_merge_eligible('lib', 'sort') is True


def test_rows_merge_eligible_rejects_schema_mismatch(monkeypatch):
    monkeypatch.setattr(su, '_get_dirty_search_index_filenames', lambda uid, kind: {'a.jpg'})
    monkeypatch.setattr(su, '_dirty_set_too_large_to_merge', lambda uid, n: False)
    monkeypatch.setattr(su, '_load_sort_index_manifest',
                         lambda uid: {'sourceVersion': 'v1', 'schemaVersion': 'stale'})
    assert su._rows_merge_eligible('lib', 'sort') is False


# --- inline_index_merge.submit: role-gating / kill switch / backpressure ----

def test_submit_rejects_when_disabled(monkeypatch):
    monkeypatch.setattr(inline_index_merge, '_ENABLED', False)
    assert inline_index_merge.submit('lib', 'people') is False


@pytest.mark.parametrize('role', ['index-worker', 'cluster', 'vision', 'upload', 'archive', 'recovery'])
def test_submit_rejects_wrong_role(monkeypatch, role):
    monkeypatch.setenv('APP_ROLE', role)
    assert inline_index_merge.submit('lib', 'people') is False


@pytest.mark.parametrize('scope', ['full', 'vector', 'compact', 'light', 'tag_embedding'])
def test_submit_rejects_scopes_outside_the_bounded_set(scope):
    assert inline_index_merge.submit('lib', scope) is False


def test_submit_rejects_empty_user_id():
    assert inline_index_merge.submit('', 'people') is False


def test_submit_backpressure_rejects_without_blocking_when_queue_is_full(monkeypatch):
    full_queue = queue_module.Queue(maxsize=1)
    full_queue.put(('someone-else', 'people', None))
    monkeypatch.setattr(inline_index_merge, '_queue', full_queue)
    monkeypatch.setattr(inline_index_merge, '_ensure_started', lambda: None)
    accepted = inline_index_merge.submit('lib', 'people')
    assert accepted is False
    assert full_queue.qsize() == 1  # nothing new was added


def test_submit_accepts_and_enqueues_when_eligible(monkeypatch):
    started = []
    monkeypatch.setattr(inline_index_merge, '_ensure_started', lambda: started.append(1) or
                         monkeypatch.setattr(inline_index_merge, '_queue', queue_module.Queue(maxsize=4)))
    assert inline_index_merge.submit('lib', 'people') is True
    assert started == [1]
    assert inline_index_merge._queue.qsize() == 1


# --- inline_index_merge.has_capacity -----------------------------------------

def test_has_capacity_false_when_disabled(monkeypatch):
    monkeypatch.setattr(inline_index_merge, '_ENABLED', False)
    assert inline_index_merge.has_capacity() is False


def test_has_capacity_true_when_queue_not_yet_started():
    assert inline_index_merge._queue is None
    assert inline_index_merge.has_capacity() is True


def test_has_capacity_reflects_queue_fullness(monkeypatch):
    not_full = queue_module.Queue(maxsize=2)
    monkeypatch.setattr(inline_index_merge, '_queue', not_full)
    assert inline_index_merge.has_capacity() is True
    full_queue = queue_module.Queue(maxsize=1)
    full_queue.put(('someone', 'people', None))
    monkeypatch.setattr(inline_index_merge, '_queue', full_queue)
    assert inline_index_merge.has_capacity() is False


# --- inline_index_merge._eligible / _attempt --------------------------------

def test_eligible_people_bails_on_pending_labels(monkeypatch):
    monkeypatch.setattr(su, '_inline_artifact_within_limit', lambda *a: True)
    monkeypatch.setattr(app, '_pending_person_labels', lambda uid: {'p1'})
    ok, _, _ = inline_index_merge._eligible('lib', 'people')
    assert ok is False


def test_eligible_people_bails_when_albums_also_dirty(monkeypatch):
    monkeypatch.setattr(su, '_inline_artifact_within_limit', lambda *a: True)
    monkeypatch.setattr(app, '_pending_person_labels', lambda uid: set())
    monkeypatch.setattr(app, 'get_dirty_people', lambda uid: {'p1'})
    monkeypatch.setattr(app, 'get_dirty_albums', lambda uid: {'a1'})
    ok, _, _ = inline_index_merge._eligible('lib', 'people')
    assert ok is False


def test_eligible_people_defers_to_the_shared_predicate(monkeypatch):
    monkeypatch.setattr(su, '_inline_artifact_within_limit', lambda *a: True)
    monkeypatch.setattr(app, '_pending_person_labels', lambda uid: set())
    monkeypatch.setattr(app, 'get_dirty_people', lambda uid: {'p1'})
    monkeypatch.setattr(app, 'get_dirty_albums', lambda uid: set())
    monkeypatch.setattr(su, '_people_merge_eligible', lambda uid, ids: ids == {'p1'})
    ok, dirty_people, dirty_albums = inline_index_merge._eligible('lib', 'people')
    assert ok is True and dirty_people == {'p1'} and dirty_albums is None


def test_eligible_albums_defers_to_the_shared_predicate(monkeypatch):
    monkeypatch.setattr(su, '_inline_artifact_within_limit', lambda *a: True)
    monkeypatch.setattr(app, 'get_dirty_albums', lambda uid: {'a1'})
    monkeypatch.setattr(su, '_albums_merge_eligible', lambda uid, ids: True)
    ok, dirty_people, dirty_albums = inline_index_merge._eligible('lib', 'albums')
    assert ok is True and dirty_people is None and dirty_albums == {'a1'}


def test_eligible_sort_access_defer_to_the_shared_predicate(monkeypatch):
    monkeypatch.setattr(su, '_inline_artifact_within_limit', lambda *a: True)
    monkeypatch.setattr(su, '_rows_merge_eligible', lambda uid, kind: kind == 'sort')
    assert inline_index_merge._eligible('lib', 'sort')[0] is True
    assert inline_index_merge._eligible('lib', 'access')[0] is False


def test_eligible_search_requires_existing_base_and_bounded_dirty_set(monkeypatch):
    import search_db
    monkeypatch.setattr(search_db, 'load_manifest', lambda uid: {
        'sourceVersion': 'v1', 'schemaVersion': search_db.SCHEMA_VERSION,
        'deltaSeq': 0, 'deltaRows': 0, 'lineage': 'v1',
    })
    monkeypatch.setattr(search_db, 'available_delta_row_capacity', lambda manifest, chunk: 10)
    monkeypatch.setattr(su, '_get_dirty_search_index_filenames', lambda *a, **k: {'a.jpg'})
    ok, people, albums = inline_index_merge._eligible('lib', 'search')
    assert ok is True and people is None and albums is None


def test_eligible_search_defers_without_base_or_when_dirty_capture_is_truncated(monkeypatch):
    import search_db
    monkeypatch.setattr(search_db, 'load_manifest', lambda uid: {})
    assert inline_index_merge._eligible('lib', 'search')[0] is False
    monkeypatch.setattr(search_db, 'load_manifest', lambda uid: {
        'sourceVersion': 'v1', 'schemaVersion': search_db.SCHEMA_VERSION,
        'deltaSeq': 0, 'deltaRows': 0,
    })
    monkeypatch.setattr(search_db, 'available_delta_row_capacity', lambda manifest, chunk: 10)
    monkeypatch.setattr(su, '_get_dirty_search_index_filenames', lambda *a, **k: SimpleNamespace(
        rows={'a.jpg'}, has_more=True, truncated=False))
    assert inline_index_merge._eligible('lib', 'search')[0] is False


@pytest.fixture
def attempt_ctx(monkeypatch):
    from contextlib import nullcontext
    monkeypatch.setattr(inline_index_merge, '_serialized_library', lambda uid: True)
    monkeypatch.setattr('index_publication.catalog_serialization', lambda *a: nullcontext())
    table = EtagTable()
    monkeypatch.setattr(app, 'jobs_table_client', table)
    # A pending intent row must already exist -- this mirrors the durable
    # enqueue_index_build call that always runs before an inline attempt is
    # ever submitted (dispatch_scoped_rebuild calls it first).
    index_work.WorkStore(table).request('lib', 'people')
    monkeypatch.setattr(inline_index_merge, '_eligible', lambda uid, scope: (True, {'p1'}, None))
    monkeypatch.setattr(su, '_library_row_count', lambda uid: 10)
    prime = Mock(return_value=[])
    monkeypatch.setattr(su, 'prime_all_user_indexes_sequentially', prime)
    drain = Mock()
    monkeypatch.setattr(app, '_drain_incremental_search_db', drain)
    monkeypatch.setattr(su, '_get_dirty_search_index_filenames', lambda *a, **k: set())
    clear_people = Mock()
    monkeypatch.setattr(app, 'clear_dirty_people', clear_people)
    clear_albums = Mock()
    monkeypatch.setattr(app, 'clear_dirty_albums', clear_albums)
    return SimpleNamespace(table=table, prime=prime, drain=drain,
                            clear_people=clear_people, clear_albums=clear_albums)


def test_attempt_claims_merges_clears_and_cancels_on_success(attempt_ctx, monkeypatch):
    deleted = []
    sent_message = SimpleNamespace(id='m1', pop_receipt='r1')
    queue_client = SimpleNamespace(delete_message=lambda mid, pr: deleted.append((mid, pr)))
    capture = [(queue_client, sent_message)]

    inline_index_merge._attempt('lib', 'people', capture)

    attempt_ctx.prime.assert_called_once()
    assert attempt_ctx.prime.call_args.kwargs['affected_person_ids'] == {'p1'}
    assert attempt_ctx.prime.call_args.kwargs['incremental_only'] is True
    attempt_ctx.drain.assert_not_called()
    attempt_ctx.clear_people.assert_called_once_with('lib', {'p1'})
    attempt_ctx.clear_albums.assert_not_called()
    assert deleted == [('m1', 'r1')]
    # WorkStore row for this key is gone -- finish() succeeded.
    assert table.rows == {} if (table := attempt_ctx.table) else True


def test_search_inline_publishes_delta_and_cancels_backstop(attempt_ctx):
    store = index_work.WorkStore(attempt_ctx.table)
    attempt_ctx.table.rows.pop((index_work.PARTITION, store.key('lib', 'people')))
    store.request('lib', 'search')
    attempt_ctx.drain.return_value = {'status': 'delta', 'published': 1}
    deleted = []
    capture = [(SimpleNamespace(delete_message=lambda mid, pr: deleted.append((mid, pr))),
                SimpleNamespace(id='m1', pop_receipt='r1'))]
    inline_index_merge._attempt('lib', 'search', capture)
    attempt_ctx.drain.assert_called_once_with(
        'lib', 'search', max_names=inline_index_merge._SEARCH_MAX_NAMES, max_passes=1)
    attempt_ctx.prime.assert_not_called()
    assert deleted == [('m1', 'r1')]
    assert not attempt_ctx.table.rows


def test_search_inline_enters_catalog_ownership_escape_hatch(attempt_ctx, monkeypatch):
    store = index_work.WorkStore(attempt_ctx.table)
    attempt_ctx.table.rows.pop((index_work.PARTITION, store.key('lib', 'people')))
    store.request('lib', 'search')
    entered = []
    from contextlib import nullcontext
    monkeypatch.setattr(index_ownership, 'allow_inline_merge',
                        lambda scope: (entered.append(scope) or nullcontext()))
    attempt_ctx.drain.return_value = {'status': 'delta'}
    inline_index_merge._attempt('lib', 'search', [])
    assert entered == ['search']


def test_search_inline_defers_compaction_to_index_worker(attempt_ctx):
    store = index_work.WorkStore(attempt_ctx.table)
    attempt_ctx.table.rows.pop((index_work.PARTITION, store.key('lib', 'people')))
    store.request('lib', 'search')
    attempt_ctx.drain.return_value = {'status': 'deferred', 'nextScope': 'compact'}
    deleted = []
    inline_index_merge._attempt(
        'lib', 'search',
        [(SimpleNamespace(delete_message=lambda *args: deleted.append(args)),
          SimpleNamespace(id='m1', pop_receipt='r1'))],
    )
    attempt_ctx.drain.assert_called_once_with(
        'lib', 'search', max_names=inline_index_merge._SEARCH_MAX_NAMES, max_passes=1)
    assert deleted == []
    assert attempt_ctx.table.rows


def test_attempt_leaves_backstop_message_when_a_newer_revision_arrived_mid_merge(attempt_ctx, monkeypatch):
    # Simulates a second concurrent mutation to the same (user, scope)
    # advancing the WorkStore row's revision while this merge is running --
    # WorkStore.finish() then returns True ("newer") and deliberately leaves
    # the intent row open instead of deleting it. The already-captured
    # backstop message must NOT be cancelled in that case, or the newer
    # revision's work loses its only pending wakeup (see
    # _run_index_maintenance_payload's matching `if newer: notify(...)`).
    store = index_work.WorkStore(attempt_ctx.table)

    def bump_revision_then_return(*a, **k):
        store.request('lib', 'people')
        return []
    attempt_ctx.prime.side_effect = bump_revision_then_return
    deleted = []
    queue_client = SimpleNamespace(delete_message=lambda mid, pr: deleted.append((mid, pr)))
    capture = [(queue_client, SimpleNamespace(id='m1', pop_receipt='r1'))]

    inline_index_merge._attempt('lib', 'people', capture)

    attempt_ctx.clear_people.assert_called_once_with('lib', {'p1'})  # the merge itself still completed
    assert deleted == []  # backstop message must survive -- newer work still needs a wakeup
    assert attempt_ctx.table.rows  # intent row stays open, not deleted


def test_attempt_skips_cleanly_when_ineligible(attempt_ctx, monkeypatch):
    monkeypatch.setattr(inline_index_merge, '_eligible', lambda uid, scope: (False, None, None))
    inline_index_merge._attempt('lib', 'people', [])
    attempt_ctx.prime.assert_not_called()


def test_attempt_revalidates_eligibility_after_claim(attempt_ctx, monkeypatch):
    answers = iter([(True, {'p1'}, None), (False, None, None)])
    monkeypatch.setattr(inline_index_merge, '_eligible', lambda *a: next(answers))
    inline_index_merge._attempt('lib', 'people', [])
    attempt_ctx.prime.assert_not_called()
    assert attempt_ctx.table.rows
    assert not next(iter(attempt_ctx.table.rows.values())).get('owner')


def test_attempt_backs_off_silently_when_already_leased(attempt_ctx, monkeypatch):
    def claim_raises(self, key):
        raise RuntimeError('Maintenance work is already leased')
    monkeypatch.setattr(index_work.WorkStore, 'claim', claim_raises)
    inline_index_merge._attempt('lib', 'people', [])
    attempt_ctx.prime.assert_not_called()


def test_attempt_backs_off_when_no_pending_intent(attempt_ctx, monkeypatch):
    monkeypatch.setattr(index_work.WorkStore, 'claim', lambda self, key: None)
    inline_index_merge._attempt('lib', 'people', [])
    attempt_ctx.prime.assert_not_called()


def test_attempt_releases_claim_and_does_not_cancel_on_merge_failure(attempt_ctx, monkeypatch):
    attempt_ctx.prime.side_effect = RuntimeError('storage down')
    deleted = []
    capture = [(SimpleNamespace(delete_message=lambda *a: deleted.append(1)), SimpleNamespace(id='m', pop_receipt='r'))]
    with pytest.raises(RuntimeError, match='storage down'):
        inline_index_merge._attempt('lib', 'people', capture)
    assert deleted == []  # cancellation never attempted -- the durable message must proceed
    attempt_ctx.clear_people.assert_not_called()


def test_cancel_backstop_swallows_a_stale_pop_receipt():
    queue_client = SimpleNamespace(
        delete_message=lambda *a: (_ for _ in ()).throw(ResourceNotFoundError('gone')))
    inline_index_merge._cancel_backstop([(queue_client, SimpleNamespace(id='m', pop_receipt='r'))])  # must not raise


def test_cancel_backstop_noop_when_no_message_was_sent():
    calls = []
    queue_client = SimpleNamespace(delete_message=lambda *a: calls.append(a))
    inline_index_merge._cancel_backstop([])
    inline_index_merge._cancel_backstop(None)
    assert calls == []


# --- app.dispatch_scoped_rebuild: capture + delay_override wiring -----------

def test_dispatch_scoped_rebuild_attempts_inline_only_for_bounded_scopes(monkeypatch):
    monkeypatch.setattr(app, 'enqueue_index_build', lambda *a, **k: 'queued')
    submitted = []
    monkeypatch.setattr('inline_index_merge.enabled', lambda: True)
    monkeypatch.setattr('inline_index_merge.submit', lambda uid, scope, **k: submitted.append((uid, scope)) or True)
    app.dispatch_scoped_rebuild('lib', 'rating-change', 'sort')
    assert submitted == [('lib', 'sort')]

    submitted.clear()
    app.dispatch_scoped_rebuild('lib', 'metadata-change', 'search')
    assert submitted == [('lib', 'search')]

    submitted.clear()
    app.dispatch_scoped_rebuild('lib', 'bulk-upload', 'full')
    assert submitted == []  # 'full' is never inline-eligible


def test_dispatch_scoped_rebuild_skips_inline_when_not_queued(monkeypatch):
    monkeypatch.setattr(app, 'enqueue_index_build', lambda *a, **k: 'already_active')
    submitted = []
    monkeypatch.setattr('inline_index_merge.enabled', lambda: True)
    monkeypatch.setattr('inline_index_merge.submit', lambda *a, **k: submitted.append(1) or True)
    status = app.dispatch_scoped_rebuild('lib', 'rating-change', 'sort')
    assert status == 'already_active'
    assert submitted == []


def test_dispatch_scoped_rebuild_passes_backstop_delay_and_capture_only_when_attempting_inline(monkeypatch):
    monkeypatch.setattr('inline_index_merge.enabled', lambda: True)
    monkeypatch.setattr('inline_index_merge.submit', lambda *a, **k: True)
    monkeypatch.setattr('inline_index_merge.BACKSTOP_DELAY_SECONDS', 17)
    calls = []
    def fake_enqueue(user_id, reason='', scope='full', *, coalesce=True, capture=None, delay_override=None):
        calls.append((delay_override, capture is not None))
        return 'queued'
    monkeypatch.setattr(app, 'enqueue_index_build', fake_enqueue)
    app.dispatch_scoped_rebuild('lib', 'rating-change', 'sort')
    assert calls == [(17, True)]

    calls.clear()
    monkeypatch.setattr('inline_index_merge.enabled', lambda: False)
    app.dispatch_scoped_rebuild('lib', 'rating-change', 'sort')
    assert calls == [(None, False)]


def test_dispatch_scoped_rebuild_skips_the_long_delay_when_the_local_queue_has_no_room(monkeypatch):
    # Even though scope/role/flag would otherwise attempt inline, a full
    # local queue means inline has no chance of actually running -- the
    # durable backstop must keep its normal short delay, not pay
    # BACKSTOP_DELAY_SECONDS for an attempt that was never going to happen.
    monkeypatch.setattr('inline_index_merge.enabled', lambda: True)
    monkeypatch.setattr('inline_index_merge.has_capacity', lambda: False)
    submitted = []
    monkeypatch.setattr('inline_index_merge.submit', lambda *a, **k: submitted.append(1) or True)
    calls = []
    def fake_enqueue(user_id, reason='', scope='full', *, coalesce=True, capture=None, delay_override=None):
        calls.append((delay_override, capture is not None))
        return 'queued'
    monkeypatch.setattr(app, 'enqueue_index_build', fake_enqueue)
    app.dispatch_scoped_rebuild('lib', 'rating-change', 'sort')
    assert calls == [(None, False)]
    assert submitted == []


def test_send_index_wakeup_captures_the_sent_message(monkeypatch):
    sent = SimpleNamespace(id='abc', pop_receipt='xyz')
    monkeypatch.setattr(app, 'index_maintenance_queue_client',
                         SimpleNamespace(send_message=lambda *a, **k: sent))
    monkeypatch.setattr(app, '_upsert_job_status', lambda *a, **k: None)
    capture = []
    app._send_index_wakeup('lib', 'people', 'rev', 0, capture=capture)
    assert capture == [(app.index_maintenance_queue_client, sent)]


def test_enqueue_index_build_delay_override_floors_at_one_second(monkeypatch):
    monkeypatch.setattr(app, 'INDEX_MAINTENANCE_QUEUE_NAME', 'index-maintenance')
    monkeypatch.setattr(app, 'jobs_table_client', EtagTable())
    monkeypatch.setattr(app, 'index_maintenance_queue_client', SimpleNamespace(send_message=lambda *a, **k: None))
    seen_delays = []
    def fake_wakeup(library, scope, revision, delay=0, *, reason='pending-recovery', capture=None):
        seen_delays.append(delay)
    monkeypatch.setattr(app, '_send_index_wakeup', fake_wakeup)
    app.enqueue_index_build('lib', scope='sort', delay_override=0)
    assert seen_delays == [1]


@pytest.mark.parametrize('kind', ['people', 'albums'])
@pytest.mark.parametrize('count', [None, -1, '5', True, 5001])
def test_inline_rejects_unknown_or_oversize_snapshot_before_blob_load(monkeypatch, kind, count):
    schema = getattr(su, f'_{kind.upper()}_INDEX_SCHEMA_VERSION')
    monkeypatch.setattr(su, f'_load_{kind}_index_manifest', lambda uid: {
        'sourceVersion': 'v1', 'schemaVersion': schema, 'rowCount': count})
    loader = Mock(side_effect=AssertionError('must not load oversized blob'))
    monkeypatch.setattr(su, f'_load_{kind}_index_blob', loader)
    assert inline_index_merge._eligible('lib', kind)[0] is False
    loader.assert_not_called()


def test_large_photo_library_with_small_people_index_is_inline_eligible(people_ctx, monkeypatch):
    people, faces, _ = people_ctx
    _seed_person(people, 'lib', 'p1', name='Alice', faceIds='["f1"]')
    _seed_face(faces, 'lib', 'f1', personId='p1', filename='a.jpg')
    su.refresh_user_people_index('lib', source_version='v1')
    monkeypatch.setattr(su, '_library_row_count', lambda uid: 1000000)
    monkeypatch.setattr(app, '_pending_person_labels', lambda uid: set())
    monkeypatch.setattr(app, 'get_dirty_people', lambda uid: {'p1'})
    monkeypatch.setattr(app, 'get_dirty_albums', lambda uid: set())
    assert inline_index_merge._eligible('lib', 'people')[0] is True
    assert su.refresh_user_people_index('lib', affected_person_ids={'p1'}, incremental_only=True)


@pytest.mark.parametrize('kind', ['people', 'albums'])
def test_strict_missing_base_never_calls_full_or_timestamp_build(monkeypatch, kind):
    monkeypatch.setattr(su, f'_load_{kind}_index_manifest', lambda uid: {})
    full = Mock(side_effect=AssertionError('full scan'))
    monkeypatch.setattr(su, f'_build_user_{kind}_index_snapshot', full)
    timestamp = Mock(side_effect=AssertionError('timestamp scan'))
    monkeypatch.setattr(su, '_incremental_people_index_snapshot', timestamp)
    kwargs = {'affected_person_ids' if kind == 'people' else 'affected_album_ids': {'one'}}
    assert getattr(su, f'refresh_user_{kind}_index')('lib', incremental_only=True, **kwargs) is None
    full.assert_not_called()
    timestamp.assert_not_called()


def test_strict_people_membership_cap_and_read_failure_never_scan(people_ctx, monkeypatch):
    people, faces, _ = people_ctx
    _seed_person(people, 'lib', 'p1', name='Alice', faceIds='["f1"]')
    _seed_face(faces, 'lib', 'f1', personId='p1', filename='a.jpg')
    su.refresh_user_people_index('lib', source_version='v1')
    full = Mock(side_effect=AssertionError('full scan'))
    monkeypatch.setattr(su, '_build_user_people_index_snapshot', full)
    monkeypatch.setattr(su, '_STRICT_INCREMENTAL_MAX_MEMBERS', 0)
    assert su.refresh_user_people_index('lib', affected_person_ids={'p1'}, incremental_only=True) is None
    monkeypatch.setattr(su, '_STRICT_INCREMENTAL_MAX_MEMBERS', 10)
    monkeypatch.setattr(faces, 'get_entity', Mock(side_effect=RuntimeError('storage down')))
    assert su.refresh_user_people_index('lib', affected_person_ids={'p1'}, incremental_only=True) is None
    full.assert_not_called()


def test_album_transient_point_failure_is_not_a_deletion(albums_ctx, monkeypatch):
    albums, _, _ = albums_ctx
    _seed_album(albums, 'lib', 'a1', name='Trip')
    initial = su.refresh_user_albums_index('lib', source_version='v1')
    monkeypatch.setattr(albums, 'get_entity', Mock(side_effect=RuntimeError('storage down')))
    assert su._incremental_albums_index_snapshot('lib', 'v2', {'a1'}, initial.rows) is None
    full = Mock(side_effect=AssertionError('full scan'))
    monkeypatch.setattr(su, '_build_user_albums_index_snapshot', full)
    assert su.refresh_user_albums_index('lib', affected_album_ids={'a1'}, incremental_only=True) is None
    assert su._load_albums_index_blob('lib').rows == initial.rows
    full.assert_not_called()


def test_album_confirmed_missing_removes_only_affected_album(albums_ctx, monkeypatch):
    _ready_album_search(monkeypatch)
    albums, _, _ = albums_ctx
    _seed_album(albums, 'lib', 'a1', name='Trip')
    _seed_album(albums, 'lib', 'a2', name='Other')
    initial = su.refresh_user_albums_index('lib', source_version='v1')
    monkeypatch.setattr(albums, 'get_entity', Mock(side_effect=ResourceNotFoundError('gone')))
    result = su._incremental_albums_index_snapshot('lib', 'v2', {'a1'}, initial.rows, incremental_only=True)
    assert [row['albumId'] for row in result.rows] == ['a2']


@pytest.mark.parametrize('manifest_failure', [False, True])
def test_album_publication_failure_propagates_without_caching_success(albums_ctx, monkeypatch, manifest_failure):
    albums, _, blobs = albums_ctx
    _seed_album(albums, 'lib', 'a1', name='Trip')
    su.refresh_user_albums_index('lib', source_version='v1')
    original = su._get_blob_client
    def client(container, name):
        if name == (su._albums_index_manifest_blob_name('lib') if manifest_failure else su._albums_index_json_blob_name('lib')):
            return SimpleNamespace(upload_blob=Mock(side_effect=RuntimeError('publication failed')))
        return original(container, name)
    monkeypatch.setattr(su, '_get_blob_client', client)
    with pytest.raises(RuntimeError, match='publication failed'):
        su.refresh_user_albums_index('lib', source_version='v2')
    assert su._ALBUMS_INDEX_CACHE['lib']['source_version'] == 'v1'


@pytest.mark.parametrize('kind', ['sort', 'access'])
def test_strict_rows_missing_base_never_scans(monkeypatch, kind):
    metadata = SimpleNamespace(query_entities=Mock(side_effect=AssertionError('full scan')))
    monkeypatch.setitem(su._CTX, 'metadata_table_client', metadata)
    monkeypatch.setattr(su, '_get_dirty_search_index_filenames', lambda uid, scope: {'a.jpg'})
    monkeypatch.setattr(su, f'_load_{kind}_index_manifest', lambda uid: {})
    assert getattr(su, f'refresh_user_{kind}_index')('lib', incremental_only=True) is None
    metadata.query_entities.assert_not_called()


def test_inline_failed_result_retains_intent_dirty_and_backstop(attempt_ctx):
    attempt_ctx.prime.return_value = ['people']
    delete = Mock()
    with pytest.raises(RuntimeError, match='produced no people'):
        inline_index_merge._attempt('lib', 'people', [(SimpleNamespace(delete_message=delete), SimpleNamespace(id='m', pop_receipt='r'))])
    delete.assert_not_called()
    attempt_ctx.clear_people.assert_not_called()
    assert attempt_ctx.table.rows


def test_real_read_failure_cannot_be_hidden_by_later_deferral(attempt_ctx, monkeypatch, caplog):
    import index_merge_diagnostics
    import logging
    caplog.set_level(logging.INFO)
    def fail_then_defer(*args, **kwargs):
        index_merge_diagnostics.record('people', 'publication_base_read_failed',
                                       error=OSError('private failure detail'))
        index_merge_diagnostics.record('people', 'publication_etag_unavailable')
        return ['people']
    attempt_ctx.prime.side_effect = fail_then_defer
    delete = Mock()
    with pytest.raises(RuntimeError, match='produced no people'):
        inline_index_merge._attempt('lib', 'people', [(SimpleNamespace(delete_message=delete),
                                                      SimpleNamespace(id='m', pop_receipt='r'))])
    delete.assert_not_called()
    attempt_ctx.clear_people.assert_not_called()
    assert 'private failure detail' not in caplog.text
    assert 'publication_base_read_failed' in caplog.text
    # Attempt-local diagnostics cannot leak into the next worker task.
    assert not index_merge_diagnostics.was_deferred('people')


@pytest.mark.parametrize('kind', ['sort', 'access'])
@pytest.mark.parametrize('reason', ['cost', 'read', 'schema', 'source', 'dirty_cap', 'photo_cap'])
def test_strict_rows_revalidation_never_falls_back_to_scan(rows_ctx, monkeypatch, kind, reason):
    import search_db
    monkeypatch.setattr(search_db, 'load_manifest', lambda uid: {'rowCount': 1})
    metadata, dirty, _ = rows_ctx
    _seed_row(metadata, 'lib', 'a.jpg', anonymousImageId='a')
    getattr(su, f'refresh_user_{kind}_index')('lib', source_version='v1', force_full=True)
    su._mark_search_index_dirty_filenames('lib', ['a.jpg'], kinds=(kind,))
    metadata.scan_count = 0
    if reason == 'cost':
        monkeypatch.setattr(su, '_dirty_set_too_large_to_merge', lambda *a: True)
    elif reason == 'read':
        monkeypatch.setattr(su, '_timed_fetch', lambda *a: False)
    elif reason == 'schema':
        monkeypatch.setattr(su, f'_load_{kind}_index_manifest', lambda uid: {
            'sourceVersion': 'v1', 'schemaVersion': 'stale', 'rowCount': 1})
    elif reason == 'source':
        schema = getattr(su, f'_{kind.upper()}_INDEX_SCHEMA_VERSION')
        monkeypatch.setattr(su, f'_load_{kind}_index_manifest', lambda uid: {
            'sourceVersion': 'different', 'schemaVersion': schema, 'rowCount': 1})
    elif reason == 'dirty_cap':
        monkeypatch.setattr(su, '_STRICT_INCREMENTAL_MAX_MEMBERS', 0)
    else:
        monkeypatch.setattr(su, '_library_row_count', lambda uid, **kw: 1000000)
    assert getattr(su, f'refresh_user_{kind}_index')('lib', incremental_only=True) is None
    assert metadata.scan_count == 0
    assert su._get_dirty_search_index_filenames('lib', kind) == {'a.jpg'}


@pytest.mark.parametrize('kind', ['sort', 'access'])
def test_strict_rows_success_uses_point_reads_and_acknowledges(rows_ctx, monkeypatch, kind):
    import search_db
    monkeypatch.setattr(search_db, 'load_manifest', lambda uid: {'rowCount': 1})
    metadata, _, _ = rows_ctx
    _seed_row(metadata, 'lib', 'a.jpg', anonymousImageId='a')
    getattr(su, f'refresh_user_{kind}_index')('lib', source_version='v1', force_full=True)
    getattr(su, f'touch_user_{kind}_index_state')('lib')
    su._mark_search_index_dirty_filenames('lib', ['a.jpg'], kinds=(kind,))
    metadata.scan_count = 0
    assert getattr(su, f'refresh_user_{kind}_index')('lib', incremental_only=True)
    assert metadata.scan_count == 0
    assert su._get_dirty_search_index_filenames('lib', kind) == set()


def test_album_dirty_touch_preserves_snapshot_size_and_lineage(albums_ctx, monkeypatch):
    _ready_album_search(monkeypatch)
    albums, _, _ = albums_ctx
    _seed_album(albums, 'lib', 'a1', name='Trip')
    su.refresh_user_albums_index('lib', source_version='v1')
    su.touch_user_albums_index_state('lib')
    manifest = su._load_albums_index_manifest('lib')
    assert manifest['dirty'] and manifest['sourceVersion'] == 'v1' and manifest['rowCount'] == 1
    assert su.refresh_user_albums_index('lib', affected_album_ids={'a1'}, incremental_only=True)


def test_album_full_build_keeps_legacy_cover_read_fallback(albums_ctx, monkeypatch):
    import search_db
    albums, _, _ = albums_ctx
    _seed_album(albums, 'lib', 'a1', name='Trip', filenames='["a.jpg"]')
    monkeypatch.setattr(search_db, 'open_database', Mock(side_effect=RuntimeError('database down')))
    result = su.refresh_user_albums_index('lib')
    assert result.rows[0]['coverFilename'] == 'a.jpg'


def test_strict_primer_forwards_flag_to_all_supported_refreshers(monkeypatch):
    refreshers = {}
    for kind in ('sort', 'access', 'people', 'albums'):
        refreshers[kind] = Mock(return_value=object())
        monkeypatch.setattr(su, f'refresh_user_{kind}_index', refreshers[kind])
    assert su.prime_all_user_indexes_sequentially('lib', wait=True,
        kinds=tuple(refreshers), incremental_only=True,
        affected_person_ids={'p1'}, affected_album_ids={'a1'}) == []
    for refresher in refreshers.values():
        assert refresher.call_args.kwargs['incremental_only'] is True


def test_strict_primer_rejects_lexical_without_full_build(monkeypatch):
    full = Mock(side_effect=AssertionError('full build'))
    monkeypatch.setattr(su, 'refresh_user_lexical_artifacts', full)
    assert su.prime_all_user_indexes_sequentially('lib', wait=True,
        kinds=('lexical',), incremental_only=True) == ['lexical']
    full.assert_not_called()


def test_inline_people_retains_search_work_without_worker_drain(attempt_ctx, monkeypatch):
    monkeypatch.setattr(su, '_get_dirty_search_index_filenames', lambda *a, **k: {'a.jpg'})
    delete = Mock()
    inline_index_merge._attempt('lib', 'people', [(SimpleNamespace(delete_message=delete), SimpleNamespace(id='m', pop_receipt='r'))])
    attempt_ctx.prime.assert_called_once()
    attempt_ctx.drain.assert_not_called()
    attempt_ctx.clear_people.assert_not_called()
    delete.assert_not_called()
    assert attempt_ctx.table.rows


def test_album_membership_cap_defers_without_publishing(albums_ctx, monkeypatch):
    albums, _, _ = albums_ctx
    _seed_album(albums, 'lib', 'a1', name='Trip', filenames='["a.jpg", "b.jpg"]')
    initial = su.refresh_user_albums_index('lib', source_version='v1')
    monkeypatch.setattr(su, '_STRICT_INCREMENTAL_MAX_MEMBERS', 1)
    full = Mock(side_effect=AssertionError('full scan'))
    monkeypatch.setattr(su, '_build_user_albums_index_snapshot', full)
    assert su.refresh_user_albums_index('lib', affected_album_ids={'a1'}, incremental_only=True) is None
    assert su._load_albums_index_blob('lib').source_version == initial.source_version
    full.assert_not_called()


@pytest.mark.parametrize('kind', ['people', 'albums'])
def test_strict_eligibility_rejects_oversize_before_data_load(monkeypatch, kind):
    schema = getattr(su, f'_{kind.upper()}_INDEX_SCHEMA_VERSION')
    monkeypatch.setattr(su, f'_load_{kind}_index_manifest', lambda uid: {
        'rowCount': 5001, 'sourceVersion': 'v1', 'schemaVersion': schema})
    blob = Mock(side_effect=AssertionError('oversized blob'))
    monkeypatch.setattr(su, f'_load_{kind}_index_blob', blob)
    assert getattr(su, f'_{kind}_merge_eligible')('lib', {'one'}, max_rows=5000) is None
    blob.assert_not_called()


@pytest.mark.parametrize('count', [None, '5', -1, True])
def test_unknown_photo_size_disables_strict_photo_artifacts(monkeypatch, count):
    import search_db
    monkeypatch.setattr(search_db, 'load_manifest', lambda uid: {'rowCount': count})
    monkeypatch.setattr(su, '_load_sort_index_manifest', lambda uid: {
        'sourceVersion': 'v1', 'schemaVersion': su._SORT_INDEX_SCHEMA_VERSION})
    assert not su._inline_artifact_within_limit('lib', 'sort', 5000)


def _ready_album_search(monkeypatch, *, seq=0, applied=0, dirty=False):
    import search_db
    from tests.fakes import FakeTable
    manifest = {'sourceVersion': 'search-v1', 'schemaVersion': search_db.SCHEMA_VERSION,
                'deltaSeq': seq, 'lineage': 'catalog-v1'}
    monkeypatch.setattr(search_db, 'load_manifest', lambda uid: manifest)
    monkeypatch.setattr(su, '_load_lexical_index_manifest', lambda uid: {
        'sourceVersion': 'catalog-v1', 'dirty': dirty})
    db = SimpleNamespace(path=search_db._local_path('lib', 'search-v1'),
                         applied_seq=lambda: applied, top_rated=lambda names, limit: names[:limit])
    monkeypatch.setattr(search_db, 'open_database', lambda uid: db)
    table = FakeTable()
    monkeypatch.setitem(su._CTX, 'search_index_dirty_table_client', table)
    monkeypatch.setattr(su, '_flush_dirty_filename_buffer', lambda *a: None)
    return db, table, manifest


@pytest.mark.parametrize('reason', ['absent', 'stale', 'dirty', 'member_pending', 'lookup_error', 'empty_result', 'lineage', 'building'])
def test_strict_album_search_not_ready_retains_artifacts_intent_and_backstop(albums_ctx, attempt_ctx, monkeypatch, caplog, reason):
    import logging
    import json
    import processing_diagnostics
    caplog.set_level(logging.INFO)
    import search_db
    albums, _, blobs = albums_ctx
    _seed_album(albums, 'lib', 'a1', name='Trip', filenames='["a.jpg"]')
    su.refresh_user_albums_index('lib', source_version='v1')
    db, table, manifest = _ready_album_search(monkeypatch, seq=1 if reason == 'stale' else 0,
                                             dirty=reason == 'dirty')
    if reason == 'absent':
        monkeypatch.setattr(search_db, 'open_database', lambda uid: None)
    elif reason == 'member_pending':
        table.upsert_entity({'PartitionKey': su._search_index_dirty_partition_key('lib', 'lexical'), 'RowKey': 'a.jpg'})
    elif reason == 'lookup_error':
        db.top_rated = Mock(side_effect=RuntimeError('search down'))
    elif reason == 'empty_result':
        db.top_rated = lambda *a, **k: []
    elif reason == 'lineage':
        manifest['lineage'] = 'old'
    elif reason == 'building':
        manifest['building'] = True
    before = dict(blobs.blobs)
    store = index_work.WorkStore(attempt_ctx.table)
    store.request('lib', 'albums')
    revision = store._get(store.key('lib', 'albums'))['revision']
    # Remove the unrelated people intent (albums conservatively conflicts
    # with people search work) and run the real strict primer.
    attempt_ctx.table.rows.pop((index_work.PARTITION, store.key('lib', 'people')))
    monkeypatch.setattr(inline_index_merge, '_eligible', lambda *a: (True, None, {'a1'}))
    monkeypatch.setattr(su, 'prime_all_user_indexes_sequentially', _REAL_PRIMER)
    delete = Mock()
    if reason in ('lookup_error', 'empty_result'):
        with pytest.raises(RuntimeError, match='produced no albums'):
            inline_index_merge._attempt('lib', 'albums', [(SimpleNamespace(delete_message=delete), SimpleNamespace(id='m', pop_receipt='r'))])
    else:
        inline_index_merge._attempt('lib', 'albums', [(SimpleNamespace(delete_message=delete), SimpleNamespace(id='m', pop_receipt='r'))])
        expected = {'absent': 'search_database_unavailable', 'stale': 'search_delta_not_applied',
                    'dirty': 'search_source_dirty', 'member_pending': 'album_member_metadata_pending',
                    'lineage': 'search_lineage_mismatch', 'building': 'search_building'}[reason]
        events = [json.loads(r.message.split('processing telemetry ', 1)[1])
                  for r in caplog.records if 'processing telemetry ' in r.message]
        assert any(e.get('reason') == expected and e['outcome'] == 'deferred'
                   and e['intent_revision'] == revision
                   and e['operation_id'] == processing_diagnostics.operation_id(f'lib:albums:{revision}')
                   for e in events)
        assert not any(r.levelno >= logging.ERROR for r in caplog.records)
    assert blobs.blobs == before
    attempt_ctx.clear_albums.assert_not_called()
    delete.assert_not_called()
    assert attempt_ctx.table.rows


_REAL_PRIMER = su.prime_all_user_indexes_sequentially


def test_strict_album_ready_uses_targeted_dependency_reads(albums_ctx, monkeypatch):
    albums, _, _ = albums_ctx
    _seed_album(albums, 'lib', 'a1', filenames='["a.jpg"]')
    su.refresh_user_albums_index('lib', source_version='v1')
    db, table, _ = _ready_album_search(monkeypatch)
    table.upsert_entity({'PartitionKey': su._search_index_dirty_partition_key('lib', 'lexical'), 'RowKey': 'unrelated.jpg'})
    table.query_entities = Mock(side_effect=AssertionError('whole dirty partition read'))
    assert su.refresh_user_albums_index('lib', affected_album_ids={'a1'}, incremental_only=True)


def test_inline_without_real_catalog_serialization_defers(attempt_ctx, monkeypatch):
    monkeypatch.setattr(inline_index_merge, '_serialized_library', lambda uid: False)
    inline_index_merge._attempt('lib', 'people', [])
    attempt_ctx.prime.assert_not_called()
    assert attempt_ctx.table.rows


@pytest.mark.parametrize('scope,conflict', [('people', 'full'), ('albums', 'full'), ('sort', 'light'), ('access', 'full'), ('albums', 'search')])
def test_inline_overlapping_pending_scopes_defer(attempt_ctx, monkeypatch, scope, conflict):
    store = index_work.WorkStore(attempt_ctx.table)
    store.request('lib', scope)
    store.request('lib', conflict)
    inline_index_merge._attempt('lib', scope, [])
    attempt_ctx.prime.assert_not_called()


def test_inline_claim_loss_cannot_upload_or_ack(attempt_ctx, monkeypatch):
    import index_publication
    client = SimpleNamespace(get_blob_properties=lambda: SimpleNamespace(etag='base'), upload_blob=Mock())
    def lose_then_publish(*a, **k):
        guard = index_publication._ACTIVE.get()
        assert guard is not None and guard.thread.is_alive()
        guard.lost = RuntimeError('claim lost')
        index_publication.fenced(client).upload_blob(b'new', overwrite=True)
    attempt_ctx.prime.side_effect = lose_then_publish
    with pytest.raises(RuntimeError, match='lease lost'):
        inline_index_merge._attempt('lib', 'people', [])
    client.upload_blob.assert_not_called()
    attempt_ctx.clear_people.assert_not_called()
    assert attempt_ctx.table.rows


def test_inline_publication_renews_claim_during_merge(attempt_ctx, monkeypatch):
    import threading
    original = index_work.WorkStore
    renewed = threading.Event()
    class ShortStore(original):
        def __init__(self, table):
            super().__init__(table, lease_seconds=0.15)
        def renew(self, claim):
            super().renew(claim)
            renewed.set()
    monkeypatch.setattr(index_work, 'WorkStore', ShortStore)
    def merge(*a, **k):
        assert renewed.wait(timeout=2)
        return []
    attempt_ctx.prime.side_effect = merge
    inline_index_merge._attempt('lib', 'people', [])
    assert renewed.is_set() and not attempt_ctx.table.rows


def test_strict_prebuild_manifest_change_cannot_publish():
    capture = [(SimpleNamespace(get_blob_properties=lambda: SimpleNamespace(etag='changed')),
                {'etag': 'base'})]
    with pytest.raises(RuntimeError, match='base changed'):
        su._strict_check_publication(capture)


@pytest.mark.parametrize('kind', ['people', 'albums'])
def test_strict_merge_race_before_publication_retains_previous_data(request, monkeypatch, kind):
    table, _, blobs = request.getfixturevalue(f'{kind}_ctx')
    if kind == 'people':
        _seed_person(table, 'lib', 'one', name='Alice')
    else:
        _seed_album(table, 'lib', 'one')
        _ready_album_search(monkeypatch)
    refresh = getattr(su, f'refresh_user_{kind}_index')
    refresh('lib', source_version='v1')
    fn = '_incremental_people_index_snapshot_by_ids' if kind == 'people' else '_incremental_albums_index_snapshot'
    original = getattr(su, fn)
    name = 'lexical-index/' + getattr(su, f'_{kind}_index_manifest_blob_name')('lib')
    data_name = 'lexical-index/' + getattr(su, f'_{kind}_index_json_blob_name')('lib')
    before = blobs.blobs[data_name]
    def racing(*a, **k):
        result = original(*a, **k)
        blobs.blobs[name] += b' '
        return result
    monkeypatch.setattr(su, fn, racing)
    with pytest.raises(RuntimeError, match='base changed'):
        refresh('lib', incremental_only=True, **{
            'affected_person_ids' if kind == 'people' else 'affected_album_ids': {'one'}})
    assert blobs.blobs[data_name] == before


@pytest.mark.parametrize('kind', ['people', 'albums'])
def test_capped_previous_snapshot_requires_exact_manifest_count(monkeypatch, kind):
    schema = getattr(su, f'_{kind.upper()}_INDEX_SCHEMA_VERSION')
    monkeypatch.setattr(su, f'_load_{kind}_index_manifest', lambda uid: {
        'rowCount': 1, 'sourceVersion': 'v1', 'schemaVersion': schema})
    monkeypatch.setattr(su, f'_load_{kind}_index_blob', lambda uid: SimpleNamespace(
        source_version='v1', schema_version=schema, rows=[{}, {}]))
    assert getattr(su, f'_{kind}_merge_eligible')('lib', {'one'}, max_rows=5000) is None


def test_strict_primer_cannot_lose_guard_to_background_thread(monkeypatch):
    refresh = Mock(side_effect=AssertionError('unguarded background build'))
    monkeypatch.setattr(su, 'refresh_user_people_index', refresh)
    assert su.prime_all_user_indexes_sequentially('lib', kinds=('people',), incremental_only=True) == ['people']
    refresh.assert_not_called()


@pytest.mark.parametrize('kind', ['sort', 'access'])
def test_strict_rows_manifest_race_cannot_publish_or_ack(rows_ctx, monkeypatch, kind):
    import search_db
    metadata, _, blobs = rows_ctx
    monkeypatch.setattr(search_db, 'load_manifest', lambda uid: {'rowCount': 1})
    _seed_row(metadata, 'lib', 'a.jpg', anonymousImageId='a')
    refresh = getattr(su, f'refresh_user_{kind}_index')
    refresh('lib', source_version='v1', force_full=True)
    su._mark_search_index_dirty_filenames('lib', ['a.jpg'], kinds=(kind,))
    manifest_name = 'lexical-index/' + getattr(su, f'_{kind}_index_manifest_blob_name')('lib')
    data_name = 'lexical-index/' + getattr(su, f'_{kind}_index_json_blob_name')('lib')
    before = blobs.blobs[data_name]
    original = su._timed_fetch
    def racing(*a):
        result = original(*a)
        blobs.blobs[manifest_name] += b' '
        return result
    monkeypatch.setattr(su, '_timed_fetch', racing)
    with pytest.raises(RuntimeError, match='base changed'):
        refresh('lib', incremental_only=True)
    assert blobs.blobs[data_name] == before
    assert su._get_dirty_search_index_filenames('lib', kind) == {'a.jpg'}


@pytest.mark.parametrize('kind', ['people', 'albums'])
def test_strict_manifest_cas_race_does_not_bless_cache(request, monkeypatch, kind):
    from azure.core.exceptions import ResourceModifiedError
    table, _, blobs = request.getfixturevalue(f'{kind}_ctx')
    if kind == 'people':
        _seed_person(table, 'lib', 'one', name='Alice')
    else:
        _seed_album(table, 'lib', 'one')
        _ready_album_search(monkeypatch)
    refresh = getattr(su, f'refresh_user_{kind}_index')
    refresh('lib', source_version='v1')
    data_name = getattr(su, f'_{kind}_index_json_blob_name')('lib')
    manifest_name = 'lexical-index/' + getattr(su, f'_{kind}_index_manifest_blob_name')('lib')
    original = blobs.get_blob_client
    def client(container, blob):
        result = original(container, blob)
        if blob == data_name:
            upload = result.upload_blob
            def racing(*a, **kw):
                outcome = upload(*a, **kw)
                blobs.blobs[manifest_name] += b' '
                return outcome
            result.upload_blob = racing
        return result
    monkeypatch.setattr(blobs, 'get_blob_client', client)
    with pytest.raises(ResourceModifiedError, match='CAS conflict'):
        refresh('lib', incremental_only=True, **{
            'affected_person_ids' if kind == 'people' else 'affected_album_ids': {'one'}})
    assert getattr(su, f'_{kind.upper()}_INDEX_CACHE')['lib']['source_version'] == 'v1'


@pytest.mark.parametrize('kind', ['people', 'albums', 'sort', 'access'])
def test_strict_without_conditional_storage_defers_without_build(monkeypatch, kind):
    monkeypatch.setattr(su, '_get_blob_client', lambda *a: SimpleNamespace(upload_blob=Mock()))
    full = Mock(side_effect=AssertionError('full build'))
    monkeypatch.setattr(su, f'_build_user_{kind}_index_snapshot', full)
    extra = {'affected_person_ids': {'one'}} if kind == 'people' else {
        'affected_album_ids': {'one'}} if kind == 'albums' else {}
    assert getattr(su, f'refresh_user_{kind}_index')('lib', incremental_only=True, **extra) is None
    full.assert_not_called()
