#!/usr/bin/env python3
"""Pause, verify, migrate and resume an existing same-protocol Azure wave deployment.

Default is a local plan. --deploy executes; --resume repeats only unfinished
phases. No password changes or data deletion. The 'migrate' phase provisions
a throwaway Manual-trigger job to run the migration script against the same
storage account every app already uses, then deletes it -- no standing job
resource is created or reused.
"""
import argparse
import copy
import json
from pathlib import Path
import re
import time
import uuid

from fresh_wave import az, run_adhoc_job, save, verify_inventory
from wave_release import PROTOCOL, validate_release

ROOT = Path(__file__).resolve().parent
APP_ROLES = ('core', 'upload', 'vision', 'cluster', 'indexer', 'archive', 'recovery', 'web')


def read_release(path):
    release = json.loads(Path(path).read_text())
    validate_release(release)
    return release


def set_env(container, values):
    env = {entry['name']: dict(entry) for entry in container.get('env', [])}
    env.update({k: dict(name=k, value=v) for k, v in values.items()})
    container['env'] = list(env.values())


def target_template(resource, release, pending, *, suffix=''):
    template = copy.deepcopy(resource['properties']['template'])
    role = resource['name'].rsplit('-', 1)[-1]
    template['revisionSuffix'] = suffix
    for container in template['containers']:
        image = release['images']['frontend' if role == 'web' else 'ipworker' if role == 'vision' else 'backend']
        container['image'] = image
        if role == 'web':
            set_env(container, dict(APP_CONFIG_UPLOAD_WAVE_EXECUTION_ENABLED=str(not pending).lower(),
                APP_CONFIG_PROCESSING_MODE='backend'))
            continue
        flags = dict(UPLOAD_WAVE_CAPABILITY_ID=release['capabilityId'], UPLOAD_WAVE_DEPLOYMENT_ID=image,
            UPLOAD_WAVE_BOOTSTRAP_PENDING=str(pending).lower(), UPLOAD_WAVE_EXECUTION_ENABLED=str(not pending).lower(),
            UPLOAD_WAVE_NEW_LIBRARIES_ENABLED=str(not pending).lower(), PROCESSING_MODE='backend',
            UPLOAD_WAVE_SOURCE_COMPLETION_ENABLED='true', PROCESSING_GENERATION_ENFORCEMENT_ENABLED='true')
        set_env(container, flags)
    return template


