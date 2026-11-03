"""Crash and liveness regressions: zero replica floors, no timer-based polling."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import wave_runtime as runtime
from execution_lease import ExecutionLeases, OwnershipLost
from upload_wave_execution import run_index_release, stage_work_store
from wave_publication import ObligationLedger
from test_wave_runtime import vision_dispatch
from test_wave_liveness import live, admit, complete_source, wake


def test_live_stage_owner_does_not_consume_retry_budget_or_create_wake(vision_dispatch):
    store, work, wave, token, payload, sent = vision_dispatch
    leases = ExecutionLeases(store.table, partition=store.partition)
    ownership = leases.claim(store.library, wave['waveId'], 'vision', 'input')
    before = dict(leases.row(ownership))
    for _ in range(8):
        assert runtime.consume(payload) == 'retry'
    assert work._get(token).get('attempts', 0) == 0
    assert dict(leases.row(ownership)) == before
    assert not sent


def test_racing_live_stage_claim_is_refunded(monkeypatch, vision_dispatch):
    store, work, wave, token, payload, sent = vision_dispatch
    leases = ExecutionLeases(store.table, partition=store.partition)
    def race(*args, **kwargs):
        leases.claim(store.library, wave['waveId'], 'vision', 'input')
        raise OwnershipLost('Stage execution is already claimed by a live owner')
    monkeypatch.setattr(runtime, 'run_vision_release', race)
    assert runtime.consume(payload) == 'retry'
    assert work._get(token)['attempts'] == 0
    assert not sent


def test_no_progress_stops_after_five_attempts_without_new_queue_wakes(monkeypatch, vision_dispatch):
    _, work, wave, token, payload, sent = vision_dispatch
    monkeypatch.setattr(runtime, 'run_vision_release', lambda *a, **kw: wave)
    for attempt in range(1, 6):
        assert runtime.consume(payload) == 'retry'
        assert work._get(token)['attempts'] == attempt
    with pytest.raises(RuntimeError, match='attempts exhausted'):
        runtime.consume(payload)
    assert work._get(token)['failed']
    assert runtime.consume(payload) == 'noop'
    assert not sent


def test_cluster_progress_continues_immediately_without_barrier_polling(monkeypatch, vision_dispatch):
    from upload_wave_execution import dispatch_outbox
    store, work, wave, _, _, sent = vision_dispatch
    wid = wave['waveId'];key = 'shadow-cluster!' + wid
    current = store._required(store.wave_key(wid))
    ready = dict(current, state='ready', frozenRevision='frozen', manifestDigest='manifest')
    store.table.submit_transaction([store._change(current, ready),
        ('create', store._row(key, waveId=wid, destination='cluster',
            inputRevision='frozen', manifestDigest='manifest', dispatchable=False))])
    assert dispatch_outbox(work, store, store.library, key, 'cluster')
    from upload_waves import digest
    scope = f'wave-cluster:{wid}:{digest(key)}';token = work.key(store.library, scope)
    payload = dict(userId=store.library, waveId=wid, stage='cluster', scope=scope,
                   intentRevision=work._get(token)['revision'])
    monkeypatch.setenv('APP_ROLE', 'cluster')
    sys.modules['app']._get_live_faiss_assigner = lambda: object()
    faces = [0]
    def slice(leases, *args, **kwargs):
        owner = leases.claim(store.library, wid, 'cluster', 'frozen')
        faces[0] += 128
        leases.checkpoint(owner, 'faces-applied:' + str(faces[0]))
        leases.release(owner)
        return store.status(wid)
    monkeypatch.setattr(runtime, 'run_cluster_release', slice)
    for _ in range(8):
        assert runtime.consume(payload) == 'done'
        assert work._get(token)['attempts'] == 0
        assert work._get(token)['retryReason'] == 'progress'
        assert sent[-1] == (store.library, scope, payload['intentRevision'], 0)
    assert faces[0] == 1024


def test_committed_transition_retains_failed_handoff_until_sent(live, monkeypatch):
    store, metadata, sent, _ = live
    wid = admit(store, metadata, 1)
    complete_source(store, metadata, 's-0.jpg')
    payload = wake(store, wid, 'vision')
    def unavailable(*args):raise OSError('queue unavailable')
    monkeypatch.setattr(runtime, 'send_stage', unavailable)
    with pytest.raises(OSError):runtime.consume(payload)
    assert store.status(wid)['state'] == 'indexing'
    monkeypatch.setattr(runtime, 'run_vision_release', lambda *a, **kw: pytest.fail('Repeated completed Vision work'))
    monkeypatch.setattr(runtime, 'send_stage', lambda *args: sent.append(args))
    assert runtime.consume(payload) == 'noop'
    assert len(sent) == 1 and sent[0][1].startswith('wave-index:')


def test_predecessor_dependency_wait_does_not_schedule_itself(monkeypatch, vision_dispatch):
    from upload_waves import digest
    store, work, wave, _, _, sent = vision_dispatch
    wid=wave['waveId'];key='shadow-cluster!' + wid
    current=store._required(store.wave_key(wid))
    store.table.submit_transaction([store._change(current, dict(current, state='ready',
        frozenRevision='frozen', predecessorWaveId='unfinished-predecessor')),
        ('create',store._row(key,waveId=wid,destination='cluster',inputRevision='frozen'))])
    monkeypatch.setenv('APP_ROLE','cluster')
    payload=dict(userId=store.library,waveId=wid,stage='cluster',
        scope=f'wave-cluster:{wid}:{digest(key)}',intentRevision='unused')
    for _ in range(8):assert runtime.consume(payload)=='noop'
    assert not sent
    assert work._get(work.key(store.library,payload['scope'])) is None


@pytest.fixture
def index_wave(live, monkeypatch):
    store, metadata, sent, _ = live
    wid = admit(store, metadata, 45)
    for i in range(45):complete_source(store, metadata, f's-{i}.jpg')
    runtime.consume(wake(store, wid, 'vision'))
    # The zero-face control path evaluates one bounded page at a time.
    monkeypatch.setenv('APP_ROLE', 'cluster')
    for _ in range(10):
        if store.status(wid)['state'] == 'indexing':break
        runtime.consume(wake(store, wid, 'cluster'))
    wave = store.status(wid)
    assert wave['state'] == 'indexing'
    ledger = ObligationLedger(store.table, wid, library=store.library,
        frozen_revision=wave['frozenRevision'], assignment_revision=wave['assignmentReceiptId'])
    return store, wave, ledger


def execute_index(context, **kwargs):
    store, wave, ledger = context
    leases = ExecutionLeases(store.table, partition=store.partition)
    result = run_index_release(leases, store, store.library, wave['waveId'],
        wave['indexPendingKey'], ledger, **kwargs)
    row = leases._get(leases.key(store.library, wave['waveId'], 'index'))
    assert not row['owner'] and row['leaseUntil'] == 0
    return result, row


def test_index_resumes_preparation_publications_and_final_markers(index_wave, monkeypatch):
    store, wave, ledger = index_wave
    prepared = []
    published = []
    pages = []
    def prepare(check, after):
        check()
        page = store._page(f"e!{wave['waveId']}!m!", after)
        prepared.extend(m['RowKey'] for m in page)
        return (page[-1]['RowKey'] if page else after), len(page) < 20
    def publish(scope, obligation, check):
        check();published.append(scope)
        return 'generation-' + scope
    # Force a yield after each durable page or published scope.
    for _ in range(20):
        result, lease = execute_index(index_wave, prepare_page=prepare, publisher=publish, should_yield=lambda: True)
        pages.append(lease['progressCheckpoint'])
        if result['state'] == 'complete':break
    assert result['state'] == 'complete'
    assert len(prepared) == len(set(prepared)) == 45
    assert len(published) == len(set(published)) == 8
    assert any('"phase": "prepare"' in p for p in pages)
    assert any('"phase": "members"' in p for p in pages)
    assert len(store._page('published-source!', size=100)) == 45


def test_index_recovers_interruption_after_all_outputs_before_completion(index_wave, monkeypatch):
    import wave_no_work
    store, wave, ledger = index_wave
    built = []
    original = wave_no_work.record_publication_members
    def fail(*a, **kw):raise OSError('interrupted after publishing')
    monkeypatch.setattr(wave_no_work, 'record_publication_members', fail)
    with pytest.raises(OSError):
        execute_index(index_wave, publisher=lambda scope, *a: built.append(scope) or scope)
    lease = ExecutionLeases(store.table, partition=store.partition)._get(
        ExecutionLeases.key(store.library, wave['waveId'], 'index'))
    assert not lease['owner'] and lease['leaseUntil'] == 0
    assert ledger.is_complete() and len(built) == 8
    monkeypatch.setattr(wave_no_work, 'record_publication_members', original)
    def forbidden(*a, **kw):pytest.fail('Rebuilt already published output')
    result, _ = execute_index(index_wave, publisher=forbidden, prepare_page=forbidden)
    assert result['state'] == 'complete'
    assert len(store._page('published-source!', size=100)) == 45


def test_index_replays_only_interrupted_marker_page(index_wave, monkeypatch):
    store, wave, ledger = index_wave
    commit = store._commit
    writes = [0]
    def interrupt(control, changes, **kwargs):
        if any(op[1]['RowKey'].startswith('published-source!') for op in changes):
            if writes[0] == 25:raise OSError('worker interrupted mid-page')
            writes[0] += 1
        return commit(control, changes, **kwargs)
    monkeypatch.setattr(store, '_commit', interrupt)
    with pytest.raises(OSError):
        execute_index(index_wave, publisher=lambda scope, *a: scope)
    assert len(store._page('published-source!', size=100)) == 25
    leases = ExecutionLeases(store.table, partition=store.partition)
    checkpoint = json.loads(leases._get(leases.key(store.library,wave['waveId'],'index'))['progressCheckpoint'][6:])
    assert checkpoint['phase'] == 'members' and checkpoint['after']
    monkeypatch.setattr(store, '_commit', commit)
    page = store._page; reads = []
    def resume(prefix, after='', **kwargs):
        if prefix.startswith('e!'):reads.append(after)
        return page(prefix, after, **kwargs)
    monkeypatch.setattr(store, '_page', resume)
    result, _ = execute_index(index_wave, publisher=lambda *a: pytest.fail('Rebuilt output'))
    assert result['state'] == 'complete'
    assert reads[0] == checkpoint['after']
    assert len(store._page('published-source!', size=100)) == 45


def test_missing_recorded_index_generation_blocks_completion(index_wave, monkeypatch):
    import wave_no_work
    original = wave_no_work.record_publication_members
    monkeypatch.setattr(wave_no_work, 'record_publication_members', lambda *a, **kw: False)
    execute_index(index_wave, publisher=lambda scope, *a: scope)
    monkeypatch.setattr(wave_no_work, 'record_publication_members', original)
    def missing(*a):raise ValueError('Recorded publication proof is missing')
    with pytest.raises(ValueError, match='proof is missing'):
        execute_index(index_wave, verify=missing, publisher=lambda *a: pytest.fail('Rebuilt output'))
    store, wave, _ = index_wave
    assert store.status(wave['waveId'])['state'] == 'indexing'


@pytest.mark.parametrize('stage', ['cluster', 'index'])
@pytest.mark.parametrize('fail_send', [False, True])
def test_deferred_wave_moves_to_main_queue_before_ack(monkeypatch, stage, fail_send):
    import app
    events = []
    monkeypatch.setenv('APP_ROLE', runtime.ROLES[stage])
    monkeypatch.setenv('UPLOAD_WAVE_EXECUTION_ENABLED', 'true')
    monkeypatch.setattr(runtime, 'durable_stage_wake', lambda *a: True)
    def send(body):
        events.append('send')
        if fail_send:raise OSError('queue unavailable')
    target = SimpleNamespace(send_message=send)
    monkeypatch.setattr(app, 'clustering_queue_client', target)
    monkeypatch.setattr(app, 'index_maintenance_queue_client', target)
    source = SimpleNamespace(delete_message=lambda message: events.append('ack'))
    name = app.CLUSTERING_DEFERRED_QUEUE_NAME if stage == 'cluster' else app.INDEX_MAINTENANCE_DEFERRED_QUEUE_NAME
    message = SimpleNamespace(content=json.dumps(dict(type='upload_wave_stage', stage=stage)), dequeue_count=1)
    assert app._process_clustering_queue_message_impl(message, source, name, 5,
        dispatch=lambda *a: pytest.fail('Processed work on visible-only queue'))
    assert events == (['send'] if fail_send else ['send', 'ack'])


def test_exhausted_transfer_preserves_replayable_dead_letter(monkeypatch):
    import app
    events=[];letters=[]
    monkeypatch.setattr(runtime,'durable_stage_wake',lambda *a:True)
    def unavailable(body):
        events.append('send');raise OSError('target unavailable')
    monkeypatch.setattr(app,'clustering_queue_client',SimpleNamespace(send_message=unavailable))
    source=SimpleNamespace(delete_message=lambda m:events.append('ack'))
    dead=SimpleNamespace(send_message=lambda body:letters.append(json.loads(body)))
    payload=dict(type='upload_wave_stage',stage='cluster')
    message=SimpleNamespace(content=json.dumps(payload),dequeue_count=5)
    app._process_clustering_queue_message_impl(message,source,app.CLUSTERING_DEFERRED_QUEUE_NAME,5,dead)
    assert events==['send','ack'] and letters[0]['originalPayload']==payload
    assert letters[0]['reason']=='Main queue transfer attempts exhausted'


def test_deferred_index_maintenance_also_transfers_before_processing(monkeypatch):
    import app
    from index_work import WorkStore
    events=[]
    monkeypatch.setenv('APP_ROLE','index-worker')
    monkeypatch.setattr(app,'jobs_table_client',object())
    monkeypatch.setattr(app,'index_maintenance_queue_client',
        SimpleNamespace(send_message=lambda body:events.append('send')))
    payload=dict(type=app.INDEX_BUILD_JOB_TYPE,userId='library',scope='people',
                 maintenanceKey=WorkStore.key('library','people'))
    message=SimpleNamespace(content=json.dumps(payload),dequeue_count=1)
    source=SimpleNamespace(delete_message=lambda m:events.append('ack'))
    app._process_clustering_queue_message_impl(message,source,app.INDEX_MAINTENANCE_DEFERRED_QUEUE_NAME,5,
        dispatch=lambda *a:pytest.fail('Ran maintenance on deferred queue'))
    assert events==['send','ack']


def test_deployment_templates_forbid_cron_and_positive_replica_floors():
    template = json.loads((Path(__file__).resolve().parents[2] / 'deploy/azuredeploy.json').read_text())
    apps = []
    def walk(value):
        if isinstance(value, dict):
            assert value.get('type') != 'cron'
            assert value.get('triggerType') != 'Schedule'
            if value.get('type', '').lower() == 'microsoft.app/containerapps':apps.append(value)
            for item in value.values():walk(item)
        elif isinstance(value, list):
            for item in value:walk(item)
    walk(template)
    assert len(apps) == 8
    for app in apps:
        minimum = app['properties']['template']['scale']['minReplicas']
        assert minimum in (0, "[parameters('clusterMinReplicas')]")
    assert template['parameters']['clusterMinReplicas']['maxValue'] == 0
