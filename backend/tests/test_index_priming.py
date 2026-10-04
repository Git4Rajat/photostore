"""Unit tests for the sequential index primer (storage_utils.
prime_all_user_indexes_sequentially / get_user_index_readiness), the backend
readiness route (/api/photos/index-status), and the tools-role index builder
routes (/api/tools/indexes/build + /status).

Added alongside the 2026-09-30 microsvcpoc-dev HAR investigation: on a
genuinely cold account, get_user_sort_index/get_user_lexical_index/
get_user_albums_index/get_user_people_index each independently kick their
own background rebuild thread, so a session start that hits all four could
land four concurrent full-account Table scans at once -- observed live to
help trigger a ContainerBackOff crash loop. The heavy build was then moved
off the 1Gi `backend` role onto the 2vCPU/4Gi `tools` role: backend only
reports readiness (cheap manifest reads) and mints SAS tokens; tools runs
the actual build. These tests pin: (1) the primer runs the four
refresh_user_*_index calls one at a time, not in parallel, (2) it's
single-flighted per user, (3) it goes through each kind's own rebuild lock so
a concurrent page-level request never races it, (4) one kind failing doesn't
stop the rest, (5) the readiness check never triggers a rebuild as a side
effect, and (6) the tools build route kicks the primer only when incomplete
while backend's index-status never builds.
"""
from __future__ import annotations

import json
import time

import threading

import pytest

import pytest

import app
from routes.photos import photos_index_status
from routes.tools import tools_build_indexes, tools_indexes_status
import storage_utils


# --- prime_all_user_indexes_sequentially -------------------------------------

def _patch_refreshers(monkeypatch, calls, *, slow_kind=None, entered=None, release=None, raise_kind=None):
    def _make(kind):
        def _fn(user_id, *, source_version=None):
            if raise_kind == kind:
                calls.append(kind)
                raise RuntimeError(f'{kind} refresh boom')
            if slow_kind == kind:
                entered.set()
                release.wait(timeout=5)
            calls.append(kind)
            return None
        return _fn

    monkeypatch.setattr(storage_utils, 'refresh_user_sort_index', _make('sort'))
    monkeypatch.setattr(storage_utils, 'refresh_user_access_index', _make('access'))
    monkeypatch.setattr(storage_utils, 'refresh_user_lexical_index', _make('lexical'))
    monkeypatch.setattr(storage_utils, 'refresh_user_albums_index', _make('albums'))
    monkeypatch.setattr(storage_utils, 'refresh_user_people_index', _make('people'))


def _wait_for_prime_lock_free(user_id: str) -> None:
    lock = storage_utils._INDEX_PRIME_LOCKS.lock_for(user_id)
    for _ in range(50):
        if lock.acquire(blocking=False):
            lock.release()
            return
        time.sleep(0.05)
    pytest.fail('sequential index priming never completed')


def test_prime_all_user_indexes_sequentially_calls_refresh_in_order(monkeypatch):
    calls = []
    _patch_refreshers(monkeypatch, calls)

    storage_utils.prime_all_user_indexes_sequentially('lib-order')
    _wait_for_prime_lock_free('lib-order')

    assert calls == ['sort', 'access', 'lexical', 'albums', 'people']


def test_prime_all_user_indexes_sequentially_is_single_flighted(monkeypatch):
    calls = []
    entered = threading.Event()
    release = threading.Event()
    _patch_refreshers(monkeypatch, calls, slow_kind='sort', entered=entered, release=release)

    storage_utils.prime_all_user_indexes_sequentially('lib-flight')
    assert entered.wait(timeout=5), 'first priming run never started'

    # A second call while the first is still mid-flight must no-op, not
    # start a second concurrent pass.
    storage_utils.prime_all_user_indexes_sequentially('lib-flight')
    time.sleep(0.1)
    assert calls == []  # 'sort' hasn't finished yet in either run

    release.set()
    _wait_for_prime_lock_free('lib-flight')
    assert calls == ['sort', 'access', 'lexical', 'albums', 'people']  # only ever ran once


