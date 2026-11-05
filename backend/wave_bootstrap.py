"""Explicit fresh-install job: seed an operator-selected image certificate.

Invoked once by deploy/fresh_wave.py while public intake is blocked. This is
not worker self-certification and never enrolls or migrates an existing library.
"""
import json
import os
import re
import time
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
from wave_policy import ROLES, PROTOCOL, CAPABILITIES, certify
from upload_waves import digest


def deployment_contract(core, vision):
    for image in (core, vision):
        if not re.fullmatch(r'ghcr\.io/[a-z0-9_./-]+@sha256:[a-f0-9]{64}', image):
            raise ValueError('Wave bootstrap requires immutable GHCR image digests')
    return {role: vision if role == 'vision' else core for role in ROLES}


def seed(service, deployments, expected):
    if set(deployments) != set(ROLES) or digest([PROTOCOL, deployments]) != expected:
        raise ValueError('Bootstrap capability does not match the selected role images')
    # A fresh run may resume after certificate creation, but never over data or
    # prior ownership. Metadata/library tables may not exist on the first start.
    for name, query in (
        ('photometadata', "PartitionKey ne ''"),
        ('photolibraries', "PartitionKey ne ''"),
        ('photojobs', "PartitionKey ge 'upload-wave-shadow-' and PartitionKey lt 'upload-wave-shadow-~'"),
    ):
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
