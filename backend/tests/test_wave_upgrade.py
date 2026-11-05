"""Qualified, paused upgrades preserve in-flight work and fail closed on races."""
import copy
import json
import sys
from pathlib import Path

import pytest
from azure.core import MatchConditions
from azure.core.exceptions import ResourceModifiedError

from test_wave_recovery import RuntimeTable
from test_upload_waves import close
from upload_waves import ShadowWaves, Conflict
from wave_policy import PROTOCOL, certify, require_capability
from wave_upgrade import migrate, main

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'deploy'))
from wave_release import release_contract
from upgrade_wave import target_template, verify_revisions


def release(char):
    result = release_contract({name: 'ghcr.io/test/' + name + '@sha256:' + char * 64
        for name in ('backend', 'frontend', 'ipworker')}, char * 40)
    return dict(result, qualification='wave-unit-tests-passed', upgradeProtocol=PROTOCOL)


class UpgradeTable(RuntimeTable):
    def query_entities(self, query, **options):
        if query == "RowKey eq 'policy'":
            return [self.get_entity(*key) for key in sorted(self.rows) if key[1] == 'policy']
        return super().query_entities(query, **options)


@pytest.fixture
def world():
    table, metadata = UpgradeTable(), RuntimeTable()
    old, new = release('a'), release('b')
    certify(table, old['deployments'])
    for library in ('one', 'two'):
        store = ShadowWaves(table, library, clock=lambda: 1000)
        table.create_entity(store._row('policy', protocol=PROTOCOL, capabilityId=old['capabilityId'],
            activationId='original-activation', cleanupState=''))
        opened = store.open_session('upload', 1)
        store.register('upload', [dict(itemId='i', filename='photo.raw')])
        resolved = store.resolve('upload', 'i', outcome='accepted', photo_id='photo',
            filename='photo.raw', processing_revision='r1', required_steps=['preview'])
        close(store, 'upload', resolved)
        metadata.create_entity(dict(PartitionKey=library, RowKey='photo.raw',
            anonymousImageId='photo', processingRevision='r1', preview_status='pending'))
    return table, metadata, old, new


def upgrade(world, **changes):
    table, metadata, old, new = world
    args = dict(previous=old['capabilityId'], target=new['capabilityId'], upgrade_id='upgrade-1',
        source_commit=new['sourceCommit'], clock=lambda: 1000)
    args.update(changes)
    return migrate(table, metadata, new['deployments'], **args)


def test_preserves_sources_and_all_wave_rows_with_atomic_audited_binding(world):
    table, metadata, old, new = world
    original = copy.deepcopy(table.rows)
    sources = copy.deepcopy(metadata.rows)
    result = upgrade(world)
    assert result['libraries'] == 2
    assert metadata.rows == sources
    for key, row in original.items():
        if key[1] != 'policy':
            assert table.rows[key] == row
    for library in ('one', 'two'):
        store = ShadowWaves(table, library)
        policy = store._required('policy')
        assert policy['capabilityId'] == new['capabilityId']
        assert policy['activationId'] == 'original-activation'
        assert store._required('upgrade!upgrade-1')['fromCapability'] == old['capabilityId']
    assert upgrade(world)['libraries'] == 2
    with pytest.raises(Conflict, match='different migration'):
        upgrade(world, source_commit='c' * 40)


@pytest.mark.parametrize('kind', ['stage', 'source', 'dispatcher', 'cleanup', 'unknown-expiry'])
def test_any_busy_library_blocks_binding_changes_for_every_library(world, kind):
    table, metadata, old, new = world
    store = ShadowWaves(table, 'two')
    if kind == 'stage':
        table.create_entity(store._row('two!wave!vision', leaseUntil=1001))
    elif kind == 'dispatcher':
        from upload_wave_execution import stage_work_store
        work = stage_work_store(table, clock=lambda: 1000)
        key = work.request('two', 'wave-vision:w:release')
        work.claim(key)
    elif kind == 'cleanup':
        row = store._required('policy')
        table.update_entity(dict(row, cleanupState='cleaning'), etag=row.metadata['etag'], match_condition=MatchConditions.IfNotModified)
    else:
        row = metadata.get_entity('two', 'photo.raw')
        metadata.update_entity(dict(row, processing_lease_owner='old-worker',
            processing_lease_expires_at='' if kind == 'unknown-expiry' else '2099-01-01T00:00:00+00:00'),
            etag=row.metadata['etag'], match_condition=MatchConditions.IfNotModified)
    before = copy.deepcopy(table.rows)
    with pytest.raises(Conflict):
        upgrade(world)
    assert table.rows == before


