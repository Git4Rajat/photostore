#!/usr/bin/env python3
"""Prepare or execute a NEW sandbox deployment with durable wave bootstrap.

No deletions, no adoption of existing groups, no migration or certificate rebind.
Default is local preparation only. --deploy explicitly executes both phases.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import urllib.request
import uuid
from wave_release import validate_release

ROOT=Path(__file__).resolve().parent

# Same grants deploy/resources.bicep gives every backend app's managed
# identity (Storage Blob/Table/Queue Data Contributor, Storage Blob
# Delegator) -- an ad-hoc job needs the identical access to run bootstrap or
# upgrade code against the same storage account.
STORAGE_ROLE_IDS = (
    'ba92f5b4-2d11-453d-a403-e96b0029c9fe',
    '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3',
    '974c5e8b-45b9-4653-ba55-5f855dd0fb88',
    'db58b8e5-c6ad-4a2a-8342-4190687cbf4a',
)


def save(path,value):
    with open(path,'w',opener=lambda p,f:os.open(p,f,0o600)) as stream:
        json.dump(value,stream,indent=2);stream.write('\n')
    path.chmod(0o600)


def parameters(args,release,password,pending):
    return {'$schema':'https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#',
        'contentVersion':'1.0.0.0','parameters':{k:{'value':v} for k,v in dict(
        appName=args.app_name,resourceGroupName=args.resource_group,location=args.location,
        adminEmail=args.admin_email,adminPassword=password,
        coreImage=release['images']['backend'],webImage=release['images']['frontend'],
        visionImage=release['images']['ipworker'],waveCapabilityId=release['capabilityId'],
        waveBootstrapPending=pending,enableWaveExecution=not pending,enableNewWaveLibraries=not pending,
        browserUploadPreparation=True).items()}}


def az(*args):
    result=subprocess.run(['az',*args,'-o','json','--only-show-errors'],capture_output=True,text=True,check=True)
    return json.loads(result.stdout) if result.stdout.strip() else None


def run_adhoc_job(common,resource_group,location,environment_id,storage_account_id,*,name,image,command,env):
    """Provision a throwaway Manual-trigger Container App Job with the same
    storage access every backend app's managed identity gets, run it once,
    wait for completion, then delete it -- regardless of success or failure.

    Used for a one-time script (certificate bootstrap, upgrade migration)
    that needs a qualified-image compute host with storage access but must
    never exist as a standing, always-declared resource.
    """
    job_id=('/subscriptions/'+common[1]+'/resourceGroups/'+resource_group
        +'/providers/Microsoft.App/jobs/'+name)
    body=dict(location=location,identity=dict(type='SystemAssigned'),properties=dict(
        environmentId=environment_id,
        configuration=dict(triggerType='Manual',replicaTimeout=900,replicaRetryLimit=0,
            manualTriggerConfig=dict(parallelism=1,replicaCompletionCount=1)),
        template=dict(containers=[dict(name=name,image=image,command=command,
            resources=dict(cpu='0.5',memory='1Gi'),
            env=[dict(name=k,value=v) for k,v in env.items()])])))
    with tempfile.NamedTemporaryFile('w',suffix='.json',delete=False) as handle:
        json.dump(body,handle);path=handle.name
    try:
        az('rest','--method','put','--url',job_id+'?api-version=2025-01-01','--body','@'+path,*common)
        deadline=time.monotonic()+120
        while True:
            created=az('rest','--method','get','--url',job_id+'?api-version=2025-01-01',*common)
            principal_id=(created.get('identity') or {}).get('principalId')
            if principal_id:break
            if time.monotonic()>=deadline:raise TimeoutError('Job managed identity did not provision in time: '+name)
            time.sleep(3)
        for role_id in STORAGE_ROLE_IDS:
            az('role','assignment','create','--role',role_id,'--assignee-object-id',principal_id,
                '--assignee-principal-type','ServicePrincipal','--scope',storage_account_id,*common)
        execution=az('containerapp','job','start','-g',resource_group,'-n',name,*common)
        execution_name=execution['name'];deadline=time.monotonic()+900
        while True:
            current=az('containerapp','job','execution','show','-g',resource_group,'-n',name,
                '--job-execution-name',execution_name,*common)
            status=current['properties']['status']
            if status=='Succeeded':return
            if status in ('Failed','Stopped','Degraded'):raise RuntimeError(name+' job failed; inspect logs before retrying')
            if time.monotonic()>=deadline:raise TimeoutError(name+' job did not finish in time')
            time.sleep(5)
    finally:
        os.unlink(path)
        az('rest','--method','delete','--url',job_id+'?api-version=2025-01-01',*common)


def verify_inventory(apps,release,pending):
    """No standing job to verify: certificate bootstrap runs as a throwaway
    ad-hoc job (see run_adhoc_job), created, run and deleted in the 'seed'
    phase, so there is nothing left over here to check against."""
    found=set()
    for app in apps:
        role=app['name'].rsplit('-',1)[-1]
        if role not in ('core','upload','vision','cluster','indexer','archive','recovery','web'):continue
        props=app['properties']
        if props.get('provisioningState')!='Succeeded' or props.get('latestRevisionName')!=props.get('latestReadyRevisionName'):
            raise RuntimeError('Latest app revision is not ready: '+app['name'])
        for c in props['template']['containers']:
            env={e['name']:e.get('value') for e in c.get('env',[])}
            expected=release['images']['frontend' if role=='web' else 'ipworker' if role=='vision' else 'backend']
            if c['image']!=expected:raise RuntimeError('Wrong image: '+app['name'])
            if role=='web':
                if (env.get('APP_CONFIG_UPLOAD_WAVE_EXECUTION_ENABLED')!=str(not pending).lower()
                    or env.get('APP_CONFIG_PROCESSING_MODE')!='backend'):
                    raise RuntimeError('Web wave flag mismatch')
                continue
            found.add(env.get('APP_ROLE'))
            flags=dict(UPLOAD_WAVE_CAPABILITY_ID=release['capabilityId'],UPLOAD_WAVE_DEPLOYMENT_ID=expected,
                UPLOAD_WAVE_BOOTSTRAP_PENDING=str(pending).lower(),UPLOAD_WAVE_EXECUTION_ENABLED=str(not pending).lower(),
                UPLOAD_WAVE_NEW_LIBRARIES_ENABLED=str(not pending).lower(),PROCESSING_MODE='backend')
            if not pending:flags.update(UPLOAD_WAVE_SOURCE_COMPLETION_ENABLED='true',PROCESSING_GENERATION_ENFORCEMENT_ENABLED='true')
            if any(env.get(k)!=v for k,v in flags.items()):raise RuntimeError('Wave environment mismatch: '+app['name'])
    if not set(release['deployments']).issubset(found):raise RuntimeError('Missing wave app role')
    if not any(a['name'].endswith('-web') for a in apps):raise RuntimeError('Missing web app')


def probe(outputs,email,password):
    """Real owner authentication and live protocol, without uploading user data."""
    def call(base,path,body=None,token=None):
        headers={'Content-Type':'application/json'}
        if token:headers['Authorization']='Bearer '+token
        req=urllib.request.Request(base+path,data=None if body is None else json.dumps(body).encode(),headers=headers)
        with urllib.request.urlopen(req,timeout=120) as response:return json.load(response)
    token=call(outputs['apiUrl']['value'],'/auth/login',dict(email=email,password=password))['token']
    protocol=call(outputs['uploadUrl']['value'],'/upload/waves/protocol',token=token)
    if protocol.get('protocol')!='live':raise RuntimeError('Fresh owner is not wave-owned; refusing success')
    session='fresh-check-'+uuid.uuid4().hex;base='/upload/waves/live/sessions';upload=outputs['uploadUrl']['value']
    wave=call(upload,base,dict(sessionId=session,declaredCount=1),token)
    call(upload,base+'/'+session+'/items',dict(items=[dict(itemId='cancel-only',filename='not-uploaded.jpg')]),token)
    call(upload,base+'/'+session+'/resolve',dict(itemId='cancel-only',outcome='cancelled'),token)
    result=call(upload,'/upload/waves/live/'+wave['waveId'],token=token)
    if result.get('state')!='complete' or result.get('completionOutcome')!='no_work':
        raise RuntimeError('Fresh owner live intake smoke failed')
    return dict(ownerProtocol='live',noWorkWave=wave['waveId'])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('subscription','resource-group','app-name','location','admin-email','release','output-dir'):
        parser.add_argument('--'+name,required=True)
    parser.add_argument('--password-env',default='PHOTOSTORE_ADMIN_PASSWORD')
    parser.add_argument('--deploy',action='store_true');parser.add_argument('--resume',action='store_true')
    args=parser.parse_args()
    if not re.fullmatch(r'[a-z][a-z0-9-]{1,15}[a-z0-9]',args.app_name):raise ValueError('App name must have 3–17 lowercase letters/digits/hyphens, start with a letter and end with a letter or digit')
    if not re.fullmatch(r'[a-fA-F0-9-]{36}',args.subscription):raise ValueError('Explicit subscription UUID required')
    release=validate_release(json.loads(Path(args.release).read_text()))
    output=Path(args.output_dir).resolve()
    # Credential-bearing plans must stay in the ignored workspace deployment area.
    private=ROOT.parent/'.azure'
    if not output.is_relative_to(private.resolve()):raise ValueError('--output-dir must be inside the workspace .azure directory')
    statefile=output/'state.json'
    if args.resume:
        state=json.loads(statefile.read_text())
        if (any(state[k]!=getattr(args,k) for k in ('subscription','resource_group','app_name','location','admin_email'))
            or state['release']!=release):
            raise ValueError('Resume target or release differs from the original plan')
        password=json.loads((output/'activate.parameters.json').read_text())['parameters']['adminPassword']['value']
    else:
        if statefile.exists():raise FileExistsError('Use --resume for this deployment plan')
        password=os.environ.get(args.password_env,'')
        if len(password)<8:raise ValueError('Set '+args.password_env+' to a password of at least eight characters')
        output.mkdir(parents=True,exist_ok=True);output.chmod(0o700)
        state={k:getattr(args,k) for k in ('subscription','resource_group','app_name','location','admin_email')}
        state.update(capabilityId=release['capabilityId'],release=release,phase='prepared')
        for pending,label in ((True,'bootstrap'),(False,'activate')):
            save(output/(label+'.parameters.json'),parameters(args,release,password,pending))
        save(statefile,state)
    print('Prepared immutable images, certificate '+release['capabilityId']+' and both deployment phases.',flush=True)
    if not args.deploy:return
    common=('--subscription',args.subscription)
    account=az('account','show',*common)
    if account['id'].lower()!=args.subscription.lower():raise RuntimeError('Subscription mismatch')
    if state['phase']=='prepared':
        if az('group','exists','-n',args.resource_group,*common):
            raise RuntimeError('Fresh deployment refuses an existing resource group; no resources were changed')
        state['phase']='provisioning';save(statefile,state)
    def deploy(label):
        params=output/(label+'.parameters.json')
        az('deployment','sub','validate','--location',args.location,'--template-file',str(ROOT/'sandbox.bicep'),'--parameters',str(params),*common)
        return az('deployment','sub','create','--name',args.app_name+'-wave-'+label,'--location',args.location,
                  '--template-file',str(ROOT/'sandbox.bicep'),'--parameters',str(params),*common)
    def inventory(pending):
        base=f'https://management.azure.com/subscriptions/{args.subscription}/resourceGroups/{args.resource_group}/providers/Microsoft.App/'
        apps=az('rest','--method','get','--url',base+'containerApps?api-version=2025-01-01',*common)['value']
        verify_inventory(apps,release,pending)
        return apps
    if state['phase']=='provisioning':
        print('Provisioning with intake blocked.',flush=True);deploy('bootstrap');inventory(True)
        state['phase']='seed';save(statefile,state)
    if state['phase']=='seed':
        apps=inventory(True)
        core=next(a for a in apps if a['name']==args.app_name+'-core')
        storage_account=next(e['value'] for c in core['properties']['template']['containers']
            for e in c.get('env',[]) if e['name']=='STORAGE_ACCOUNT_NAME')
        storage_id=az('storage','account','show','-n',storage_account,'-g',args.resource_group,*common)['id']
        try:
            run_adhoc_job(common,args.resource_group,args.location,core['properties']['environmentId'],storage_id,
                name=args.app_name+'-wave-bootstrap',image=release['images']['backend'],
                command=['python','-u','-m','wave_bootstrap'],
                env=dict(APP_ROLE='wave-recovery',UPLOAD_WAVE_BOOTSTRAP_PENDING='true',
                    UPLOAD_WAVE_EXECUTION_ENABLED='false',UPLOAD_WAVE_NEW_LIBRARIES_ENABLED='false',
                    UPLOAD_WAVE_DEPLOYMENT_ID=release['images']['backend'],
                    UPLOAD_WAVE_BOOTSTRAP_VISION_IMAGE=release['images']['ipworker'],
                    UPLOAD_WAVE_CAPABILITY_ID=release['capabilityId'],
                    STORAGE_ACCOUNT_NAME=storage_account,USE_MANAGED_IDENTITY='true'))
        except (RuntimeError,TimeoutError) as error:
            raise RuntimeError('Certificate bootstrap failed; intake remains blocked') from error
        state['phase']='activate';save(statefile,state)
    if state['phase']=='activate':
        print('Certificate persisted. Activating wave execution/enrollment.',flush=True)
        result=deploy('activate');inventory(False)
        state['outputs']=result['properties']['outputs'];state['phase']='verify';save(statefile,state)
    inventory(False)
    smoke=probe(state['outputs'],args.admin_email,password)
    state.update(phase='verified',smoke=smoke);save(statefile,state)
    print(json.dumps(dict(verified=True,capabilityId=release['capabilityId'],**smoke,
                         appUrl=state['outputs']['appUrl']['value']),indent=2))


if __name__=='__main__':
    main()
