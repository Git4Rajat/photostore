"""Bounded queue waves prepare indexes without changing per-message ACKs."""
import json
import logging
from concurrent.futures import Future
from types import SimpleNamespace

import pytest

import app
from test_face_by_filename_lookup import AzureFaceTable
from test_ipworker_diagnostics import _prepare_queue, _StopLoop


def message(index, *, user='u1', steps=None, dequeue_count=1):
    return SimpleNamespace(id=str(index), dequeue_count=dequeue_count, content=json.dumps({
        'user_id': user, 'filename': f'{index}.jpg', 'steps': steps or ['face'],
    }))


def test_prepare_eligibility_and_library_grouping(monkeypatch):
    metadata = AzureFaceTable()
    messages = [message(i) for i in range(9)]
    messages[1] = message(1, steps=['ocr'])
    messages[2] = message(2, dequeue_count=app.IPWORKER_MAX_RETRIES + 1)
    messages[8] = message(8, user='u2')
    for index in [0, 1, 2, 4, 5, 6, 7, 8]:
        metadata.upsert_entity({'PartitionKey': 'u2' if index == 8 else 'u1',
                                'RowKey': f'{index}.jpg', 'face_status': 'pending'})
    metadata.rows[('u1', '4.jpg')]['processing_state'] = 'deleted'
    metadata.rows[('u1', '5.jpg')]['face_status'] = 'done'
    metadata.rows[('u1', '6.jpg')]['face_status'] = 'no_data'
    monkeypatch.setattr(app, 'metadata_table_client', metadata)
    monkeypatch.setattr(app, '_browser_processing_face_version_stale', lambda row: False)
    calls = []
    monkeypatch.setattr(app, 'reconcile_face_filename_indexes_batch',
                        lambda user, names, **kw: calls.append((user, set(names))))
    app._prepare_ipwork_face_indexes(messages, lambda: False)
    assert calls == [('u1', {'0.jpg', '7.jpg'}), ('u2', {'8.jpg'})]


def test_prepare_invalid_payloads_and_shutdown_do_not_scan(monkeypatch):
    calls = []
    monkeypatch.setattr(app, 'reconcile_face_filename_indexes_batch', lambda *a, **kw: calls.append(a))
    msgs = [SimpleNamespace(content=value) for value in ['{', '[]', '{}', '{"steps":"face"}']]
    app._prepare_ipwork_face_indexes(msgs, lambda: False)
    app._prepare_ipwork_face_indexes([message(1)], lambda: True)
    assert calls == []


def test_prepare_includes_stale_done_face_version(monkeypatch):
    metadata = AzureFaceTable()
    metadata.upsert_entity({'PartitionKey': 'u1', 'RowKey': '1.jpg', 'face_status': 'done'})
    monkeypatch.setattr(app, 'metadata_table_client', metadata)
    monkeypatch.setattr(app, '_browser_processing_face_version_stale', lambda row: True)
    calls = []
    monkeypatch.setattr(app, 'reconcile_face_filename_indexes_batch', lambda *a, **kw: calls.append(a))
    app._prepare_ipwork_face_indexes([message(1)], lambda: False)
    assert calls == [('u1', {'1.jpg'})]


@pytest.mark.parametrize('prepare_failure', [False, True])
def test_bounded_wave_prepared_once_ack_only_after_processing(monkeypatch, prepare_failure):
    events = []
    first = [message(i) for i in range(8)]

    class Queue:
        def __init__(self):
            self.calls = []
            self.deletes = []

        def create_queue(self):
            pass

        def receive_messages(self, **kw):
            self.calls.append(kw)
            if len(self.calls) == 1:
                return first
            assert len(events) == (1 if prepare_failure else 9)
            return []

        def delete_message(self, msg):
            assert ('process', msg.id) in events
            self.deletes.append(msg.id)

    queue = Queue()
    _prepare_queue(monkeypatch, queue)
    monkeypatch.setattr(app, 'IPWORKER_FACE_RECONCILE_BATCH_SIZE', 8)
    monkeypatch.setattr(app, 'IPWORKER_CONCURRENCY', 2)

    def prepare(messages, cancelled):
        events.append(('prepare', len(messages)))
        if prepare_failure:
            raise OSError('page failed')

    def process(msg):
        events.append(('process', msg.id))
        return 'error' if msg.id == '3' else 'done'

    monkeypatch.setattr(app, '_prepare_ipwork_face_indexes', prepare)
    monkeypatch.setattr(app, '_process_ipwork_message', process)
    monkeypatch.setattr(app.time, 'sleep', lambda seconds: (_ for _ in ()).throw(_StopLoop()))
    with pytest.raises(_StopLoop):
        app.run_ipworker()
    assert events[0] == ('prepare', 8)
    assert sorted(queue.deletes) == ([] if prepare_failure else ['0', '1', '2', '4', '5', '6', '7'])
    assert all(c['max_messages'] == 8 for c in queue.calls)


