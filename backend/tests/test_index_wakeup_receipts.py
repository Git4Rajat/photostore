"""Queue receipt cancellation across completion, failed sends and fast consumers."""
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError

import app
import automatic_index_updates as automatic
import index_work
import wave_runtime
from tests.test_automatic_index_updates import runtime
from tests.test_index_barrier_retry import clocked
from tests.test_index_work import EtagTable

send_index_wakeup = app._send_index_wakeup


class Queue:
    def __init__(self, name, now):
        self.name, self.now, self.rows, self.deleted = name, now, {}, []
        self.next_id = 0
        self.fail_send = False
        self.fail_delete = False

    def send_message(self, body, visibility_timeout=None):
        if self.fail_send:
            raise OSError('queue unavailable')
        self.next_id += 1
        message_id = f'{self.name}-{self.next_id}'
        row = dict(id=message_id, pop_receipt='sent', content=body, dequeue_count=0,
                   due=self.now() + (visibility_timeout or 0))
        self.rows[message_id] = row
        return SimpleNamespace(**row)

    def take(self):
        for row in self.rows.values():
            if row['due'] <= self.now():
                row.update(dequeue_count=row['dequeue_count'] + 1,
                           pop_receipt='received', due=self.now() + 300)
                return SimpleNamespace(**row)
        return None

    def delete_message(self, message, pop_receipt=None):
        if self.fail_delete:
            raise OSError('delete unavailable')
        message_id = message if isinstance(message, str) else message.id
        receipt = pop_receipt if isinstance(message, str) else message.pop_receipt
        if message_id not in self.rows:
            raise ResourceNotFoundError('already consumed')
        if self.rows[message_id]['pop_receipt'] != receipt:
            error = HttpResponseError('receipt is held by a consumer')
            error.error_code = 'PopReceiptMismatch'
            raise error
        del self.rows[message_id]
        self.deleted.append(message_id)

    def publish(self, library, scope, revision, delay=0):
        sent = self.send_message('{}', delay)
        return index_work.QueueWakeup(self.name, sent.id, sent.pop_receipt,
            lambda receipt: self.delete_message(receipt['messageId'], receipt['popReceipt']))


@pytest.fixture
def queued(clocked, monkeypatch):
    env = clocked
    env.main = Queue(app.INDEX_MAINTENANCE_QUEUE_NAME, lambda: env.now[0])
    env.deferred = Queue(app.INDEX_MAINTENANCE_DEFERRED_QUEUE_NAME, lambda: env.now[0])
    monkeypatch.setattr(app, 'index_maintenance_queue_client', env.main)
    monkeypatch.setattr(app, 'index_maintenance_deferred_queue_client', env.deferred)
    monkeypatch.setattr(app, '_send_index_wakeup', send_index_wakeup)
    env.pending = [True]
    monkeypatch.setattr(automatic, 'wave_pending', lambda _: env.pending[0])
    env.runtime.store.begin('A', 'vector', ['one'])
    env.runtime.store.finish('A', 'vector')
    env.key = env.store.request('A', 'vector')
    env.payload = dict(userId='A', scope='vector', jobId='job', maintenanceKey=env.key)
    assert app._run_index_maintenance_payload(env.payload) == 'deferred'
    # Seed a deferred recovery receipt to test backup receipt cancellation/retention
    sent = app._send_index_wakeup('A', 'vector', 1, 300)
    reservation = uuid.uuid4().hex
    row = dict(env.store._get(env.key), wakeReceipt=json.dumps(sent.receipt),
               wakeReceiptReservation=reservation, wakeUntil=env.now[0] + 300)
    env.store.table.update_entity(row)
    assert len(env.deferred.rows) == 1
    return env


def test_completion_cancels_backup_and_no_message_wakes_after_work_finishes(queued):
    env = queued
    receipt = json.loads(env.store._get(env.key)['wakeReceipt'])
    assert receipt['messageId'] in env.deferred.rows
    env.now[0] += 1
    env.pending[0] = False
    assert app.resume_index_maintenance_after_wave('A') == 1
    assert env.deferred.rows and not env.deferred.deleted  # Keep recovery until publication finishes.
    message = env.main.take()
    assert app._process_clustering_queue_message(message, env.main, env.main.name, 5)
    assert env.store._get(env.key) is None
    assert len(env.runtime.calls) == 1
    assert not env.deferred.rows and len(env.deferred.deleted) == 1
    env.now[0] = 1400
    assert env.main.take() is None and env.deferred.take() is None


