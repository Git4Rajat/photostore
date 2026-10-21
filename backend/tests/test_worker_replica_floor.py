"""Regression coverage for the checked-in compiled deployment entry point."""
import json
from pathlib import Path


def test_compiled_worker_scales_to_zero_by_default():
    template = json.loads((Path(__file__).parents[2] / 'deploy' / 'azuredeploy.json').read_text())
    parameter = template['parameters']['clusterMinReplicas']
    assert parameter['defaultValue'] == 0
    assert (parameter['minValue'], parameter['maxValue']) == (0, 1)
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
    assert "'identity', 'system'" in rules and "'queueLengthStrategy', 'visibleonly'" in rules


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
