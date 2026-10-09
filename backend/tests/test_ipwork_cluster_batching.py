"""ipworker's producer-side filename batching.

Before this, every detected-face photo enqueued its own
'people_incremental_assign' queue message immediately -- one Azure Queue
PUT/DELETE pair per photo, regardless of how well the clustering worker
batched the processing side afterward. _buffer_incremental_assign_filename
accumulates filenames per user in memory and flushes them as one message
carrying 'filenames' once either a count cap or a time-based idle window is
hit, so N photos cost roughly one queue round trip instead of N.
"""
import threading

import app
import pytest


@pytest.fixture(autouse=True)
def clean_buffers():
    """Module-level buffers are shared mutable state -- never leak between
    tests regardless of how a test exits."""
    app._cluster_batch_buffers.clear()
    app._cluster_batch_started_at.clear()
    yield
    app._cluster_batch_buffers.clear()
    app._cluster_batch_started_at.clear()


@pytest.fixture
def enqueued(monkeypatch):
    calls = []
    monkeypatch.setattr(app, '_enqueue_incremental_assign_job',
                        lambda user_id, filenames: calls.append((user_id, list(filenames))))
    return calls


def test_buffer_does_not_flush_below_count_cap(enqueued, monkeypatch):
    monkeypatch.setattr(app, 'IPWORK_CLUSTER_BATCH_MAX_FILENAMES', 3)
    app._buffer_incremental_assign_filename('lib-A', 'a.jpg')
    app._buffer_incremental_assign_filename('lib-A', 'b.jpg')
    assert enqueued == []
    assert app._cluster_batch_buffers['lib-A'] == ['a.jpg', 'b.jpg']


def test_buffer_flushes_exactly_at_count_cap(enqueued, monkeypatch):
    monkeypatch.setattr(app, 'IPWORK_CLUSTER_BATCH_MAX_FILENAMES', 3)
    for filename in ('a.jpg', 'b.jpg', 'c.jpg'):
        app._buffer_incremental_assign_filename('lib-A', filename)
    assert enqueued == [('lib-A', ['a.jpg', 'b.jpg', 'c.jpg'])]
    # Buffer is reset after flushing -- the next filename starts a fresh batch.
    assert app._cluster_batch_buffers.get('lib-A') == []
    app._buffer_incremental_assign_filename('lib-A', 'd.jpg')
    assert enqueued == [('lib-A', ['a.jpg', 'b.jpg', 'c.jpg'])]
    assert app._cluster_batch_buffers['lib-A'] == ['d.jpg']


def test_buffers_are_independent_per_user(enqueued, monkeypatch):
    monkeypatch.setattr(app, 'IPWORK_CLUSTER_BATCH_MAX_FILENAMES', 2)
    app._buffer_incremental_assign_filename('lib-A', 'a.jpg')
    app._buffer_incremental_assign_filename('lib-B', 'x.jpg')
    assert enqueued == []
    app._buffer_incremental_assign_filename('lib-A', 'a2.jpg')  # flushes lib-A only
    assert enqueued == [('lib-A', ['a.jpg', 'a2.jpg'])]
    assert app._cluster_batch_buffers['lib-B'] == ['x.jpg']


def test_time_based_flush_ignores_buffers_younger_than_the_interval(enqueued, monkeypatch):
    monkeypatch.setattr(app, 'IPWORK_CLUSTER_BATCH_FLUSH_SECONDS', 60)
    app._buffer_incremental_assign_filename('lib-A', 'a.jpg')
    app._flush_stale_incremental_assign_buffers()
    assert enqueued == []
    assert app._cluster_batch_buffers['lib-A'] == ['a.jpg']


def test_time_based_flush_fires_once_the_interval_elapses(enqueued, monkeypatch):
    monkeypatch.setattr(app, 'IPWORK_CLUSTER_BATCH_FLUSH_SECONDS', 60)
    fake_now = [1000.0]
    monkeypatch.setattr(app.time, 'monotonic', lambda: fake_now[0])
    app._buffer_incremental_assign_filename('lib-A', 'a.jpg')
    app._buffer_incremental_assign_filename('lib-A', 'b.jpg')
    fake_now[0] += 61
    app._flush_stale_incremental_assign_buffers()
    assert enqueued == [('lib-A', ['a.jpg', 'b.jpg'])]
    assert 'lib-A' not in app._cluster_batch_started_at


def test_flush_loop_stops_on_shutdown_event(enqueued, monkeypatch):
    monkeypatch.setattr(app, 'IPWORK_CLUSTER_BATCH_FLUSH_SECONDS', 0.03)
    shutdown_requested = threading.Event()
    thread = threading.Thread(target=app._run_incremental_assign_flush_loop, args=(shutdown_requested,))
    thread.start()
    app._buffer_incremental_assign_filename('lib-A', 'a.jpg')
    shutdown_requested.set()
    thread.join(timeout=2)
    assert not thread.is_alive()


def test_flush_all_drains_every_remaining_buffer_regardless_of_age(enqueued, monkeypatch):
    monkeypatch.setattr(app, 'IPWORK_CLUSTER_BATCH_FLUSH_SECONDS', 9999)
    app._buffer_incremental_assign_filename('lib-A', 'a.jpg')
    app._buffer_incremental_assign_filename('lib-B', 'x.jpg')
    app._flush_all_incremental_assign_buffers()
    assert sorted(enqueued) == [('lib-A', ['a.jpg']), ('lib-B', ['x.jpg'])]
    assert app._cluster_batch_buffers == {}
    assert app._cluster_batch_started_at == {}


def test_flush_all_is_a_noop_when_nothing_buffered(enqueued):
    app._flush_all_incremental_assign_buffers()
    assert enqueued == []
