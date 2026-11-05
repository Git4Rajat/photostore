"""Explicit, resumable same-protocol release migration during deployment downtime.

Only the deployment helper invokes this job, after proving all old revisions
inactive and all target roles paused. It preserves wave/source/proof rows and
changes a library policy together with an immutable per-library audit receipt.
"""
import argparse
from datetime import datetime
import json
import os
import re
import time

from upload_waves import ShadowWaves, Conflict, digest, identifier
from wave_bootstrap import deployment_contract
from wave_policy import CAPABILITIES, PROTOCOL, certify, _supported_certificate_roles


class UpgradeBusy(Conflict):
    pass


def validate_contract(deployments, expected):
    # The certificate being migrated away from may still be an immutable
    # pre-removal one carrying the retired wave-recovery role (see
    # wave_policy._supported_certificate_roles); deployment_contract() only
    # ever builds the current role set, so compare per-role values directly
    # instead of requiring an exact dict match.
    core = deployments.get('core', '')
    vision = deployments.get('vision', '')
    if (not _supported_certificate_roles(deployments)
            or any(value != (vision if role == 'vision' else core) for role, value in deployments.items())
            or digest([PROTOCOL, deployments]) != expected):
        raise ValueError('Upgrade requires an exact, same-protocol immutable release contract')


def library_policies(table):
    # Authoritative policies, including an enrollment interrupted before its
    # directory registration. This operator-only scan is streamed by the SDK.
    for row in table.query_entities("RowKey eq 'policy'", results_per_page=100):
        library = row.get('libraryId')
        if row.get('protocol') == PROTOCOL:
            if not library or row['PartitionKey'] != ShadowWaves(table, library).partition:
                raise Conflict('Invalid wave library policy')
            yield row


def check_drained(store, metadata):
    policy = store._required('policy')
    if policy.get('cleanupState') or float(policy.get('gcUntil') or 0) > store.clock():
        raise Conflict('Library cleanup must finish before upgrading')
    query = "PartitionKey eq '" + store.partition + "'"
    for row in store.table.query_entities(query, results_per_page=100):
        if float(row.get('leaseUntil') or 0) > store.clock():
            raise UpgradeBusy('Wave stage must drain before upgrading')
    from upload_wave_execution import STAGE_PARTITION
    for row in store.table.query_entities("PartitionKey eq '" + STAGE_PARTITION
            + "' and libraryId eq '" + store.library.replace("'", "''") + "'", results_per_page=100):
        if float(row.get('leaseUntil') or 0) > store.clock():
            raise UpgradeBusy('Wave dispatcher must drain before upgrading')
    query = "PartitionKey eq '" + store.library.replace("'", "''") + "'"
    for row in metadata.query_entities(query, select=['processing_lease_owner', 'processing_lease',
            'processing_lease_expires_at'], results_per_page=100):
        if row.get('processing_lease_owner') or row.get('processing_lease'):
            try:
                expiry = datetime.fromisoformat(row.get('processing_lease_expires_at', '').replace('Z', '+00:00'))
                if expiry.tzinfo is None:
                    raise ValueError('Missing lease timezone')
            except (ValueError, TypeError):
                raise Conflict('Source lease expiry is unknown; refusing upgrade')
            if expiry.timestamp() > store.clock():
                raise UpgradeBusy('Source processing must drain before upgrading')


