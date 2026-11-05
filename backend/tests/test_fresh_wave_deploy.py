"""Fresh install ordering, immutable release identity and fail-closed activation."""
import json
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import sys
import pytest
import app
import wave_policy
from wave_bootstrap import deployment_contract, seed
from upload_waves import ShadowWaves, digest
from test_wave_recovery import RuntimeTable
from fakes import make_store

DEPLOY=Path(__file__).resolve().parents[2]/'deploy'
sys.path.insert(0,str(DEPLOY))
from wave_release import release_contract, validate_release
import fresh_wave


def release():
    result=release_contract({name:'ghcr.io/test/'+name+'@sha256:'+char*64
        for name,char in [('backend','a'),('frontend','b'),('ipworker','c')]},'d'*40)
    return dict(result,qualification='wave-unit-tests-passed')


def service():
    jobs=RuntimeTable();jobs.query_entities=lambda *a,**k:iter([])
    tables={name:Mock(query_entities=Mock(return_value=iter([]))) for name in ('photometadata','photolibraries')}
    tables['photojobs']=jobs
    return SimpleNamespace(get_table_client=lambda n:tables[n],create_table_if_not_exists=lambda n:tables[n]),tables


def test_release_certificate_matches_runtime_algorithm():
    r=release();builds=deployment_contract(r['images']['backend'],r['images']['ipworker'])
    assert r['deployments']==builds
    assert r['capabilityId']==digest([wave_policy.PROTOCOL,builds])
    assert validate_release(r)['capabilityId']==r['capabilityId']


@pytest.mark.parametrize('mutation',[lambda r:r['images'].update(backend='ghcr.io/test/backend:latest'),
    lambda r:r.update(capabilityId='e'*64),lambda r:r.pop('qualification'),
    lambda r:r['deployments'].pop('vision')])
def test_unqualified_or_changed_release_is_rejected(mutation):
    r=release();mutation(r)
    with pytest.raises(ValueError):validate_release(r)


def test_bootstrap_persists_certificate_before_fresh_owner_enrollment(monkeypatch):
    svc,tables=service();r=release();jobs=tables['photojobs']
    assert seed(svc,r['deployments'],r['capabilityId'])==r['capabilityId']
    # An interrupted bootstrap may safely verify the same certificate again.
    assert seed(svc,r['deployments'],r['capabilityId'])==r['capabilityId']
    for flag in ('UPLOAD_WAVE_NEW_LIBRARIES_ENABLED','UPLOAD_WAVE_SOURCE_COMPLETION_ENABLED','PROCESSING_GENERATION_ENFORCEMENT_ENABLED'):
        monkeypatch.setenv(flag,'true')
    monkeypatch.setenv('PROCESSING_MODE','backend');monkeypatch.setenv('UPLOAD_WAVE_CAPABILITY_ID',r['capabilityId'])
    monkeypatch.setenv('UPLOAD_WAVE_BOOTSTRAP_PENDING','true')
    store=make_store();store.before_create=lambda lib:wave_policy.enroll_new(jobs,lib,tables['photometadata'])
    with pytest.raises(RuntimeError,match='pending'):store.ensure_personal_library('owner')
    assert store.get_library('owner') is None
    monkeypatch.setenv('UPLOAD_WAVE_BOOTSTRAP_PENDING','false')
    store.ensure_personal_library('owner')
    assert wave_policy.owned(jobs,'owner') and store.is_owner('owner','owner')
    activation=ShadowWaves(jobs,'owner')._required('policy')['activationId']
    store.ensure_personal_library('owner')
    assert ShadowWaves(jobs,'owner')._required('policy')['activationId']==activation


@pytest.mark.parametrize('existing_table',['photometadata','photolibraries','photojobs'])
def test_bootstrap_never_overwrites_existing_data_or_ownership(existing_table):
    svc,tables=service();r=release();tables[existing_table].query_entities=lambda *a,**k:iter([{'existing':True}])
    with pytest.raises(RuntimeError,match='existing library'):seed(svc,r['deployments'],r['capabilityId'])
    assert not tables['photojobs'].rows


def test_bootstrap_certificate_mismatch_cannot_write():
    svc,tables=service();r=release()
    with pytest.raises(ValueError):seed(svc,r['deployments'],'e'*64)
    assert not tables['photojobs'].rows


def test_both_parameter_phases_have_explicit_certificate_and_images():
    args=SimpleNamespace(app_name='test',resource_group='test-rg',location='westeurope',admin_email='test@example.invalid')
    r=release()
    for pending in (True,False):
        params={k:v['value'] for k,v in fresh_wave.parameters(args,r,'private-password',pending)['parameters'].items()}
        assert params['waveCapabilityId']==r['capabilityId']
        assert params['coreImage']==r['images']['backend'] and params['visionImage']==r['images']['ipworker']
        assert params['waveBootstrapPending'] is pending
        assert params['enableNewWaveLibraries'] is not pending and params['enableWaveExecution'] is not pending