def test_prime_all_user_indexes_sequentially_skips_kind_whose_own_lock_is_held(monkeypatch):
    """If a page-level request is already rebuilding one kind (e.g. a user
    who hit /api/photos/sort-index a moment earlier), the primer must not
    start a second, parallel rebuild of that same kind -- it should see the
    lock held and move on, exactly like today's existing dedup behavior."""
    calls = []
    _patch_refreshers(monkeypatch, calls)

    sort_lock = storage_utils._SORT_INDEX_REBUILD_LOCKS.lock_for('lib-skip')
    assert sort_lock.acquire(blocking=False)
    try:
        storage_utils.prime_all_user_indexes_sequentially('lib-skip')
        _wait_for_prime_lock_free('lib-skip')
    finally:
        sort_lock.release()

    assert calls == ['access', 'lexical', 'albums', 'people']  # sort skipped, rest still ran


def test_prime_all_user_indexes_sequentially_continues_after_one_kind_raises(monkeypatch):
    calls = []
    _patch_refreshers(monkeypatch, calls, raise_kind='lexical')

    storage_utils.prime_all_user_indexes_sequentially('lib-raise')
    _wait_for_prime_lock_free('lib-raise')

    assert calls == ['sort', 'access', 'lexical', 'albums', 'people']  # lexical failed but didn't block the rest


def test_prime_all_user_indexes_sequentially_noop_for_blank_user_id(monkeypatch):
    calls = []
    _patch_refreshers(monkeypatch, calls)

    storage_utils.prime_all_user_indexes_sequentially('   ')

    assert calls == []


def test_prime_all_user_indexes_sequentially_wait_runs_on_calling_thread(monkeypatch):
    # wait=True is what /api/tools/indexes/build now uses so the HTTP request
    # spans the whole build (a scale-to-zero tools replica otherwise gets torn
    # down by the autoscaler mid-build, per the route's comment) -- pin that
    # the call doesn't return until every refresh has actually run, i.e. it's
    # not just spawning the usual background thread and returning early.
    calls = []
    _patch_refreshers(monkeypatch, calls)

    storage_utils.prime_all_user_indexes_sequentially('lib-wait', wait=True)

    assert calls == ['sort', 'access', 'lexical', 'albums', 'people']


def test_prime_all_user_indexes_sequentially_wait_blocks_for_in_flight_prime(monkeypatch):
    # If another prime for the same user is already running (e.g. two
    # overlapping triggers on the same replica), wait=True must block until
    # it finishes rather than returning immediately -- and must not re-run
    # the build itself once it does.
    calls = []
    entered = threading.Event()
    release = threading.Event()
    _patch_refreshers(monkeypatch, calls, slow_kind='sort', entered=entered, release=release)

    storage_utils.prime_all_user_indexes_sequentially('lib-wait-blocked')
    assert entered.wait(timeout=5), 'first priming run never started'

    waited_done = threading.Event()

    def _waiter():
        storage_utils.prime_all_user_indexes_sequentially('lib-wait-blocked', wait=True)
        waited_done.set()

    waiter_thread = threading.Thread(target=_waiter, daemon=True)
    waiter_thread.start()
    time.sleep(0.1)
    assert not waited_done.is_set(), 'wait=True returned before the in-flight prime finished'

    release.set()
    waiter_thread.join(timeout=5)
    assert waited_done.is_set()
    assert calls == ['sort', 'access', 'lexical', 'albums', 'people']  # only ran once, not re-run by the waiter


def test_prime_emits_progress_at_start_each_step_and_end(monkeypatch):
    calls = []
    _patch_refreshers(monkeypatch, calls)
    # Keep readiness cheap/deterministic for the progress snapshots.
    monkeypatch.setattr(storage_utils, 'get_user_index_readiness',
                        lambda uid: {'sort': True, 'lexical': True, 'albums': True, 'people': True})
    progress = []

    storage_utils.prime_all_user_indexes_sequentially(
        'lib-progress', on_progress=lambda indexes, building: progress.append(building),
    )
    _wait_for_prime_lock_free('lib-progress')

    # start(True) + one per kind(True x5) + terminal(False) = 7 emissions.
    assert progress == [True, True, True, True, True, True, False]


def test_index_prime_in_progress_reflects_held_lock():
    assert storage_utils.index_prime_in_progress('lib-probe') is False
    lock = storage_utils._INDEX_PRIME_LOCKS.lock_for('lib-probe')
    assert lock.acquire(blocking=False)
    try:
        assert storage_utils.index_prime_in_progress('lib-probe') is True
    finally:
        lock.release()
    assert storage_utils.index_prime_in_progress('lib-probe') is False
    assert storage_utils.index_prime_in_progress('') is False


