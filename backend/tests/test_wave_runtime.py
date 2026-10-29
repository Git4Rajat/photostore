import json
import sys
from types import SimpleNamespace

import pytest
import wave_runtime as runtime


def test_disabled_consumer_and_sender_do_not_touch_apps(monkeypatch):
    monkeypatch.delenv('UPLOAD_WAVE_EXECUTION_ENABLED',raising=False)
    assert runtime.consume({})=='noop'
    assert runtime.send_stage('u','invalid','r') is None


@pytest.mark.parametrize('fail_apply',[False,True])
def test_wave_source_retains_preview_and_original_through_apply_and_cleans_up(monkeypatch,fail_apply):
    from contextlib import nullcontext
    import storage_utils as storage
    import processing_completion,processing_generation
    from ipwork_image import (crop_preview,remember_crop_preview,original_input,
                              remember_original_input)
    row=dict(PartitionKey='lib',RowKey='photo.jpg',anonymousImageId='photo',
        processingRevision='rev',fileHash='hash',size=4,processingFence='fence')
    lease=dict(entity=row,ownerId='owner')
    data=b'\xff\xd8validated-preview';seen=[];released=[]
    monkeypatch.setattr(processing_completion,'completion_capture_enabled',lambda:True)
    monkeypatch.setattr(processing_generation,'enforcement_enabled',lambda:True)
    monkeypatch.setattr(processing_generation,'required_steps_for_media',lambda _:['ocr'])
    monkeypatch.setattr(storage,'claim_processing_lease',lambda *a,**k:lease)
    monkeypatch.setattr(storage,'release_processing_lease',lambda *a,**k:released.append(1))
    monkeypatch.setattr(runtime,'_source_heartbeat',lambda *a:nullcontext(lambda:None))
    def steps(*a,**k):
        remember_crop_preview('lib','photo.jpg',row,data,40,40)
        remember_original_input('lib','photo.jpg',row,b'orig')
        return {}
    def apply(*a,**k):
        assert crop_preview('lib','photo.jpg',row).data==data
        assert original_input('lib','photo.jpg',row)==b'orig'
        seen.append(1)
        if fail_apply:raise RuntimeError('apply failed')
    monkeypatch.setitem(sys.modules,'app',SimpleNamespace(_register_ipwork_processors=lambda:None,_run_ipwork_steps=steps))
    monkeypatch.setattr(storage,'apply_client_processing_results_for_file',apply)
    if fail_apply:
        with pytest.raises(RuntimeError,match='apply failed'):runtime.process_source('lib','photo.jpg','rev',lambda:None)
    else:runtime.process_source('lib','photo.jpg','rev',lambda:None)
    assert seen==released==[1]
    assert crop_preview('lib','photo.jpg',row) is None
    assert original_input('lib','photo.jpg',row) is None


def test_stage_wakes_use_only_existing_role_queue(monkeypatch):
    monkeypatch.setenv('UPLOAD_WAVE_EXECUTION_ENABLED','true')
    sent={stage:[] for stage in runtime.ROLES}
    app=SimpleNamespace()
    for stage,attr in [('vision','ipwork_queue_client'),('cluster','clustering_queue_client'),('index','index_maintenance_queue_client')]:
        def send(body,_stage=stage,**options):sent[_stage].append((json.loads(body),options))
        setattr(app,attr,SimpleNamespace(send_message=send))
    monkeypatch.setitem(sys.modules,'app',app)
    for stage in runtime.ROLES:
        runtime.send_stage('u',f'wave-{stage}:w:key','r')
        assert len(sent[stage])==1
        assert sent[stage][0][0]['stage']==stage
        assert sent[stage][0][0]['intentRevision']=='r'
    assert sum(map(len,sent.values()))==3


def test_worker_cannot_consume_another_roles_stage(monkeypatch):
    monkeypatch.setenv('UPLOAD_WAVE_EXECUTION_ENABLED','true')
    monkeypatch.setenv('APP_ROLE','core')
    monkeypatch.setitem(sys.modules,'app',SimpleNamespace())
    with pytest.raises(ValueError,match='Wrong app role'):
        runtime.consume(dict(userId='u',stage='index',waveId='w'))


