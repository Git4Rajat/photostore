import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
azure_core = types.ModuleType('azure.core.exceptions')
class _NF(Exception): pass
class _HR(Exception): pass
class _EX(Exception): pass
azure_core.ResourceNotFoundError, azure_core.HttpResponseError, azure_core.ResourceExistsError = _NF, _HR, _EX
for name in ('azure', 'azure.core'):
    sys.modules.setdefault(name, types.ModuleType(name))
sys.modules['azure.core.exceptions'] = azure_core

import wave_bootstrap  # noqa: E402
from wave_policy import PROTOCOL  # noqa: E402
from upload_waves import digest  # noqa: E402

CORE = 'ghcr.io/o/photostore-backend@sha256:' + 'a' * 64
VISION = 'ghcr.io/o/photostore-ipworker@sha256:' + 'b' * 64


EXPECTED = digest([PROTOCOL, wave_bootstrap.deployment_contract(CORE, VISION)])


def env(**extra):
    base = {'UPLOAD_WAVE_AUTO_BOOTSTRAP': 'true', 'UPLOAD_WAVE_CORE_IMAGE': CORE, 'UPLOAD_WAVE_VISION_IMAGE': VISION}
    base['UPLOAD_WAVE_CAPABILITY_ID'] = ''
    base.update(extra)
    return base


def test_does_nothing_unless_the_template_asks_for_it():
    assert wave_bootstrap.auto_seed(object(), env(UPLOAD_WAVE_AUTO_BOOTSTRAP='false')) == ''


def test_refuses_a_capability_that_does_not_match_the_pinned_images():
    with pytest.raises(ValueError):
        wave_bootstrap.auto_seed(object(), env(UPLOAD_WAVE_CAPABILITY_ID='c' * 64))


def test_existing_matching_certificate_is_a_noop(monkeypatch):
    e = env()
    deployments = wave_bootstrap.deployment_contract(CORE, VISION)

    class Table:
        def get_entity(self, pk, rk):
            return {'protocol': PROTOCOL, 'deployments': __import__('json').dumps(deployments)}

    class Service:
        def get_table_client(self, name):
            return Table()

    monkeypatch.setattr(wave_bootstrap, 'seed', lambda *a: pytest.fail('must not reseed'))
    assert wave_bootstrap.auto_seed(Service(), e) == EXPECTED


def test_missing_certificate_seeds_on_fresh_storage(monkeypatch):
    e = env()

    class Table:
        def get_entity(self, pk, rk):
            raise wave_bootstrap.ResourceNotFoundError()

    class Service:
        def get_table_client(self, name):
            return Table()

    seen = {}
    monkeypatch.setattr(wave_bootstrap, 'seed', lambda svc, d, exp: seen.update(exp=exp) or exp)
    assert wave_bootstrap.auto_seed(Service(), e) == EXPECTED == seen['exp']


def test_every_role_derives_the_same_certificate_from_the_shared_images():
    import wave_policy
    core_env, vision_env = env(UPLOAD_WAVE_DEPLOYMENT_ID=CORE), env(UPLOAD_WAVE_DEPLOYMENT_ID=VISION)
    assert wave_policy.capability_id(core_env) == wave_policy.capability_id(vision_env) == EXPECTED


def test_an_explicit_capability_is_never_overridden():
    import wave_policy
    assert wave_policy.capability_id(env(UPLOAD_WAVE_CAPABILITY_ID='d' * 64)) == 'd' * 64
    assert wave_policy.capability_id(env(UPLOAD_WAVE_AUTO_BOOTSTRAP='false')) == ''
