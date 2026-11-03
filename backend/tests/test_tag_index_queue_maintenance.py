"""Real enqueue/outbox, exact model-owner dispatch, and queue ACK regressions."""
import json
import signal
import threading
import time
from types import SimpleNamespace

import pytest
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError

import app
import automatic_index_updates as automatic
import index_publication
import index_work
import storage_utils as su
from tests.test_automatic_index_updates import runtime, mark  # noqa: F401

REAL_SEND = app._send_tag_index_wakeup
REAL_WORK_STORE = index_work.WorkStore


class Queue:
    def __init__(self):
        self.sent, self.deleted, self.updated = [], [], []
        self.error = None

    def send_message(self, content, **options):
        if self.error:
            raise self.error
        message = SimpleNamespace(id=str(len(self.sent)), content=content, dequeue_count=1, pop_receipt='sent')
        self.sent.append((json.loads(content), options, message))
        return message

    def create_queue(self):
        pass

    def delete_message(self, message):
        self.deleted.append(message)

    def update_message(self, message, **options):
        self.updated.append((message, options))
        return SimpleNamespace(id=message.id, pop_receipt='renewed', dequeue_count=message.dequeue_count)


class LeaseBlobs:
    """Fenced blob lease protocol, used by the REAL renewable catalog guard."""
    def __init__(self):
        self.blobs = {}
        self.acquired = self.released = self.renewed = 0

    def get_blob_client(self, *, container, blob):
        service = self
        key = container, blob

        class Blob:
            def upload_blob(self, content, *, overwrite=False, lease=None):
                if key in service.blobs and not overwrite:
                    raise ResourceExistsError('exists')
                if key in service.blobs and service.blobs[key][1]:
                    assert lease is service.blobs[key][1]
                service.blobs[key] = (content, lease)

            def download_blob(self, **options):
                if key not in service.blobs:
                    raise ResourceNotFoundError('missing')
                return SimpleNamespace(readall=lambda: service.blobs[key][0])

            def acquire_lease(self, **options):
                assert service.blobs[key][1] is None
                service.acquired += 1

                class Lease:
                    def renew(self):
                        assert service.blobs[key][1] is self
                        service.renewed += 1

                    def release(self):
                        assert service.blobs[key][1] is self
                        service.blobs[key] = (service.blobs[key][0], None)
                        service.released += 1
                lease = Lease()
                service.blobs[key] = (service.blobs[key][0], lease)
                return lease
        return Blob()


@pytest.fixture
def env(runtime, monkeypatch):
    now = [1000.0]

    class ClockStore(REAL_WORK_STORE):
        def __init__(self, table, **options):
            super().__init__(table, **dict(options, clock=lambda: now[0]))
    monkeypatch.setattr(index_work, 'WorkStore', ClockStore)
    queue, deadletter, blobs = Queue(), Queue(), LeaseBlobs()
    monkeypatch.setattr(app, '_send_tag_index_wakeup', REAL_SEND)
    monkeypatch.setattr(app, 'ipwork_queue_client', queue)
    monkeypatch.setattr(app, 'queue_service_client', SimpleNamespace(get_queue_client=lambda _: deadletter))
    monkeypatch.setattr(app, 'INDEX_MAINTENANCE_DEADLETTER_QUEUE_NAME', 'existing-index-deadletter')
    monkeypatch.setattr(app, '_notify_index_ready', lambda *args: None)
    monkeypatch.setenv('APP_ROLE', 'vision')
    monkeypatch.setattr(app.vision_utils, 'image_encoder_available', lambda: True)
    monkeypatch.setattr(app.vision_utils, 'get_text_embedding_version', lambda: 'clip-real')
    su._CTX['blob_service_client'] = blobs
    monkeypatch.setattr(su, '_lexical_index_container_name', lambda: 'lexical')
    monkeypatch.setattr(app, '_handle_ipwork_queue_payload', lambda *args: pytest.fail('fake photo dispatch'))
    monkeypatch.setattr(app, '_sweep_tag_embedding_indexes', lambda: pytest.fail('unbounded queue sweep'))
    statuses = []
    monkeypatch.setattr(app, '_upsert_job_status', lambda *args, **kw: statuses.append((args, kw)))

    def refresh(user, **options):
        guard = index_publication._ACTIVE.get()
        assert guard is not None
        assert guard.claim.library == user and guard.claim.scope == 'tag_embedding'
        assert guard.store.partition == automatic.TAG_PARTITION
        assert guard.thread.is_alive()
        guard.check()
        guard.store.renew(guard.claim)
        catalog_user, catalog = index_publication._CATALOG.get()
        assert catalog_user == user and catalog._thread.is_alive()
        catalog.check()
        catalog._renew()
        runtime.calls.append(('tag_embedding', options))
        runtime.bases['tag_embedding'] = 'tag-base-' + str(len(runtime.calls))
        return SimpleNamespace(source_version=runtime.bases['tag_embedding'])
    monkeypatch.setattr(su, 'refresh_user_tag_embedding_index', refresh)
    store = ClockStore(runtime.jobs, partition=automatic.TAG_PARTITION)
    return SimpleNamespace(runtime=runtime, queue=queue, deadletter=deadletter, blobs=blobs,
                           store=store, now=now, statuses=statuses)


