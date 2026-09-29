from __future__ import annotations

import threading
import time

import app


def test_get_or_kick_background_returns_none_and_populates_cache_on_cold_miss():
    cache = app._UserScanCache(60)
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def fetch_fn():
        calls.append(1)
        entered.set()
        release.wait(timeout=5)
        return [{'RowKey': 'a'}]

    result = cache.get_or_kick_background('user-1', fetch_fn)
    assert result is None

    assert entered.wait(timeout=5), 'background worker never started the scan'
    release.set()

    for _ in range(50):
        if cache._fresh('user-1') is not None:
            break
        time.sleep(0.05)

    assert cache._fresh('user-1') == [{'RowKey': 'a'}]
    assert len(calls) == 1


def test_get_or_kick_background_returns_cached_rows_when_fresh():
    cache = app._UserScanCache(60)
    cache.set('user-2', [{'RowKey': 'b'}])

    calls = []

    def fetch_fn():
        calls.append(1)
        return [{'RowKey': 'should-not-be-used'}]

    result = cache.get_or_kick_background('user-2', fetch_fn)
    assert result == [{'RowKey': 'b'}]
    assert calls == []


def test_get_or_kick_background_dedupes_concurrent_kicks():
    cache = app._UserScanCache(60)
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def fetch_fn():
        calls.append(1)
        entered.set()
        release.wait(timeout=5)
        return [{'RowKey': 'c'}]

    first = cache.get_or_kick_background('user-3', fetch_fn)
    assert first is None
    assert entered.wait(timeout=5)

    # A second caller arriving while the background scan is still running
    # must not start a duplicate scan -- it just gets None back too.
    second = cache.get_or_kick_background('user-3', fetch_fn)
    assert second is None

    release.set()
    for _ in range(50):
        if cache._fresh('user-3') is not None:
            break
        time.sleep(0.05)

    assert len(calls) == 1


def test_cached_metadata_list_rows_for_user_non_blocking_on_cold_account(monkeypatch):
    monkeypatch.setattr(app, 'get_user_listing_index', lambda user_id, allow_refresh=True: None)
    fresh_cache = app._UserScanCache(app.METADATA_SCAN_CACHE_TTL_SECONDS)
    monkeypatch.setattr(app, '_metadata_list_scan_cache', fresh_cache)

    entered = threading.Event()
    release = threading.Event()

    def fake_query(user_id, select=None, purpose='metadata'):
        entered.set()
        release.wait(timeout=5)
        return [{'RowKey': 'z'}]

    monkeypatch.setattr(app, '_query_metadata_rows_for_user', fake_query)

    rows = app._cached_metadata_list_rows_for_user('user-4', purpose='photos.timeline', allow_sync_build=False)
    assert rows == []
    assert entered.wait(timeout=5), 'expected the scan to be kicked off in the background'
    release.set()
