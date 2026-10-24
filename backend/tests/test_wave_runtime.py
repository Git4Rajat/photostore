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