def enqueue(env, library='A', **options):
    return app.enqueue_index_build(library, reason='metadata-change', scope='tag_embedding', **options)


def message(env, library='A'):
    key = env.store.request(library, 'tag_embedding')
    payload = app._tag_index_payload(library, env.store._get(key)['revision'])
    return SimpleNamespace(id='maintenance', content=json.dumps(payload), dequeue_count=500, pop_receipt='old')


def test_real_enqueue_wakes_only_vision_and_coalesces_revisions(env, monkeypatch):
    monkeypatch.setattr(app, '_send_index_wakeup', lambda *args, **kw: pytest.fail('tag sent to index queue'))
    capture = []
    assert enqueue(env, capture=capture) == 'queued'
    first = env.store._get(env.store.key('A', 'tag_embedding'))['revision']
    assert len(capture) == 1 and capture[0][0] is env.queue
    for _ in range(20):
        assert enqueue(env) == 'already_active'
    assert len(env.queue.sent) == 1
    payload, options, _ = env.queue.sent[0]
    assert payload['type'] == 'tag_index_maintenance'
    assert payload['maintenancePartition'] == automatic.TAG_PARTITION
    assert payload['scope'] == 'tag_embedding'
    assert not {'filename', 'steps', 'user_id'} & payload.keys()
    assert options['visibility_timeout'] == 2
    assert env.store._get(payload['maintenanceKey'])['revision'] != first
    assert REAL_WORK_STORE(env.runtime.jobs)._get(payload['maintenanceKey']) is None


@pytest.mark.parametrize('missing', [False, True])
def test_queue_failure_returns_unavailable_retains_intent_and_recovers(env, monkeypatch, missing):
    env.queue.error = OSError('offline')
    if missing:
        monkeypatch.setattr(app, 'ipwork_queue_client', None)
    assert enqueue(env) == 'unavailable'
    key = env.store.key('A', 'tag_embedding')
    assert env.store._get(key) and not env.store._get(key)['failed']
    assert env.queue.sent == []
    env.now[0] += 63
    env.queue.error = None
    monkeypatch.setattr(app, 'ipwork_queue_client', env.queue)
    monkeypatch.setenv('APP_ROLE', 'index-worker')
    monkeypatch.setattr(app.vision_utils, 'image_encoder_available', lambda: pytest.fail('recovery loads model'))
    monkeypatch.setattr(env.store.__class__, 'claim', lambda *args: pytest.fail('recovery claims work'))
    monkeypatch.setattr(app, 'index_maintenance_queue_client', None)
    assert app.recover_pending_index_work('A') == 1
    assert app.recover_pending_index_work('A') == 0
    assert len(env.queue.sent) == 1
    assert not env.store._get(key).get('owner')


def test_recovery_resends_expired_wake_and_restores_missing_coverage_outbox(env, monkeypatch):
    mark(env.runtime, 'tag_embedding', ['one'])
    monkeypatch.setenv('APP_ROLE', 'index-worker')
    assert app.recover_pending_index_work('A') == 1
    assert app.recover_pending_index_work('A') == 0
    env.now[0] += 61
    assert app.recover_pending_index_work('A') == 1
    assert len(env.queue.sent) == 2
    assert not env.runtime.calls


