"""One-way wave ownership; explicit paused upgrades may change the release binding."""
from contextvars import ContextVar
from functools import wraps
import os
import re
import uuid
import json

from azure.core.exceptions import ResourceNotFoundError, ResourceExistsError
from upload_waves import ShadowWaves, Conflict, digest

PROTOCOL = 'library-wave-v1'
ROLES = ('core','upload','vision','cluster','index-worker','archive','recovery')
CAPABILITIES = 'upload-wave-capabilities'


def _supported_certificate_roles(builds):
    # Existing immutable v1 certificates predate removal of the standing job.
    # Keep validating their complete digest, but do not require that job to run.
    return set(builds) in (set(ROLES), set(ROLES) | {'wave-recovery'})


_SOURCE = ContextVar('wave_source_change', default=False)
_FINALIZE = ContextVar('wave_finalization_check', default=None)


def source_change(function):
    @wraps(function)
    def wrapped(*args,**kwargs):
        token=_SOURCE.set(True)
        try:return function(*args,**kwargs)
        finally:_SOURCE.reset(token)
    return wrapped


def source_change_active():return _SOURCE.get()
def begin_source_change():return _SOURCE.set(True)
def end_source_change(token):_SOURCE.reset(token)
def begin_finalization(check):return _FINALIZE.set(check)
def end_finalization(token):_FINALIZE.reset(token)
def finalization_check():
    check=_FINALIZE.get()
    if check is None:return False
    check();return True


def check_finalization(store,bindings):
    policy=store._required('policy')
    if policy.get('cleanupState') or policy.get('gcUntil',0)>store.clock():
        raise Conflict('Cleanup fenced upload source writes')
    for session,item in bindings:
        reservation=store._required(store.item_key(session,item))
        if reservation.get('finalizeLeaseUntil',0)-store.clock()<60:
            raise Conflict('Upload finalization lease expired; retry admission')
        if reservation['outcome'] not in ('reserved','accepted','duplicate'):
            raise Conflict('Upload reservation was cancelled')


def configuration_ready():
    return (os.getenv('PROCESSING_MODE','').lower()=='backend'
        and all(os.getenv(name,'').lower()=='true' for name in
            ('UPLOAD_WAVE_SOURCE_COMPLETION_ENABLED','PROCESSING_GENERATION_ENFORCEMENT_ENABLED')))


def certify(table, deployments):
    """Operator supplies independently qualified immutable image IDs for every role.
    No automatic certification from an environment flag or a worker heartbeat.
    """
    if set(deployments)!=set(ROLES) or any(not isinstance(v,str) or not v for v in deployments.values()):
        raise ValueError('Every wave role requires a qualified deployment identity')
    identity=digest([PROTOCOL,deployments])
    row=dict(PartitionKey=CAPABILITIES,RowKey=identity,protocol=PROTOCOL,
             deployments=json.dumps(deployments,sort_keys=True))
    try:table.create_entity(row)
    except ResourceExistsError:pass
    return identity


def owned(table,library):
    if table is None:
        if os.getenv('UPLOAD_WAVE_NEW_LIBRARIES_ENABLED','').lower()=='true':
            raise RuntimeError('Wave policy storage unavailable')
        return False
    policy=ShadowWaves(table,library)._get('policy')
    return bool(policy and policy.get('protocol')==PROTOCOL)


def require_capability(store,role,*,allow_cleanup=False):
    policy=store._required('policy')
    proof=store.table.get_entity(CAPABILITIES,policy['capabilityId'])
    builds=json.loads(proof['deployments'])
    if (os.getenv('UPLOAD_WAVE_BOOTSTRAP_PENDING','').lower()=='true'
            or not configuration_ready() or proof.get('protocol')!=PROTOCOL
            or not _supported_certificate_roles(builds) or digest([PROTOCOL,builds])!=policy['capabilityId']
            or os.getenv('UPLOAD_WAVE_CAPABILITY_ID','')!=policy['capabilityId']
            or not os.getenv('UPLOAD_WAVE_DEPLOYMENT_ID','')
            or builds.get(role)!=os.getenv('UPLOAD_WAVE_DEPLOYMENT_ID')):
        raise Conflict('This app deployment is not qualified for the library wave protocol')
    if policy.get('cleanupState') and not allow_cleanup:raise Conflict('Library cleanup is in progress')


