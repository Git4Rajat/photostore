"""Read-only ACA samples and strict inactive-stage residency verification.

collect --subscription UUID --group RG --prefix NAME --phase vision --entered-at UNIX_SECONDS
prints one JSONL sample using control-plane reads, never application HTTP.
Append samples every 30s through each measured stage. phase/entered-at must come
from independently observed durable wave state, not inferred replica counts.
verify FILE requires all five inactive phases and a settled sample in each.
"""
import argparse
import json
import subprocess
import time
from urllib.parse import quote

ROLES = ('core','web','upload','vision','cluster','indexer','archive','recovery')
PHASES = {'upload': set(), 'vision': {'vision'}, 'cluster': {'cluster'},
          'index': {'indexer'}, 'complete': set()}


def az(*args):
    return json.loads(subprocess.check_output(['az',*args,'-o','json','--only-show-errors'],text=True))


def collect(group,prefix,phase,entered_at,subscription):
    roles={}
    from scripts.wave_qualification import collect as inventory
    apps=inventory(subscription,group,prefix)['containerApps']
    for role in ROLES:
        name=prefix+'-'+role
        found=next((a for a in apps if a['name']==name),None)
        if not found:raise ValueError('Missing app inventory: '+role)
        scale=found['scale']
        url=(f'https://management.azure.com/subscriptions/{subscription}/resourceGroups/{quote(group)}'
             f'/providers/Microsoft.App/containerApps/{name}/revisions?api-version=2025-01-01')
        replicas=0
        while True:
            page=az('rest','--method','get','--url',url)
            for revision in page['value']:
                if revision['properties'].get('active'):
                    # Replica-list can retain a stopped container as "Running".
                    # The revision's count reflects ScaledToZero; missing is not zero.
                    count=revision['properties'].get('replicas')
                    if type(count) is not int or count<0:
                        raise ValueError('Missing or invalid live replica count: '+name)
                    replicas+=count
            link=page.get('nextLink')
            if not link:break
            if not link.startswith(url.split('?')[0]+'?'):
                raise ValueError('Revision pagination escaped app scope')
            url=link
        roles[role]={'replicas':replicas,'min':scale.get('minReplicas',0),
                     'cooldown':scale.get('cooldownPeriod')}
    return dict(at=time.time(),phase=phase,enteredAt=entered_at,foreground=False,roles=roles)


def verify(samples):
    qualified=set();errors=[];previous=0
    for row in samples:
        at=row['at'];phase=row['phase'];entered=row['enteredAt']
        if at<previous or entered>at:raise ValueError('Invalid sample timeline')
        previous=at
        if phase not in PHASES:raise ValueError('Unknown phase')
        if set(row['roles'])!=set(ROLES):raise ValueError('Incomplete app inventory')
        if row['foreground']:raise ValueError('Use a separate inactive qualification capture')
        for role,app in row['roles'].items():
            if app['min']!=0 or app['cooldown']!=300:
                errors.append(f'{phase}: {role} configuration prevents five-minute sleep')
            if app['replicas']<0:raise ValueError('Invalid replica count')
        # Allow the explicit five-minute idle tail plus one polling minute.
        if at-entered<360:continue
        qualified.add(phase)
        for role,app in row['roles'].items():
            if role not in PHASES[phase] and app['replicas']:
                errors.append(f'{phase}: unexpected resident {role} at {at}')
        if PHASES[phase] and not any(row['roles'][role]['replicas'] for role in PHASES[phase]):
            errors.append(f'{phase}: stage owner absent at {at}')
    missing=set(PHASES)-qualified
    if missing:errors.append('No settled observation for: '+', '.join(sorted(missing)))
    return errors


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='command',required=True)
    capture=commands.add_parser('collect')
    capture.add_argument('--subscription',required=True)
    capture.add_argument('--group',required=True);capture.add_argument('--prefix',required=True)
    capture.add_argument('--phase',choices=PHASES,required=True)
    capture.add_argument('--entered-at',type=float,required=True)
    check=commands.add_parser('verify');check.add_argument('file')
    args=parser.parse_args()
    if args.command=='collect':print(json.dumps(collect(args.group,args.prefix,args.phase,args.entered_at,args.subscription)))
    else:
        with open(args.file) as data:errors=verify([json.loads(line) for line in data if line.strip()])
        if errors:raise SystemExit('\n'.join(errors))
        print('Inactive residency capture passed; source timestamps and phase evidence require operator review.')


if __name__=='__main__':main()
