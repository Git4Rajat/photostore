"""Regression coverage for the checked-in compiled deployment entry point."""
import json
from pathlib import Path


def test_compiled_worker_keeps_one_replica_by_default():
    template = json.loads((Path(__file__).parents[2] / 'deploy' / 'azuredeploy.json').read_text())
    parameter = template['parameters']['workerMinReplicas']
    assert parameter['defaultValue'] == 1
    assert (parameter['minValue'], parameter['maxValue']) == (0, 1)
    deployment = next(r for r in template['resources']
                      if r['type'] == 'Microsoft.Resources/deployments')
    assert deployment['properties']['parameters']['workerMinReplicas']['value'] == (
        "[parameters('workerMinReplicas')]")
    module = deployment['properties']['template']
    assert module['parameters']['workerMinReplicas']['defaultValue'] == 1
    worker = next(r for r in module['resources']
                  if r.get('name') == "[variables('workerAppName')]")
    scale = worker['properties']['template']['scale']
    assert scale['minReplicas'] == "[parameters('workerMinReplicas')]"
    assert scale['maxReplicas'] == 1