import importlib.util
import json
from pathlib import Path
import pytest

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


JSON = ('"waveCapabilityId": {{\n      "type": "string",\n      "defaultValue": "{cap}",\n    }},\n'
        '"waveCompatibleCapabilities": {{\n      "type": "string",\n      "defaultValue": "{compat}",\n    }},\n'
        '"waveSourceCommit": {{\n      "type": "string",\n      "defaultValue": "",\n    }},\n')
RELEASE = {'images': IMAGES, 'capabilityId': CAP, 'sourceCommit': COMMIT, 'protocol': 'library-wave-v1',
           'upgradeProtocol': 'library-wave-v1'}


def templates(bicep_compat=OLDER, json_compat=OLDER):
    return {'main.bicep': BICEP.format(OLD, bicep_compat), 'resources.bicep': BICEP.format(OLD, bicep_compat),
            'azuredeploy.json': JSON.format(cap=OLD, compat=json_compat)}


def test_the_compiled_template_gets_the_predecessor_list_too():
    # Regression: the compiled template (what the Deploy to Azure button deploys) used to be rewritten with an
    # empty predecessor list because only Bicep syntax was parsed, so the library never migrated.
    text = pin_images.pin(JSON.format(cap=OLD, compat=OLDER), IMAGES, CAP, COMMIT, upgradable=True)
    assert pin_images.declared(text, 'compatible') == [f'{OLD},{OLDER}']
    assert pin_images.declared(text, 'capability') == [CAP]


def test_every_template_gets_the_same_chain_from_the_bicep_source():
    pinned = pin_images.pin_all(templates(json_compat=''), RELEASE)
    for name, text in pinned.items():
        assert pin_images.declared(text, 'compatible') == [f'{OLD},{OLDER}'], name
        assert pin_images.declared(text, 'capability') == [CAP], name
        assert pin_images.declared(text, 'commit') == [COMMIT], name


def test_inconsistent_templates_fail_the_pin_instead_of_being_committed():
    stale = templates(json_compat='')
    not_upgradable = {**RELEASE, 'upgradeProtocol': None}
    with pytest.raises(ValueError, match='templates disagree'):
        pin_images.pin_all(stale, not_upgradable)
    with pytest.raises(ValueError, match='not declared'):
        pin_images.check_consistent({'main.bicep': BICEP.format(OLD, OLDER), 'azuredeploy.json': '{}'})


def test_checked_in_templates_agree_and_list_a_predecessor():
    texts = {name: (ROOT / 'deploy' / name).read_text() for name in pin_images.TARGETS}
    pin_images.check_consistent(texts)
    capability = pin_images.declared(texts['main.bicep'], 'capability')[0]
    compatible = pin_images.declared(texts['main.bicep'], 'compatible')[0].split(',')
    assert compatible != [''] and capability not in compatible
    for text in texts.values():
        assert pin_images.declared(text, 'compatible') == [','.join(compatible)] * len(pin_images.declared(text, 'compatible'))
