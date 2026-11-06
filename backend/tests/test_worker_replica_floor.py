"""Regression coverage for the checked-in compiled deployment entry point."""
import json
from pathlib import Path


def test_core_can_negotiate_index_push_without_waking_indexer():
    template = json.loads((Path(__file__).parents[2] / 'deploy' / 'azuredeploy.json').read_text())
    module = next(r for r in template['resources']
                  if r['type'] == 'Microsoft.Resources/deployments')['properties']['template']
    grant = next(r for r in module['resources']
                 if r.get('copy', {}).get('name') == 'indexPushRoles')
    # The dedicated index-worker app also receives the push grant, but only
    # when it is actually deployed (see the conditional on the role itself).
    assert "createArray('indexer', 'cluster', 'core', 'index-worker')" in grant['copy']['count']
    assert "variables('coreAppName')" in grant['properties']['principalId']
    assert "[resourceId('Microsoft.App/containerApps', variables('coreAppName'))]" in grant['dependsOn']


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
    # By default the index queue is drained by the separate index-worker app;
    # the indexer only keeps the queue rules when no separate worker exists.
    rules = spec['scale']['rules']
    assert "variables('indexMaintenanceScaleRules')" in rules
    assert "not(parameters('separateIndexWorker'))" in rules
    worker = next(r for r in module['resources'] if r.get('name') == "[variables('indexWorkerAppName')]")
    assert worker['condition'] == "[and(parameters('dedicatedIndexWorker'), parameters('separateIndexWorker'))]"
    worker_spec = worker['properties']['template']
    assert worker_spec['scale']['maxReplicas'] == 1 and worker_spec['scale']['minReplicas'] == 0
    assert worker_spec['scale']['rules'] == "[variables('indexMaintenanceScaleRules')]"
    queue_rules = module['variables']['indexMaintenanceScaleRules']
    by_queue = {r['custom']['metadata']['queueName']: r['custom'] for r in queue_rules}
    assert set(by_queue) == {'photostore-index-maintenance', 'photostore-index-maintenance-deferred'}
    assert all(c['type'] == 'azure-queue' and c['identity'] == 'system' for c in by_queue.values())
    assert by_queue['photostore-index-maintenance']['metadata']['queueLengthStrategy'] == 'all'


def test_all_container_apps_allow_scale_to_zero_by_default():
    template = json.loads((Path(__file__).parents[2] / 'deploy' / 'azuredeploy.json').read_text())
    deployment = next(r for r in template['resources']
                      if r['type'] == 'Microsoft.Resources/deployments')
    module = deployment['properties']['template']
    apps = [r for r in module['resources'] if r['type'].lower() == 'microsoft.app/containerapps']
    assert len(apps) == 9
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
    # Deferred queues hold delayed retries that are invisible by design, so
    # they must not keep a worker awake; every other queue counts claimed work.

    def check_queue_rule(rule):
        custom = rule.get('custom', {})
        if custom.get('type') != 'azure-queue':
            return
        queue = custom['metadata']['queueName']
        assert custom['metadata']['queueLengthStrategy'] == (
            'visibleonly' if queue.endswith('-deferred') else 'all'), rule['name']
        assert custom['identity'] == 'system'
        seen.add(queue)

    for app in module['resources']:
        if app['type'].lower() != 'microsoft.app/containerapps':
            continue
        rules = app['properties']['template']['scale'].get('rules', [])
        if isinstance(rules, str):
            # The index apps take their queue rules from a shared variable
            # (the indexer only when no separate index-worker is deployed).
            assert app['name'] in ("[variables('indexerAppName')]", "[variables('indexWorkerAppName')]")
            assert 'indexMaintenanceScaleRules' in rules
        else:
            for rule in rules:
                check_queue_rule(rule)
    for rule in module['variables']['indexMaintenanceScaleRules']:
        check_queue_rule(rule)
    assert seen == {'photostore-ipwork', 'photostore-clustering',
                    'photostore-library-ops', 'photostore-index-maintenance',
                    'photostore-index-maintenance-deferred', 'photostore-ipwork-deferred',
                    'photostore-clustering-deferred'}


def test_upload_expiry_has_no_queue_resource_scaler_or_dependency():
    root = Path(__file__).parents[2]
    for filename in ('resources.bicep', 'azuredeploy.json'):
        template = (root / 'deploy' / filename).read_text()
        assert 'upload-expiry' not in template
        assert 'uploadExpiry' not in template
