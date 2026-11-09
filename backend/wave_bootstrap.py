"""Explicit fresh-install job: seed an operator-selected image certificate.

Invoked once by deploy/fresh_wave.py while public intake is blocked. This is
not worker self-certification and never enrolls or migrates an existing library.
"""
import json
import os
import re
import time
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
from wave_policy import ROLES, PROTOCOL, CAPABILITIES, certify, capability_id
from upload_waves import digest


def deployment_contract(core, vision):
    for image in (core, vision):
        if not re.fullmatch(r'ghcr\.io/[a-z0-9_./-]+@sha256:[a-f0-9]{64}', image):
            raise ValueError('Wave bootstrap requires immutable GHCR image digests')
    return {role: vision if role == 'vision' else core for role in ROLES}


def seed(service, deployments, expected, allow_empty_libraries=False):
    if set(deployments) != set(ROLES) or digest([PROTOCOL, deployments]) != expected:
        raise ValueError('Bootstrap capability does not match the selected role images')
    # A fresh run may resume after certificate creation, but never over data or
    # prior ownership. Metadata/library tables may not exist on the first start.
    checks = (
        ('photometadata', "PartitionKey ne ''"),
        ('photolibraries', "PartitionKey ne ''"),
        ('photojobs', "PartitionKey ge 'upload-wave-shadow-' and PartitionKey lt 'upload-wave-shadow-~'"),
    )
    # A library row with no photos and no wave policy (e.g. the owner row a half-finished first boot left
    # behind) has nothing to protect; the photo and wave-policy checks still apply.
    for name, query in (checks[:1] + checks[2:] if allow_empty_libraries else checks):
        try:
            row = next(iter(service.get_table_client(name).query_entities(query, results_per_page=1)), None)
        except ResourceNotFoundError:
            row = None
        if row is not None:
            raise RuntimeError('Fresh wave bootstrap refused: existing library data or ownership')
    table = service.create_table_if_not_exists('photojobs')
    identity = certify(table, deployments)
    proof = table.get_entity(CAPABILITIES, identity)
    if identity != expected or proof.get('protocol') != PROTOCOL or json.loads(proof['deployments']) != deployments:
        raise RuntimeError('Persisted wave capability verification failed')
    return identity


def auto_seed(service, env=os.environ):
    """One-click portal deployment: the template pins the core and Vision images and hands them to every
    role, and a brand-new library has nothing to protect, so the first boot seeds the certificate for those
    images -- only on never-used storage (``seed`` refuses any existing library data). Does nothing unless the
    template asked for it via UPLOAD_WAVE_AUTO_BOOTSTRAP, and is a no-op once the certificate exists. An
    explicit UPLOAD_WAVE_CAPABILITY_ID must still match the images."""
    if env.get('UPLOAD_WAVE_AUTO_BOOTSTRAP') != 'true':
        return ''
    deployments = deployment_contract(env.get('UPLOAD_WAVE_CORE_IMAGE', ''), env.get('UPLOAD_WAVE_VISION_IMAGE', ''))
    expected = digest([PROTOCOL, deployments])
    if env.get('UPLOAD_WAVE_CAPABILITY_ID') not in (None, '', expected):
        raise ValueError('Template capability ID does not match its pinned images')
    try:
        proof = service.get_table_client('photojobs').get_entity(CAPABILITIES, expected)
        if proof.get('protocol') == PROTOCOL and json.loads(proof['deployments']) == deployments:
            return expected
    except ResourceNotFoundError:
        pass
    if upgrade_existing(service, deployments, expected, env):
        return expected
    try:
        return seed(service, deployments, expected, allow_empty_libraries=True)
    except RuntimeError as error:
        if 'existing library data' not in str(error):
            raise
        raise RuntimeError(
            f'{error}. This release (certificate {expected}) cannot take over the existing library: its current '
            'certificate must be listed in UPLOAD_WAVE_COMPATIBLE_CAPABILITIES (the release pin job writes it into '
            'the Deploy to Azure template; listed now: '
            f"{env.get('UPLOAD_WAVE_COMPATIBLE_CAPABILITIES') or 'nothing'}), or run deploy/upgrade_wave.py.") from error


def upgrade_existing(service, deployments, expected, env=os.environ):
    """Same-protocol image change over an enrolled library. The release pin job lists the earlier
    certificates this release was qualified to replace (UPLOAD_WAVE_COMPATIBLE_CAPABILITIES) and its source
    commit; when every library is bound to one of them, run the same preserving migration deploy/upgrade_wave.py
    does (certify the new images, rebind each policy with an audit receipt). Anything else -- unknown binding,
    several earlier releases, no listed predecessors -- is left to the explicit operator path. Raises
    UpgradeBusy while work is still leased; the next owner-bootstrap retry tries again."""
    compatible = {c for c in env.get('UPLOAD_WAVE_COMPATIBLE_CAPABILITIES', '').split(',')
                  if re.fullmatch(r'[a-f0-9]{64}', c)}
    commit = env.get('UPLOAD_WAVE_SOURCE_COMMIT', '')
    if not compatible or not re.fullmatch(r'[a-f0-9]{40}', commit):
        return False
    import wave_upgrade
    table = service.get_table_client('photojobs')
    bound = {row['capabilityId'] for row in wave_upgrade.library_policies(table)}
    if not bound or not bound <= compatible:
        return False
    if len(bound) > 1:
        raise RuntimeError('Libraries are bound to several earlier releases; use deploy/upgrade_wave.py')
    previous = next(iter(bound))
    wave_upgrade.migrate(table, service.get_table_client('photometadata'), deployments, previous=previous,
                         target=expected, upgrade_id='auto-' + previous[:16] + '-' + expected[:16],
                         source_commit=commit)
    return True


def main():
    from azure.identity import ManagedIdentityCredential
    from azure.data.tables import TableServiceClient
    if os.getenv('UPLOAD_WAVE_BOOTSTRAP_PENDING') != 'true' or os.getenv('APP_ROLE') != 'wave-recovery':
        raise RuntimeError('Bootstrap is only allowed in the explicit fresh-install job phase')
    if os.getenv('UPLOAD_WAVE_EXECUTION_ENABLED') != 'false' or os.getenv('UPLOAD_WAVE_NEW_LIBRARIES_ENABLED') != 'false':
        raise RuntimeError('Bootstrap requires execution and enrollment to remain disabled')
    core = os.environ['UPLOAD_WAVE_DEPLOYMENT_ID']
    deployments = deployment_contract(core, os.environ['UPLOAD_WAVE_BOOTSTRAP_VISION_IMAGE'])
    service = TableServiceClient('https://' + os.environ['STORAGE_ACCOUNT_NAME'] + '.table.core.windows.net',
                                 credential=ManagedIdentityCredential())
    # Storage grants can propagate after the ARM role assignment finishes.
    for attempt in range(31):
        try:
            identity = seed(service, deployments, os.environ['UPLOAD_WAVE_CAPABILITY_ID'])
            print(json.dumps(dict(event='wave_bootstrap_ready', capabilityId=identity)), flush=True)
            return
        except HttpResponseError as error:
            if error.status_code not in (403, 429, 500, 502, 503, 504) or attempt == 30:
                raise
            time.sleep(5)


if __name__ == '__main__':
    main()