@pytest.mark.parametrize('stuck', [False, True])
def test_shutdown_during_preparation_never_processes_or_acks_batch(monkeypatch, stuck):
    handlers = {}
    deletes = []
    processed = []
    now = SimpleNamespace(value=0)

    class Queue:
        def create_queue(self):
            pass

        def receive_messages(self, **kw):
            return [message(1), message(2)]

        def delete_message(self, msg):
            deletes.append(msg.id)

    _prepare_queue(monkeypatch, Queue())
    monkeypatch.setattr(app, 'IPWORKER_FACE_RECONCILE_BATCH_SIZE', 8)
    monkeypatch.setattr(app, 'IPWORKER_SHUTDOWN_GRACE_SECONDS', 1)
    monkeypatch.setattr(app.signal, 'signal', lambda sig, handler: handlers.update({sig: handler}))
    monkeypatch.setattr(app.time, 'monotonic', lambda: now.value)
    monkeypatch.setattr(app, '_process_ipwork_message', lambda msg: processed.append(msg))
    exits = []
    monkeypatch.setattr(app.os, '_exit', exits.append)

    def prepare(*a):
        handlers[app.signal.SIGTERM](app.signal.SIGTERM, None)

    monkeypatch.setattr(app, '_prepare_ipwork_face_indexes', prepare)
    if stuck:
        class Executor:
            def __init__(self, **kw):
                pass

            def submit(self, function, *args):
                function(*args)
                return Future()

            def shutdown(self, **kw):
                pass
        monkeypatch.setattr(app, 'ThreadPoolExecutor', Executor)
        def wait(futures, **kw):
            now.value += 1
            return set(), set(futures)
        monkeypatch.setattr(app, 'wait', wait)
    app.run_ipworker()
    assert processed == deletes == []
    assert exits == ([0] if stuck else [])


def test_old_visibility_budget_leaves_unstarted_messages_unacked(monkeypatch):
    now = SimpleNamespace(value=0)
    processed, deletes = [], []

    class Queue:
        count = 0

        def create_queue(self):
            pass

        def receive_messages(self, **kw):
            self.count += 1
            return [message(1), message(2)] if self.count == 1 else []

        def delete_message(self, msg):
            deletes.append(msg.id)

    _prepare_queue(monkeypatch, Queue())
    monkeypatch.setattr(app, 'IPWORKER_FACE_RECONCILE_BATCH_SIZE', 8)
    monkeypatch.setattr(app.time, 'monotonic', lambda: now.value)
    monkeypatch.setattr(app, '_prepare_ipwork_face_indexes', lambda *a: setattr(now, 'value', 151))
    monkeypatch.setattr(app, '_process_ipwork_message', lambda msg: processed.append(msg))
    monkeypatch.setattr(app.time, 'sleep', lambda seconds: (_ for _ in ()).throw(_StopLoop()))
    with pytest.raises(_StopLoop):
        app.run_ipworker()
    assert processed == deletes == []