def test_failed_immediate_send_retains_original_backup_then_recovery_supersedes_it(queued):
    env = queued
    original = dict(env.deferred.rows)
    env.pending[0] = False
    env.main.fail_send = True
    assert wave_runtime.resume_completed_wave_maintenance('A') == 0
    assert env.deferred.rows == original and not env.deferred.deleted
    row = env.store._get(env.key)
    assert json.loads(row['wakeReceipt'])['messageId'] in original
    assert not row['failed']
    env.main.fail_send = False
    env.now[0] = row['wakeUntil']
    assert app.recover_pending_index_work('A') == 1
    assert len(env.main.rows) == 1 and env.deferred.rows
    assert app._process_clustering_queue_message(env.main.take(), env.main, env.main.name, 5)
    assert not env.deferred.rows


def test_lost_completion_signal_original_backup_still_runs(queued):
    env = queued
    env.pending[0] = False
    env.now[0] = 1300
    message = env.deferred.take()
    assert app._process_clustering_queue_message(message, env.deferred, env.deferred.name, 5)
    assert env.store._get(env.key) is None
    assert len(env.runtime.calls) == 1 and not env.deferred.rows


def test_cancellation_never_deletes_a_receipt_already_claimed_by_a_consumer(queued):
    env = queued
    env.pending[0] = False
    env.now[0] = 1300
    received = env.deferred.take()  # Azure rotates the pop receipt.
    assert app.resume_index_maintenance_after_wave('A') == 1
    assert received.id in env.deferred.rows
    assert env.deferred.rows[received.id]['pop_receipt'] == received.pop_receipt
    assert app._process_clustering_queue_message(received, env.deferred, env.deferred.name, 5)
    duplicate = env.main.take()
    assert app._process_clustering_queue_message(duplicate, env.main, env.main.name, 5)
    assert len(env.runtime.calls) == 1


def test_delete_outage_does_not_discard_replacement_or_maintenance_intent(queued, caplog):
    env = queued
    env.pending[0] = False
    env.deferred.fail_delete = True
    assert app.resume_index_maintenance_after_wave('A') == 1
    assert env.deferred.rows and env.main.rows and env.store._get(env.key)
    assert app._process_clustering_queue_message(env.main.take(), env.main, env.main.name, 5)
    assert env.store._get(env.key) is None and env.deferred.rows
    assert 'cancellation failed' in caplog.text


def test_slow_send_response_after_completion_cancels_its_unrecorded_delayed_receipt():
    now = [1000.0]
    store = index_work.WorkStore(EtagTable(), clock=lambda: now[0])
    queue = Queue('deferred', lambda: now[0])
    key = store.request('A', 'vector')
    claim = store.claim(key)
    store.release(claim, deferred=True, retry_delay=300, retry_reason='wave_wait')
    def delayed_send(*args):
        published = queue.publish(*args)
        # Completion and a fast worker win while the delayed send response is in flight.
        now[0] = 1001
        assert store.resume_wave_waits('A', lambda *args: None, scopes={'vector'}) == 1
        store.finish(store.claim(key))
        return published
    assert store.notify(key, delayed_send)
    assert store._get(key) is None and not queue.rows


@pytest.mark.parametrize('resume_again', [False, True])
def test_out_of_order_delayed_responses_cancel_only_the_superseded_wakeup(resume_again):
    now = [1000.0]
    store = index_work.WorkStore(EtagTable(), clock=lambda: now[0])
    queue = Queue('deferred', lambda: now[0])
    key = store.request('A', 'vector')
    store.release(store.claim(key), deferred=True, retry_delay=300, retry_reason='wave_wait')

    def slow_send(*args):
        published = queue.publish(*args)
        now[0] = 1001
        assert store.resume_wave_waits('A', lambda *args: None, scopes={'vector'}) == 1
        # Another wave blocks the resumed worker while the first send response
        # is still pending. Its newer delayed receipt is recorded first.
        store.release(store.claim(key), deferred=True, retry_delay=300, retry_reason='wave_wait')
        assert store.notify(key, queue.publish)
        assert len(queue.rows) == 2
        return published

    assert store.notify(key, slow_send)
    receipt = json.loads(store._get(key)['wakeReceipt'])
    assert list(queue.rows) == [receipt['messageId']]
    assert queue.deleted == ['deferred-1']
    if resume_again:
        now[0] = 1002
        assert store.resume_wave_waits('A', lambda *args: None, scopes={'vector'}) == 1
    else:
        # Losing a completion signal still leaves the latest fallback usable.
        now[0] = 1301
        assert queue.take().id == receipt['messageId']
    claim = store.claim(key)
    assert claim is not None
    assert store.finish(claim, cancel_wakeup=lambda receipt: queue.delete_message(
        receipt['messageId'], receipt['popReceipt'])) is False
    if not resume_again:
        # The consumer holds the rotated receipt and ACKs its own delivery.
        queue.delete_message(receipt['messageId'], 'received')
    assert store._get(key) is None and not queue.rows


