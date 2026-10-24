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
