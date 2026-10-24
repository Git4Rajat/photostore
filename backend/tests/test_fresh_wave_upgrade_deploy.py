"""Exercise the full helper state machine against a fake ARM/job boundary."""
import copy
import json
from pathlib import Path
import sys

import pytest

from test_fresh_wave_deploy import inventory
from test_wave_upgrade import release
import upgrade_wave
from fresh_wave import verify_inventory


class Cloud:
    def __init__(self, old, new):
        self.apps, self.job = inventory(old, False)
        for resource in self.apps:
            resource['id'] = '/fake/apps/' + resource['name']
            resource['location'] = 'westeurope'
            resource['properties']['configuration'] = dict(activeRevisionsMode='Single', ingress=dict(fqdn='example.test'))
        self.job.update(id='/fake/jobs/test-wave-recovery', name='test-wave-recovery')
        self.events = []
        self.new = new
        self.statuses = {}
        self.fail_migration = False

    def az(self, *args):
        if args[:2] == ('rest', '--method'):
            method = args[2]
            url = args[args.index('--url') + 1]
            if method == 'get':
                return dict(value=copy.deepcopy(self.apps))
            assert method == 'patch'
            resource_id = url.split('?')[0]
            resource = next((app for app in self.apps if app['id'] == resource_id), self.job)
            body = json.loads(Path(args[args.index('--body') + 1][1:]).read_text())
            resource['properties']['template'] = body['properties']['template']
            if resource is not self.job:
                revision = resource['name'] + '--' + body['properties']['template']['revisionSuffix']
                resource['properties'].update(latestRevisionName=revision, latestReadyRevisionName=revision)
            self.events.append(('patch', resource.get('name', 'job')))
            return copy.deepcopy(resource)
        if args[:3] == ('containerapp', 'revision', 'list'):
            name = args[args.index('-n') + 1]
            app = next(app for app in self.apps if app['name'] == name)
            return [dict(name=app['properties']['latestReadyRevisionName'], properties=dict(active=True))]
        if args[:4] == ('containerapp', 'job', 'execution', 'list'):
            return []
        if args[:4] == ('containerapp', 'job', 'execution', 'show'):
            name = args[args.index('--job-execution-name') + 1]
            return dict(properties=dict(status=self.statuses[name]))
        if args[:3] == ('containerapp', 'job', 'show'):
            return copy.deepcopy(self.job)
        if args[:3] == ('containerapp', 'job', 'start'):
            name = 'execution-' + str(len(self.statuses))
            if '--yaml' in args:
                verify_inventory(self.apps, self.job, self.new, True)
                template = json.loads(Path(args[args.index('--yaml') + 1]).read_text())
                command = template['containers'][0]['command']
                assert command[:4] == ['python', '-u', '-m', 'wave_upgrade']
                assert json.loads(command[command.index('--release') + 1]) == self.new
                self.events.append(('migrate', name))
                self.statuses[name] = 'Failed' if self.fail_migration else 'Succeeded'
            else:
                verify_inventory(self.apps, self.job, self.new, False)
                self.events.append(('recover', name))
                self.statuses[name] = 'Succeeded'
            return dict(name=name)
        pytest.fail('Unexpected Azure call: ' + str(args))


@pytest.fixture
def plan(monkeypatch, tmp_path):
    root = tmp_path / 'repo'
    (root / 'deploy').mkdir(parents=True)
    monkeypatch.setattr(upgrade_wave, 'ROOT', root / 'deploy')
    old, new = release('a'), release('b')
    for name, contract in [('old', old), ('new', new)]:
        (tmp_path / (name + '.json')).write_text(json.dumps(contract))
    output = root / '.azure' / 'upgrade'
    argv = ['upgrade_wave', '--subscription', '12345678-1234-1234-1234-123456789abc',
        '--resource-group', 'test-rg', '--app-name', 'test', '--previous-release', str(tmp_path / 'old.json'),
        '--release', str(tmp_path / 'new.json'), '--output-dir', str(output)]
    cloud = Cloud(old, new)
    monkeypatch.setattr(upgrade_wave, 'az', cloud.az)
    monkeypatch.setattr(sys, 'argv', argv)
    upgrade_wave.main()
    assert not cloud.events
    assert (output / 'state.json').stat().st_mode & 0o777 == 0o600
    monkeypatch.setattr(sys, 'argv', argv + ['--deploy', '--resume'])
    return cloud, output, argv


def test_binding_migration_occurs_only_after_both_paused_inventory_checks(plan):
    cloud, output, _ = plan
    upgrade_wave.main()
    assert json.loads((output / 'state.json').read_text())['phase'] == 'verified'
    migration_index = next(i for i, event in enumerate(cloud.events) if event[0] == 'migrate')
    # Both pauses patch recovery + all eight apps before the binding job.
    assert migration_index == 18
    assert cloud.events[-1][0] == 'recover'
    assert (output / 'before-apps.json').stat().st_mode & 0o777 == 0o600


def test_failed_migration_never_activates_and_resume_does_not_repeat_completed_rollouts(plan):
    cloud, output, _ = plan
    cloud.fail_migration = True
    with pytest.raises(RuntimeError, match='keep roles paused'):
        upgrade_wave.main()
    assert json.loads((output / 'state.json').read_text())['phase'] == 'migrate'
    verify_inventory(cloud.apps, cloud.job, cloud.new, True)
    assert not any(event[0] == 'recover' for event in cloud.events)
    patches = sum(event[0] == 'patch' for event in cloud.events)
    cloud.fail_migration = False
    upgrade_wave.main()
    # Resume migrates then activates each app and finally the recovery job.
    assert sum(event[0] == 'patch' for event in cloud.events) == patches + 9
    assert json.loads((output / 'state.json').read_text())['phase'] == 'verified'


def test_changed_release_on_resume_makes_no_cloud_mutations(plan, monkeypatch):
    cloud, output, argv = plan
    path = Path(argv[argv.index('--release') + 1])
    path.write_text(json.dumps(release('c')))
    with pytest.raises(ValueError, match='Resume target'):
        upgrade_wave.main()
    assert not cloud.events
