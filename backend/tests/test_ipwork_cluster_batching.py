"""ipworker's producer-side filename batching.

Before this, every detected-face photo enqueued its own
'people_incremental_assign' queue message immediately -- one Azure Queue
PUT/DELETE pair per photo, regardless of how well the clustering worker
batched the processing side afterward. _buffer_incremental_assign_filename
accumulates filenames per user in memory and flushes them once either the
count cap is hit, or _is_upload_active reports the user's upload has gone
quiet (a recency check on an activity heartbeat upload.py writes -- see
_mark_upload_activity_heartbeat -- not a fixed timer, which couldn't tell a
sustained upload apart from a finished one). A hard fallback ceiling is
pure defense-in-depth, not an abandonment timer: the activity check can't
get stuck, since staleness is recency-based rather than waiting for an
explicit 'done' signal that might never arrive.
"""
import threading
from datetime import datetime, timedelta, timezone

import app
import pytest
from fakes import FakeTable


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


class _RecordingQueue:
    def __init__(self):
        self.messages = []

    def send_message(self, content, **kwargs):
        self.messages.append(content)


def test_enqueue_splits_large_batch_into_bounded_messages(monkeypatch):
    import json
    queue = _RecordingQueue()
    monkeypatch.setattr(app, 'clustering_queue_client', queue)
    monkeypatch.setattr(app, 'INCREMENTAL_ASSIGN_MAX_FILENAMES_PER_MESSAGE', 2)

    filenames = [f'{i}.jpg' for i in range(5)]
    result = app._enqueue_incremental_assign_job('lib-A', filenames)

    assert result == {'status': 'queued', 'messages': 3}  # 2 + 2 + 1
    sent = [json.loads(m) for m in queue.messages]
    assert [m['filenames'] for m in sent] == [['0.jpg', '1.jpg'], ['2.jpg', '3.jpg'], ['4.jpg']]
    # No filename is dropped or duplicated across the split messages.
    assert sorted(f for m in sent for f in m['filenames']) == sorted(filenames)
    assert all(m['type'] == 'people_incremental_assign' and m['user_id'] == 'lib-A' for m in sent)


def test_enqueue_small_batch_is_a_single_message(monkeypatch):
    queue = _RecordingQueue()
    monkeypatch.setattr(app, 'clustering_queue_client', queue)
    monkeypatch.setattr(app, 'INCREMENTAL_ASSIGN_MAX_FILENAMES_PER_MESSAGE', 128)

    result = app._enqueue_incremental_assign_job('lib-A', ['a.jpg', 'b.jpg'])

    assert result == {'status': 'queued', 'messages': 1}
    assert len(queue.messages) == 1


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


def test_flush_skips_buffer_while_upload_is_active(enqueued, monkeypatch):
    monkeypatch.setattr(app, 'IPWORK_CLUSTER_BATCH_HARD_FALLBACK_SECONDS', 300)
    monkeypatch.setattr(app, '_is_upload_active', lambda user_id: True)
    app._buffer_incremental_assign_filename('lib-A', 'a.jpg')
    app._flush_stale_incremental_assign_buffers()
    assert enqueued == []
    assert app._cluster_batch_buffers['lib-A'] == ['a.jpg']


def test_flush_fires_once_upload_goes_inactive(enqueued, monkeypatch):
    monkeypatch.setattr(app, 'IPWORK_CLUSTER_BATCH_HARD_FALLBACK_SECONDS', 300)
    monkeypatch.setattr(app, '_is_upload_active', lambda user_id: False)
    app._buffer_incremental_assign_filename('lib-A', 'a.jpg')
    app._buffer_incremental_assign_filename('lib-A', 'b.jpg')
    app._flush_stale_incremental_assign_buffers()
    assert enqueued == [('lib-A', ['a.jpg', 'b.jpg'])]
    assert 'lib-A' not in app._cluster_batch_started_at


def test_hard_fallback_flushes_even_when_reported_active(enqueued, monkeypatch):
    # Defense-in-depth: even if _is_upload_active reports active the whole
    # time (e.g. a stuck/misbehaving activity signal), a buffer must not
    # wait past the hard fallback ceiling.
    monkeypatch.setattr(app, 'IPWORK_CLUSTER_BATCH_HARD_FALLBACK_SECONDS', 60)
    monkeypatch.setattr(app, '_is_upload_active', lambda user_id: True)
    fake_now = [1000.0]
    monkeypatch.setattr(app.time, 'monotonic', lambda: fake_now[0])
    app._buffer_incremental_assign_filename('lib-A', 'a.jpg')
    fake_now[0] += 61
    app._flush_stale_incremental_assign_buffers()
    assert enqueued == [('lib-A', ['a.jpg'])]