def test_expired_source_and_stage_leases_allow_upgrade(world):
    table, metadata, *_ = world
    table.create_entity(ShadowWaves(table, 'one')._row('one!wave!vision', leaseUntil=999))
    row = metadata.get_entity('one', 'photo.raw')
    metadata.update_entity(dict(row, processing_lease_owner='dead-worker',
        processing_lease_expires_at='1970-01-01T00:01:00+00:00'), etag=row.metadata['etag'], match_condition=MatchConditions.IfNotModified)
    assert upgrade(world)['libraries'] == 2


def test_upgrade_retries_failed_dispatch_without_erasing_source_attempts(world):
    from upload_wave_execution import stage_work_store
    from upload_waves import digest
    table, _, old, new = world
    store = ShadowWaves(table, 'one')
    wave = store._page('wave!')[0]
    scope = f"wave-vision:{wave['waveId']}:{digest(wave['visionPendingKey'])}"
    work = stage_work_store(table, clock=lambda: 1000)
    key = work.request('one', scope)
    claim = work.claim(key)
    work.finish(claim, failed=True)
    source_counter = store._row('source-attempt!keep', count=5)
    table.create_entity(source_counter)
    revision = work._get(key)['revision']
    upgrade(world)
    assert not work._get(key)['failed'] and work._get(key)['revision'] != revision
    assert work.claim(key).attempt == 1
    assert store._required('source-attempt!keep')['count'] == 5


def test_foreign_library_binding_is_rejected_before_any_policy_changes(world):
    table, _, old, new = world
    store = ShadowWaves(table, 'two')
    row = store._required('policy')
    table.update_entity(dict(row, capabilityId='c' * 64), etag=row.metadata['etag'],
        match_condition=MatchConditions.IfNotModified)
    before = copy.deepcopy(table.rows)
    with pytest.raises(Conflict, match='unexpected release'):
        upgrade(world)
    assert table.rows == before


def test_changed_certificate_cannot_qualify_an_upgrade(world):
    table, _, old, new = world
    broken = dict(new['deployments'], vision=old['images']['ipworker'])
    with pytest.raises(ValueError, match='exact'):
        migrate(table, world[1], broken, previous=old['capabilityId'], target=new['capabilityId'],
            upgrade_id='broken', source_commit=new['sourceCommit'])
    assert ShadowWaves(table, 'one')._required('policy')['capabilityId'] == old['capabilityId']


def test_partial_migration_replays_without_rewriting_completed_library(world, monkeypatch):
    table, metadata, old, new = world
    submit = table.submit_transaction
    def crash(operations):
        if any(op[1]['RowKey'] == 'upgrade!upgrade-1' and op[1].get('libraryId') == 'two' for op in operations):
            raise OSError('interrupted')
        return submit(operations)
    monkeypatch.setattr(table, 'submit_transaction', crash)
    with pytest.raises(OSError):
        upgrade(world)
    first = ShadowWaves(table, 'one')._required('policy')
    assert first['capabilityId'] == new['capabilityId']
    assert ShadowWaves(table, 'two')._required('policy')['capabilityId'] == old['capabilityId']
    monkeypatch.setattr(table, 'submit_transaction', submit)
    upgrade(world)
    assert ShadowWaves(table, 'one')._required('policy').metadata['etag'] == first.metadata['etag']


def test_rollback_only_accepts_immediate_predecessor_and_is_audited(world):
    table, metadata, old, new = world
    with pytest.raises(Conflict, match='immediately previous'):
        upgrade(world, rollback=True)
    upgrade(world)
    result = migrate(table, metadata, old['deployments'], previous=new['capabilityId'], target=old['capabilityId'],
        upgrade_id='rollback-1', source_commit=old['sourceCommit'], rollback=True, clock=lambda: 1000)
    assert result['rollback']
    assert ShadowWaves(table, 'one')._required('policy')['capabilityId'] == old['capabilityId']
    assert ShadowWaves(table, 'one')._required('upgrade!upgrade-1')