def test_recovery_preserves_poison_ledger_until_explicit_repair(env):
    msg = message(env)
    key = json.loads(msg.content)['maintenanceKey']
    claim = env.store.claim(key)
    env.store.finish(claim, failed=True)
    mark(env.runtime, 'tag_embedding', ['one'])
    assert app.recover_pending_index_work('A') == 0
    assert env.store._get(key)['failed']
    assert app.recover_pending_index_work('A', retry_failed=True) == 1
    assert not env.store._get(key)['failed']


@pytest.mark.parametrize('override', [
    {'userId': 'B'}, {'userId': ' A'}, {'userId': 123}, {'scope': 'vector'},
    {'maintenanceKey': 'wrong'}, {'maintenancePartition': index_work.PARTITION},
    {'protocolVersion': True}, {'protocolVersion': 2}, {'intentRevision': ''},
    {'jobId': 'fake-photo'}, {'filename': 'fake.jpg', 'steps': ['face']},
])
def test_invalid_payload_never_claims_builds_or_changes_job_status(env, override):
    msg = message(env)
    payload = dict(json.loads(msg.content), **override)
    msg.content = json.dumps(payload)
    before = dict(env.runtime.jobs.rows)
    assert app._process_ipwork_message(msg) == 'tag_index_invalid'
    assert env.runtime.jobs.rows == before and not env.runtime.calls and not env.statuses


@pytest.mark.parametrize('role', ['core', 'index-worker', 'indexer', 'cluster', 'upload'])
def test_wrong_role_retains_valid_intent_without_photo_status(env, monkeypatch, role):
    msg = message(env)
    monkeypatch.setenv('APP_ROLE', role)
    monkeypatch.setattr(app.vision_utils, 'image_encoder_available', lambda: pytest.fail('wrong role model check'))
    before = dict(env.runtime.jobs.rows)
    assert app._process_ipwork_message(msg) == 'tag_index_retry'
    assert env.runtime.jobs.rows == before and not env.statuses and not env.runtime.calls


def test_exact_library_real_job_uses_both_renewers_and_ignores_photo_delivery_budget(env):
    mark(env.runtime, 'tag_embedding', ['one'])
    other = env.store.request('B', 'tag_embedding')
    # A malformed unrelated library row must not be scanned/claimed.
    env.runtime.jobs.upsert_entity({'PartitionKey': automatic.TAG_PARTITION, 'RowKey': 'bad', 'scope': 'vector'})
    msg = message(env)
    assert app._process_ipwork_message(msg) == 'tag_index_done'
    assert env.store._get(env.store.key('A', 'tag_embedding')) is None
    assert env.store._get(other) and env.store._get('bad')
    assert env.runtime.calls == [('tag_embedding', {'model_version': 'clip-real', 'bootstrap_incremental': True})]
    assert not env.runtime.store.capture('A', 'tag_embedding').ids
    assert env.blobs.acquired == env.blobs.released == env.blobs.renewed == 1
    assert app._process_ipwork_message(msg) == 'tag_index_noop'


def test_request_during_publication_is_not_consumed_and_sends_next_wake(env, monkeypatch):
    mark(env.runtime, 'tag_embedding', ['one'])
    msg = message(env)
    original = su.refresh_user_tag_embedding_index

    def refresh(*args, **kw):
        assert enqueue(env) == 'already_active'
        return original(*args, **kw)
    monkeypatch.setattr(su, 'refresh_user_tag_embedding_index', refresh)
    assert app._process_ipwork_message(msg) == 'tag_index_done'
    assert env.store._get(env.store.key('A', 'tag_embedding'))
    assert len(env.queue.sent) == 1


def test_source_busy_is_durable_visibility_retry_without_exhausting_budget(env):
    env.runtime.store.begin('A', 'tag_embedding', ['one'])
    msg = message(env)
    key = json.loads(msg.content)['maintenanceKey']
    for attempt in range(app.INDEX_BUILD_MAX_RETRIES + 4):
        assert app._process_ipwork_message(msg) == 'tag_index_deferred'
        row = env.store._get(key)
        assert not row['failed'] and row['attempts'] == 0 and row['notBefore'] == env.now[0] + 10
        assert len(env.queue.sent) == attempt + 1
        assert env.queue.sent[-1][1]['visibility_timeout'] == 10
        env.now[0] += 11
    assert not env.deadletter.sent and not env.runtime.calls
    env.runtime.store.finish('A', 'tag_embedding')
    # The reserved recovery wake also covers a later source revision.
    assert enqueue(env) == 'already_active'
    assert enqueue(env) == 'already_active'
    assert app._process_ipwork_message(msg) == 'tag_index_done'
    assert env.store._get(key) is None


