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


OLD, OLDER, COMMIT = 'c' * 64, 'd' * 64, 'e' * 40
BICEP = ("param waveCapabilityId string = '{}'\nparam waveCompatibleCapabilities string = '{}'\n"
         "param waveSourceCommit string = ''\n")


def test_an_upgradable_release_lists_the_previous_certificates_it_replaces():
    text = pin_images.pin(BICEP.format(OLD, OLDER), IMAGES, CAP, COMMIT, upgradable=True)
    assert f"waveCompatibleCapabilities string = '{OLD},{OLDER}'" in text
    assert f"waveSourceCommit string = '{COMMIT}'" in text
    assert f"waveCapabilityId string = '{CAP}'" in text
    # re-pinning the same release never lists a certificate as compatible with itself
    again = pin_images.pin(text, IMAGES, CAP, COMMIT, upgradable=True)
    assert f"waveCompatibleCapabilities string = '{OLD},{OLDER}'" in again


def test_a_release_not_qualified_for_upgrades_keeps_the_compatible_list():
    text = pin_images.pin(BICEP.format(OLD, OLDER), IMAGES, CAP, COMMIT, upgradable=False)
    assert f"waveCompatibleCapabilities string = '{OLDER}'" in text


def test_compatible_list_is_capped():
    ids = ','.join(format(i, 'x') * 64 for i in range(1, 15))
    text = pin_images.pin(BICEP.format(OLD, ids), IMAGES, CAP, COMMIT, upgradable=True)
    listed = text.split("waveCompatibleCapabilities string = '")[1].split("'")[0].split(',')
    assert len(listed) == pin_images.MAX_COMPATIBLE and listed[0] == OLD
