"""Configuration readiness cannot stand in for measured cloud qualification."""
import json
from scripts.wave_qualification import check,clean
from scripts.wave_residency import ROLES
from wave_policy import ROLES as CERT_ROLES,PROTOCOL
from upload_waves import digest


def deployment():
    images={role:'registry/image@sha256:'+('1' if role=='vision' else '2')*64 for role in CERT_ROLES}
    certificate=digest([PROTOCOL,images])
    def container(name,role):
        image=images.get(role,images['core'])
        return dict(name=name,image=image,env={'APP_ROLE':role,'PROCESSING_MODE':'backend',
            'UPLOAD_WAVE_EXECUTION_ENABLED':'true','UPLOAD_WAVE_NEW_LIBRARIES_ENABLED':'true',
            'UPLOAD_WAVE_SOURCE_COMPLETION_ENABLED':'true','PROCESSING_GENERATION_ENFORCEMENT_ENABLED':'true',
            'UPLOAD_WAVE_CAPABILITY_ID':certificate,'UPLOAD_WAVE_DEPLOYMENT_ID':image,
            'APP_CONFIG_UPLOAD_WAVE_EXECUTION_ENABLED':'true','APP_CONFIG_PROCESSING_MODE':'backend'})
    apps=[]
    for role in ROLES:
        containers=[container(role,role)]
        if role=='indexer':containers.append(container('index-maintenance','index-worker'))
        apps.append(dict(name='test-'+role,provisioning='Succeeded',revision='ready',containers=containers,
            scale=dict(minReplicas=0,cooldownPeriod=300,rules=[dict(custom=dict(type='azure-queue',
                metadata=dict(queueName='photostore-index-maintenance')))])))
    return dict(prefix='test',containerApps=apps,jobs=[])


def test_configuration_pass_still_requires_measured_cloud_evidence():
    result=check(deployment())
    assert result['configurationReady'] and not result['cloudQualified']
    assert result['stillRequired']


def test_current_single_container_indexer_cannot_qualify():
    snapshot=deployment()
    indexer=next(a for a in snapshot['containerApps'] if a['name']=='test-indexer')
    indexer['containers']=indexer['containers'][:1]
    assert any('missing container index-maintenance' in e for e in check(snapshot)['blockers'])


def test_standing_job_or_inferred_cooldown_blocks_readiness():
    snapshot=deployment()
    snapshot['jobs']=[dict(name='test-wave-recovery',identityType='SystemAssigned',containers=[],
        configuration=dict(triggerType='Schedule',replicaTimeout=240))]
    snapshot['containerApps'][0]['scale'].pop('cooldownPeriod')
    snapshot['containerApps'][2]['containers'][0]['env']['UPLOAD_WAVE_EXECUTION_ENABLED']='false'
    errors=check(snapshot)['blockers']
    assert any('standing job must not exist' in e for e in errors)
    assert any('cooldown' in e for e in errors)
    assert any('not enabled' in e for e in errors)


def test_tagged_images_and_foreign_certificates_are_not_accepted():
    snapshot=deployment();snapshot['containerApps'][0]['containers'][0]['image']='registry/image:latest'
    snapshot['containerApps'][1]['containers'][0]['env']['UPLOAD_WAVE_CAPABILITY_ID']='a'*64
    result=check(snapshot)
    assert not result['configurationReady']
    assert any('immutable' in e for e in result['blockers'])
    assert any('exact deployed' in e for e in result['blockers'])


def test_saved_inventory_never_contains_unrelated_environment_or_scaler_secrets():
    resource=dict(name='test-core',properties=dict(template=dict(containers=[dict(name='core',image='image',
        env=[dict(name='ADMIN_PASSWORD',value='private-password'),dict(name='APP_ROLE',value='core')])],
        scale=dict(rules=[dict(custom=dict(type='azure-queue',metadata=dict(connectionString='private-connection',queueName='q')))]))))
    encoded=json.dumps(clean(resource))
    assert 'private-' not in encoded
    assert 'core' in encoded and 'queueName' in encoded


def test_arm_null_rules_and_identity_are_not_treated_as_configuration_proof():
    resource=dict(name='test-web',identity=None,properties=dict(template=dict(
        scale=dict(rules=None),containers=[dict(name='web',image='old',env=None)])))
    result=clean(resource)
    assert result['scale']['rules']==[]
    assert result['scale']['cooldownPeriod'] is None
    assert result['containers'][0]['env']=={}
    resource['properties']['template']['scale']['rules']=[dict(http=dict(concurrentRequests=10),custom=None)]
    assert clean(resource)['scale']['rules'][0]['custom']['type'] is None
