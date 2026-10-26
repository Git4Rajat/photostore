import json
import sys
from types import SimpleNamespace

import pytest
import wave_runtime as runtime


def test_disabled_consumer_and_sender_do_not_touch_apps(monkeypatch):
    monkeypatch.delenv('UPLOAD_WAVE_EXECUTION_ENABLED',raising=False)
    assert runtime.consume({})=='noop'
    assert runtime.send_stage('u','invalid','r') is None


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


@pytest.mark.parametrize('errors,progressed,expected_attempts', [(1, False, 1), (0, False, 0), (1, True, 0)])
def test_continuation_refunds_only_healthy_or_progressing_passes(monkeypatch, errors, progressed, expected_attempts):
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
    monkeypatch.setattr(runtime, 'relay_current', lambda *a: None)
    def run(*args, inspection_failures, **kwargs):
        inspection_failures[0] = errors
        return dict(wave, state='processing', lastProgressAt=float(wave.get('lastProgressAt') or 0) + int(progressed))
    monkeypatch.setattr(runtime, 'run_vision_release', run)
    assert runtime.consume(dict(userId=store.library, stage='vision', waveId=wave_id,
                                scope=scope, intentRevision=revision)) == 'done'
    assert work._get(token)['attempts'] == expected_attempts