# --- get_user_index_readiness -------------------------------------------------

def test_get_user_index_readiness_reads_manifests_without_triggering_rebuilds(monkeypatch):
    rebuild_calls = []
    monkeypatch.setattr(storage_utils, '_load_sort_index_manifest', lambda uid: {'sourceVersion': 'v1'})
    monkeypatch.setattr(storage_utils, '_load_lexical_index_manifest', lambda uid: {})
    monkeypatch.setattr(storage_utils, '_load_albums_index_manifest', lambda uid: {'sourceVersion': ''})
    monkeypatch.setattr(storage_utils, '_load_people_index_manifest', lambda uid: {'sourceVersion': 'v4'})
    monkeypatch.setattr(storage_utils, '_rebuild_sort_index_in_background', lambda *a, **k: rebuild_calls.append('sort'))
    monkeypatch.setattr(storage_utils, '_rebuild_lexical_index_in_background', lambda *a, **k: rebuild_calls.append('lexical'))

    result = storage_utils.get_user_index_readiness('lib-read')

    assert result == {'sort': True, 'lexical': False, 'albums': False, 'people': True}
    assert rebuild_calls == []  # a pure read -- never kicks a rebuild


def test_get_index_manifest_summary_returns_none_when_never_built(monkeypatch):
    monkeypatch.setattr(storage_utils, '_load_lexical_index_manifest', lambda uid: {})
    assert storage_utils.get_index_manifest_summary('u', 'lexical') is None


def test_get_index_manifest_summary_returns_version_and_dirty(monkeypatch):
    monkeypatch.setattr(storage_utils, '_load_sort_index_manifest',
                        lambda uid: {'sourceVersion': 'v9', 'updatedAt': '2026-01-01', 'dirty': True})
    summary = storage_utils.get_index_manifest_summary('u', 'sort')
    assert summary == {'source_version': 'v9', 'updated_at': '2026-01-01', 'dirty': True}


def test_get_user_index_build_state_flags_dirty_as_needs_rebuild(monkeypatch):
    monkeypatch.setattr(storage_utils, '_load_sort_index_manifest', lambda uid: {'sourceVersion': 'v1', 'dirty': True})
    monkeypatch.setattr(storage_utils, '_load_lexical_index_manifest', lambda uid: {'sourceVersion': 'v1'})
    monkeypatch.setattr(storage_utils, '_load_albums_index_manifest', lambda uid: {'sourceVersion': 'v1'})
    monkeypatch.setattr(storage_utils, '_load_people_index_manifest', lambda uid: {'sourceVersion': 'v1'})

    state = storage_utils.get_user_index_build_state('u')
    assert state['ready'] is True          # all built
    assert state['needs_rebuild'] is True  # but sort is dirty
    assert state['indexes'] == {'sort': True, 'lexical': True, 'albums': True, 'people': True}


def test_get_user_index_build_state_flags_missing_as_needs_rebuild(monkeypatch):
    monkeypatch.setattr(storage_utils, '_load_sort_index_manifest', lambda uid: {'sourceVersion': 'v1'})
    monkeypatch.setattr(storage_utils, '_load_lexical_index_manifest', lambda uid: {})  # missing
    monkeypatch.setattr(storage_utils, '_load_albums_index_manifest', lambda uid: {'sourceVersion': 'v1'})
    monkeypatch.setattr(storage_utils, '_load_people_index_manifest', lambda uid: {'sourceVersion': 'v1'})

    state = storage_utils.get_user_index_build_state('u')
    assert state['ready'] is False
    assert state['needs_rebuild'] is True
    assert state['indexes']['lexical'] is False


def test_get_user_index_readiness_all_false_for_blank_user_id():
    assert storage_utils.get_user_index_readiness('') == {
        'sort': False, 'lexical': False, 'albums': False, 'people': False,
    }


@pytest.fixture
def route_ctx(monkeypatch):
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('owner', None))


# --- GET /api/photos/index-status (backend: readiness only, never builds) -----