def test_duplicate_tag_wakes_are_acked_without_failure_or_renewing_live_owner(env, monkeypatch):
    msg = message(env)
    key = json.loads(msg.content)['maintenanceKey']
    owner = env.store.claim(key)
    before = dict(env.store._get(key))
    monkeypatch.setattr(app.worker_logger, 'exception', lambda *a, **k: pytest.fail('Duplicate logged as failure'))
    for _ in range(20):
        assert app._process_ipwork_message(msg) == 'tag_index_noop'
    assert dict(env.store._get(key)) == before
    assert not env.runtime.calls and not env.queue.sent
    # The durable intent remains recoverable if the real owner crashes.
    env.now[0] += env.store.lease_seconds + env.store.wake_seconds + 1
    assert env.store.recover(REAL_SEND) == 1
    assert env.store._get(key)['owner'] == owner.owner


def test_wave_blocked_tag_job_does_not_acquire_catalog_lease(env, monkeypatch):
    msg = message(env)
    monkeypatch.setattr(automatic, 'wave_pending', lambda _: True)
    assert app._process_ipwork_message(msg) == 'tag_index_deferred'
    assert env.blobs.acquired == 0
    assert not env.runtime.calls
    key = json.loads(msg.content)['maintenanceKey']
    row = env.store._get(key)
    assert row['retryReason'] == 'wave_wait'
    assert row['notBefore'] == 0
    assert len(env.queue.sent) == 0
    for _ in range(20):
        assert app._process_ipwork_message(msg) == 'tag_index_noop'
    assert len(env.queue.sent) == 0  # No extra wakeups while wave is pending.


def test_terminal_wave_signal_resumes_tag_recovery_wake(env, monkeypatch):
    msg = message(env)
    key = json.loads(msg.content)['maintenanceKey']
    pending = [True]
    monkeypatch.setattr(automatic, 'wave_pending', lambda _: pending[0])
    monkeypatch.setattr(app, 'index_maintenance_queue_client', Queue())
    monkeypatch.setattr(app, 'recover_pending_index_work', lambda _: 0)
    assert app._process_ipwork_message(msg) == 'tag_index_deferred'
    assert len(env.queue.sent) == 0
    assert app.resume_index_maintenance_after_wave('A') == 0
    pending[0] = False
    assert app.resume_index_maintenance_after_wave('A') == 1
    assert env.store._get(key)['notBefore'] == 0
    assert len(env.queue.sent) == 1
    assert app._process_ipwork_message(env.queue.sent[-1][2]) == 'tag_index_done'
    assert env.store._get(key) is None


def test_source_busy_with_retry_send_failure_keeps_both_evidence_and_receipt(env):
    env.runtime.store.begin('A', 'tag_embedding', ['one'])
    msg = message(env)
    env.queue.error = OSError('offline')
    assert app._process_ipwork_message(msg) == 'tag_index_retry'
    row = env.store._get(json.loads(msg.content)['maintenanceKey'])
    assert row and row['attempts'] == 0 and not row['failed']
    assert not env.queue.sent  # Busy receipts are retried, not multiplied.
    env.runtime.store.finish('A', 'tag_embedding')
    env.now[0] += 11
    assert app._process_ipwork_message(msg) == 'tag_index_done'


def test_missing_real_catalog_ownership_never_runs_or_finishes(env, monkeypatch):
    msg = message(env)
    monkeypatch.setitem(su._CTX, 'blob_service_client', None)
    assert app._process_ipwork_message(msg) == 'tag_index_retry'
    assert env.store._get(json.loads(msg.content)['maintenanceKey']) and not env.runtime.calls


