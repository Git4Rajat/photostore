import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('pin_images', ROOT / 'deploy' / 'pin_images.py')
pin_images = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pin_images)

IMAGES = {name: f'ghcr.io/o/photostore-{name}@sha256:' + str(i) * 64 for i, name in enumerate(('backend', 'frontend', 'ipworker'))}
CAP = 'a' * 64


def test_capability_default_is_pinned_in_bicep_and_compiled_template():
    bicep = "param waveCapabilityId string = ''\n"
    compiled = '"waveCapabilityId": {\n      "type": "string",\n      "defaultValue": "",\n'
    assert pin_images.pin(bicep, IMAGES, CAP) == f"param waveCapabilityId string = '{CAP}'\n"
    assert f'"defaultValue": "{CAP}"' in pin_images.pin(compiled, IMAGES, CAP)
    # re-pinning replaces the previous value rather than stacking
    assert pin_images.pin(pin_images.pin(bicep, IMAGES, CAP), IMAGES, 'b' * 64).count('b' * 64) == 1


def test_without_a_capability_only_images_change():
    assert pin_images.pin("param waveCapabilityId string = ''\n", IMAGES) == "param waveCapabilityId string = ''\n"


def test_checked_in_templates_still_match_the_pin_patterns():
    for name in ('main.bicep', 'resources.bicep', 'azuredeploy.json'):
        text = (ROOT / 'deploy' / name).read_text()
        assert pin_images.pin(text, IMAGES, CAP).count(CAP) >= 1, name