def test_backend_index_status_never_builds(monkeypatch, route_ctx):
    monkeypatch.setattr(app, 'get_user_index_readiness', lambda uid: {
        'sort': True, 'lexical': False, 'albums': True, 'people': False,
    })
    primed = []
    # backend no longer imports the primer, but pin that even if something tried
    # to reach it through app.* it isn't called from this route.
    monkeypatch.setattr(app, 'prime_all_user_indexes_sequentially', lambda *a, **k: primed.append(a))

    with app.app.test_request_context('/api/photos/index-status'):
        response = photos_index_status()

    assert primed == []  # read-only: backend must never build (OOM risk it was moved off)
    payload = response.get_json()
    assert payload['ready'] is False
    assert payload['indexes'] == {'sort': True, 'lexical': False, 'albums': True, 'people': False}


def test_backend_index_status_ready_true_when_all_built(monkeypatch, route_ctx):
    monkeypatch.setattr(app, 'get_user_index_readiness', lambda uid: {
        'sort': True, 'lexical': True, 'albums': True, 'people': True,
    })

    with app.app.test_request_context('/api/photos/index-status'):
        response = photos_index_status()

    assert response.get_json()['ready'] is True


# --- POST /api/tools/indexes/build (tools: the actual builder) ----------------

def _build_state(indexes, needs_rebuild):
    return {'ready': all(indexes.values()), 'needs_rebuild': needs_rebuild, 'indexes': indexes}


def test_tools_build_does_not_prime_when_all_built_and_clean(monkeypatch, route_ctx):
    monkeypatch.setattr(app, 'get_user_index_build_state', lambda uid: _build_state(
        {'sort': True, 'lexical': True, 'albums': True, 'people': True}, needs_rebuild=False))
    monkeypatch.setattr(app, 'index_build_needed', lambda uid: False)
    queued = []
    monkeypatch.setattr(app, 'enqueue_index_build', lambda uid, reason='': queued.append(uid) or 'queued')

    with app.app.test_request_context('/api/tools/indexes/build', method='POST'):
        response = tools_build_indexes()

    assert queued == []
    payload = response.get_json()
    assert payload['ok'] is True
    assert payload['ready'] is True
    assert payload['building'] is False and payload['queued'] == 'not_needed'


def test_tools_build_enqueues_when_any_index_missing(monkeypatch, route_ctx):
    monkeypatch.setattr(app, 'get_user_index_build_state', lambda uid: _build_state(
        {'sort': True, 'lexical': False, 'albums': True, 'people': True}, needs_rebuild=True))
    monkeypatch.setattr(app, 'index_build_needed', lambda uid: True)
    queued = []
    monkeypatch.setattr(app, 'enqueue_index_build', lambda uid, reason='': queued.append((uid, reason)) or 'queued')
    # The route must NOT build inline any more (that is what orphaned jobs on the scale-to-zero tools app).
    monkeypatch.setattr(app, 'prime_all_user_indexes_sequentially', lambda *a, **k: (_ for _ in ()).throw(AssertionError('no inline build')))

    with app.app.test_request_context('/api/tools/indexes/build', method='POST'):
        response = tools_build_indexes()

    assert queued == [('owner', 'client')]
    payload = response.get_json()
    assert payload['ok'] is True and payload['ready'] is False
    assert payload['building'] is True and payload['queued'] == 'queued'
    assert payload['indexes']['lexical'] is False


def test_tools_build_enqueues_when_built_but_dirty_without_blocking_the_gate(monkeypatch, route_ctx):
    # All built (ready=True) but one dirty -> still queue a rebuild; ready stays
    # True so the frontend gate is not held.
    monkeypatch.setattr(app, 'get_user_index_build_state', lambda uid: _build_state(
        {'sort': True, 'lexical': True, 'albums': True, 'people': True}, needs_rebuild=True))
    monkeypatch.setattr(app, 'index_build_needed', lambda uid: True)
    monkeypatch.setattr(app, 'enqueue_index_build', lambda uid, reason='': 'already_active')

    with app.app.test_request_context('/api/tools/indexes/build', method='POST'):
        payload = tools_build_indexes().get_json()

    assert payload['ready'] is True and payload['building'] is True and payload['queued'] == 'already_active'