@pytest.fixture
def vision_dispatch(monkeypatch):
    from contextlib import nullcontext
    import index_publication
    import wave_policy
    from tests.test_index_work import EtagTable
    from upload_waves import ShadowWaves, digest
    from upload_wave_execution import stage_work_store, dispatch_outbox
    from tests.test_upload_waves import AtomicTable, close
    from tests.test_wave_execution_consumers import register_accepted
    monkeypatch.setenv('UPLOAD_WAVE_EXECUTION_ENABLED', 'true')
    monkeypatch.setenv('APP_ROLE', 'vision')
    table = AtomicTable()
    store = ShadowWaves(table, 'library-A')
    wave_id, resolved = register_accepted(store)
    close(store, 'parent', resolved)
    wave = store.status(wave_id)
    key = wave['visionPendingKey']
    work = stage_work_store(EtagTable())
    monkeypatch.setattr(runtime, 'stage_work_store', lambda table: work)
    assert dispatch_outbox(work, store, store.library, key, 'vision')
    scope = f'wave-vision:{wave_id}:{digest(key)}'
    token = work.key(store.library, scope)
    revision = work._get(token)['revision']
    app = SimpleNamespace(jobs_table_client=table, metadata_table_client=object(),
                          _ipwork_metrics_context=SimpleNamespace(collector=None), IPWORKER_CONCURRENCY=2)
    monkeypatch.setitem(sys.modules, 'app', app)
    monkeypatch.setattr(wave_policy, 'owned', lambda *a: False)
    monkeypatch.setattr(index_publication, 'publication', lambda *a: nullcontext())
    monkeypatch.setattr(runtime, '_source_executor', lambda *a: None)
    sent = []
    monkeypatch.setattr(runtime, 'send_stage', lambda *a: sent.append(a))
    payload = dict(userId=store.library, stage='vision', waveId=wave_id, scope=scope, intentRevision=revision)
    return store, work, wave, token, payload, sent


@pytest.mark.parametrize('errors,progressed,expected_attempts', [(1, False, 1), (0, False, 0), (1, True, 0)])
def test_continuation_refunds_only_healthy_or_progressing_passes(monkeypatch, vision_dispatch, errors, progressed, expected_attempts):
    store, work, wave, token, payload, sent = vision_dispatch
    def run(*args, inspection_failures, **kwargs):
        inspection_failures[0] = errors
        return dict(wave, state='processing', lastProgressAt=float(wave.get('lastProgressAt') or 0) + int(progressed))
    monkeypatch.setattr(runtime, 'run_vision_release', run)
    assert runtime.consume(payload) == 'done'
    assert work._get(token)['attempts'] == expected_attempts

    assert sent[0][3] == (0 if progressed else 5)


def test_coordination_contention_queues_short_retry_and_exhausts_existing_budget(monkeypatch, vision_dispatch):
    from upload_waves import MutationContention
    store, work, wave, token, payload, sent = vision_dispatch
    def conflict(*args, **kwargs):
        raise MutationContention('Concurrent wave mutation')
    monkeypatch.setattr(runtime, 'run_vision_release', conflict)
    now = [1000.0]
    work.clock = lambda: now[0]
    for attempt, delay in enumerate([2, 4, 8, 16, 30], 1):
        assert runtime.consume(payload) == 'done'
        row = work._get(token)
        assert row['attempts'] == attempt
        assert row['leaseUntil'] == 0 and not row['owner']
        assert row['notBefore'] == now[0] + delay
        assert sent[-1][3] == delay
        now[0] += delay + 1
    with pytest.raises(RuntimeError, match='attempts exhausted'):
        runtime.consume(payload)
    assert work._get(token)['failed'] is True
    assert len(sent) == 5


def test_artifact_cleanup_wait_preserves_budget_and_durable_wakeup(monkeypatch, vision_dispatch):
    from upload_waves import ArtifactCleanupBusy
    _, work, wave, token, payload, sent = vision_dispatch
    def busy(*args, **kwargs):
        raise ArtifactCleanupBusy('temporary GC')
    monkeypatch.setattr(runtime, 'run_vision_release', busy)
    now = [1000.0]
    work.clock = lambda: now[0]
    for _ in range(8):
        assert runtime.consume(payload) == 'done'
        row = work._get(token)
        assert row['attempts'] == 0 and not row.get('failed')
        assert row['notBefore'] == now[0] + 30
        assert sent[-1][3] == 30
        now[0] += 31
    monkeypatch.setattr(runtime, 'run_vision_release', lambda *a, **k: dict(wave, state='processing'))
    assert runtime.consume(payload) == 'done'
    assert work._get(token)['attempts'] == 0


@pytest.mark.parametrize('failure', [ValueError('invalid evidence'), RuntimeError('lost lease')])
def test_other_failures_are_not_acknowledged_as_coordination_retries(monkeypatch, vision_dispatch, failure):
    _, work, _, token, payload, sent = vision_dispatch
    def fail(*args, **kwargs): raise failure
    monkeypatch.setattr(runtime, 'run_vision_release', fail)
    with pytest.raises(type(failure), match=str(failure)):
        runtime.consume(payload)
    assert not sent
    assert work._get(token)['attempts'] == 1


