"""Concurrent finalizes of one session take turns instead of racing (2026-11-19)."""
import threading
import time

import pytest

import wave_write_combiner as combiner


def run_concurrently(requests, commit, key='k'):
    results, errors, start = {}, {}, threading.Barrier(len(requests), timeout=5)
    def one(name, entries):
        start.wait()
        try:
            results[name] = combiner.combine(key, entries, commit)
        except Exception as error:
            errors[name] = error
    threads = [threading.Thread(target=one, args=item) for item in requests.items()]
    [thread.start() for thread in threads]
    [thread.join(5) for thread in threads]
    return results, errors


def test_concurrent_requests_share_pages_and_never_commit_concurrently():
    commits, active, overlap = [], [0], [False]
    lock = threading.Lock()
    def commit(page):
        with lock:
            active[0] += 1
            overlap[0] |= active[0] > 1
        time.sleep(0.02)
        with lock:
            active[0] -= 1
            commits.append(list(page))
        return {'committed': len(page)}
    requests = {f'r{n}': [f'r{n}-{m}' for m in range(3)] for n in range(12)}
    results, errors = run_concurrently(requests, commit)
    assert not errors and set(results) == set(requests)
    assert not overlap[0]
    assert sorted(entry for page in commits for entry in page) == sorted(e for v in requests.values() for e in v)
    assert all(len(page) <= combiner.PAGE for page in commits)
    assert len(commits) < len(requests)  # actually coalesced


def test_a_failed_shared_page_is_retried_per_request_so_one_conflict_fails_only_its_owner():
    def commit(page):
        time.sleep(0.01)
        if 'bad' in page:
            raise ValueError('binding conflict')
        return {'page': list(page)}
    results, errors = run_concurrently({'good': ['a', 'b'], 'bad': ['bad'], 'other': ['c']}, commit)
    assert set(errors) == {'bad'} and isinstance(errors['bad'], ValueError)
    assert set(results) == {'good', 'other'}


def test_a_request_larger_than_a_page_is_split_and_waits_for_every_page():
    pages = []
    def commit(page):
        pages.append(list(page))
        return len(pages)
    entries = [str(n) for n in range(45)]
    assert combiner.combine('big', entries, commit) == 3
    assert [len(page) for page in pages] == [20, 20, 5]


def test_an_empty_request_is_rejected():
    with pytest.raises(ValueError):
        combiner.combine('k', [], lambda page: None)


def test_a_committer_hands_over_after_its_page_budget(monkeypatch):
    monkeypatch.setattr(combiner, 'MAX_PAGES_PER_COMMITTER', 1)
    committers = set()
    def commit(page):
        committers.add(threading.get_ident())
        time.sleep(0.01)
        return None
    requests = {f'r{n}': [f'r{n}-{m}' for m in range(combiner.PAGE)] for n in range(6)}
    _results, errors = run_concurrently(requests, commit)
    assert not errors
    assert len(committers) > 1
    assert not combiner._QUEUES


def test_timings_split_queue_wait_from_commit():
    gate = threading.Event()
    def commit(page):
        gate.wait(2)
        time.sleep(0.03)
        return len(page)
    first, second = {}, {}
    leader = threading.Thread(target=lambda: combiner.combine('t', ['a'], commit, first))
    leader.start()
    time.sleep(0.05)
    follower = threading.Thread(target=lambda: combiner.combine('t', ['b'], commit, second))
    follower.start()
    time.sleep(0.05)
    gate.set()
    leader.join(5), follower.join(5)
    assert first['pages'] == 1 and first['commit_ms'] >= 30 and first['wait_ms'] < 30
    # The follower queued behind the leader's page, so it waited about one commit.
    assert second['pages'] == 1 and second['commit_ms'] >= 30 and second['wait_ms'] >= 30
    assert second['total_ms'] >= second['wait_ms'] + second['commit_ms'] - 5


def test_timings_count_each_page_of_a_large_request_once():
    timings = {}
    combiner.combine('big', list(range(45)), lambda page: len(page), timings)
    assert timings['pages'] == 3 and set(timings) == {'wait_ms', 'commit_ms', 'pages', 'total_ms'}