def migrate(table, metadata, deployments, *, previous, target, upgrade_id, source_commit,
            rollback=False, clock=time.time):
    identifier(upgrade_id)
    if not re.fullmatch(r'[a-f0-9]{40}', source_commit):
        raise ValueError('Qualified source commit required')
    validate_contract(deployments, target)
    if not re.fullmatch(r'[a-f0-9]{64}', previous) or previous == target:
        raise ValueError('Distinct previous and target capabilities required')
    proof = table.get_entity(CAPABILITIES, previous)
    validate_contract(json.loads(proof['deployments']), previous)
    if proof.get('protocol') != PROTOCOL:
        raise Conflict('Previous release protocol does not match')
    policies = list(library_policies(table))
    receipt_fields = dict(fromCapability=previous, toCapability=target,
        sourceCommit=source_commit, rollback=rollback)
    # Fail before changing any binding if another library is incompatible or
    # still busy. Recheck each library immediately before its conditional write.
    for policy in policies:
        store = ShadowWaves(table, policy['libraryId'], clock=clock)
        audit = store._get('upgrade!' + upgrade_id)
        if audit and any(audit.get(k) != v for k, v in receipt_fields.items()):
            raise Conflict('Upgrade ID already describes a different migration')
        if policy['capabilityId'] == target:
            if not audit:
                raise Conflict('Target binding has no matching upgrade receipt')
        elif policy['capabilityId'] != previous:
            raise Conflict('Library belongs to an unexpected release')
        elif rollback and policy.get('previousCapabilityId') != target:
            raise Conflict('Rollback is only allowed to the immediately previous release')
        check_drained(store, metadata)
    certify(table, deployments)
    proof = table.get_entity(CAPABILITIES, target)
    if proof.get('protocol') != PROTOCOL or json.loads(proof['deployments']) != deployments:
        raise Conflict('Persisted target certificate does not match')
    for policy in policies:
        store = ShadowWaves(table, policy['libraryId'], clock=clock)
        def commit():
            current = store._required('policy')
            audit = store._get('upgrade!' + upgrade_id)
            if current['capabilityId'] == target and audit:
                if any(audit.get(k) != v for k, v in receipt_fields.items()):
                    raise Conflict('Upgrade receipt changed')
                return
            if current['capabilityId'] != previous:
                raise Conflict('Library binding changed during upgrade')
            check_drained(store, metadata)
            receipt = store._row('upgrade!' + upgrade_id, **receipt_fields, completedAt=clock())
            updated = dict(current, capabilityId=target, previousCapabilityId=previous,
                lastUpgradeId=upgrade_id, upgradedAt=clock())
            table.submit_transaction([store._change(current, updated), ('create', receipt)])
        store._retry(commit)
        # Explicit operator retry of eligible preserved releases. No successful
        # work, source retry counters, checkpoints or immutable proofs are reset.
        # This is idempotent while paused, including a crash after policy CAS.
        from upload_wave_execution import stage_work_store
        work = stage_work_store(table, clock=clock)
        query = "PartitionKey eq '" + store.partition + "' and RowKey ge 'wave!' and RowKey lt 'wave!~'"
        for wave in table.query_entities(query, results_per_page=100):
            state = wave['state']
            stage = ('vision' if state in ('processing_pending', 'processing') else
                'cluster' if state == 'ready' else 'index' if state == 'indexing' else None)
            key = (wave.get('visionPendingKey') if stage == 'vision' else
                'shadow-cluster!' + wave['waveId'] if stage == 'cluster' else wave.get('indexPendingKey'))
            outbox = store._get(key) if key else None
            if stage and outbox and store.outbox_eligible(wave, outbox):
                scope = f"wave-{stage}:{wave['waveId']}:{digest(key)}"
                work.request(store.library, scope, advance=True)
    return dict(event='wave_upgrade_complete', upgradeId=upgrade_id, fromCapability=previous,
        toCapability=target, libraries=len(policies), sourceCommit=source_commit, rollback=rollback)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--from-capability', required=True)
    parser.add_argument('--upgrade-id', required=True)
    parser.add_argument('--release', required=True, help='Qualified release JSON supplied by the deployment helper')
    parser.add_argument('--rollback', action='store_true')
    args = parser.parse_args()
    # Runs only as a throwaway ad-hoc job the deployment helper provisions for
    # this one execution and deletes afterward (see deploy/fresh_wave.py's
    # run_adhoc_job). There is no API endpoint, automatic recertification,
    # account-key fallback, or grant of new roles.
    if (os.getenv('APP_ROLE') != 'wave-recovery'
            or os.getenv('UPLOAD_WAVE_BOOTSTRAP_PENDING') != 'true'
            or any(os.getenv(k) != 'false' for k in ('UPLOAD_WAVE_EXECUTION_ENABLED',
                'UPLOAD_WAVE_NEW_LIBRARIES_ENABLED'))):
        raise RuntimeError('Upgrade requires the explicit paused activation phase')
    release = json.loads(args.release)
    if (release.get('protocol') != PROTOCOL or release.get('qualification') != 'wave-unit-tests-passed'
            or (not args.rollback and release.get('upgradeProtocol') != PROTOCOL)):
        raise ValueError('Use a release qualified for same-protocol upgrades')
    if os.getenv('UPLOAD_WAVE_CAPABILITY_ID') != release['capabilityId']:
        raise ValueError('Paused job does not select the target release')
    from azure.identity import ManagedIdentityCredential
    from azure.data.tables import TableServiceClient
    service = TableServiceClient('https://' + os.environ['STORAGE_ACCOUNT_NAME'] + '.table.core.windows.net',
        credential=ManagedIdentityCredential())
    deadline = time.monotonic() + 180
    while True:
        try:
            result = migrate(service.get_table_client('photojobs'), service.get_table_client('photometadata'),
                release['deployments'], previous=args.from_capability, target=release['capabilityId'],
                upgrade_id=args.upgrade_id, source_commit=release['sourceCommit'], rollback=args.rollback)
            break
        except UpgradeBusy as error:
            if time.monotonic() >= deadline:
                raise
            print(json.dumps(dict(event='wave_upgrade_draining', reason=str(error))), flush=True)
            time.sleep(10)
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