def test_real_build_failure_consumes_work_budget_not_delivery_count_and_deadletters(env, monkeypatch):
    mark(env.runtime, 'tag_embedding', ['one'])
    msg = message(env)
    monkeypatch.setattr(app, 'INDEX_BUILD_MAX_RETRIES', 2)
    monkeypatch.setattr(su, 'refresh_user_tag_embedding_index', lambda *args, **kw: (_ for _ in ()).throw(OSError('build failed')))
    key = json.loads(msg.content)['maintenanceKey']
    for attempt in (1, 2):
        assert app._process_ipwork_message(msg) == 'tag_index_retry'
        assert env.store._get(key)['attempts'] == attempt
        env.now[0] += 31
    env.deadletter.error = OSError('deadletter failed')
    assert app._process_ipwork_message(msg) == 'tag_index_retry'
    assert not env.store._get(key)['failed']
    env.now[0] += 31
    env.deadletter.error = None
    assert app._process_ipwork_message(msg) == 'tag_index_failed'
    assert env.store._get(key)['failed'] and len(env.deadletter.sent) == 1
    assert app._process_ipwork_message(msg) == 'tag_index_failed'
    assert len(env.deadletter.sent) == 1
    assert all(args[2] == app.INDEX_BUILD_JOB_TYPE for args, _ in env.statuses)


def run_queue(env, monkeypatch, msg):
    callbacks, batches = {}, [[msg]]
    monkeypatch.setattr(app.signal, 'signal', lambda sig, callback: callbacks.__setitem__(sig, callback))

    def receive(**options):
        if batches:
            return batches.pop()
        callbacks[signal.SIGTERM](signal.SIGTERM, None)
        return []
    env.queue.receive_messages = receive
    monkeypatch.setattr(app, 'queue_service_client', SimpleNamespace(get_queue_client=lambda _: env.queue))
    monkeypatch.setattr(app, 'IPWORKER_FACE_RECONCILE_BATCH_SIZE', 1)
    monkeypatch.setenv('IPWORKER_POLL_SECONDS', '0.01')
    for name in ('_register_ipwork_processors', '_prewarm_ipwork_models', '_ipwork_sweep_loop',
                 '_recover_stranded_cluster_buffer_checkpoints', '_run_incremental_assign_flush_loop',
                 '_flush_all_incremental_assign_buffers'):
        monkeypatch.setattr(app, name, lambda *args: None)
    monkeypatch.setattr(app, '_trigger_indexer_rebuild', lambda *args, **kw: pytest.fail('tag counted as photo'))
    app.run_ipworker()


def test_real_poll_loop_acks_only_after_exact_job_and_never_triggers_photo_milestone(env, monkeypatch):
    mark(env.runtime, 'tag_embedding', ['one'])
    msg = message(env)
    original = env.queue.delete_message

    def delete(receipt):
        assert env.store._get(json.loads(msg.content)['maintenanceKey']) is None
        assert env.runtime.store.control('A', 'tag_embedding')['coveredRevision']
        original(receipt)
    monkeypatch.setattr(env.queue, 'delete_message', delete)
    run_queue(env, monkeypatch, msg)
    assert [m.id for m in env.queue.deleted] == [msg.id]
    assert len(env.runtime.calls) == 1


def test_real_poll_loop_source_busy_acks_only_after_replacement_wake(env, monkeypatch):
    env.runtime.store.begin('A', 'tag_embedding', ['one'])
    msg = message(env)
    run_queue(env, monkeypatch, msg)
    assert env.queue.deleted == [msg]
    assert not env.queue.updated
    assert len(env.queue.sent) == 1
    assert env.queue.sent[0][1]['visibility_timeout'] == 10
    assert env.store._get(json.loads(msg.content)['maintenanceKey'])['attempts'] == 0


def test_fake_photo_fields_on_special_payload_never_enter_face_preparation(env):
    msg = message(env)
    msg.content = json.dumps(dict(json.loads(msg.content), filename='fake.jpg', steps=['face']))
    app._prepare_ipwork_face_indexes([msg], lambda: False)
    assert not env.runtime.calls


def test_real_source_pre_wake_consumed_before_begin_still_has_post_commit_wake(env, monkeypatch):
    original = env.queue.send_message
    consumed = []

    def send(content, **options):
        result = original(content, **options)
        if not consumed:
            consumed.append(True)
            env.now[0] += 3
            assert app._process_ipwork_message(result) == 'tag_index_done'
        return result
    monkeypatch.setattr(env.queue, 'send_message', send)
    source = automatic.SourceTable(env.runtime.source, 'metadata')
    source.upsert_entity({'PartitionKey': 'A', 'RowKey': 'one', 'tags': '["cat"]'})
    key = env.store.key('A', 'tag_embedding')
    assert len(env.queue.sent) == 2 and env.store._get(key)
    assert env.runtime.store.control('A', 'tag_embedding')['active'] == 0
    env.now[0] += 3
    assert app._process_ipwork_message(env.queue.sent[-1][2]) == 'tag_index_done'
    assert env.store._get(key) is None


