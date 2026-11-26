"""The vision model thread count is one deployment parameter, so a live-tested value survives template deploys."""
import json
from pathlib import Path

ROOT = Path(__file__).parents[2]


def _vision_env():
    template = json.loads((ROOT / 'deploy' / 'azuredeploy.json').read_text())
    deployment = next(r for r in template['resources'] if r['name'] == 'photostore-resources')
    module = deployment['properties']['template']
    return template, deployment, module, json.dumps(module)


def test_parameter_defaults_to_one_everywhere():
    template, deployment, module, _ = _vision_env()
    assert template['parameters']['visionModelThreads']['defaultValue'] == 1
    assert module['parameters']['visionModelThreads']['defaultValue'] == 1
    assert deployment['properties']['parameters']['visionModelThreads']['value'] == "[parameters('visionModelThreads')]"


def test_vision_app_sets_model_openmp_and_blas_threads_from_it():
    _, _, _, text = _vision_env()
    for name in ('IPWORK_MODEL_THREADS', 'OMP_THREAD_LIMIT', 'OPENBLAS_NUM_THREADS'):
        assert name in text
    assert text.count("string(parameters('visionModelThreads'))") >= 3


def test_bicep_sources_declare_it():
    for filename in ('main.bicep', 'resources.bicep'):
        assert 'param visionModelThreads int = 1' in (ROOT / 'deploy' / filename).read_text()