def test_flush_loop_stops_on_shutdown_event(enqueued, monkeypatch):
    monkeypatch.setattr(app, 'IPWORK_CLUSTER_BATCH_POLL_SECONDS', 0.03)
    monkeypatch.setattr(app, '_is_upload_active', lambda user_id: True)
    shutdown_requested = threading.Event()
    thread = threading.Thread(target=app._run_incremental_assign_flush_loop, args=(shutdown_requested,))
    thread.start()
    app._buffer_incremental_assign_filename('lib-A', 'a.jpg')
    shutdown_requested.set()
    thread.join(timeout=2)
    assert not thread.is_alive()


def test_flush_all_drains_every_remaining_buffer_regardless_of_age(enqueued, monkeypatch):
    monkeypatch.setattr(app, '_is_upload_active', lambda user_id: True)
    app._buffer_incremental_assign_filename('lib-A', 'a.jpg')
    app._buffer_incremental_assign_filename('lib-B', 'x.jpg')
    app._flush_all_incremental_assign_buffers()
    assert sorted(enqueued) == [('lib-A', ['a.jpg']), ('lib-B', ['x.jpg'])]
    assert app._cluster_batch_buffers == {}
    assert app._cluster_batch_started_at == {}


def test_flush_all_is_a_noop_when_nothing_buffered(enqueued):
    app._flush_all_incremental_assign_buffers()
    assert enqueued == []


@pytest.fixture
def metadata_table(monkeypatch):
    table = FakeTable()
    monkeypatch.setattr(app, 'metadata_table_client', table)
    return table


def test_is_upload_active_true_for_a_fresh_heartbeat(metadata_table, monkeypatch):
    monkeypatch.setattr(app, 'UPLOAD_ACTIVITY_WINDOW_SECONDS', 30)
    metadata_table.rows[('clustering_maintenance', 'lib-A')] = {
        'PartitionKey': 'clustering_maintenance', 'RowKey': 'lib-A',
        'lastUploadActivityAt': datetime.now(timezone.utc).isoformat(),
    }
    assert app._is_upload_active('lib-A') is True


def test_is_upload_active_false_for_a_stale_heartbeat(metadata_table, monkeypatch):
    monkeypatch.setattr(app, 'UPLOAD_ACTIVITY_WINDOW_SECONDS', 30)
    stale = datetime.now(timezone.utc) - timedelta(seconds=120)
    metadata_table.rows[('clustering_maintenance', 'lib-A')] = {
        'PartitionKey': 'clustering_maintenance', 'RowKey': 'lib-A',
        'lastUploadActivityAt': stale.isoformat(),
    }
    assert app._is_upload_active('lib-A') is False


def test_is_upload_active_false_when_no_row_exists(metadata_table):
    assert app._is_upload_active('lib-A') is False


def test_is_upload_active_false_when_table_client_unavailable(monkeypatch):
    monkeypatch.setattr(app, 'metadata_table_client', None)
    assert app._is_upload_active('lib-A') is False


def test_mark_upload_activity_heartbeat_uses_merge_mode(metadata_table, monkeypatch):
    # FakeTable.upsert_entity doesn't simulate real field-level MERGE
    # semantics (it just replaces the stored row) -- what's actually under
    # test here is that this call asks for MERGE at all, trusting the real
    # azure-data-tables SDK to honor it and leave runsSinceUpload/
    # lastStartedAt on the same row untouched.
    calls = []
    monkeypatch.setattr(metadata_table, 'upsert_entity',
                        lambda entity, mode=None, **kw: calls.append((entity, mode)))
    app._mark_upload_activity_heartbeat('lib-A')
    assert len(calls) == 1
    entity, mode = calls[0]
    assert mode is app.UpdateMode.MERGE
    assert entity['PartitionKey'] == 'clustering_maintenance'
    assert entity['RowKey'] == 'lib-A'
    assert 'lastUploadActivityAt' in entity
