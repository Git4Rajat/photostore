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


@pytest.mark.parametrize('scope', ['full', 'vector', 'compact', 'light', 'search', 'tag_embedding'])
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
    monkeypatch.setattr(app, '_pending_person_labels', lambda uid: {'p1'})
    ok, _, _ = inline_index_merge._eligible('lib', 'people')
    assert ok is False


def test_eligible_people_bails_when_albums_also_dirty(monkeypatch):
    monkeypatch.setattr(app, '_pending_person_labels', lambda uid: set())
    monkeypatch.setattr(app, 'get_dirty_people', lambda uid: {'p1'})
    monkeypatch.setattr(app, 'get_dirty_albums', lambda uid: {'a1'})
    ok, _, _ = inline_index_merge._eligible('lib', 'people')
    assert ok is False


def test_eligible_people_defers_to_the_shared_predicate(monkeypatch):
    monkeypatch.setattr(app, '_pending_person_labels', lambda uid: set())
    monkeypatch.setattr(app, 'get_dirty_people', lambda uid: {'p1'})
    monkeypatch.setattr(app, 'get_dirty_albums', lambda uid: set())
    monkeypatch.setattr(su, '_people_merge_eligible', lambda uid, ids: ids == {'p1'})
    ok, dirty_people, dirty_albums = inline_index_merge._eligible('lib', 'people')
    assert ok is True and dirty_people == {'p1'} and dirty_albums is None


def test_eligible_albums_defers_to_the_shared_predicate(monkeypatch):
    monkeypatch.setattr(app, 'get_dirty_albums', lambda uid: {'a1'})
    monkeypatch.setattr(su, '_albums_merge_eligible', lambda uid, ids: True)
    ok, dirty_people, dirty_albums = inline_index_merge._eligible('lib', 'albums')
    assert ok is True and dirty_people is None and dirty_albums == {'a1'}


def test_eligible_sort_access_defer_to_the_shared_predicate(monkeypatch):
    monkeypatch.setattr(su, '_rows_merge_eligible', lambda uid, kind: kind == 'sort')
    assert inline_index_merge._eligible('lib', 'sort')[0] is True
    assert inline_index_merge._eligible('lib', 'access')[0] is False


@pytest.fixture
def attempt_ctx(monkeypatch):
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
    attempt_ctx.drain.assert_called_once_with('lib', 'people')
    attempt_ctx.clear_people.assert_called_once_with('lib', {'p1'})
    attempt_ctx.clear_albums.assert_not_called()
    assert deleted == [('m1', 'r1')]
    # WorkStore row for this key is gone -- finish() succeeded.
    assert table.rows == {} if (table := attempt_ctx.table) else True


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


def test_attempt_respects_the_row_count_ceiling(attempt_ctx, monkeypatch):
    monkeypatch.setattr(su, '_library_row_count', lambda uid: 10 ** 9)
    inline_index_merge._attempt('lib', 'people', [])
    attempt_ctx.prime.assert_not_called()


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
