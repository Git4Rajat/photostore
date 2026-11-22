"""The upload app's scale limits are deployment parameters, not literals."""
import json
from pathlib import Path

ROOT = Path(__file__).parents[2]


def _upload_scale():
    template = json.loads((ROOT / 'deploy' / 'azuredeploy.json').read_text())
    deployment = next(r for r in template['resources']
                      if r['type'] == 'Microsoft.Resources/deployments'
                      and r['name'] == 'photostore-resources')
    module = deployment['properties']['template']
    upload = next(r for r in module['resources']
                  if r.get('name') == "[variables('uploadAppName')]")
    return template, deployment, module, upload['properties']['template']['scale']


def test_upload_scale_uses_parameters_with_live_defaults():
    template, deployment, module, scale = _upload_scale()
    assert scale['maxReplicas'] == "[parameters('uploadMaxReplicas')]"
    rule = scale['rules'][0]['http']['metadata']
    assert rule['concurrentRequests'] == "[string(parameters('uploadConcurrentRequests'))]"
    for name, default in (('uploadConcurrentRequests', 32), ('uploadMaxReplicas', 2)):
        assert template['parameters'][name]['defaultValue'] == default
        assert module['parameters'][name]['defaultValue'] == default
        assert deployment['properties']['parameters'][name]['value'] == f"[parameters('{name}')]"
    assert scale['minReplicas'] == 0


def test_bicep_sources_declare_the_upload_scale_parameters():
    for filename in ('main.bicep', 'resources.bicep'):
        text = (ROOT / 'deploy' / filename).read_text()
        assert 'param uploadConcurrentRequests int = 32' in text
        assert 'param uploadMaxReplicas int = 2' in text