def enroll_new(table,library,metadata=None):
    if os.getenv('UPLOAD_WAVE_BOOTSTRAP_PENDING','').lower()=='true':
        raise RuntimeError('Wave deployment bootstrap is pending')
    if os.getenv('UPLOAD_WAVE_NEW_LIBRARIES_ENABLED','').lower()!='true':return
    if not configuration_ready():raise RuntimeError('New wave libraries require backend processing and source fencing')
    if metadata is None:raise RuntimeError('New-library emptiness proof unavailable')
    query="PartitionKey eq '"+library.replace("'","''")+"'"
    if next(iter(metadata.query_entities(query,results_per_page=1)),None) is not None:
        raise Conflict('Wave enrollment requires a new empty library')
    certificate=os.getenv('UPLOAD_WAVE_CAPABILITY_ID','')
    if not re.fullmatch(r'[a-f0-9]{64}',certificate):
        raise RuntimeError('A valid wave capability ID is required before enrollment')
    try:proof=table.get_entity(CAPABILITIES,certificate)
    except ResourceNotFoundError:raise RuntimeError('Qualified wave capabilities are missing')
    builds=json.loads(proof.get('deployments') or '{}')
    if (proof.get('protocol')!=PROTOCOL or not _supported_certificate_roles(builds)
            or digest([PROTOCOL,builds])!=certificate):
        raise RuntimeError('Unsupported wave capability')
    store=ShadowWaves(table,library)
    def commit():
        policy=store._get('policy')
        if policy:
            if policy.get('capabilityId')!=certificate:raise Conflict('Library ownership cannot be rebound')
            return
        if store._get('control'):raise Conflict('Only a never-admitted new library can activate')
        table.submit_transaction([('create',store._row('policy',protocol=PROTOCOL,
            capabilityId=certificate,activationId=uuid.uuid4().hex,cleanupState='',createdAt=store.clock()))])
    store._retry(commit)


def validate_admission(store,data,*,finalizing=False):
    session,item=data.get('waveSessionId',''),data.get('waveItemId','')
    old=store._required(store.item_key(session,item))
    parent=store._required(store.session_key(session))
    if parent.get('abandoning') or (parent['state']!='open' and old['outcome']=='reserved'):
        raise Conflict('Upload intake is closed')
    if old['filename']!=data.get('filename'):raise Conflict('Upload differs from its reservation')
    sha=data.get('sha256','')
    if not isinstance(sha,str) or len(sha)!=64 or any(c not in '0123456789abcdef' for c in sha):
        raise ValueError('Wave upload requires a source SHA-256')
    def bind():
        row=store._required(old['RowKey'])
        if row.get('sourceHash'):
            if row['sourceHash']!=sha:raise Conflict('Upload reservation cannot change source bytes')
            if not finalizing:return
            # Init already reserves ten minutes for this exact source. Avoid
            # making every finalize/retry compete for the library control row
            # just to extend an otherwise healthy reservation by a few seconds.
            if row.get('finalizeLeaseUntil',0)-store.clock()>=120:return
        control=store._required('control')
        store._commit(control,[store._change(row,dict(row,sourceHash=sha,
            finalizeLeaseUntil=store.clock()+600 if finalizing else 0))])
    store._retry(bind)
    return session,item


def legacy_message_suppressed(table,library,payload):
    if not owned(table,library):return False
    kind=payload.get('type')
    if kind=='upload_wave_stage':return False
    if kind in ('people_incremental_assign','people_cluster','ipwork','ipwork_release','ipwork_release_check'):
        return True
    if kind=='index_build':
        return str(payload.get('reason','')).startswith(('upload','ipwork','post-finalize'))
    return False
