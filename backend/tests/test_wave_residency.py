import pytest

from scripts.wave_residency import PHASES,ROLES,verify


def samples():
    return [dict(at=i*1000+400,enteredAt=i*1000,phase=phase,foreground=False,
        roles={role:dict(min=0,cooldown=300,replicas=int(role in owners)) for role in ROLES})
        for i,(phase,owners) in enumerate(PHASES.items())]


def test_requires_measured_settled_samples_for_entire_flow():
    assert verify(samples())==[]
    assert verify(samples()[:-1])==['No settled observation for: complete']


def test_detects_idle_upload_polling_and_sidecar_co_wake():
    rows=samples()
    rows[0]['roles']['core']['replicas']=1
    rows[2]['roles']['indexer']['replicas']=1
    errors=verify(rows)
    assert any('unexpected resident core' in e for e in errors)
    assert any('unexpected resident indexer' in e for e in errors)


def test_warm_cluster_configuration_is_not_residency_proof():
    rows=samples();rows[0]['roles']['cluster']['min']=1
    assert any('configuration' in e for e in verify(rows))


def inventory(*args):
    return {'containerApps':[{'name':'test-'+role,'scale':{'minReplicas':0,'cooldownPeriod':300}}
                             for role in ROLES]}


def test_collector_uses_live_revision_counts_without_replica_or_log_commands(monkeypatch):
    from scripts import wave_residency as residency, wave_qualification
    monkeypatch.setattr(wave_qualification,'collect',inventory)
    calls=[]
    def arm(*args):
        calls.append(args)
        assert args[:4]==('rest','--method','get','--url')
        assert args[4].endswith('/revisions?api-version=2025-01-01')
        return {'value':[
            {'properties':{'active':True,'replicas':0,'runningState':'ScaledToZero'}},
            {'properties':{'active':False,'replicas':3}},
        ]}
    monkeypatch.setattr(residency,'az',arm)
    sample=residency.collect('test-rg','test','complete',0,'a'*36)
    assert len(calls)==len(ROLES)
    assert all(app['replicas']==0 for app in sample['roles'].values())


@pytest.mark.parametrize('count',[None,-1,'0',True])
def test_collector_rejects_unknown_or_invalid_live_replica_counts(monkeypatch,count):
    from scripts import wave_residency as residency, wave_qualification
    monkeypatch.setattr(wave_qualification,'collect',inventory)
    monkeypatch.setattr(residency,'az',lambda *args:{'value':[{'properties':{'active':True,'replicas':count}}]})
    with pytest.raises(ValueError,match='live replica count'):
        residency.collect('test-rg','test','complete',0,'a'*36)


def test_collector_counts_active_revisions_on_later_pages(monkeypatch):
    from scripts import wave_residency as residency, wave_qualification
    monkeypatch.setattr(wave_qualification,'collect',inventory)
    def arm(*args):
        url=args[4]
        if 'skiptoken' in url:
            return {'value':[{'properties':{'active':True,'replicas':2}}]}
        return {'value':[{'properties':{'active':False,'replicas':0}}],
                'nextLink':url+'&skiptoken=next'}
    monkeypatch.setattr(residency,'az',arm)
    sample=residency.collect('test-rg','test','complete',0,'a'*36)
    assert all(app['replicas']==2 for app in sample['roles'].values())
