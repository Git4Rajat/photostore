"""Read-only cloud configuration preflight. Never enrolls or certifies a library.

Use explicit subscription/group/prefix. ARM 2025-01-01 exposes scale cooldowns
that older az containerapp output omits. Saved evidence excludes secret values.
Passing configuration checks is not end-to-end qualification.
"""
import argparse
import json
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import quote

from scripts.wave_residency import ROLES
from wave_policy import ROLES as CAPABILITY_ROLES, PROTOCOL
from upload_waves import digest

API='2025-01-01'
ENV_NAMES={'APP_ROLE','PROCESSING_MODE','STORAGE_ACCOUNT_NAME','JOBS_TABLE',
    'UPLOAD_WAVE_EXECUTION_ENABLED','UPLOAD_WAVE_NEW_LIBRARIES_ENABLED',
    'UPLOAD_WAVE_SOURCE_COMPLETION_ENABLED','PROCESSING_GENERATION_ENFORCEMENT_ENABLED',
    'UPLOAD_WAVE_CAPABILITY_ID','UPLOAD_WAVE_DEPLOYMENT_ID','UPLOAD_WAVE_RECOVERY_ENABLED',
    'APP_CONFIG_UPLOAD_WAVE_EXECUTION_ENABLED','APP_CONFIG_PROCESSING_MODE'}
STILL_REQUIRED=['durable capability certificate verification',
    'managed identity data-plane and scheduled recovery execution',
    'whole-selection admission across 900-file transport children and overlapping uploads',
    'real SDK/queue crash, duplicate delivery and stale-owner rejection',
    'assignment oracle and projection receipt coverage on controlled input',
    'foreground CRUD, deletion and reprocessing races',
    'representative latency, memory, disk and transaction-cost budgets',
    'alert ingestion and all-eight-app inactive residency capture']


def az(*args):
    return json.loads(subprocess.check_output(['az',*args,'-o','json','--only-show-errors'],text=True,timeout=90))


def clean(resource):
    props=resource['properties'];template=props.get('template') or {}
    scale=template.get('scale') or {}
    return dict(name=resource['name'],revision=props.get('latestReadyRevisionName'),
        provisioning=props.get('provisioningState'),scale={
            **{k:scale.get(k) for k in ('minReplicas','maxReplicas','cooldownPeriod')},
            'rules':[dict(name=r.get('name'),custom=dict(type=(r.get('custom') or {}).get('type'),
                metadata={'queueName':((r.get('custom') or {}).get('metadata') or {}).get('queueName')}))
                for r in (scale.get('rules') or [])]},
        configuration={key:props.get('configuration',{}).get(key) for key in
            ('triggerType','replicaTimeout','manualTriggerConfig')},
        identityType=(resource.get('identity') or {}).get('type',''),
        containers=[dict(name=c['name'],image=c['image'],
            env={e['name']:e.get('value') for e in (c.get('env') or []) if e['name'] in ENV_NAMES})
            for c in template.get('containers',[])])


def collect(subscription,group,prefix):
    if not re.fullmatch(r'[0-9a-fA-F-]{36}',subscription):raise ValueError('Explicit subscription UUID required')
    if not re.fullmatch(r'[A-Za-z0-9_.()-]{1,90}',group):raise ValueError('Invalid resource group')
    if not re.fullmatch(r'[a-z][a-z0-9-]{1,30}',prefix):raise ValueError('Invalid app prefix')
    account=az('account','show','--subscription',subscription)
    if account['id'].lower()!=subscription.lower():raise ValueError('Subscription mismatch')
    base=f'https://management.azure.com/subscriptions/{subscription}/resourceGroups/{quote(group)}/providers/Microsoft.App/'
    inventory={}
    for kind in ('containerApps','jobs'):
        page=az('rest','--method','get','--url',base+kind+'?api-version='+API)
        rows=[]
        while True:
            rows.extend(page.get('value',[]))
            link=page.get('nextLink')
            if not link:break
            if not link.startswith(base+kind+'?'):raise ValueError('Inventory pagination escaped target scope')
            page=az('rest','--method','get','--url',link)
        names={prefix+'-'+role for role in ROLES}|{prefix+'-wave-recovery'}
        inventory[kind]=[clean(row) for row in rows if row['name'] in names]
    return dict(at=time.time(),subscription=subscription,group=group,prefix=prefix,
        apiVersion=API,**inventory)


