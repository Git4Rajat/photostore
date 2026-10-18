"""ipworker's producer-side filename batching.

Before this, every detected-face photo enqueued its own
'people_incremental_assign' queue message immediately -- one Azure Queue
PUT/DELETE pair per photo, regardless of how well the clustering worker
batched the processing side afterward. _buffer_incremental_assign_filename
accumulates filenames per user in memory. The normal flush trigger is the
vision/face pipeline itself going quiet for IPWORK_CLUSTER_IDLE_SECONDS
(tracked per-user via _cluster_batch_last_activity_at, bumped on every
buffered filename) -- deliberately NOT tied to upload activity, since an
unrelated ongoing upload says nothing about whether more faces are still
coming from ipworker, and deliberately NOT a count cap either: flushing
mid-run every time some fixed count of filenames buffers would wake the
clustering worker repeatedly during one long upload instead of once, after
it goes quiet. IPWORK_CLUSTER_BATCH_SAFETY_MAX_FILENAMES is only a
defensive backstop against unbounded in-memory growth during an unusually
long continuous run, not a normal trigger. A manual "Cluster now" request
(request_cluster_batch_flush) can also force an early flush via a shared
recency signal every replica's poll loop checks.
"""
import json
import os
import threading
import time
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
    app._cluster_batch_last_activity_at.clear()
    yield
    app._cluster_batch_buffers.clear()
    app._cluster_batch_started_at.clear()
    app._cluster_batch_last_activity_at.clear()


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


def test_buffer_does_not_flush_below_safety_cap(enqueued, monkeypatch):
    monkeypatch.setattr(app, 'IPWORK_CLUSTER_BATCH_SAFETY_MAX_FILENAMES', 3)
    app._buffer_incremental_assign_filename('lib-A', 'a.jpg')
    app._buffer_incremental_assign_filename('lib-A', 'b.jpg')
    assert enqueued == []
    assert app._cluster_batch_buffers['lib-A'] == ['a.jpg', 'b.jpg']


def test_buffer_flushes_exactly_at_safety_cap(enqueued, monkeypatch):
    monkeypatch.setattr(app, 'IPWORK_CLUSTER_BATCH_SAFETY_MAX_FILENAMES', 3)
    for filename in ('a.jpg', 'b.jpg', 'c.jpg'):
        app._buffer_incremental_assign_filename('lib-A', filename)
    assert enqueued == [('lib-A', ['a.jpg', 'b.jpg', 'c.jpg'])]
    # Buffer is reset after flushing -- the next filename starts a fresh batch.
    assert app._cluster_batch_buffers.get('lib-A') == []
    app._buffer_incremental_assign_filename('lib-A', 'd.jpg')
    assert enqueued == [('lib-A', ['a.jpg', 'b.jpg', 'c.jpg'])]
    assert app._cluster_batch_buffers['lib-A'] == ['d.jpg']


def test_buffers_are_independent_per_user(enqueued, monkeypatch):
    monkeypatch.setattr(app, 'IPWORK_CLUSTER_BATCH_SAFETY_MAX_FILENAMES', 2)
    app._buffer_incremental_assign_filename('lib-A', 'a.jpg')
    app._buffer_incremental_assign_filename('lib-B', 'x.jpg')
    assert enqueued == []
    app._buffer_incremental_assign_filename('lib-A', 'a2.jpg')  # flushes lib-A only
    assert enqueued == [('lib-A', ['a.jpg', 'a2.jpg'])]
    assert app._cluster_batch_buffers['lib-B'] == ['x.jpg']


def test_buffer_keeps_growing_past_the_old_1500_default_while_active(enqueued, monkeypatch):
    # The safety cap defaults far above any normal session size, so a long
    # continuous run of faces (e.g. >1500 photos in one upload) must not
    # trigger a mid-run flush -- only the idle gap or a manual request should.
    now = [1000.0]
    monkeypatch.setattr(app.time, 'monotonic', lambda: now[0])
    for i in range(2000):
        app._buffer_incremental_assign_filename('lib-A', f'{i}.jpg')
        now[0] += 0.01  # stays well under the idle window throughout
    assert enqueued == []
    assert len(app._cluster_batch_buffers['lib-A']) == 2000