def verify_revisions(apps, revisions):
    for app in apps:
        ready = app['properties']['latestReadyRevisionName']
        active = [row['name'] for row in revisions[app['name']] if row['properties'].get('active')]
        if active != [ready]:
            raise RuntimeError('Old or unexpected active revisions remain: ' + app['name'])
        if any(row['name'] != ready and row['properties'].get('replicas', 0) != 0
                for row in revisions[app['name']]):
            raise RuntimeError('Old revision replicas are still draining: ' + app['name'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('subscription', 'resource-group', 'app-name', 'previous-release', 'release', 'output-dir'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--migrator-release', help='Qualified image containing wave_upgrade; needed to roll back to an older image')
    parser.add_argument('--rollback', action='store_true', help='Only restore the immediately previous library binding')
    parser.add_argument('--deploy', action='store_true')
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    if not re.fullmatch(r'[a-fA-F0-9-]{36}', args.subscription):
        raise ValueError('Explicit subscription UUID required')
    if not re.fullmatch(r'[a-z][a-z0-9-]{1,15}[a-z0-9]', args.app_name):
        raise ValueError('Explicit app name required')
    previous, release = read_release(args.previous_release), read_release(args.release)
    migrator = read_release(args.migrator_release) if args.migrator_release else release
    if release['capabilityId'] == previous['capabilityId']:
        raise ValueError('Select distinct previous and target releases')
    if (migrator.get('upgradeProtocol') != PROTOCOL
            or (not args.rollback and release.get('upgradeProtocol') != PROTOCOL)):
        raise ValueError('Release must pass the same-protocol upgrade qualification suite')
    if migrator['capabilityId'] not in (previous['capabilityId'], release['capabilityId']):
        raise ValueError('Migrator must be one of the two selected qualified releases')
    output = Path(args.output_dir).resolve()
    if not output.is_relative_to((ROOT.parent / '.azure').resolve()):
        raise ValueError('Private plan/snapshots must be inside workspace .azure')
    statefile = output / 'state.json'
    identity = dict(subscription=args.subscription, resourceGroup=args.resource_group, appName=args.app_name,
        previous=previous, release=release, migrator=migrator, rollback=args.rollback)
    if args.resume:
        state = json.loads(statefile.read_text())
        if any(state.get(k) != v for k, v in identity.items()):
            raise ValueError('Resume target/releases differ from the prepared upgrade')
    else:
        if statefile.exists():
            raise FileExistsError('Use --resume for this plan')
        output.mkdir(parents=True, exist_ok=True); output.chmod(0o700)
        state = dict(identity, upgradeId=uuid.uuid4().hex, phase='prepared')
        save(statefile, state)
    print(json.dumps(dict(phase=state['phase'], upgradeId=state['upgradeId'],
        fromCapability=previous['capabilityId'], toCapability=release['capabilityId'],
        preserves='library, members, uploads, waves, receipts and indexes')), flush=True)
    if not args.deploy:
        return
    common = ('--subscription', args.subscription)
    base = ('https://management.azure.com/subscriptions/' + args.subscription + '/resourceGroups/'
        + args.resource_group + '/providers/Microsoft.App/')
    def inventory():
        apps = az('rest', '--method', 'get', '--url', base + 'containerApps?api-version=2025-01-01', *common)['value']
        apps = [app for app in apps if app['name'] in {args.app_name + '-' + role for role in APP_ROLES}]
        if len(apps) != len(APP_ROLES):
            raise RuntimeError('Expected all eight existing app roles')
        return apps
    def patch(resource, template):
        if resource['properties']['template'] == template:
            return
        body = output / 'request.json'
        payload = dict(properties=dict(template=template), location=resource['location'])
        save(body, payload)
        try:
            az('rest', '--method', 'patch', '--url', resource['id'] + '?api-version=2025-01-01',
                '--body', '@' + str(body), *common)
        finally:
            body.unlink(missing_ok=True)
    def apply(selected, pending, label):
        for app in inventory():
            print(label + ': ' + app['name'], flush=True)
            patch(app, target_template(app, selected, pending, suffix='u' + state['upgradeId'][:12] + label))
    def wait_ready(selected, pending):
        deadline = time.monotonic() + 1200
        while time.monotonic() < deadline:
            try:
                apps = inventory()
                verify_inventory(apps, selected, pending)
                revisions = {app['name']: az('containerapp', 'revision', 'list', '-g', args.resource_group,
                    '-n', app['name'], *common) for app in apps}
                verify_revisions(apps, revisions)
                return apps
            except RuntimeError as error:
                print('Waiting: ' + str(error), flush=True)
                time.sleep(15)
        raise TimeoutError('Roles are not ready; deployment remains at its saved phase')
    if state['phase'] == 'prepared':
        apps = inventory()
        verify_inventory(apps, previous, False)
        if any(app['properties']['configuration'].get('activeRevisionsMode') != 'Single' for app in apps):
            raise RuntimeError('Upgrade requires Single revision mode for every app')
        save(output / 'before-apps.json', apps)
        state['phase'] = 'pause'; save(statefile, state)
    if state['phase'] == 'pause':
        apply(previous, True, 'p'); wait_ready(previous, True)
        state['phase'] = 'stage'; save(statefile, state)
    if state['phase'] == 'stage':
        apply(release, True, 's'); wait_ready(release, True)
        state['phase'] = 'migrate'; save(statefile, state)
    if state['phase'] == 'migrate':
        apps = wait_ready(release, True)
        core = next(a for a in apps if a['name'] == args.app_name + '-core')
        storage_account = next(e['value'] for c in core['properties']['template']['containers']
            for e in c.get('env', []) if e['name'] == 'STORAGE_ACCOUNT_NAME')
        storage_id = az('storage', 'account', 'show', '-n', storage_account, '-g', args.resource_group, *common)['id']
        command = ['python', '-u', '-m', 'wave_upgrade', '--from-capability', previous['capabilityId'],
            '--upgrade-id', state['upgradeId'], '--release', json.dumps(release, separators=(',', ':'))]
        if args.rollback:
            command.append('--rollback')
        try:
            run_adhoc_job(common, args.resource_group, core['location'], core['properties']['environmentId'],
                storage_id, name=args.app_name + '-wave-upgrade', image=migrator['images']['backend'],
                command=command, env=dict(APP_ROLE='wave-recovery', UPLOAD_WAVE_BOOTSTRAP_PENDING='true',
                    UPLOAD_WAVE_EXECUTION_ENABLED='false', UPLOAD_WAVE_NEW_LIBRARIES_ENABLED='false',
                    UPLOAD_WAVE_CAPABILITY_ID=release['capabilityId'],
                    UPLOAD_WAVE_DEPLOYMENT_ID=migrator['images']['backend'],
                    STORAGE_ACCOUNT_NAME=storage_account, USE_MANAGED_IDENTITY='true'))
        except (RuntimeError, TimeoutError) as error:
            raise RuntimeError('Upgrade job failed; keep roles paused, inspect logs, then --resume') from error
        state['phase'] = 'activate'; save(statefile, state)
    if state['phase'] == 'activate':
        apply(release, False, 'a'); wait_ready(release, False)
        state['phase'] = 'verified'; save(statefile, state)
    apps = inventory(); verify_inventory(apps, release, False)
    print(json.dumps(dict(verified=True, upgradeId=state['upgradeId'], capabilityId=release['capabilityId'],
        appUrl='https://' + next(app for app in apps if app['name'].endswith('-web'))['properties']['configuration']['ingress']['fqdn'])), flush=True)


if __name__ == '__main__':
    main()
