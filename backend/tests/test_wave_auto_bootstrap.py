import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
pytest.importorskip('azure.core.exceptions')   # runs in the backend image, where the Azure SDK is installed

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
    monkeypatch.setattr(wave_bootstrap, 'seed', lambda svc, d, exp, **kw: seen.update(exp=exp) or exp)
    assert wave_bootstrap.auto_seed(Service(), e) == EXPECTED == seen['exp']


def test_every_role_derives_the_same_certificate_from_the_shared_images():
    import wave_policy
    core_env, vision_env = env(UPLOAD_WAVE_DEPLOYMENT_ID=CORE), env(UPLOAD_WAVE_DEPLOYMENT_ID=VISION)
    assert wave_policy.capability_id(core_env) == wave_policy.capability_id(vision_env) == EXPECTED


def test_an_explicit_capability_is_never_overridden():
    import wave_policy
    assert wave_policy.capability_id(env(UPLOAD_WAVE_CAPABILITY_ID='d' * 64)) == 'd' * 64
    assert wave_policy.capability_id(env(UPLOAD_WAVE_AUTO_BOOTSTRAP='false')) == ''


def test_auto_seed_tolerates_an_empty_library_row_but_the_manual_seed_does_not():
    import json

    class Table:
        def __init__(self, rows): self.rows = rows
        def query_entities(self, query, results_per_page=1): return iter(self.rows)
        def get_entity(self, pk, rk): raise wave_bootstrap.ResourceNotFoundError()
        def create_entity(self, row): pass

    class Service:
        tables = {'photolibraries': [{'PartitionKey': 'owner'}], 'photometadata': [], 'photojobs': []}
        def get_table_client(self, name): return Table(self.tables.get(name, []))
        def create_table_if_not_exists(self, name): return Table([])

    deployments = wave_bootstrap.deployment_contract(CORE, VISION)
    with pytest.raises(RuntimeError):
        wave_bootstrap.seed(Service(), deployments, EXPECTED)
    Service.tables['photometadata'] = [{'PartitionKey': 'owner'}]
    with pytest.raises(RuntimeError):
        wave_bootstrap.seed(Service(), deployments, EXPECTED, allow_empty_libraries=True)


PREVIOUS = 'f' * 64
COMMIT = '1' * 40


def _upgrade_env(**extra):
    return env(UPLOAD_WAVE_COMPATIBLE_CAPABILITIES=PREVIOUS, UPLOAD_WAVE_SOURCE_COMMIT=COMMIT, **extra)


def _service():
    class Service:
        def get_table_client(self, name):
            if name == 'photojobs':
                class Table:
                    def get_entity(self, pk, rk):
                        raise wave_bootstrap.ResourceNotFoundError()
                return Table()
            return object()
    return Service()


def _patch_bound(monkeypatch, bound):
    import wave_upgrade
    monkeypatch.setattr(wave_upgrade, 'library_policies', lambda table: iter([{'capabilityId': c} for c in bound]))
    calls = []
    monkeypatch.setattr(wave_upgrade, 'migrate', lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(wave_bootstrap, 'seed', lambda *a, **k: pytest.fail('must not reseed over a library'))
    return calls


def test_library_bound_to_a_listed_predecessor_is_migrated_in_place(monkeypatch):
    calls = _patch_bound(monkeypatch, [PREVIOUS])
    assert wave_bootstrap.auto_seed(_service(), _upgrade_env()) == EXPECTED
    (args, kwargs), = calls
    assert kwargs['previous'] == PREVIOUS and kwargs['target'] == EXPECTED and kwargs['source_commit'] == COMMIT
    assert kwargs['upgrade_id'] == 'auto-' + PREVIOUS[:16] + '-' + EXPECTED[:16]


def test_unlisted_or_mixed_bindings_are_never_migrated(monkeypatch):
    for bound in (['9' * 64], [PREVIOUS, '9' * 64], []):
        calls = _patch_bound(monkeypatch, bound)
        monkeypatch.setattr(wave_bootstrap, 'seed', lambda *a, **k: 'seeded')
        assert wave_bootstrap.auto_seed(_service(), _upgrade_env()) == 'seeded'
        assert calls == []


def test_without_predecessors_or_commit_nothing_is_migrated(monkeypatch):
    calls = _patch_bound(monkeypatch, [PREVIOUS])
    monkeypatch.setattr(wave_bootstrap, 'seed', lambda *a, **k: 'seeded')
    assert wave_bootstrap.auto_seed(_service(), env()) == 'seeded'
    assert wave_bootstrap.auto_seed(_service(), env(UPLOAD_WAVE_COMPATIBLE_CAPABILITIES=PREVIOUS)) == 'seeded'
    assert calls == []


def test_an_unlisted_library_binding_is_reported_with_the_fix(monkeypatch):
    _patch_bound(monkeypatch, ['9' * 64])

    def refuse(*a, **k):
        raise RuntimeError('Fresh wave bootstrap refused: existing library data or ownership')
    monkeypatch.setattr(wave_bootstrap, 'seed', refuse)
    with pytest.raises(RuntimeError) as info:
        wave_bootstrap.auto_seed(_service(), _upgrade_env())
    message = str(info.value)
    assert 'refused' in message and 'UPLOAD_WAVE_COMPATIBLE_CAPABILITIES' in message and EXPECTED in message
    assert 'upgrade_wave.py' in message