def test_flush_skips_buffer_while_vision_pipeline_is_still_active(enqueued, monkeypatch):
    monkeypatch.setattr(app, 'IPWORK_CLUSTER_IDLE_SECONDS', 300)
    monkeypatch.setattr(app, '_cluster_flush_recently_requested', lambda user_id: False)
    now = [1000.0]
    monkeypatch.setattr(app.time, 'monotonic', lambda: now[0])
    app._buffer_incremental_assign_filename('lib-A', 'a.jpg')
    now[0] += 60  # well under the idle window
    app._flush_stale_incremental_assign_buffers()
    assert enqueued == []
    assert app._cluster_batch_buffers['lib-A'] == ['a.jpg']


def test_flush_fires_once_vision_pipeline_goes_idle(enqueued, monkeypatch):
    monkeypatch.setattr(app, 'IPWORK_CLUSTER_IDLE_SECONDS', 300)
    monkeypatch.setattr(app, '_cluster_flush_recently_requested', lambda user_id: False)
    now = [1000.0]
    monkeypatch.setattr(app.time, 'monotonic', lambda: now[0])
    app._buffer_incremental_assign_filename('lib-A', 'a.jpg')
    app._buffer_incremental_assign_filename('lib-A', 'b.jpg')
    now[0] += 301
    app._flush_stale_incremental_assign_buffers()
    assert enqueued == [('lib-A', ['a.jpg', 'b.jpg'])]
    assert 'lib-A' not in app._cluster_batch_started_at


def test_idle_clock_resets_on_each_new_buffered_filename(enqueued, monkeypatch):
    # A buffer that keeps receiving faces should never go idle just because
    # it started a while ago -- the clock tracked is time-since-last-face,
    # not time-since-buffer-started. Unrelated upload/video activity must
    # play no role in this decision at all.
    monkeypatch.setattr(app, 'IPWORK_CLUSTER_IDLE_SECONDS', 300)
    monkeypatch.setattr(app, '_cluster_flush_recently_requested', lambda user_id: False)
    now = [1000.0]
    monkeypatch.setattr(app.time, 'monotonic', lambda: now[0])
    app._buffer_incremental_assign_filename('lib-A', 'a.jpg')
    now[0] += 299
    app._buffer_incremental_assign_filename('lib-A', 'b.jpg')  # resets idle clock
    now[0] += 299
    app._flush_stale_incremental_assign_buffers()
    assert enqueued == []
    assert app._cluster_batch_buffers['lib-A'] == ['a.jpg', 'b.jpg']
    now[0] += 2
    app._flush_stale_incremental_assign_buffers()
    assert enqueued == [('lib-A', ['a.jpg', 'b.jpg'])]


def test_manual_flush_request_fires_before_idle_window_elapses(enqueued, monkeypatch):
    monkeypatch.setattr(app, 'IPWORK_CLUSTER_IDLE_SECONDS', 300)
    requested = {'lib-A': True}
    monkeypatch.setattr(app, '_cluster_flush_recently_requested', lambda user_id: requested.get(user_id, False))
    app._buffer_incremental_assign_filename('lib-A', 'a.jpg')
    app._flush_stale_incremental_assign_buffers()
    assert enqueued == [('lib-A', ['a.jpg'])]


def test_manual_flush_request_marks_reason_manual(enqueued, monkeypatch):
    monkeypatch.setattr(app, 'IPWORK_CLUSTER_IDLE_SECONDS', 300)
    monkeypatch.setattr(app, '_cluster_flush_recently_requested', lambda user_id: True)
    emitted = []
    monkeypatch.setattr(app.processing_diagnostics, 'emit', lambda *a, **kw: emitted.append(kw))
    app._buffer_incremental_assign_filename('lib-A', 'a.jpg')
    app._flush_stale_incremental_assign_buffers()
    assert any(e.get('reason') == 'manual' for e in emitted)