def test_failed_prewrite_wake_preserves_intent_and_prevents_source_mutation(env):
    env.queue.error = OSError('offline')
    source = automatic.SourceTable(env.runtime.source, 'metadata')
    with pytest.raises(RuntimeError, match='unavailable'):
        source.upsert_entity({'PartitionKey': 'A', 'RowKey': 'one', 'tags': '["cat"]'})
    assert ('A', 'one') not in env.runtime.source.rows
    assert env.store._get(env.store.key('A', 'tag_embedding'))


def test_real_source_write_busy_wake_coalesces_post_request_and_retries(env, monkeypatch):
    original = env.runtime.source.upsert_entity
    observed = []

    def write(entity, **options):
        # The source reservation is live while the raw storage call runs.
        env.now[0] += 3
        observed.append(app._process_ipwork_message(env.queue.sent[0][2]))
        return original(entity, **options)
    monkeypatch.setattr(env.runtime.source, 'upsert_entity', write)
    source = automatic.SourceTable(env.runtime.source, 'metadata')
    source.upsert_entity({'PartitionKey': 'A', 'RowKey': 'one', 'tags': '["cat"]'})
    assert observed == ['tag_index_deferred']
    key = env.store.key('A', 'tag_embedding')
    assert env.store._get(key)['attempts'] == 0
    assert len(env.queue.sent) == 2  # pre-wake + coalesced post-commit wake
    assert env.runtime.store.control('A', 'tag_embedding')['active'] == 0
    env.now[0] += 11
    assert app._process_ipwork_message(env.queue.sent[0][2]) == 'tag_index_done'
    assert env.store._get(key) is None


def test_post_commit_wake_failure_reports_retry_but_keeps_committed_source_and_outbox(env, monkeypatch):
    original = env.queue.send_message
    consumed = []

    def send(content, **options):
        result = original(content, **options)
        if not consumed:
            consumed.append(True)
            env.now[0] += 3
            assert app._process_ipwork_message(result) == 'tag_index_done'
            env.queue.error = OSError('post-wake offline')
        return result
    monkeypatch.setattr(env.queue, 'send_message', send)
    source = automatic.SourceTable(env.runtime.source, 'metadata')
    with pytest.raises(BaseExceptionGroup, match='finalization failed'):
        source.upsert_entity({'PartitionKey': 'A', 'RowKey': 'one', 'tags': '["cat"]'})
    assert ('A', 'one') in env.runtime.source.rows
    assert env.runtime.store.control('A', 'tag_embedding')['active'] == 0
    assert env.store._get(env.store.key('A', 'tag_embedding'))
    env.queue.error = None
    env.now[0] += 63
    assert app.recover_pending_index_work('A') >= 1


def test_source_barrier_is_checked_even_after_prior_failure_budget_exhausted(env, monkeypatch):
    env.runtime.store.begin('A', 'tag_embedding', ['one'])
    msg = message(env)
    key = json.loads(msg.content)['maintenanceKey']
    row = env.store._get(key)
    env.runtime.jobs.upsert_entity(dict(row, attempts=10, attemptRevision=row['revision']))
    monkeypatch.setattr(app, 'INDEX_BUILD_MAX_RETRIES', 2)
    assert app._process_ipwork_message(msg) == 'tag_index_deferred'
    assert env.store._get(key)['attempts'] == 10 and not env.store._get(key)['failed']
    assert not env.deadletter.sent


def test_concurrent_claim_is_obsolete_not_poison_or_photo_failure(env):
    msg = message(env)
    key = json.loads(msg.content)['maintenanceKey']
    claim = env.store.claim(key)
    assert app._process_ipwork_message(msg) == 'tag_index_noop'
    assert env.store._get(key)['owner'] == claim.owner and not env.statuses


