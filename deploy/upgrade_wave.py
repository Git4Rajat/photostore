#!/usr/bin/env python3
"""Pause, verify, migrate and resume an existing same-protocol Azure wave deployment.

Default is a local plan. --deploy executes; --resume repeats only unfinished
phases. No resource provisioning, password changes, data deletion or role grants.
"""
import argparse
import copy
import json
from pathlib import Path
import re
import time
import uuid

from fresh_wave import az, save, verify_inventory
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


def target_template(resource, release, pending, *, job=False, suffix=''):
    template = copy.deepcopy(resource['properties']['template'])
    role = 'wave-recovery' if job else resource['name'].rsplit('-', 1)[-1]
    if not job:
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
        if job:
            flags.update(UPLOAD_WAVE_RECOVERY_ENABLED=str(not pending).lower(),
                UPLOAD_WAVE_BOOTSTRAP_VISION_IMAGE=release['images']['ipworker'])
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
    job_name = args.app_name + '-wave-recovery'
    def inventory():
        apps = az('rest', '--method', 'get', '--url', base + 'containerApps?api-version=2025-01-01', *common)['value']
        apps = [app for app in apps if app['name'] in {args.app_name + '-' + role for role in APP_ROLES}]
        if len(apps) != len(APP_ROLES):
            raise RuntimeError('Expected all eight existing app roles')
        job = az('containerapp', 'job', 'show', '-g', args.resource_group, '-n', job_name, *common)
        return apps, job
    def patch(resource, template, *, job=False):
        if resource['properties']['template'] == template:
            return
        body = output / 'request.json'
        payload = dict(properties=dict(template=template))
        if not job:
            payload['location'] = resource['location']
        save(body, payload)
        try:
            az('rest', '--method', 'patch', '--url', resource['id'] + '?api-version=2025-01-01',
                '--body', '@' + str(body), *common)
        finally:
            body.unlink(missing_ok=True)
    def apply(selected, pending, label):
        apps, job = inventory()
        print(label + ': pausing recovery job first' if pending else label + ': updating paused recovery job', flush=True)
        # Activation of the scheduled job happens AFTER every app is ready.
        if pending:
            patch(job, target_template(job, selected, True, job=True), job=True)
        for app in apps:
            print(label + ': ' + app['name'], flush=True)
            patch(app, target_template(app, selected, pending, suffix='u' + state['upgradeId'][:12] + label))
        if not pending:
            # Keep job disabled until the app readiness verification below.
            return
    def wait_ready(selected, pending):
        deadline = time.monotonic() + 1200
        while time.monotonic() < deadline:
            apps, job = inventory()
            try:
                # While app activation is in progress the job remains paused.
                job_check = copy.deepcopy(job)
                if not pending:
                    job_check['properties']['template'] = target_template(job, selected, False, job=True)
                verify_inventory(apps, job_check, selected, pending)
                revisions = {app['name']: az('containerapp', 'revision', 'list', '-g', args.resource_group,
                    '-n', app['name'], *common) for app in apps}
                verify_revisions(apps, revisions)
                return apps, job
            except RuntimeError as error:
                print('Waiting: ' + str(error), flush=True)
                time.sleep(15)
        raise TimeoutError('Roles are not ready; deployment remains at its saved phase')
    def stop_old_jobs():
        executions = az('containerapp', 'job', 'execution', 'list', '-g', args.resource_group, '-n', job_name, *common)
        for execution in executions:
            if execution.get('properties', {}).get('status') in ('Running', 'Processing', 'Pending'):
                az('containerapp', 'job', 'stop', '-g', args.resource_group, '-n', job_name,
                    '--job-execution-name', execution['name'], *common)
    if state['phase'] == 'prepared':
        apps, job = inventory()
        verify_inventory(apps, job, previous, False)
        if any(app['properties']['configuration'].get('activeRevisionsMode') != 'Single' for app in apps):
            raise RuntimeError('Upgrade requires Single revision mode for every app')
        save(output / 'before-apps.json', apps); save(output / 'before-job.json', job)
        state['phase'] = 'pause'; save(statefile, state)
    if state['phase'] == 'pause':
        apply(previous, True, 'p'); wait_ready(previous, True); stop_old_jobs()
        state['phase'] = 'stage'; save(statefile, state)
    if state['phase'] == 'stage':
        apply(release, True, 's'); wait_ready(release, True); stop_old_jobs()
        state['phase'] = 'migrate'; save(statefile, state)
    if state['phase'] == 'migrate':
        apps, job = wait_ready(release, True)
        template = copy.deepcopy(job['properties']['template'])
        container = template['containers'][0]
        container['image'] = migrator['images']['backend']
        container['command'] = ['python', '-u', '-m', 'wave_upgrade', '--from-capability', previous['capabilityId'],
            '--upgrade-id', state['upgradeId'], '--release', json.dumps(release, separators=(',', ':'))]
        if args.rollback:
            container['command'].append('--rollback')
        # Job args override any inherited entrypoint arguments.
        container['args'] = []
        override = output / 'upgrade-job.yaml'; save(override, template)
        execution_name = state.get('execution')
        if execution_name:
            current = az('containerapp', 'job', 'execution', 'show', '-g', args.resource_group, '-n', job_name,
                '--job-execution-name', execution_name, *common)
            if current['properties']['status'] in ('Failed', 'Stopped', 'Degraded'):
                execution_name = None
        if not execution_name:
            execution = az('containerapp', 'job', 'start', '-g', args.resource_group, '-n', job_name,
                '--yaml', str(override), *common)
            execution_name = execution['name']; state['execution'] = execution_name; save(statefile, state)
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            current = az('containerapp', 'job', 'execution', 'show', '-g', args.resource_group, '-n', job_name,
                '--job-execution-name', execution_name, *common)
            status = current['properties']['status']
            if status == 'Succeeded':
                break
            if status in ('Failed', 'Stopped', 'Degraded'):
                raise RuntimeError('Upgrade job failed; keep roles paused, inspect logs, then --resume')
            time.sleep(10)
        else:
            raise TimeoutError('Upgrade job still pending; keep paused and --resume')
        state['phase'] = 'activate'; save(statefile, state)
    if state['phase'] == 'activate':
        apply(release, False, 'a'); apps, job = wait_ready(release, False)
        patch(job, target_template(job, release, False, job=True), job=True)
        apps, job = inventory(); verify_inventory(apps, job, release, False)
        # Give preserved releases an immediate recovery wake instead of waiting
        # for the next five-minute schedule. This is the ordinary recovery job.
        az('containerapp', 'job', 'start', '-g', args.resource_group, '-n', job_name, *common)
        state['phase'] = 'verified'; save(statefile, state)
    apps, job = inventory(); verify_inventory(apps, job, release, False)
    print(json.dumps(dict(verified=True, upgradeId=state['upgradeId'], capabilityId=release['capabilityId'],
        appUrl='https://' + next(app for app in apps if app['name'].endswith('-web'))['properties']['configuration']['ingress']['fqdn'])), flush=True)


if __name__ == '__main__':
    main()