def test_ready_wave_never_exceeds_inference_slots(monkeypatch):
    futures, processed, events = [], [], []

    class Executor:
        def __init__(self, **kw):
            self.preparation = kw.get('thread_name_prefix') == 'ipwork-index'

        def submit(self, function, *args):
            future = Future()
            if self.preparation:
                function(*args)
                future.set_result(None)
            else:
                processed.append(args[0].id)
                futures.append(future)
                assert sum(not f.done() for f in futures) <= 2
            return future

        def shutdown(self, **kw):
            pass

    class Queue:
        receives = 0

        def create_queue(self):
            pass

        def receive_messages(self, **kw):
            self.receives += 1
            if self.receives == 1:
                return [message(i) for i in range(8)]
            # The next wave's fetch+scan now starts once `ready` is drained
            # (every message handed to the executor), not once the previous
            # wave's futures are also done -- preparation for wave 2 should
            # overlap wave 1's last 1-2 still-finishing tasks instead of
            # waiting for them, bounded by IPWORKER_CONCURRENCY (2).
            assert len(processed) == 8
            assert 0 < sum(not f.done() for f in futures) <= 2
            return []

        def delete_message(self, msg):
            events.append(msg.id)

    queue = Queue()
    _prepare_queue(monkeypatch, queue)
    monkeypatch.setattr(app, 'IPWORKER_FACE_RECONCILE_BATCH_SIZE', 8)
    monkeypatch.setattr(app, 'IPWORKER_CONCURRENCY', 2)
    monkeypatch.setattr(app, 'ThreadPoolExecutor', Executor)
    monkeypatch.setattr(app, '_prepare_ipwork_face_indexes', lambda *args: None)

    def complete_one(waiting, **kw):
        if all(f.done() for f in waiting):
            return set(waiting), set()
        # Completing only one proves refill does not submit all eight jobs
        # to an unbounded executor backlog.
        for f in waiting:
            if not f.done():
                f.set_result('done')
                return {f}, set(waiting) - {f}

    monkeypatch.setattr(app, 'wait', complete_one)
    monkeypatch.setattr(app.time, 'sleep', lambda seconds: (_ for _ in ()).throw(_StopLoop()))
    with pytest.raises(_StopLoop):
        app.run_ipworker()
    assert len(processed) == len(events) == 8


@pytest.mark.parametrize('phase', ['preparation', 'photo', 'rollback_photo'])
def test_watchdog_recycles_blocked_tasks_without_ack_or_thread_replacement(monkeypatch, phase, caplog):
    import faulthandler
    dumps = []
    monkeypatch.setattr(faulthandler, 'dump_traceback', lambda **kw: dumps.append(kw))
    now = SimpleNamespace(value=0)
    executors, exits, deletes = [], [], []

    class Executor:
        def __init__(self, **kw):
            self.preparation = kw.get('thread_name_prefix') == 'ipwork-index'
            self.shutdown_calls = []
            self.submitted = 0
            executors.append(self)

        def submit(self, function, *args):
            self.submitted += 1
            future = Future()
            if self.preparation and phase != 'preparation':
                future.set_result(None)
            # All photo futures, or the preparation future itself, remain
            # blocked as though a native/socket call ignored cancellation.
            return future

        def shutdown(self, **kw):
            self.shutdown_calls.append(kw)

    class Queue:
        receives = 0

        def create_queue(self):
            pass

        def receive_messages(self, **kw):
            self.receives += 1
            if self.receives == 1:
                return [message(1), message(2)]
            # Once this batch's `ready` drains into the (now permanently
            # stuck, phase != 'preparation') in_flight slots, preparation may
            # legitimately prefetch again before the watchdog notices nothing
            # is progressing -- a real queue just has nothing new to offer.
            return []

        def delete_message(self, msg):
            deletes.append(msg.id)

    queue = Queue()
    _prepare_queue(monkeypatch, queue)
    monkeypatch.setattr(app, 'IPWORKER_FACE_RECONCILE_BATCH_SIZE', 1 if phase == 'rollback_photo' else 8)
    monkeypatch.setattr(app, 'IPWORKER_CONCURRENCY', 2)
    monkeypatch.setattr(app, 'IPWORKER_VISIBILITY_TIMEOUT_SECONDS', 300)
    monkeypatch.setattr(app, 'IPWORKER_TASK_TIMEOUT_SECONDS', 120)
    monkeypatch.setattr(app, 'ThreadPoolExecutor', Executor)
    monkeypatch.setattr(app.time, 'monotonic', lambda: now.value)
    monkeypatch.setattr(app.os, '_exit', exits.append)

    def wait(futures, **kw):
        done = {future for future in futures if future.done()}
        if not done:
            now.value += 121
        return done, set(futures) - done

    monkeypatch.setattr(app, 'wait', wait)
    app.run_ipworker()
    assert exits == [1]
    assert dumps == [{'all_threads': True}]
    assert deletes == [] and queue.receives >= 1
    assert len(executors) == (1 if phase == 'rollback_photo' else 2)
    assert all(ex.shutdown_calls == [{'wait': False, 'cancel_futures': True}] for ex in executors)
    assert 'ipwork watchdog timeout' in caplog.text