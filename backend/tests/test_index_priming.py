"""Unit tests for the sequential index primer (storage_utils.
prime_all_user_indexes_sequentially / get_user_index_readiness) and the
/api/photos/prime-indexes + /api/photos/index-status routes that use them.

Added alongside the 2026-09-30 microsvcpoc-dev HAR investigation: on a
genuinely cold account, get_user_sort_index/get_user_lexical_index/
get_user_albums_index/get_user_people_index each independently kick their
own background rebuild thread, so a session start that hits all four could
land four concurrent full-account Table scans on the backend at once --
observed live to help trigger a ContainerBackOff crash loop. These tests
pin: (1) the primer runs the four refresh_user_*_index calls one at a time,
not in parallel, (2) it's single-flighted per user, (3) it goes through each
kind's own rebuild lock so a concurrent page-level request never races it,
(4) one kind failing doesn't stop the rest, and (5) the readiness check
itself never triggers a rebuild as a side effect (unlike calling
get_user_*_index directly).
"""
from __future__ import annotations

import threading
import time

import pytest

import app
from routes.photos import photos_index_status, photos_prime_indexes
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

    assert calls == ['sort', 'lexical', 'albums', 'people']


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
    assert calls == ['sort', 'lexical', 'albums', 'people']  # only ever ran once


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

    assert calls == ['lexical', 'albums', 'people']  # sort skipped, rest still ran


def test_prime_all_user_indexes_sequentially_continues_after_one_kind_raises(monkeypatch):
    calls = []
    _patch_refreshers(monkeypatch, calls, raise_kind='lexical')

    storage_utils.prime_all_user_indexes_sequentially('lib-raise')
    _wait_for_prime_lock_free('lib-raise')

    assert calls == ['sort', 'lexical', 'albums', 'people']  # lexical failed but didn't block the rest


def test_prime_all_user_indexes_sequentially_noop_for_blank_user_id(monkeypatch):
    calls = []
    _patch_refreshers(monkeypatch, calls)

    storage_utils.prime_all_user_indexes_sequentially('   ')

    assert calls == []


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


def test_get_user_index_readiness_all_false_for_blank_user_id():
    assert storage_utils.get_user_index_readiness('') == {
        'sort': False, 'lexical': False, 'albums': False, 'people': False,
    }


# --- POST /api/photos/prime-indexes ------------------------------------------

@pytest.fixture
def prime_route_ctx(monkeypatch):
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('owner', None))


def test_prime_indexes_route_does_not_prime_when_all_ready(monkeypatch, prime_route_ctx):
    monkeypatch.setattr(app, 'get_user_index_readiness', lambda uid: {
        'sort': True, 'lexical': True, 'albums': True, 'people': True,
    })
    primed = []
    monkeypatch.setattr(app, 'prime_all_user_indexes_sequentially', lambda uid: primed.append(uid))

    with app.app.test_request_context('/api/photos/prime-indexes', method='POST'):
        response = photos_prime_indexes()

    assert primed == []
    assert response.get_json() == {
        'ok': True, 'ready': True,
        'indexes': {'sort': True, 'lexical': True, 'albums': True, 'people': True},
    }


def test_prime_indexes_route_kicks_primer_when_any_index_missing(monkeypatch, prime_route_ctx):
    monkeypatch.setattr(app, 'get_user_index_readiness', lambda uid: {
        'sort': True, 'lexical': False, 'albums': True, 'people': True,
    })
    primed = []
    monkeypatch.setattr(app, 'prime_all_user_indexes_sequentially', lambda uid: primed.append(uid))

    with app.app.test_request_context('/api/photos/prime-indexes', method='POST'):
        response = photos_prime_indexes()

    assert primed == ['owner']
    payload = response.get_json()
    assert payload['ok'] is True
    assert payload['ready'] is False
    assert payload['indexes']['lexical'] is False


def test_prime_indexes_route_survives_primer_exception(monkeypatch, prime_route_ctx):
    monkeypatch.setattr(app, 'get_user_index_readiness', lambda uid: {
        'sort': False, 'lexical': False, 'albums': False, 'people': False,
    })

    def _boom(uid):
        raise RuntimeError('thread pool exhausted')

    monkeypatch.setattr(app, 'prime_all_user_indexes_sequentially', _boom)

    with app.app.test_request_context('/api/photos/prime-indexes', method='POST'):
        response = photos_prime_indexes()  # must not raise

    assert response.get_json()['ready'] is False


# --- GET /api/photos/index-status ---------------------------------------------

def test_index_status_route_never_primes(monkeypatch, prime_route_ctx):
    monkeypatch.setattr(app, 'get_user_index_readiness', lambda uid: {
        'sort': True, 'lexical': False, 'albums': True, 'people': False,
    })
    primed = []
    monkeypatch.setattr(app, 'prime_all_user_indexes_sequentially', lambda uid: primed.append(uid))

    with app.app.test_request_context('/api/photos/index-status'):
        response = photos_index_status()

    assert primed == []  # read-only: polling must never itself kick a rebuild
    payload = response.get_json()
    assert payload['ready'] is False
    assert payload['indexes'] == {'sort': True, 'lexical': False, 'albums': True, 'people': False}


def test_index_status_route_ready_true_when_all_built(monkeypatch, prime_route_ctx):
    monkeypatch.setattr(app, 'get_user_index_readiness', lambda uid: {
        'sort': True, 'lexical': True, 'albums': True, 'people': True,
    })

    with app.app.test_request_context('/api/photos/index-status'):
        response = photos_index_status()

    assert response.get_json()['ready'] is True