def test_real_vision_retry_replays_durable_completion_without_another_model_attempt(monkeypatch, vision_dispatch):
    import wave_source_batch
    from tests.test_wave_execution_consumers import FakeMetadata, settled_row, publish_completion
    from upload_waves import MutationContention
    store, work, wave, token, payload, sent = vision_dispatch
    app = sys.modules['app']
    metadata = FakeMetadata()
    metadata.put(store.library, 'original.jpg', **dict(settled_row('photo-1', 'revision-1'), face_status='pending'))
    app.metadata_table_client = metadata
    app.reconcile_face_filename_indexes_batch = lambda *a, **kw: None
    monkeypatch.setattr(runtime, 'invalidate_wave_tags', lambda *a: None)
    calls = []
    def process(*args):
        calls.append(args[1])
        metadata.rows[store.library, 'original.jpg']['face_status'] = 'no_data'
        publish_completion(store, metadata)
    monkeypatch.setattr(runtime, 'process_source', process)
    original = wave_source_batch.commit_observations
    def interrupted(*args):
        raise MutationContention('Concurrent wave mutation')
    monkeypatch.setattr(wave_source_batch, 'commit_observations', interrupted)
    assert runtime.consume(payload) == 'done'
    assert calls == ['original.jpg'] and sent[-1][3] == 2
    monkeypatch.setattr(wave_source_batch, 'commit_observations', original)
    # The normal short wake drives the real coordinator and receipt validation.
    assert runtime.consume(payload) == 'done'
    assert calls == ['original.jpg']
    assert store._page(f"e!{wave['waveId']}!m!")[0]['outcome'] == 'settled'
    assert [row['count'] for row in store._page('source-attempt!')] == [1]


def test_retry_send_failure_propagates_and_preserves_durable_dispatch(monkeypatch, vision_dispatch):
    from upload_waves import MutationContention
    _, work, _, token, payload, _ = vision_dispatch
    def conflict(*a, **kw): raise MutationContention('Concurrent wave mutation')
    def unavailable(*a): raise OSError('queue unavailable')
    monkeypatch.setattr(runtime, 'run_vision_release', conflict)
    monkeypatch.setattr(runtime, 'send_stage', unavailable)
    with pytest.raises(OSError, match='queue unavailable'):
        runtime.consume(payload)
    row = work._get(token)
    assert row['attempts'] == 1 and row['wakeUntil'] > 0
    assert not row['owner'] and not row['failed']


def test_actual_retry_delay_is_measured_at_stage_start(monkeypatch, vision_dispatch, caplog):
    from test_processing_diagnostics import records
    from upload_waves import MutationContention
    _, work, wave, _, payload, _ = vision_dispatch
    now = [1000.0]; work.clock = lambda: now[0]
    def conflict(*a, **kw): raise MutationContention('Concurrent wave mutation')
    monkeypatch.setattr(runtime, 'run_vision_release', conflict)
    assert runtime.consume(payload) == 'done'
    now[0] += 7
    monkeypatch.setattr(runtime, 'run_vision_release', lambda *a, **kw: dict(wave, state='processing'))
    assert runtime.consume(payload) == 'done'
    rows = records(caplog)
    scheduled = [r for r in rows if r['event'] == 'wave_retry_scheduled'][0]
    resumed = [r for r in rows if r['event'] == 'wave_stage_started'][-1]
    assert resumed['schedule_id'] == scheduled['schedule_id']
    assert resumed['actual_delay_seconds'] == 7
    assert resumed['requested_delay_seconds'] == 2 and resumed['excess_delay_seconds'] == 5


@pytest.mark.parametrize('reason,expire',[('caller_cancelled',True),('caller_cancelled',False),('scan_timeout',True)])
def test_prepare_deadline_uses_healthy_continuation_only_for_stage_expiry(monkeypatch,vision_dispatch,caplog,reason,expire):
    import time
    import storage_utils
    from tests.test_wave_execution_consumers import FakeMetadata,settled_row
    from test_processing_diagnostics import records
    store,work,wave,token,payload,sent=vision_dispatch
    app=sys.modules['app'];metadata=FakeMetadata()
    metadata.put(store.library,'original.jpg',**dict(settled_row('photo-1','revision-1'),face_status='pending'))
    app.metadata_table_client=metadata
    now=[time.monotonic()]
    monkeypatch.setattr(time,'monotonic',lambda:now[0])
    def cancel(*a,**k):
        now[0]+=101 if expire else 0
        raise storage_utils.FaceReconciliationCancelled(reason)
    app.reconcile_face_filename_indexes_batch=cancel
    monkeypatch.setattr(runtime,'process_source',lambda *a:pytest.fail('Source admitted after preparation cancellation'))
    if reason=='scan_timeout' or not expire:
        with pytest.raises(storage_utils.FaceReconciliationCancelled):runtime.consume(payload,deadline=now[0]+100)
        assert work._get(token)['attempts']==1 and not sent
    else:
        assert runtime.consume(payload,deadline=now[0]+100)=='done'
        assert work._get(token)['attempts']==0 and sent[-1][3]==5
        events=records(caplog)
        assert any(r['event']=='vision_deadline_yield' for r in events)
        config=next(r for r in events if r['event']=='vision_runtime_config')
        assert config['semantic_text_mode']=='direct'
        assert config['semantic_text_batch_size']==2 and config['semantic_text_window_ms']==5
        from execution_lease import ExecutionLeases
        lease=store._get(ExecutionLeases.key(store.library,wave['waveId'],'vision'))
        assert json.loads(lease['progressCheckpoint'][7:])==dict(phase='sources',after='')
    assert not store._page('source-attempt!')
    assert store._page(f"e!{wave['waveId']}!m!")[0]['outcome']=='pending'