def test_request_cluster_batch_flush_writes_force_timestamp(monkeypatch):
    table = FakeTable()
    monkeypatch.setattr(app, 'metadata_table_client', table)
    status = app.request_cluster_batch_flush('lib-A')
    assert status == 'queued'
    row = table.rows[('clustering_maintenance', 'lib-A')]
    assert 'forceClusterFlushAt' in row


def test_request_cluster_batch_flush_unavailable_without_table(monkeypatch):
    monkeypatch.setattr(app, 'metadata_table_client', None)
    assert app.request_cluster_batch_flush('lib-A') == 'unavailable'


def test_cluster_flush_recently_requested_true_within_window(monkeypatch):
    table = FakeTable()
    monkeypatch.setattr(app, 'metadata_table_client', table)
    monkeypatch.setattr(app, 'IPWORK_CLUSTER_FORCE_FLUSH_WINDOW_SECONDS', 120)
    table.rows[('clustering_maintenance', 'lib-A')] = {
        'PartitionKey': 'clustering_maintenance', 'RowKey': 'lib-A',
        'forceClusterFlushAt': datetime.now(timezone.utc).isoformat(),
    }
    assert app._cluster_flush_recently_requested('lib-A') is True


def test_cluster_flush_recently_requested_false_once_stale(monkeypatch):
    table = FakeTable()
    monkeypatch.setattr(app, 'metadata_table_client', table)
    monkeypatch.setattr(app, 'IPWORK_CLUSTER_FORCE_FLUSH_WINDOW_SECONDS', 120)
    stale = datetime.now(timezone.utc) - timedelta(seconds=200)
    table.rows[('clustering_maintenance', 'lib-A')] = {
        'PartitionKey': 'clustering_maintenance', 'RowKey': 'lib-A',
        'forceClusterFlushAt': stale.isoformat(),
    }
    assert app._cluster_flush_recently_requested('lib-A') is False


