"""Regression coverage for the checked-in compiled deployment entry point."""
import json
from pathlib import Path


def test_compiled_worker_scales_to_zero_by_default():
    template = json.loads((Path(__file__).parents[2] / 'deploy' / 'azuredeploy.json').read_text())
    parameter = template['parameters']['clusterMinReplicas']
    assert parameter['defaultValue'] == 0
    assert (parameter['minValue'], parameter['maxValue']) == (0, 0)
    deployment = next(r for r in template['resources']
                      if r['type'] == 'Microsoft.Resources/deployments')
    assert deployment['properties']['parameters']['clusterMinReplicas']['value'] == (
        "[parameters('clusterMinReplicas')]")
    module = deployment['properties']['template']
    assert module['parameters']['clusterMinReplicas']['defaultValue'] == 0
    worker = next(r for r in module['resources']
                  if r.get('name') == "[variables('clusterAppName')]")
    scale = worker['properties']['template']['scale']
    assert scale['minReplicas'] == "[parameters('clusterMinReplicas')]"
    assert scale['maxReplicas'] == 1


def test_bicep_worker_floor_matches_compiled_default():
    root = Path(__file__).parents[2]
    for filename in ('main.bicep', 'resources.bicep'):
        assert 'param clusterMinReplicas int = 0' in (root / 'deploy' / filename).read_text()


def test_dedicated_index_queue_wakes_an_isolated_single_publisher_sidecar():
    template = json.loads((Path(__file__).parents[2] / 'deploy' / 'azuredeploy.json').read_text())
    assert template['parameters']['dedicatedIndexWorker']['defaultValue'] is True
    assert template['parameters']['dedicatedIndexWorker']['allowedValues'] == [True]
    module = next(r for r in template['resources'] if r['type'] == 'Microsoft.Resources/deployments')['properties']['template']
    indexer = next(r for r in module['resources'] if r.get('name') == "[variables('indexerAppName')]")
    spec = indexer['properties']['template']
    assert spec['terminationGracePeriodSeconds'] == 180
    assert spec['scale']['maxReplicas'] == "[if(parameters('dedicatedIndexWorker'), 1, 2)]"
    containers = spec['containers']
    assert "'index-maintenance'" in containers and "'index-worker'" in containers
    assert "'image', parameters('coreImage')" in containers
    rules = spec['scale']['rules']
    assert "'azure-queue'" in rules and "'photostore-index-maintenance'" in rules
    assert "'identity', 'system'" in rules and "'queueLengthStrategy', 'all'" in rules


def test_all_container_apps_allow_scale_to_zero_by_default():
    template = json.loads((Path(__file__).parents[2] / 'deploy' / 'azuredeploy.json').read_text())
    deployment = next(r for r in template['resources']
                      if r['type'] == 'Microsoft.Resources/deployments')
    module = deployment['properties']['template']
    apps = [r for r in module['resources'] if r['type'].lower() == 'microsoft.app/containerapps']
    assert len(apps) == 8
    for app in apps:
        minimum = app['properties']['template']['scale']['minReplicas']
        if minimum == "[parameters('clusterMinReplicas')]":
            minimum = template['parameters']['clusterMinReplicas']['defaultValue']
        assert minimum == 0, app['name']


def test_every_queue_scaler_counts_claimed_work_until_ack():
    """A single invisible long-running message must keep its worker active."""
    root = Path(__file__).parents[2]
    template = json.loads((root / 'deploy' / 'azuredeploy.json').read_text())
    module = next(r for r in template['resources']
                  if r['type'] == 'Microsoft.Resources/deployments')['properties']['template']
    seen = set()
    for app in module['resources']:
        if app['type'].lower() != 'microsoft.app/containerapps':
            continue
        rules = app['properties']['template']['scale'].get('rules', [])
        if isinstance(rules, str):
            # The indexer combines HTTP and conditional queue rules in ARM.
            assert app['name'] == "[variables('indexerAppName')]"
            assert "'queueLengthStrategy', 'all'" in rules
            assert 'visibleonly' not in rules
            seen.add('photostore-index-maintenance')
        else:
            for rule in rules:
                custom = rule.get('custom', {})
                if custom.get('type') == 'azure-queue':
                    assert custom['metadata']['queueLengthStrategy'] == 'all', rule['name']
                    assert custom['identity'] == 'system'
                    seen.add(custom['metadata']['queueName'])
    assert seen == {'photostore-ipwork', 'photostore-clustering',
                    'photostore-library-ops', 'photostore-index-maintenance'}
    assert 'visibleonly' not in (root / 'deploy' / 'resources.bicep').read_text()