def test_old_mixed_and_paused_workers_cannot_use_new_binding(world, monkeypatch):
    table, _, old, new = world
    upgrade(world)
    store = ShadowWaves(table, 'one')
    for key in ('UPLOAD_WAVE_SOURCE_COMPLETION_ENABLED', 'PROCESSING_GENERATION_ENFORCEMENT_ENABLED'):
        monkeypatch.setenv(key, 'true')
    monkeypatch.setenv('PROCESSING_MODE', 'backend')
    for capability, image, pending, allowed in [
        (old['capabilityId'], old['images']['backend'], 'false', False),
        (new['capabilityId'], old['images']['backend'], 'false', False),
        (new['capabilityId'], new['images']['backend'], 'true', False),
        (new['capabilityId'], new['images']['backend'], 'false', True),
    ]:
        monkeypatch.setenv('UPLOAD_WAVE_CAPABILITY_ID', capability)
        monkeypatch.setenv('UPLOAD_WAVE_DEPLOYMENT_ID', image)
        monkeypatch.setenv('UPLOAD_WAVE_BOOTSTRAP_PENDING', pending)
        if allowed:
            require_capability(store, 'core')
        else:
            with pytest.raises(Conflict):
                require_capability(store, 'core')


def test_policy_cas_conflict_cannot_overwrite_cleanup_or_create_false_audit(world, monkeypatch):
    table, _, old, new = world
    submit = table.submit_transaction
    def race(operations):
        policy = next((op[1] for op in operations if op[1]['RowKey'] == 'policy'), None)
        if policy:
            current = table.get_entity(policy['PartitionKey'], 'policy')
            submit([('update', dict(current, cleanupState='cleaning'), dict(etag=current.metadata['etag'], match_condition=MatchConditions.IfNotModified))])
            raise ResourceModifiedError('raced cleanup')
        return submit(operations)
    monkeypatch.setattr(table, 'submit_transaction', race)
    with pytest.raises(Conflict, match='cleanup'):
        upgrade(world)
    assert not ShadowWaves(table, 'one')._get('upgrade!upgrade-1')
    assert ShadowWaves(table, 'one')._required('policy')['capabilityId'] == old['capabilityId']


def test_migration_entrypoint_refuses_an_unpaused_worker(monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['wave_upgrade', '--from-capability', 'a' * 64,
        '--upgrade-id', 'u1', '--release', json.dumps(release('b'))])
    monkeypatch.setenv('APP_ROLE', 'wave-recovery')
    monkeypatch.setenv('UPLOAD_WAVE_BOOTSTRAP_PENDING', 'false')
    with pytest.raises(RuntimeError, match='paused'):
        main()


def test_template_update_preserves_secrets_resources_and_sidecar_roles():
    old, new = release('a'), release('b')
    resource = dict(name='app-indexer', properties=dict(template=dict(revisionSuffix='old',
        scale=dict(minReplicas=0, maxReplicas=3), containers=[dict(name=role, image=old['images']['backend'],
            resources=dict(cpu=2, memory='4Gi'), env=[dict(name='APP_ROLE', value=role),
                dict(name='SECRET', secretRef='original-secret')]) for role in ('indexer', 'index-worker')])))
    original = copy.deepcopy(resource)
    template = target_template(resource, new, True, suffix='upgrade')
    assert resource == original and template['scale'] == original['properties']['template']['scale']
    for container in template['containers']:
        assert container['image'] == new['images']['backend'] and container['resources']['cpu'] == 2
        env = {entry['name']: entry for entry in container['env']}
        assert env['SECRET'] == dict(name='SECRET', secretRef='original-secret')
        assert env['UPLOAD_WAVE_EXECUTION_ENABLED']['value'] == 'false'
        assert env['APP_ROLE']['value'] == container['name']


def test_old_active_revision_prevents_cutover():
    apps = [dict(name='app', properties=dict(latestReadyRevisionName='new'))]
    with pytest.raises(RuntimeError, match='active revisions'):
        verify_revisions(apps, dict(app=[dict(name='old', properties=dict(active=True)),
            dict(name='new', properties=dict(active=True))]))


def test_inactive_revision_with_remaining_replicas_prevents_cutover():
    apps = [dict(name='app', properties=dict(latestReadyRevisionName='new'))]
    with pytest.raises(RuntimeError, match='still draining'):
        verify_revisions(apps, dict(app=[dict(name='old', properties=dict(active=False, replicas=1)),
            dict(name='new', properties=dict(active=True))]))