def test_stale_snapshot_cannot_flush_replacement_buffer(enqueued, monkeypatch):
    # The manual-flush check (a table read) is the one remaining slow step
    # between snapshotting candidates and acting on them. If, while that
    # read is in flight, the snapshotted buffer hits the safety cap and gets
    # replaced by a fresh one, the re-check under the lock must detect the
    # swap and leave the new buffer alone rather than flushing it early.
    now = [1000.0]
    monkeypatch.setattr(app.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(app, 'IPWORK_CLUSTER_IDLE_SECONDS', 300)
    monkeypatch.setattr(app, 'IPWORK_CLUSTER_BATCH_SAFETY_MAX_FILENAMES', 2)
    def mutated_during_manual_check(user_id):
        app._buffer_incremental_assign_filename(user_id, 'b.jpg')  # hits the safety cap -> flushes ['a.jpg', 'b.jpg'], starts a fresh buffer
        now[0] += 1
        app._buffer_incremental_assign_filename(user_id, 'new.jpg')  # lands in the fresh buffer
        return True
    monkeypatch.setattr(app, '_cluster_flush_recently_requested', mutated_during_manual_check)
    app._buffer_incremental_assign_filename('lib-A', 'a.jpg')
    app._flush_stale_incremental_assign_buffers()
    assert enqueued == [('lib-A', ['a.jpg', 'b.jpg'])]
    assert app._cluster_batch_buffers['lib-A'] == ['new.jpg']


def test_flush_loop_stops_on_shutdown_event(enqueued, monkeypatch):
    monkeypatch.setattr(app, 'IPWORK_CLUSTER_BATCH_POLL_SECONDS', 0.03)
    monkeypatch.setattr(app, '_cluster_flush_recently_requested', lambda user_id: False)
    shutdown_requested = threading.Event()
    thread = threading.Thread(target=app._run_incremental_assign_flush_loop, args=(shutdown_requested,))
    thread.start()
    app._buffer_incremental_assign_filename('lib-A', 'a.jpg')
    shutdown_requested.set()
    thread.join(timeout=2)
    assert not thread.is_alive()


def test_flush_all_drains_every_remaining_buffer_regardless_of_age(enqueued):
    app._buffer_incremental_assign_filename('lib-A', 'a.jpg')
    app._buffer_incremental_assign_filename('lib-B', 'x.jpg')
    app._flush_all_incremental_assign_buffers()
    assert sorted(enqueued) == [('lib-A', ['a.jpg']), ('lib-B', ['x.jpg'])]
    assert app._cluster_batch_buffers == {}
    assert app._cluster_batch_started_at == {}
    assert app._cluster_batch_last_activity_at == {}


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


# --- Shared-volume checkpointing: survives a SIGKILL that never reaches
# _flush_all_incremental_assign_buffers (see CLUSTER_BUFFER_CHECKPOINT_DIR). ---

@pytest.fixture
def checkpoint_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(app, 'CLUSTER_BUFFER_CHECKPOINT_DIR', str(tmp_path))
    return tmp_path


def test_checkpoint_path_is_none_when_dir_unset(monkeypatch):
    monkeypatch.setattr(app, 'CLUSTER_BUFFER_CHECKPOINT_DIR', '')
    assert app._cluster_buffer_checkpoint_path('lib-A') is None


def test_write_and_clear_checkpoint_round_trip(checkpoint_dir):
    app._write_cluster_buffer_checkpoint('lib-A', ['a.jpg', 'b.jpg'])
    path = app._cluster_buffer_checkpoint_path('lib-A')
    assert os.path.exists(path)
    with open(path) as f:
        data = json.load(f)
    assert data['filenames'] == ['a.jpg', 'b.jpg']
    app._clear_cluster_buffer_checkpoint('lib-A')
    assert not os.path.exists(path)


def test_clear_checkpoint_is_a_noop_when_file_missing(checkpoint_dir):
    app._clear_cluster_buffer_checkpoint('lib-A')  # must not raise


def test_write_checkpoint_is_a_noop_when_dir_unset(monkeypatch):
    monkeypatch.setattr(app, 'CLUSTER_BUFFER_CHECKPOINT_DIR', '')
    app._write_cluster_buffer_checkpoint('lib-A', ['a.jpg'])  # must not raise, nothing to check


def test_checkpoint_all_cluster_buffers_writes_one_file_per_buffered_user(checkpoint_dir, enqueued):
    app._buffer_incremental_assign_filename('lib-A', 'a.jpg')
    app._buffer_incremental_assign_filename('lib-B', 'x.jpg')
    app._checkpoint_all_cluster_buffers()
    with open(app._cluster_buffer_checkpoint_path('lib-A')) as f:
        assert json.load(f)['filenames'] == ['a.jpg']
    with open(app._cluster_buffer_checkpoint_path('lib-B')) as f:
        assert json.load(f)['filenames'] == ['x.jpg']


def test_idle_flush_clears_the_checkpoint(checkpoint_dir, enqueued, monkeypatch):
    monkeypatch.setattr(app, 'IPWORK_CLUSTER_IDLE_SECONDS', 300)
    monkeypatch.setattr(app, '_cluster_flush_recently_requested', lambda user_id: False)
    now = [1000.0]
    monkeypatch.setattr(app.time, 'monotonic', lambda: now[0])
    app._buffer_incremental_assign_filename('lib-A', 'a.jpg')
    app._write_cluster_buffer_checkpoint('lib-A', ['a.jpg'])
    now[0] += 301
    app._flush_stale_incremental_assign_buffers()
    assert enqueued == [('lib-A', ['a.jpg'])]
    assert app._cluster_buffer_checkpoint_path('lib-A') is not None
    assert not os.path.exists(app._cluster_buffer_checkpoint_path('lib-A'))


def test_safety_cap_flush_clears_the_checkpoint(checkpoint_dir, enqueued, monkeypatch):
    monkeypatch.setattr(app, 'IPWORK_CLUSTER_BATCH_SAFETY_MAX_FILENAMES', 2)
    app._write_cluster_buffer_checkpoint('lib-A', ['a.jpg'])
    app._buffer_incremental_assign_filename('lib-A', 'a.jpg')
    app._buffer_incremental_assign_filename('lib-A', 'b.jpg')
    assert enqueued == [('lib-A', ['a.jpg', 'b.jpg'])]
    assert not os.path.exists(app._cluster_buffer_checkpoint_path('lib-A'))


def test_shutdown_flush_clears_the_checkpoint(checkpoint_dir, enqueued):
    app._buffer_incremental_assign_filename('lib-A', 'a.jpg')
    app._write_cluster_buffer_checkpoint('lib-A', ['a.jpg'])
    app._flush_all_incremental_assign_buffers()
    assert enqueued == [('lib-A', ['a.jpg'])]
    assert not os.path.exists(app._cluster_buffer_checkpoint_path('lib-A'))


def test_recovery_skips_fresh_checkpoint_files(checkpoint_dir, enqueued):
    app._write_cluster_buffer_checkpoint('lib-A', ['a.jpg'])
    app._recover_stranded_cluster_buffer_checkpoints()
    assert enqueued == []
    assert os.path.exists(app._cluster_buffer_checkpoint_path('lib-A'))


def test_recovery_re_enqueues_and_removes_stale_checkpoint_files(checkpoint_dir, enqueued, monkeypatch):
    monkeypatch.setattr(app, 'CLUSTER_BUFFER_CHECKPOINT_STALE_SECONDS', 1)
    app._write_cluster_buffer_checkpoint('lib-A', ['a.jpg', 'b.jpg'])
    path = app._cluster_buffer_checkpoint_path('lib-A')
    old = time.time() - 10
    os.utime(path, (old, old))
    app._recover_stranded_cluster_buffer_checkpoints()
    assert enqueued == [('lib-A', ['a.jpg', 'b.jpg'])]
    assert not os.path.exists(path)


def test_recovery_handles_empty_checkpoint_dir(checkpoint_dir, enqueued):
    app._recover_stranded_cluster_buffer_checkpoints()  # must not raise
    assert enqueued == []


def test_recovery_is_a_noop_when_dir_unset(monkeypatch, enqueued):
    monkeypatch.setattr(app, 'CLUSTER_BUFFER_CHECKPOINT_DIR', '')
    app._recover_stranded_cluster_buffer_checkpoints()  # must not raise
    assert enqueued == []


def test_clear_checkpoint_removes_the_now_empty_user_directory(checkpoint_dir):
    app._write_cluster_buffer_checkpoint('lib-A', ['a.jpg'])
    user_dir = os.path.dirname(app._cluster_buffer_checkpoint_path('lib-A'))
    assert os.path.isdir(user_dir)
    app._clear_cluster_buffer_checkpoint('lib-A')
    assert not os.path.exists(user_dir)


def test_clear_checkpoint_leaves_directory_when_another_writer_still_has_a_file(checkpoint_dir):
    app._write_cluster_buffer_checkpoint('lib-A', ['a.jpg'])
    user_dir = os.path.dirname(app._cluster_buffer_checkpoint_path('lib-A'))
    # Simulate a second replica's own checkpoint file for the same user.
    with open(os.path.join(user_dir, 'other-writer.json'), 'w') as f:
        json.dump({'filenames': ['x.jpg']}, f)
    app._clear_cluster_buffer_checkpoint('lib-A')
    assert os.path.isdir(user_dir)
    assert os.path.exists(os.path.join(user_dir, 'other-writer.json'))


def test_recovery_removes_the_now_empty_user_directory(checkpoint_dir, enqueued, monkeypatch):
    monkeypatch.setattr(app, 'CLUSTER_BUFFER_CHECKPOINT_STALE_SECONDS', 1)
    app._write_cluster_buffer_checkpoint('lib-A', ['a.jpg'])
    path = app._cluster_buffer_checkpoint_path('lib-A')
    old = time.time() - 10
    os.utime(path, (old, old))
    user_dir = os.path.dirname(path)
    app._recover_stranded_cluster_buffer_checkpoints()
    assert enqueued == [('lib-A', ['a.jpg'])]
    assert not os.path.exists(user_dir)