@pytest.mark.parametrize('valid', [False, True])
def test_poll_loop_invalid_ack_and_wrong_role_no_ack(env, monkeypatch, valid):
    msg = message(env)
    if valid:
        monkeypatch.setenv('APP_ROLE', 'index-worker')
    else:
        msg.content = json.dumps(dict(json.loads(msg.content), scope='vector'))
    run_queue(env, monkeypatch, msg)
    assert bool(env.queue.deleted) is not valid
    assert not env.runtime.calls and not env.statuses


def test_many_source_writes_coalesce_one_wake_without_losing_revisions(env):
    source = automatic.SourceTable(env.runtime.source, 'metadata')
    for i in range(25):
        source.upsert_entity({'PartitionKey': 'A', 'RowKey': f'photo-{i}', 'tags': '["cat"]'})
    assert len(env.queue.sent) == 1
    assert env.runtime.store.control('A', 'tag_embedding')['active'] == 0
    assert len(env.runtime.store.capture('A', 'tag_embedding').ids) == 25
    env.now[0] += 3
    assert app._process_ipwork_message(env.queue.sent[0][2]) == 'tag_index_done'
    assert not env.runtime.store.capture('A', 'tag_embedding').ids


def test_poll_loop_healthy_tag_scan_outlives_watchdog_budget(env, monkeypatch):
    """Real row progress, not renewals, keeps the long-running task alive."""
    mark(env.runtime, 'tag_embedding', ['one'])
    msg = message(env)
    original = su.refresh_user_tag_embedding_index
    monkeypatch.setattr(app, 'IPWORKER_TASK_TIMEOUT_SECONDS', 0.5)
    monkeypatch.setattr(app.os, '_exit', lambda code: pytest.fail('healthy tag scan killed'))

    def rows(*args, **kwargs):
        for i in range(20):
            time.sleep(0.05)
            yield {'PartitionKey': 'A', 'RowKey': str(i), 'tags': '["cat"]'}

    monkeypatch.setattr(su, '_query_projected', rows)
    monkeypatch.setattr(app.vision_utils, 'encode_text_embeddings_batch', lambda tags: [[1.0, 0.0] for _ in tags])
    elapsed = []

    def refresh(*args, **kwargs):
        started = time.monotonic()
        snapshot = su._build_user_tag_embedding_index_snapshot('A', 'scan-version')
        elapsed.append(time.monotonic() - started)
        assert snapshot.tags == ['cat']
        return original(*args, **kwargs)

    monkeypatch.setattr(su, 'refresh_user_tag_embedding_index', refresh)
    run_queue(env, monkeypatch, msg)
    assert elapsed[0] > app.IPWORKER_TASK_TIMEOUT_SECONDS
    assert [item.id for item in env.queue.deleted] == [msg.id]
    assert env.store._get(json.loads(msg.content)['maintenanceKey']) is None


def test_poll_loop_stalled_tag_build_still_exits_without_ack(env, monkeypatch):
    mark(env.runtime, 'tag_embedding', ['one'])
    msg = message(env)
    release, finished = threading.Event(), threading.Event()
    monkeypatch.setattr(app, 'IPWORKER_TASK_TIMEOUT_SECONDS', 0.1)
    import faulthandler
    monkeypatch.setattr(faulthandler, 'dump_traceback', lambda **kwargs: None)

    class WatchdogExit(BaseException):
        pass

    def exit_process(code):
        assert code == 1
        raise WatchdogExit()

    def blocked(*args, **kwargs):
        # Deliberately no progress: a hung source/encoder/transfer must
        # not be kept alive by the maintenance/catalog lease renewers.
        release.wait(5)
        raise OSError('interrupted test build')

    original = app._process_ipwork_message

    def process(message):
        try:
            return original(message)
        finally:
            finished.set()

    monkeypatch.setattr(app.os, '_exit', exit_process)
    monkeypatch.setattr(app, '_process_ipwork_message', process)
    monkeypatch.setattr(su, 'refresh_user_tag_embedding_index', blocked)
    try:
        with pytest.raises(WatchdogExit):
            run_queue(env, monkeypatch, msg)
        assert not env.queue.deleted
        assert env.store._get(json.loads(msg.content)['maintenanceKey'])
        assert env.runtime.store.capture('A', 'tag_embedding').ids == ['one']
    finally:
        release.set()
        assert finished.wait(3)