def test_tools_build_reports_not_building_when_queue_unavailable(monkeypatch, route_ctx):
    monkeypatch.setattr(app, 'get_user_index_build_state', lambda uid: _build_state(
        {'sort': False, 'lexical': False, 'albums': False, 'people': False}, needs_rebuild=True))
    monkeypatch.setattr(app, 'index_build_needed', lambda uid: True)
    monkeypatch.setattr(app, 'enqueue_index_build', lambda uid, reason='': 'unavailable')

    with app.app.test_request_context('/api/tools/indexes/build', method='POST'):
        payload = tools_build_indexes().get_json()

    assert payload['ok'] is True and payload['building'] is False and payload['queued'] == 'unavailable'


def test_progress_callback_writes_job_row_and_derives_summaries_before_done(monkeypatch):
    job_rows = []
    monkeypatch.setattr(app, '_upsert_job_status', lambda job_id, uid, jt, status, **f: job_rows.append((job_id, uid, jt, status, f)))
    derived = []
    monkeypatch.setattr(app, 'refresh_user_explore_summary', lambda uid: derived.append('explore'))
    monkeypatch.setattr(app, 'refresh_user_timeline_summary', lambda uid: derived.append('timeline'))
    monkeypatch.setattr(app, 'storage_utils_ensure_search_db', lambda uid: derived.append('searchdb'))

    cb = app._index_build_progress_callback('owner')
    cb({'sort': True, 'lexical': False, 'albums': False, 'people': False}, True)
    assert derived == [] and [r[3] for r in job_rows] == ['running']  # still building
    # a failing derived step must not fail the build
    monkeypatch.setattr(app, 'refresh_user_timeline_summary', lambda uid: (_ for _ in ()).throw(RuntimeError('boom')))
    cb({'sort': True, 'lexical': True, 'albums': True, 'people': True}, False)
    assert derived == ['explore'] or derived == ['explore', 'searchdb']
    assert 'searchdb' in derived
    assert [r[3] for r in job_rows] == ['running', 'done'] and job_rows[0][2] == app.INDEX_BUILD_JOB_TYPE
    assert job_rows[-1][0] == app._index_build_job_id('owner')


def test_progress_callback_marks_failed_when_not_ready(monkeypatch):
    rows = []
    monkeypatch.setattr(app, '_upsert_job_status', lambda job_id, uid, jt, status, **f: rows.append(status))
    monkeypatch.setattr(app, 'refresh_user_explore_summary', lambda uid: None)
    app._index_build_progress_callback('owner')({'sort': True, 'lexical': False, 'albums': True, 'people': True}, False)
    assert rows == ['failed']


# --- GET /api/tools/indexes/status --------------------------------------------

def test_tools_status_reports_ready_without_building(monkeypatch, route_ctx):
    monkeypatch.setattr(app, 'get_user_index_readiness', lambda uid: {
        'sort': True, 'lexical': True, 'albums': True, 'people': True,
    })
    monkeypatch.setattr(app, '_index_build_job_active', lambda uid: False)

    with app.app.test_request_context('/api/tools/indexes/status'):
        response = tools_indexes_status()

    payload = response.get_json()
    assert payload['ready'] is True
    assert payload['building'] is False


def test_tools_status_reports_building_from_the_shared_job_row(monkeypatch, route_ctx):
    monkeypatch.setattr(app, 'get_user_index_readiness', lambda uid: {
        'sort': True, 'lexical': False, 'albums': False, 'people': False,
    })
    monkeypatch.setattr(app, '_index_build_job_active', lambda uid: True)

    with app.app.test_request_context('/api/tools/indexes/status'):
        response = tools_indexes_status()

    payload = response.get_json()
    assert payload['ready'] is False
    assert payload['building'] is True


# --- ipworker rebuild trigger helpers -----------------------------------------

class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _NoopThread:
    def start(self):
        pass


class _ImmediateThread:
    def __init__(self, target, sink):
        self._target = target
        sink.append(target)

    def start(self):
        if self._target:
            self._target()


def test_ipwork_message_user_id_parses_both_key_spellings():
    assert app._ipwork_message_user_id(_FakeMessage('{"user_id": "u1"}')) == 'u1'
    assert app._ipwork_message_user_id(_FakeMessage('{"userId": "u2"}')) == 'u2'
    assert app._ipwork_message_user_id(_FakeMessage('not json')) == ''
    assert app._ipwork_message_user_id(_FakeMessage('{}')) == ''