def test_probe_refuses_success_when_owner_falls_back_to_legacy(monkeypatch):
    responses=iter([{'token':'private'},{'protocol':'legacy'}]);calls=[]
    def request(req,**kw):
        calls.append(req.full_url);return BytesIO(json.dumps(next(responses)).encode())
    monkeypatch.setattr(fresh_wave.urllib.request,'urlopen',request)
    outputs={k:{'value':'https://example.invalid'} for k in ('apiUrl','uploadUrl')}
    with pytest.raises(RuntimeError,match='not wave-owned'):fresh_wave.probe(outputs,'test@example.invalid','private')
    assert len(calls)==2


def test_owner_bootstrap_is_deferred_during_preparation(monkeypatch):
    store=Mock();monkeypatch.setattr(app,'library_store',store)
    monkeypatch.setenv('UPLOAD_WAVE_BOOTSTRAP_PENDING','true')
    app._bootstrap_owner_account()
    assert not store.mock_calls


@pytest.mark.parametrize('role',['vision','cluster','index-worker'])
def test_preparation_workers_wait_without_importing_or_consuming(role):
    import os, subprocess
    process=subprocess.Popen(['bash',str(Path(app.__file__).parent/'entrypoint.sh')],
        env={**os.environ,'PATH':str(Path(sys.executable).parent)+os.pathsep+os.environ['PATH'],
             'APP_ROLE':role,'UPLOAD_WAVE_BOOTSTRAP_PENDING':'true'},stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        assert process.stdout.readline().strip()=='Wave bootstrap pending; worker intake blocked.'
        process.terminate()
        stdout,stderr=process.communicate(timeout=10)
        assert process.returncode==0 and not stderr
        assert 'Starting' not in stdout
    finally:
        if process.poll() is None:process.kill();process.wait()


def test_preparation_blocks_auth_and_library_mutations(monkeypatch):
    monkeypatch.setenv('UPLOAD_WAVE_BOOTSTRAP_PENDING','true')
    client=app.app.test_client()
    for path in ('/auth/login','/api/library/invite/accept','/api/albums'):
        assert client.post(path,json={}).status_code==503
    assert client.get('/health').status_code==200


def inventory(r,pending):
    flags=dict(UPLOAD_WAVE_CAPABILITY_ID=r['capabilityId'],PROCESSING_MODE='backend',
        UPLOAD_WAVE_BOOTSTRAP_PENDING=str(pending).lower(),UPLOAD_WAVE_EXECUTION_ENABLED=str(not pending).lower(),
        UPLOAD_WAVE_NEW_LIBRARIES_ENABLED=str(not pending).lower(),UPLOAD_WAVE_SOURCE_COMPLETION_ENABLED='true',
        PROCESSING_GENERATION_ENFORCEMENT_ENABLED='true')
    def container(role):
        image=r['images']['ipworker' if role=='vision' else 'backend']
        env=dict(flags,APP_ROLE=role,UPLOAD_WAVE_DEPLOYMENT_ID=image)
        if role=='core':env['STORAGE_ACCOUNT_NAME']='teststorage'
        return dict(image=image,env=[dict(name=k,value=v) for k,v in env.items()])
    apps=[]
    for role in ('core','upload','vision','cluster','indexer','archive','recovery','web'):
        containers=[container(role)]
        if role=='indexer':containers.append(container('index-worker'))
        if role=='web':containers=[dict(image=r['images']['frontend'],env=[
            dict(name='APP_CONFIG_UPLOAD_WAVE_EXECUTION_ENABLED',value=str(not pending).lower()),
            dict(name='APP_CONFIG_PROCESSING_MODE',value='backend')])]
        apps.append(dict(name='test-'+role,properties=dict(provisioningState='Succeeded',latestRevisionName='r',
            latestReadyRevisionName='r',environmentId='test-env',template=dict(containers=containers))))
    return apps


@pytest.mark.parametrize('pending',[True,False])
def test_inventory_requires_every_role_and_exact_certificate(pending):
    from copy import deepcopy
    r=release();apps=inventory(r,pending)
    fresh_wave.verify_inventory(apps,r,pending)
    broken=deepcopy(apps)
    broken[0]['properties']['template']['containers'][0]['image']='ghcr.io/test/backend:latest'
    with pytest.raises(RuntimeError,match='Wrong image'):fresh_wave.verify_inventory(broken,r,pending)
    broken=deepcopy(apps)
    broken[4]['properties']['template']['containers'].pop()
    with pytest.raises(RuntimeError,match='Missing wave app role'):fresh_wave.verify_inventory(broken,r,pending)


def test_prepare_is_local_and_deploy_refuses_existing_group(monkeypatch,tmp_path):
    root=tmp_path/'repo';(root/'deploy').mkdir(parents=True)
    monkeypatch.setattr(fresh_wave,'ROOT',root/'deploy')
    r=release();artifact=tmp_path/'release.json';artifact.write_text(json.dumps(r))
    output=root/'.azure'/'test'
    argv=['fresh_wave','--subscription','12345678-1234-1234-1234-123456789abc','--resource-group','test-rg',
        '--app-name','test','--location','westeurope','--admin-email','test@example.invalid',
        '--release',str(artifact),'--output-dir',str(output)]
    monkeypatch.setenv('PHOTOSTORE_ADMIN_PASSWORD','private-password');monkeypatch.setattr(sys,'argv',argv)
    calls=[]
    def az(*args):
        calls.append(args)
        if args[:2]==('account','show'):return dict(id=argv[2])
        if args[:2]==('group','exists'):return True
        pytest.fail('Unexpected cloud mutation: '+str(args[:3]))
    monkeypatch.setattr(fresh_wave,'az',az)
    fresh_wave.main()
    assert not calls and (output/'activate.parameters.json').stat().st_mode & 0o777==0o600
    monkeypatch.setattr(sys,'argv',argv+['--deploy','--resume'])
    with pytest.raises(RuntimeError,match='existing resource group'):fresh_wave.main()
    assert len(calls)==2
    # Frontend is outside the capability role map, but is still pinned on resume.
    changed=dict(r,images=dict(r['images'],frontend='ghcr.io/test/frontend@sha256:'+'f'*64))
    artifact.write_text(json.dumps(changed))
    with pytest.raises(ValueError,match='release differs'):fresh_wave.main()
    assert len(calls)==2


@pytest.mark.parametrize('bootstrap_status',['Succeeded','Failed'])
def test_activation_waits_for_durable_bootstrap(monkeypatch,tmp_path,bootstrap_status):
    root=tmp_path/'repo';(root/'deploy').mkdir(parents=True)
    monkeypatch.setattr(fresh_wave,'ROOT',root/'deploy')
    r=release();artifact=tmp_path/'release.json';artifact.write_text(json.dumps(r))
    output=root/'.azure'/'test';subscription='12345678-1234-1234-1234-123456789abc'
    monkeypatch.setattr(sys,'argv',['fresh_wave','--subscription',subscription,'--resource-group','test-rg',
        '--app-name','test','--location','westeurope','--admin-email','test@example.invalid',
        '--release',str(artifact),'--output-dir',str(output),'--deploy'])
    monkeypatch.setenv('PHOTOSTORE_ADMIN_PASSWORD','private-password')
    events=[];pending=True
    def az(*args):
        nonlocal pending
        if args[:2]==('account','show'):return dict(id=subscription)
        if args[:2]==('group','exists'):return False
        if args[:3]==('deployment','sub','validate'):return {}
        if args[:3]==('deployment','sub','create'):
            label=args[args.index('--name')+1];events.append(label)
            pending=label.endswith('bootstrap')
            if not pending:assert 'certificate-Succeeded' in events
            return dict(properties=dict(outputs={}))
        if args[:3]==('storage','account','show'):return dict(id='storage-id')
        if args[:2]==('role','assignment'):return None
        if args[0]=='rest':
            method=args[args.index('--method')+1];url=args[args.index('--url')+1]
            if 'containerApps' in url:return dict(value=inventory(r,pending))
            if method=='get':return dict(identity=dict(principalId='11111111-1111-1111-1111-111111111111'))
            return None
        if args[:3]==('containerapp','job','start'):
            assert pending;events.append('certificate-'+bootstrap_status)
            return dict(name='seed-execution')
        if args[:4]==('containerapp','job','execution','show'):return dict(properties=dict(status=bootstrap_status))
        pytest.fail('Unexpected CLI call: '+str(args[:4]))
    monkeypatch.setattr(fresh_wave,'az',az)
    monkeypatch.setattr(fresh_wave,'probe',lambda *args:dict(ownerProtocol='live',noWorkWave='checked'))
    if bootstrap_status=='Failed':
        with pytest.raises(RuntimeError,match='intake remains blocked'):fresh_wave.main()
        assert events==['test-wave-bootstrap','certificate-Failed']
        assert json.loads((output/'state.json').read_text())['phase']=='seed'
    else:
        # The real probe consumes deployment outputs after inventory validation.
        original_az=az
        def with_outputs(*args):
            result=original_az(*args)
            if args[:3]==('deployment','sub','create'):
                result['properties']['outputs']={'appUrl':{'value':'https://example.invalid'}}
            return result
        monkeypatch.setattr(fresh_wave,'az',with_outputs)
        fresh_wave.main()
        assert events==['test-wave-bootstrap','certificate-Succeeded','test-wave-activate']
        assert json.loads((output/'state.json').read_text())['phase']=='verified'
