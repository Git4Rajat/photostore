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

import threading
import time

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
    primed = []
    monkeypatch.setattr(app, 'prime_all_user_indexes_sequentially', lambda uid, **k: primed.append(uid))

    with app.app.test_request_context('/api/tools/indexes/build', method='POST'):
        response = tools_build_indexes()

    assert primed == []
    payload = response.get_json()
    assert payload['ok'] is True
    assert payload['ready'] is True
    assert payload['building'] is False


def test_tools_build_kicks_primer_when_any_index_missing(monkeypatch, route_ctx):
    monkeypatch.setattr(app, 'get_user_index_build_state', lambda uid: _build_state(
        {'sort': True, 'lexical': False, 'albums': True, 'people': True}, needs_rebuild=True))
    primed = []
    monkeypatch.setattr(app, 'prime_all_user_indexes_sequentially', lambda uid, **k: primed.append(uid))

    with app.app.test_request_context('/api/tools/indexes/build', method='POST'):
        response = tools_build_indexes()

    assert primed == ['owner']
    payload = response.get_json()
    assert payload['ok'] is True
    assert payload['ready'] is False
    assert payload['building'] is True
    assert payload['indexes']['lexical'] is False


def test_tools_build_kicks_primer_when_built_but_dirty(monkeypatch, route_ctx):
    # All four built (ready=True) but one dirty -> still rebuild, and the gate
    # is already satisfied (ready stays True) so the frontend won't block.
    monkeypatch.setattr(app, 'get_user_index_build_state', lambda uid: _build_state(
        {'sort': True, 'lexical': True, 'albums': True, 'people': True}, needs_rebuild=True))
    primed = []
    monkeypatch.setattr(app, 'prime_all_user_indexes_sequentially', lambda uid, **k: primed.append(uid))

    with app.app.test_request_context('/api/tools/indexes/build', method='POST'):
        response = tools_build_indexes()

    assert primed == ['owner']
    payload = response.get_json()
    assert payload['ready'] is True
    assert payload['building'] is True


def test_tools_build_survives_primer_exception(monkeypatch, route_ctx):
    monkeypatch.setattr(app, 'get_user_index_build_state', lambda uid: _build_state(
        {'sort': False, 'lexical': False, 'albums': False, 'people': False}, needs_rebuild=True))

    def _boom(uid, **k):
        raise RuntimeError('thread pool exhausted')

    monkeypatch.setattr(app, 'prime_all_user_indexes_sequentially', _boom)

    with app.app.test_request_context('/api/tools/indexes/build', method='POST'):
        response = tools_build_indexes()  # must not raise

    payload = response.get_json()
    assert payload['ready'] is False
    assert payload['building'] is False  # kick failed, so not reported as building


def test_tools_build_passes_progress_callback_that_writes_job_row(monkeypatch, route_ctx):
    monkeypatch.setattr(app, 'get_user_index_build_state', lambda uid: _build_state(
        {'sort': False, 'lexical': False, 'albums': False, 'people': False}, needs_rebuild=True))
    captured = {}

    def _fake_prime(uid, *, on_progress=None):
        captured['on_progress'] = on_progress

    monkeypatch.setattr(app, 'prime_all_user_indexes_sequentially', _fake_prime)
    job_rows = []
    monkeypatch.setattr(app, '_upsert_job_status', lambda job_id, uid, jt, status, **f: job_rows.append((job_id, uid, jt, status, f)))
    explore_refreshed = []
    monkeypatch.setattr(app, 'refresh_user_explore_summary', lambda uid: explore_refreshed.append(uid))

    with app.app.test_request_context('/api/tools/indexes/build', method='POST'):
        tools_build_indexes()

    # The route must hand the primer a progress callback; invoking it should
    # mirror progress into an index_build jobs-table row.
    assert callable(captured.get('on_progress'))
    captured['on_progress']({'sort': True, 'lexical': False, 'albums': False, 'people': False}, True)
    assert explore_refreshed == []  # not yet: build still running
    captured['on_progress']({'sort': True, 'lexical': True, 'albums': True, 'people': True}, False)
    assert len(job_rows) == 2
    assert job_rows[0][2] == app.INDEX_BUILD_JOB_TYPE
    # On completion (building=False) with lexical built, the callback recomputes
    # the Explore summary here on tools so backend never has to.
    assert explore_refreshed == ['owner']
    assert job_rows[0][3] == 'running'
    assert job_rows[1][3] == 'done'  # building=False + all ready


# --- GET /api/tools/indexes/status --------------------------------------------

def test_tools_status_reports_ready_without_building(monkeypatch, route_ctx):
    monkeypatch.setattr(app, 'get_user_index_readiness', lambda uid: {
        'sort': True, 'lexical': True, 'albums': True, 'people': True,
    })
    monkeypatch.setattr(app, 'index_prime_in_progress', lambda uid: False)

    with app.app.test_request_context('/api/tools/indexes/status'):
        response = tools_indexes_status()

    payload = response.get_json()
    assert payload['ready'] is True
    assert payload['building'] is False


def test_tools_status_reports_building_when_incomplete_and_prime_running(monkeypatch, route_ctx):
    monkeypatch.setattr(app, 'get_user_index_readiness', lambda uid: {
        'sort': True, 'lexical': False, 'albums': False, 'people': False,
    })
    monkeypatch.setattr(app, 'index_prime_in_progress', lambda uid: True)

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
