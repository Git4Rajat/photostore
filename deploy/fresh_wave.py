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
import time
import urllib.request
import uuid
from wave_release import validate_release

ROOT=Path(__file__).resolve().parent


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


def verify_inventory(apps,job,release,pending):
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
    expected_roles=set(release['deployments'])-{'wave-recovery'}
    if not expected_roles.issubset(found):raise RuntimeError('Missing wave app role')
    if not any(a['name'].endswith('-web') for a in apps):raise RuntimeError('Missing web app')
    config=job['properties']['configuration'];c=job['properties']['template']['containers'][0]
    env={e['name']:e.get('value') for e in c['env']}
    if (c['image']!=release['images']['backend'] or env.get('UPLOAD_WAVE_CAPABILITY_ID')!=release['capabilityId']
        or env.get('UPLOAD_WAVE_DEPLOYMENT_ID')!=c['image']
        or env.get('UPLOAD_WAVE_BOOTSTRAP_PENDING')!=str(pending).lower()
        or env.get('UPLOAD_WAVE_RECOVERY_ENABLED')!=str(not pending).lower()
        or env.get('UPLOAD_WAVE_EXECUTION_ENABLED')!=str(not pending).lower()
        or env.get('UPLOAD_WAVE_NEW_LIBRARIES_ENABLED')!=str(not pending).lower()
        or env.get('APP_ROLE')!='wave-recovery' or env.get('PROCESSING_MODE')!='backend'
        or env.get('UPLOAD_WAVE_BOOTSTRAP_VISION_IMAGE')!=release['images']['ipworker']
        or config['triggerType']!='Schedule'
        or config['scheduleTriggerConfig']['cronExpression']!='*/5 * * * *'
        or c['command']!=['python','-u','-m','wave_recovery']):
        raise RuntimeError('Recovery/bootstrap job mismatch')


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
        job=az('containerapp','job','show','-g',args.resource_group,'-n',args.app_name+'-wave-recovery',*common)
        verify_inventory(apps,job,release,pending)
        return job
    if state['phase']=='provisioning':
        print('Provisioning with intake blocked.',flush=True);deploy('bootstrap');inventory(True)
        state['phase']='seed';save(statefile,state)
    if state['phase']=='seed':
        job=inventory(True)
        template=job['properties']['template']
        template['containers'][0]['command']=['python','-u','-m','wave_bootstrap']
        # JSON is valid YAML; use the documented per-execution template override.
        override=output/'bootstrap-job.yaml';save(override,template)
        execution=az('containerapp','job','start','-g',args.resource_group,'-n',args.app_name+'-wave-recovery','--yaml',str(override),*common)
        name=execution['name'];deadline=time.monotonic()+600
        while time.monotonic()<deadline:
            result=az('containerapp','job','execution','show','-g',args.resource_group,'-n',args.app_name+'-wave-recovery','--job-execution-name',name,*common)
            status=result['properties']['status']
            if status=='Succeeded':break
            if status in ('Failed','Stopped','Degraded'):raise RuntimeError('Certificate bootstrap failed; intake remains blocked')
            time.sleep(5)
        else:raise TimeoutError('Bootstrap did not finish; intake remains blocked')
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