def test_trigger_tools_index_rebuild_noop_without_env(monkeypatch):
    monkeypatch.delenv('TOOLS_INTERNAL_URL', raising=False)
    started = []
    monkeypatch.setattr(app.threading, 'Thread', lambda *a, **k: started.append((a, k)) or _NoopThread())

    app._trigger_tools_index_rebuild('u1')

    assert started == []  # no env -> no thread spawned at all


def test_trigger_tools_index_rebuild_posts_with_bearer_token(monkeypatch):
    monkeypatch.setenv('TOOLS_INTERNAL_URL', 'https://tools.example.invalid')
    monkeypatch.setattr(app, '_issue_session_for', lambda uid: f'token-for-{uid}')

    captured_fns = []
    monkeypatch.setattr(app.threading, 'Thread',
                        lambda target=None, **k: _ImmediateThread(target, captured_fns))

    posts = []

    class _FakeRequests:
        @staticmethod
        def post(url, json=None, headers=None, timeout=None):
            posts.append({'url': url, 'headers': headers, 'timeout': timeout})

    import sys
    monkeypatch.setitem(sys.modules, 'requests', _FakeRequests)

    app._trigger_tools_index_rebuild('u1')

    assert len(posts) == 1
    assert posts[0]['url'] == 'https://tools.example.invalid/api/tools/indexes/build'
    assert posts[0]['headers']['Authorization'] == 'Bearer token-for-u1'


# --- queue-based builds (worker) ------------------------------------------------

class _FakeQueue:
    def __init__(self):
        self.sent = []

    def send_message(self, body):
        self.sent.append(json.loads(body))


def test_enqueue_index_build_queues_once_and_marks_the_job(monkeypatch):
    queue = _FakeQueue()
    monkeypatch.setattr(app, 'library_ops_queue_client', queue)
    monkeypatch.setattr(app, '_index_build_job_active', lambda uid: False)
    rows = []
    monkeypatch.setattr(app, '_upsert_job_status', lambda job_id, uid, jt, status, **f: rows.append((job_id, status)))

    assert app.enqueue_index_build('lib-1', reason='x') == 'queued'
    assert queue.sent == [{'type': 'index_build', 'userId': 'lib-1', 'jobId': 'index-build-lib-1', 'reason': 'x'}]
    assert rows == [('index-build-lib-1', 'queued')]


def test_enqueue_index_build_dedupes_against_an_active_job(monkeypatch):
    queue = _FakeQueue()
    monkeypatch.setattr(app, 'library_ops_queue_client', queue)
    monkeypatch.setattr(app, '_index_build_job_active', lambda uid: True)
    assert app.enqueue_index_build('lib-1') == 'already_active' and queue.sent == []


def test_enqueue_index_build_unavailable_without_queue_or_on_send_failure(monkeypatch):
    monkeypatch.setattr(app, '_index_build_job_active', lambda uid: False)
    monkeypatch.setattr(app, 'library_ops_queue_client', None)
    assert app.enqueue_index_build('lib-1') == 'unavailable'

    class _Boom:
        def send_message(self, body):
            raise RuntimeError('queue down')

    rows = []
    monkeypatch.setattr(app, 'library_ops_queue_client', _Boom())
    monkeypatch.setattr(app, '_upsert_job_status', lambda job_id, uid, jt, status, **f: rows.append(status))
    assert app.enqueue_index_build('lib-1') == 'unavailable' and rows == ['queued', 'failed']
    assert app.enqueue_index_build('') == 'unavailable'


def test_index_build_job_active_requires_fresh_queued_or_running_row(monkeypatch):
    from datetime import datetime, timedelta, timezone
    monkeypatch.setattr(app, 'jobs_table_client', object())
    fresh = datetime.now(timezone.utc).isoformat()
    stale = (datetime.now(timezone.utc) - timedelta(minutes=app.CLUSTERING_ACTIVE_JOB_STALE_MINUTES + 1)).isoformat()
    for status, updated, expected in (('running', fresh, True), ('queued', fresh, True), ('done', fresh, False), ('running', stale, False)):
        monkeypatch.setattr(app, '_get_job_row', lambda pk, jid, s=status, u=updated: {'status': s, 'updatedAt': u})
        assert app._index_build_job_active('lib-1') is expected
    monkeypatch.setattr(app, '_get_job_row', lambda pk, jid: None)
    assert app._index_build_job_active('lib-1') is False