@pytest.mark.parametrize('resume_again', [False, True])
def test_old_response_during_new_send_cancels_the_receipt_actually_displaced(resume_again):
    now = [1000.0]
    store = index_work.WorkStore(EtagTable(), clock=lambda: now[0])
    queue = Queue('deferred', lambda: now[0])
    key = store.request('A', 'vector')
    store.release(store.claim(key), deferred=True, retry_delay=300, retry_reason='wave_wait')
    accepted, return_old = Event(), Event()

    def send_old(*args):
        published = queue.publish(*args)
        accepted.set()
        assert return_old.wait(5)
        return published

    with ThreadPoolExecutor(max_workers=1) as pool:
        old = pool.submit(store.notify, key, send_old)
        try:
            assert accepted.wait(5)
            now[0] = 1001
            assert store.resume_wave_waits('A', lambda *args: None, scopes={'vector'}) == 1
            store.release(store.claim(key), deferred=True, retry_delay=300, retry_reason='wave_wait')

            def send_new(*args):
                published = queue.publish(*args)
                # Let the old response attach its receipt after the new send's
                # reservation, but before the new response records its receipt.
                return_old.set()
                assert old.result(timeout=5)
                assert json.loads(store._get(key)['wakeReceipt'])['messageId'] == 'deferred-1'
                return published

            assert store.notify(key, send_new)
        finally:
            return_old.set()
    receipt = json.loads(store._get(key)['wakeReceipt'])
    assert list(queue.rows) == [receipt['messageId']]
    assert queue.deleted == ['deferred-1']
    if resume_again:
        now[0] = 1002
        assert store.resume_wave_waits('A', lambda *args: None, scopes={'vector'}) == 1
    else:
        now[0] = 1301
        message = queue.take()
        assert message.id == receipt['messageId']
    assert store.finish(store.claim(key), cancel_wakeup=lambda receipt: queue.delete_message(
        receipt['messageId'], receipt['popReceipt'])) is False
    if not resume_again:
        queue.delete_message(message)
    assert store._get(key) is None and not queue.rows


def test_receipt_bookkeeping_failure_does_not_turn_accepted_send_into_failure(monkeypatch):
    store = index_work.WorkStore(EtagTable(), clock=lambda: 1000)
    queue = Queue('deferred', lambda: 1000)
    key = store.request('A', 'vector', delay=300)
    replace = store._replace
    def fail_record(current, row):
        if 'wakeReceipt' in row:
            raise OSError('table unavailable after send')
        return replace(current, row)
    monkeypatch.setattr(store, '_replace', fail_record)
    assert store.notify(key, queue.publish)
    assert len(queue.rows) == 1 and store._get(key)


def test_racing_inline_claim_keeps_the_only_accepted_recovery_receipt():
    store = index_work.WorkStore(EtagTable(), clock=lambda: 1000)
    queue = Queue('deferred', lambda: 1000)
    key = store.request('A', 'people')
    claims = []
    def send(*args):
        published = queue.publish(*args)
        # Inline execution can claim while the send response is still pending;
        # it has no received queue message to be redelivered after a crash.
        claims.append(store.claim(key))
        return published
    assert store.notify(key, send, minimum_delay=300)
    assert claims[0] and len(queue.rows) == 1 and not queue.deleted
    def cancel(receipt):
        queue.delete_message(receipt['messageId'], receipt['popReceipt'])
    assert store.finish(claims[0], cancel_wakeup=cancel) is False
    assert not queue.rows


def test_early_immediate_delivery_cannot_remove_the_delayed_recovery_path(queued):
    env = queued
    env.pending[0] = False
    assert app.resume_index_maintenance_after_wave('A') == 1
    # A concurrent source edit coalesces the intent after the immediate wake was sent.
    env.store.request('A', 'vector', delay=2)
    assert app._process_clustering_queue_message(env.main.take(), env.main, env.main.name, 5)
    assert not env.runtime.calls and env.deferred.rows
    env.now[0] = 1300
    assert app._process_clustering_queue_message(env.deferred.take(), env.deferred, env.deferred.name, 5)
    assert len(env.runtime.calls) == 1 and env.store._get(env.key) is None


def test_newer_revision_retains_backup_until_the_latest_intent_finishes(queued):
    env = queued
    env.pending[0] = False
    assert app.resume_index_maintenance_after_wave('A') == 1
    first = env.store.claim(env.key)
    env.store.request('A', 'vector')
    assert env.store.finish(first, cancel_wakeup=app._cancel_index_wakeup) is True
    assert env.deferred.rows and not env.deferred.deleted
    latest = env.store.claim(env.key)
    assert env.store.finish(latest, cancel_wakeup=app._cancel_index_wakeup) is False
    assert not env.deferred.rows