def check(snapshot):
    prefix=snapshot['prefix'];errors=[];deployments={};certificates=set()
    apps={row['name']:row for row in snapshot['containerApps']}
    def require(condition,message):
        if not condition:errors.append(message)
    def backend(container,role,label):
        env=container['env'];image=container['image']
        require(bool(re.fullmatch(r'.+@sha256:[0-9a-f]{64}',image)),label+': immutable image digest required')
        require(env.get('APP_ROLE')==role,label+': wrong APP_ROLE')
        require(env.get('PROCESSING_MODE')=='backend',label+': backend processing required')
        for flag in ('UPLOAD_WAVE_EXECUTION_ENABLED','UPLOAD_WAVE_SOURCE_COMPLETION_ENABLED',
                     'PROCESSING_GENERATION_ENFORCEMENT_ENABLED'):
            require(env.get(flag)=='true',label+': '+flag+' is not enabled')
        certificate=env.get('UPLOAD_WAVE_CAPABILITY_ID','')
        require(bool(re.fullmatch(r'[0-9a-f]{64}',certificate)),label+': capability ID missing or malformed')
        certificates.add(certificate)
        require(env.get('UPLOAD_WAVE_DEPLOYMENT_ID')==image,label+': deployment ID must equal its pinned image')
        if role in CAPABILITY_ROLES:deployments[role]=image
        if role in ('core','archive'):
            require(env.get('UPLOAD_WAVE_NEW_LIBRARIES_ENABLED')=='true',label+': new-library enrollment disabled')
    for role in ROLES:
        label=prefix+'-'+role;row=apps.get(label)
        if not row:
            errors.append(label+': app missing');continue
        require(row.get('provisioning')=='Succeeded',label+': provisioning incomplete')
        require(bool(row.get('revision')),label+': no ready revision')
        scale=row['scale']
        require(scale.get('minReplicas')==0,label+': min replicas must be zero')
        require(scale.get('cooldownPeriod')==300,label+': explicit 300-second cooldown required')
        containers={c['name']:c for c in row['containers']}
        if role=='web':
            container=containers.get('web')
            require(bool(container),label+': web container missing')
            if container:
                require(bool(re.fullmatch(r'.+@sha256:[0-9a-f]{64}',container['image'])),label+': immutable image digest required')
                require(container['env'].get('APP_CONFIG_UPLOAD_WAVE_EXECUTION_ENABLED')=='true',label+': runtime wave flag disabled')
                require(container['env'].get('APP_CONFIG_PROCESSING_MODE')=='backend',label+': browser AI must be disabled')
        else:
            expected={role:role} if role!='indexer' else {'indexer':'indexer','index-maintenance':'index-worker'}
            for name,app_role in expected.items():
                container=containers.get(name)
                if container:backend(container,app_role,label+'/'+name)
                else:errors.append(label+': missing container '+name)
            if role=='indexer':
                rules=scale.get('rules',[])
                require(any((r.get('custom') or {}).get('type')=='azure-queue' and
                    ((r.get('custom') or {}).get('metadata') or {}).get('queueName')=='photostore-index-maintenance' for r in rules),
                    label+': dedicated index-maintenance queue scaler missing')
    job=next((row for row in snapshot['jobs'] if row['name']==prefix+'-wave-recovery'),None)
    if job is None:errors.append(prefix+'-wave-recovery: job missing')
    else:
        config=job['configuration'];manual=config.get('manualTriggerConfig') or {}
        require(config.get('triggerType')=='Manual',
            job['name']+': must be manually triggered, not a standalone schedule -- '
            'recovery rides on real wave-lifecycle events instead of a timer')
        require(manual.get('parallelism')==1,job['name']+': recovery must be bounded to one replica')
        require(0<(config.get('replicaTimeout') or 0)<=240,job['name']+': bounded timeout required')
        require('SystemAssigned' in job['identityType'],job['name']+': managed identity missing')
        if len(job['containers'])!=1:errors.append(job['name']+': expected one recovery container')
        else:
            container=job['containers'][0];backend(container,'wave-recovery',job['name'])
            require(container['env'].get('UPLOAD_WAVE_RECOVERY_ENABLED')=='true',job['name']+': recovery scanner disabled')
    if set(deployments)==set(CAPABILITY_ROLES):
        expected=digest([PROTOCOL,deployments])
        require(certificates=={expected},'Capability ID does not bind the exact deployed role images')
    else:require(False,'Capability role-image inventory incomplete')
    return dict(configurationReady=not errors,cloudQualified=False,blockers=errors,
        stillRequired=STILL_REQUIRED)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--subscription',required=True);parser.add_argument('--group',required=True)
    parser.add_argument('--prefix',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args()
    # Fail before cloud calls when an output would overwrite prior evidence.
    target=Path(args.output)
    if target.exists():raise FileExistsError('Refusing to overwrite qualification evidence')
    snapshot=collect(args.subscription,args.group,args.prefix)
    result=check(snapshot)
    with target.open('x') as output:
        import os
        os.chmod(args.output,0o600)
        json.dump(dict(snapshot=snapshot,**result),output,indent=2);output.write('\n')
    print(json.dumps(result,indent=2))
    raise SystemExit(0 if result['configurationReady'] else 2)


if __name__=='__main__':main()