def test_run_index_build_job_heartbeats_while_a_long_step_runs(monkeypatch):
    import threading as _t
    monkeypatch.setattr(app, 'INDEX_BUILD_HEARTBEAT_SECONDS', 0.02)
    rows = []
    lock = _t.Lock()

    def upsert(job_id, uid, jt, status, **f):
        with lock:
            rows.append(status)

    monkeypatch.setattr(app, '_upsert_job_status', upsert)
    monkeypatch.setattr(app, 'get_user_index_readiness', lambda uid: {'sort': True})
    monkeypatch.setattr(app, 'storage_utils_ensure_sort_current', lambda uid: False)

    def slow_prime(uid, *, on_progress=None, wait=False):
        assert wait is True
        time.sleep(0.2)  # one long single step with no progress callbacks

    monkeypatch.setattr(app, 'prime_all_user_indexes_sequentially', slow_prime)
    app._run_index_build_job('lib-1', 'index-build-lib-1')
    assert rows[0] == 'running' and rows.count('running') >= 3  # initial + several heartbeats


def test_run_index_build_job_marks_failed_and_reraises_for_queue_retry(monkeypatch):
    rows = []
    monkeypatch.setattr(app, '_upsert_job_status', lambda job_id, uid, jt, status, **f: rows.append(status))
    monkeypatch.setattr(app, 'storage_utils_ensure_sort_current', lambda uid: False)

    def boom(uid, **k):
        raise RuntimeError('oom-ish')

    monkeypatch.setattr(app, 'prime_all_user_indexes_sequentially', boom)
    with pytest.raises(RuntimeError):
        app._run_index_build_job('lib-1', 'index-build-lib-1')
    assert rows[0] == 'running' and rows[-1] == 'failed'


def test_trigger_prefers_the_queue_over_the_tools_http_path(monkeypatch):
    monkeypatch.setattr(app, 'library_ops_queue_client', _FakeQueue())
    monkeypatch.setattr(app, '_TOOLS_REBUILD_TRIGGER_LAST', {})
    monkeypatch.setenv('TOOLS_INTERNAL_URL', 'https://tools.example')
    seen = []
    monkeypatch.setattr(app, 'enqueue_index_build', lambda uid, reason='': seen.append(uid) or 'queued')
    posted = []
    monkeypatch.setattr('requests.post', lambda *a, **k: posted.append(a))
    app._trigger_tools_index_rebuild('lib-9')
    assert seen == ['lib-9'] and posted == []


def test_worker_processes_an_index_build_message_and_acks_it(monkeypatch):
    """End to end through the real message processor: an index_build message from
    the library-ops queue runs the build job and is deleted only on success."""
    ran, deleted = [], []
    monkeypatch.setattr(app, '_run_index_build_job', lambda uid, jid: ran.append((uid, jid)))

    class _Msg:
        content = json.dumps({'type': 'index_build', 'userId': 'lib-1', 'jobId': 'index-build-lib-1'})
        dequeue_count = 1
        id = 'm1'
        pop_receipt = 'r'

    class _Q:
        def update_message(self, msg, visibility_timeout=0):
            return msg

        def delete_message(self, msg):
            deleted.append(msg)

    app._process_clustering_queue_message(_Msg(), _Q(), 'photostore-library-ops', 3)
    assert ran == [('lib-1', 'index-build-lib-1')] and len(deleted) == 1


def test_worker_leaves_the_message_for_redelivery_when_the_build_raises(monkeypatch):
    deleted = []
    monkeypatch.setattr(app, '_run_index_build_job', lambda uid, jid: (_ for _ in ()).throw(RuntimeError('killed')))

    class _Msg:
        content = json.dumps({'type': 'index_build', 'userId': 'lib-1', 'jobId': 'index-build-lib-1'})
        dequeue_count = 1
        id = 'm1'
        pop_receipt = 'r'

    class _Q:
        def update_message(self, msg, visibility_timeout=0):
            return msg

        def delete_message(self, msg):
            deleted.append(msg)

    app._process_clustering_queue_message(_Msg(), _Q(), 'photostore-library-ops', 3)
    assert deleted == []  # not acked -> becomes visible again and is retried (bounded by max_retries)
