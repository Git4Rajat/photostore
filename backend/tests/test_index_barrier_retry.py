"""Deterministic durable-state replay of Nov 2 SourceBusy/duplicate wakeups."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import app
import automatic_index_updates as automatic
import index_work
from tests.test_automatic_index_updates import runtime
from tests.test_index_work import EtagTable


write_status = app._upsert_job_status


@pytest.fixture
def clocked(runtime, monkeypatch):
    now = [1000.0]
    real_store = index_work.WorkStore
    class ClockStore(real_store):
        def __init__(self, table, **kw):
            super().__init__(table, clock=lambda: now[0], **kw)
    monkeypatch.setattr(index_work, 'WorkStore', ClockStore)
    monkeypatch.setenv('APP_ROLE', 'index-worker')
    monkeypatch.setattr(app, 'index_maintenance_queue_client', object())
    statuses, sent, events = [], [], []
    def status(*args, **kwargs):
        statuses.append((args, kwargs))
        write_status(*args, **kwargs)
    monkeypatch.setattr(app, '_upsert_job_status', status)
    monkeypatch.setattr(app, '_send_index_wakeup', lambda *args: sent.append(args))
    monkeypatch.setattr(app, '_notify_index_ready', lambda *args: events.append(args))
    monkeypatch.setattr(app, '_cleanup_completed_index_artifacts', lambda _: None)
    return SimpleNamespace(runtime=runtime, now=now, store=real_store(runtime.jobs, clock=lambda: now[0]),
        statuses=statuses, sent=sent, events=events)


@pytest.mark.parametrize('kind', ['vector', 'lexical_artifact', 'people_embedding'])
def test_actual_barrier_wait_duplicate_storm_then_recovery_completes(clocked, monkeypatch, kind):
    env = clocked
    env.runtime.store.begin('A', kind, ['one'])
    env.runtime.store.finish('A', kind)
    active = [True]
    monkeypatch.setattr(automatic, 'wave_pending', lambda _: active[0])
    scope = automatic.SCOPES.get(kind, kind)
    key = env.store.request('A', scope)
    revision = env.store._get(key)['revision']
    payload = dict(type=app.INDEX_BUILD_JOB_TYPE, userId='A', scope=scope, jobId='job',
        maintenanceKey=key, intentRevision=revision)
    app._run_index_maintenance_payload(payload)
    row = env.store._get(key)
    assert row['attempts'] == 0 and not row['failed'] and not row['owner']
    assert row['notBefore'] == 0 and row['retryReason'] == 'wave_wait'
    assert len(env.sent) == 0
    status = env.runtime.jobs.get_entity('A', 'job')
    assert status['status'] == 'queued'
    assert json.loads(status['result']) == dict(status='deferred', reason='barrier_wait', ready=False)
    assert not env.runtime.calls and env.runtime.store.capture('A', kind).ids == ['one']

    # A hundred stale deliveries, even beyond queue's poison ceiling, cannot
    # run, increment attempts, extend the delay, or multiply scheduled wakes.
    deleted, deadletters = [], []
    for i in range(100):
        message = SimpleNamespace(content=json.dumps(payload), dequeue_count=100 + i)
        app._process_clustering_queue_message_impl(message,
            SimpleNamespace(delete_message=lambda msg: deleted.append(msg)), 'index', 3,
            SimpleNamespace(send_message=lambda body: deadletters.append(body)))
    assert len(deleted) == 100 and not deadletters
    assert env.store._get(key) == row
    assert len(env.sent) == 0 and not env.runtime.calls and not env.events
    assert all(args[3] != 'failed' for args, _ in env.statuses)

    # New source/job revision cannot silently cancel the barrier embargo.
    env.runtime.store.begin('A', kind, ['two'])
    env.runtime.store.finish('A', kind)
    env.store.request('A', scope)
    assert env.store.claim(key) is None
    active[0] = False
    import wave_runtime
    assert wave_runtime.resume_completed_wave_maintenance('A') == 1
    assert len(env.sent) == 1 and env.sent[-1][-1] == 0
    app._run_index_maintenance_payload(payload)
    assert env.store._get(key) is None
    assert len(env.runtime.calls) == 1 and env.runtime.calls[0][0] == kind
    assert not env.runtime.store.capture('A', kind).ids
    assert env.runtime.jobs.get_entity('A', 'job')['status'] == 'done'
    assert len(env.events) == 1


def test_failed_barrier_wakeup_send_is_nonterminal_and_recovers(clocked, monkeypatch):
    env = clocked
    monkeypatch.setattr(automatic, 'wave_pending', lambda _: False)
    monkeypatch.setattr(automatic, 'run', Mock(side_effect=automatic.SourceBusy('source busy')))
    monkeypatch.setattr(app, '_send_index_wakeup', Mock(side_effect=OSError('queue down')))
    key = env.store.request('A', 'vector')
    payload = dict(userId='A', scope='vector', jobId='job', maintenanceKey=key)
    app._run_index_maintenance_payload(payload)
    row = env.store._get(key)
    assert not row['failed'] and row['attempts'] == 0
    assert env.runtime.jobs.get_entity('A', 'job')['status'] == 'queued'
    assert env.store.recover(lambda *args: pytest.fail('reservation ignored')) == 0
    env.now[0] = row['wakeUntil']
    assert env.store.recover(lambda *args: env.sent.append(args)) == 1
    assert len(env.sent) == 1


def test_early_unreserved_delivery_schedules_only_one_replacement(clocked):
    env = clocked
    key = env.store.request('A', 'vector', delay=10)
    payload = dict(userId='A', scope='vector', jobId='job', maintenanceKey=key)
    for _ in range(100):
        app._run_index_maintenance_payload(payload)
    assert len(env.sent) == 1 and env.sent[0][-1] == 10
    assert 'attempts' not in env.store._get(key)
    assert not env.statuses and not env.events


def test_live_owner_duplicates_are_acked_without_failure_or_more_wakeups(clocked):
    env = clocked
    key = env.store.request('A', 'vector')
    claim = env.store.claim(key)
    before = env.store._get(key)
    payload = dict(userId='A', scope='vector', jobId='job', maintenanceKey=key)
    for _ in range(100):
        app._run_index_maintenance_payload(payload)
    assert env.store._get(key) == before
    assert not env.sent and not env.statuses and not env.events
    env.store.finish(claim)


def test_barrier_at_failure_ceiling_waits_without_poison_then_real_budget_applies(clocked, monkeypatch):
    env = clocked
    key = env.store.request('A', 'vector')
    first = env.store.claim(key)
    env.store.release(first)
    monkeypatch.setattr(app, 'INDEX_BUILD_MAX_RETRIES', 1)
    active = [True]
    monkeypatch.setattr(automatic, 'wave_pending', lambda _: active[0])
    deadletters = []
    monkeypatch.setattr(app, 'queue_service_client', SimpleNamespace(get_queue_client=lambda _:
        SimpleNamespace(send_message=lambda body: deadletters.append(json.loads(body)))))
    monkeypatch.setattr(app, 'INDEX_MAINTENANCE_DEADLETTER_QUEUE_NAME', 'dead')
    payload = dict(userId='A', scope='vector', jobId='job', maintenanceKey=key)
    for _ in range(10):
        app._run_index_maintenance_payload(payload)
        assert env.store._get(key)['attempts'] == 1
        assert env.runtime.jobs.get_entity('A', 'job')['status'] == 'queued'
        env.now[0] += 10
    assert not deadletters and not env.events
    active[0] = False
    env.now[0] = env.store._get(key)['notBefore']
    app._run_index_maintenance_payload(payload)
    assert env.store._get(key)['failed'] and deadletters[0]['attempts'] == 1


def test_real_failures_still_exhaust_ledger_budget_not_queue_deliveries(clocked, monkeypatch):
    env = clocked
    key = env.store.request('A', 'vector')
    payload = dict(type=app.INDEX_BUILD_JOB_TYPE, userId='A', scope='vector', jobId='job',
        maintenanceKey=key, intentRevision=env.store._get(key)['revision'])
    monkeypatch.setattr(app, 'INDEX_BUILD_MAX_RETRIES', 2)
    monkeypatch.setattr(automatic, 'run', Mock(side_effect=OSError('actual build failure')))
    deadletters = []
    monkeypatch.setattr(app, 'queue_service_client', SimpleNamespace(get_queue_client=lambda _:
        SimpleNamespace(send_message=lambda body: deadletters.append(json.loads(body)))))
    monkeypatch.setattr(app, 'INDEX_MAINTENANCE_DEADLETTER_QUEUE_NAME', 'dead')
    deleted = []
    queue = SimpleNamespace(delete_message=lambda msg: deleted.append(msg))
    for delivery in range(3):
        # Already far past the queue delivery ceiling on the FIRST build.
        msg = SimpleNamespace(content=json.dumps(payload), dequeue_count=100 + delivery)
        app._process_clustering_queue_message_impl(msg, queue, 'index', 2)
        assert len(deleted) == (1 if delivery == 2 else 0)
    assert env.store._get(key)['failed']
    assert deadletters[0]['attempts'] == 2
    assert env.runtime.jobs.get_entity('A', 'job')['status'] == 'failed'
    assert not env.events


def test_deadletter_failure_cannot_discard_or_poison_the_durable_intent(clocked, monkeypatch):
    env = clocked
    key = env.store.request('A', 'vector')
    first = env.store.claim(key)
    env.store.release(first)
    monkeypatch.setattr(app, 'INDEX_BUILD_MAX_RETRIES', 1)
    send = Mock(side_effect=OSError('deadletter down'))
    monkeypatch.setattr(app, 'queue_service_client', SimpleNamespace(get_queue_client=lambda _:
        SimpleNamespace(send_message=send)))
    monkeypatch.setattr(app, 'INDEX_MAINTENANCE_DEADLETTER_QUEUE_NAME', 'dead')
    payload = dict(userId='A', scope='vector', jobId='job', maintenanceKey=key)
    with pytest.raises(OSError, match='deadletter down'):
        app._run_index_maintenance_payload(payload)
    assert not env.store._get(key)['failed'] and not env.store._get(key)['owner']
    send.side_effect = None
    app._run_index_maintenance_payload(payload)
    assert env.store._get(key)['failed']


@pytest.mark.parametrize('partition', [index_work.PARTITION, automatic.TAG_PARTITION])
def test_claim_embargo_is_cas_rechecked_and_preserves_attempts(partition, monkeypatch):
    table, now = EtagTable(), [1000.0]
    store = index_work.WorkStore(table, clock=lambda: now[0], partition=partition)
    key = store.request('A', 'tag_embedding' if partition == automatic.TAG_PARTITION else 'vector')
    get = store._get
    stale = get(key)
    first = store.claim(key)
    store.release(first, deferred=True, retry_delay=10, retry_reason='barrier_wait')
    latest = get(key)
    # First read is stale; failed ETag CAS must reread the embargo, not claim.
    reads = iter([stale, latest])
    monkeypatch.setattr(store, '_get', lambda _: next(reads))
    assert store.claim(key) is None
    assert table.get_entity(partition, key) == latest


def test_tag_dispatch_coalesces_early_duplicates_into_one_delayed_wake(clocked, monkeypatch):
    env = clocked
    monkeypatch.setenv('APP_ROLE', 'vision')
    monkeypatch.setattr(app.vision_utils, 'image_encoder_available', lambda: True)
    tags = index_work.WorkStore(env.runtime.jobs, partition=automatic.TAG_PARTITION)
    key = tags.request('A', 'tag_embedding')
    first = tags.claim(key)
    tags.release(first, deferred=True, retry_delay=10, retry_reason='barrier_wait')
    before = tags._get(key)
    payload = app._tag_index_payload('A', before['revision'])
    send = Mock(return_value=None)
    monkeypatch.setattr(app, '_send_tag_index_wakeup', send)
    for _ in range(100):
        assert app._run_tag_index_maintenance_payload(payload) == 'tag_index_noop'
    after = tags._get(key)
    for field in ('attempts', 'revision', 'notBefore', 'owner', 'failed'):
        assert after[field] == before[field]
    assert not env.statuses and not env.runtime.calls and not env.events
    send.assert_called_once_with('A', 'tag_embedding', before['revision'], 10)


def test_wave_completion_resumes_maintenance_before_recovery_deadline(clocked, monkeypatch):
    env = clocked
    env.runtime.store.begin('A', 'vector', ['one'])
    env.runtime.store.finish('A', 'vector')
    pending = [True]
    monkeypatch.setattr(automatic, 'wave_pending', lambda _: pending[0])
    key = env.store.request('A', 'vector')
    payload = dict(userId='A', scope='vector', jobId='job', maintenanceKey=key)
    assert app._run_index_maintenance_payload(payload) == 'deferred'
    assert env.store._get(key)['notBefore'] == 0
    assert env.store._get(key)['retryReason'] == 'wave_wait'
    # A completed predecessor must not release the still-pending tail.
    assert app.resume_index_maintenance_after_wave('A') == 0
    assert len(env.sent) == 0
    pending[0] = False
    assert app.resume_index_maintenance_after_wave('A') == 1
    assert env.sent[-1][-1] == 0
    assert app.resume_index_maintenance_after_wave('A') == 0
    assert app._run_index_maintenance_payload(payload) == 'intent_completed'
    assert env.store._get(key) is None
    assert len(env.runtime.calls) == 1
    # The old delayed receipt is an obsolete advisory wake, never a rebuild.
    assert app._run_index_maintenance_payload(payload) == 'obsolete'
    assert len(env.runtime.calls) == 1


def test_completion_does_not_release_unrelated_barriers_or_live_work():
    # Use real ledger CAS rules without importing an application runtime.
    now = [1000.0]
    store = index_work.WorkStore(EtagTable(), clock=lambda: now[0])
    keys = {}
    for library, scope, reason in [('A', 'vector', 'wave_wait'),
                                   ('A', 'people_embedding', 'barrier_wait'),
                                   ('B', 'vector', 'wave_wait'),
                                   ('A', 'search', 'wave_wait')]:
        key = store.request(library, scope)
        claim = store.claim(key)
        store.release(claim, deferred=True, retry_delay=300, retry_reason=reason)
        keys[library, scope] = key
    # A recovered owner may be working; the completion signal cannot steal it.
    current = store._get(keys['A', 'search'])
    store._replace(current, dict(current, leaseUntil=2000, owner='live'))
    sent = []
    assert store.resume_wave_waits('A', lambda *args: sent.append(args),
        scopes={'vector', 'people_embedding', 'search'}) == 1
    assert sent[0][1] == 'vector' and sent[0][-1] == 0
    assert store._get(keys['A', 'people_embedding'])['notBefore'] == 1300
    assert store._get(keys['B', 'vector'])['notBefore'] == 1300
    assert store._get(keys['A', 'search'])['owner'] == 'live'


def test_completion_send_failure_keeps_intent_recoverable(clocked, monkeypatch):
    env = clocked
    monkeypatch.setattr(automatic, 'wave_pending', lambda _: False)
    key = env.store.request('A', 'vector')
    claim = env.store.claim(key)
    env.store.release(claim, deferred=True, retry_delay=300, retry_reason='wave_wait')
    monkeypatch.setattr(app, '_send_index_wakeup', Mock(side_effect=OSError('queue down')))
    import wave_runtime
    assert wave_runtime.resume_completed_wave_maintenance('A') == 0
    row = env.store._get(key)
    assert not row['failed'] and row['notBefore'] == 0
    env.now[0] = row['wakeUntil']
    assert env.store.recover(lambda *args: env.sent.append(args)) == 1
    assert env.sent[-1][-1] == 0


def test_index_worker_reports_processing_and_deferred_outcome(monkeypatch, caplog):
    import processing_diagnostics as diagnostics
    now = [0.0]
    reporter = diagnostics.Worker('indexer', clock=lambda: now[0])
    reporter.state('idle')
    monkeypatch.setenv('APP_ROLE', 'index-worker')
    monkeypatch.setitem(diagnostics._WORKERS, 'indexer', reporter)
    message = SimpleNamespace(content=json.dumps(dict(type='index_build', userId='A', scope='vector')),
                              dequeue_count=1)
    def dispatch(*args):
        assert reporter.current == 'processing'
        now[0] += 2
        return 'deferred'
    queue = SimpleNamespace(delete_message=lambda _: None)
    assert app._process_clustering_queue_message(message, queue, 'index', 5, dispatch=dispatch)
    reporter.report()
    assert reporter.seconds['processing'] == 2
    assert reporter.counts['acknowledged_messages'] == 1
    events = [json.loads(r.message.split('processing telemetry ', 1)[1])
              for r in caplog.records if 'processing telemetry ' in r.message]
    assert any(e['event'] == 'index_message_outcome' and e['outcome'] == 'deferred' for e in events)
    assert all(e.get('role') == 'indexer' for e in events)


def test_wave_wait_eliminates_unnecessary_wakeups_and_resumes_on_completion(clocked, monkeypatch):
    env = clocked
    active = [True]
    monkeypatch.setattr(automatic, 'wave_pending', lambda _: active[0])
    key = env.store.request('A', 'vector')
    payload = dict(type=app.INDEX_BUILD_JOB_TYPE, userId='A', scope='vector', jobId='job',
                   maintenanceKey=key)

    # 1st attempt: defers with wave_wait, sends ZERO queue wakeups
    app._run_index_maintenance_payload(payload)
    row = env.store._get(key)
    assert row['retryReason'] == 'wave_wait'
    assert row['notBefore'] == 0
    assert len(env.sent) == 0

    # Repeated calls while wave is active: all defer with 0 queue sends
    for _ in range(5):
        env.now[0] += 3600
        app._run_index_maintenance_payload(payload)
        row = env.store._get(key)
        assert row['retryReason'] == 'wave_wait'
        assert row['notBefore'] == 0
        assert len(env.sent) == 0

    # Wave is still active: periodic recover() skips wave_wait and sends 0 wakeups
    assert env.store.recover(lambda *args: env.sent.append(args)) == 0
    assert len(env.sent) == 0

    # Now wave completes: resume_completed_wave_maintenance immediately wakes with delay=0
    active[0] = False
    import wave_runtime
    resumed = wave_runtime.resume_completed_wave_maintenance('A')
    assert resumed == 1
    row = env.store._get(key)
    assert row['notBefore'] == 0
    assert row['retryReason'] == ''
    assert len(env.sent) == 1
    assert env.sent[-1][-1] == 0


def test_wave_runtime_resumes_maintenance_on_cancelled_wave(clocked, monkeypatch):
    env = clocked
    active = [True]
    monkeypatch.setattr(automatic, 'wave_pending', lambda _: active[0])
    key = env.store.request('A', 'vector')
    payload = dict(type=app.INDEX_BUILD_JOB_TYPE, userId='A', scope='vector', jobId='job',
                   maintenanceKey=key)
    app._run_index_maintenance_payload(payload)
    assert env.store._get(key)['retryReason'] == 'wave_wait'

    # When wave is cancelled (and no more waves pending), maintenance wakes immediately
    active[0] = False
    import wave_runtime
    resumed = wave_runtime.resume_completed_wave_maintenance('A')
    assert resumed == 1
    assert env.store._get(key)['notBefore'] == 0
    assert env.sent[-1][-1] == 0
