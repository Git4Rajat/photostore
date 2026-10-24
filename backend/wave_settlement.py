"""Explicit failure/exclusion witnesses; these are never processing-success receipts."""
import json
from azure.core.exceptions import ResourceNotFoundError
from execution_lease import guard_operations
from upload_waves import Conflict,digest

MAX_SOURCE_ATTEMPTS=5


def settle(store,wave_id,member,outcome,reason,*,witness,maintenance=False):
    if outcome not in ('deleted','superseded','failed_exhausted','excluded'):
        raise ValueError('Not a terminal settlement outcome')
    payload=dict(waveId=wave_id,photoId=member['photoId'],revision=member['processingRevision'],
                 outcome=outcome,reason=reason,witnessDigest=digest(json.loads(json.dumps(witness,default=str))))
    identity=digest(payload);key='settlement!'+identity
    def commit():
        control=store._required('control');wave=store._required(store.wave_key(wave_id))
        old=store._required(member['RowKey'])
        if old['outcome']==outcome and old['evidenceId']==identity:return
        allowed=('collecting','waiting_predecessor','processing_pending','processing') if maintenance and outcome=='deleted' else ('processing_pending','processing')
        if wave['state'] not in allowed:
            raise Conflict('Settlement cannot alter frozen or collecting inputs')
        proof=store._get(key)
        changes=[] if proof else [('create',store._row(key,payload=json.dumps(payload,sort_keys=True)))]
        row=dict(old,outcome=outcome,evidenceId=identity,evidenceQualified=True,
                 outstandingLeases=0,observedAt=store.clock())
        store._commit(control,[*changes,store._change(old,row),
            store._change(wave,dict(wave,lastProgressAt=store.clock(),updatedAt=store.clock()))],maintenance=maintenance)
    store._retry(commit)
    return store._required(member['RowKey'])


def verified(store,member):
    proof=store._get('settlement!'+member.get('evidenceId',''))
    if not proof:return False
    body=json.loads(proof['payload'])
    return (digest(body)==member['evidenceId'] and body.get('outcome')==member['outcome']
        and body.get('photoId')==member['photoId'] and body.get('revision')==member['processingRevision']
        and body.get('waveId')==member['waveId'])


def deletion_key(photo,revision):return 'deleted-source!'+digest([photo,revision])


def source_state(store,wave_id,member,metadata):
    """Only durable deletion/successor proof can automatically exclude old input."""
    deleted=store._get(deletion_key(member['photoId'],member['processingRevision']))
    if deleted:
        return settle(store,wave_id,member,'deleted','permanent_deletion',witness=dict(deleted))
    try:row=metadata.get_entity(store.library,member['filename'])
    except ResourceNotFoundError:return None
    if row.get('anonymousImageId')==member['photoId'] and row.get('processing_state')=='deleted':
        return settle(store,wave_id,member,'deleted','source_tombstone',witness=dict(row))
    if row.get('anonymousImageId') and row.get('processingRevision') and (
            row['anonymousImageId']!=member['photoId'] or row['processingRevision']!=member['processingRevision']):
        admitted=store._get('admitted-source!'+digest([row['anonymousImageId'],row['processingRevision']]))
        if admitted and admitted['filename']==member['filename']:
            return settle(store,wave_id,member,'superseded','admitted_replacement',witness=dict(admitted))
    successor=store._get('supersession!'+digest([member['photoId'],member['processingRevision']]))
    if successor:
        return settle(store,wave_id,member,'superseded','admitted_successor',witness=dict(successor))
    return None


def attempt(store,member):
    key='source-attempt!'+digest([member['waveId'],member['photoId'],member['processingRevision']])
    def commit():
        old=store._get(key)
        count=int((old or {}).get('count',0))+1
        row=store._row(key,count=count,updatedAt=store.clock())
        store.table.submit_transaction([store._change(old,row),*guard_operations(store)])
        return count
    return store._retry(commit)


def exhausted(store,wave_id,member,metadata,count):
    if count<MAX_SOURCE_ATTEMPTS:return False
    row=metadata.get_entity(store.library,member['filename'])
    from storage_utils import _processing_is_expired
    if (row.get('anonymousImageId')!=member['photoId'] or row.get('processingRevision')!=member['processingRevision']
        or (row.get('processing_lease_owner') and not _processing_is_expired(row))):return False
    steps=json.loads(member['requiredSteps'])
    statuses={step:row.get(step+'_status','pending') for step in steps}
    if not any(status=='failed' for status in statuses.values()):return False
    settle(store,wave_id,member,'failed_exhausted','required_step_retries_exhausted',
           witness=dict(attempts=count,statuses=statuses,generation=row.get('processingGeneration','')))
    return True


def admit_reprocess(store,metadata):
    """The new revision is admitted before old members can be excluded."""
    from image_utils import is_video_file
    from processing_generation import required_steps_for_media
    revision=metadata['processingRevision'];photo=metadata['anonymousImageId']
    session='reprocess-'+digest([photo,revision])
    row=store.open_session(session,1)
    store.register(session,[dict(itemId='source',filename=metadata['RowKey'])])
    row=store.resolve(session,'source',outcome='accepted',filename=metadata['RowKey'],photo_id=photo,
        processing_revision=revision,required_steps=required_steps_for_media(is_video_file(metadata['RowKey'])))
    store.close(session,row['manifestRevision'],'reprocess')
    return row['waveId']


def abandon(store,session,manifest_revision,*,reason):
    """Operator-confirmed stop, one bounded reservation page per call.
    Resolved sources remain members; unknown/missing reservations are cancelled,
    not counted as successfully processed. A stale revision fails before changes.
    """
    if not isinstance(reason,str) or not reason.strip() or len(reason)>256:raise ValueError('Abandonment reason required')
    parent=store._required(store.session_key(session))
    if parent['manifestRevision']!=manifest_revision:raise Conflict('Abandonment revision is stale')
    if parent['state']!='open':return dict(parent)
    cursor=parent.get('abandonCursor','')
    items=store._page('item!'+session+'!',cursor)
    if any(item['outcome']=='reserved' and item.get('finalizeLeaseUntil',0)>store.clock() for item in items):
        raise Conflict('A finalization is still in flight; retry abandonment later')
    for item in items:
        if item['outcome']=='reserved':
            parent=store.resolve(session,item['itemId'],outcome='cancelled')
    more=len(items)==20
    def advance():
        old=store._required(store.session_key(session));control=store._required('control')
        row=dict(old,abandoning=True,abandonReason=reason,
            abandonCursor=items[-1]['RowKey'] if items else cursor,abandonScanComplete=not more)
        store._commit(control,[store._change(old,row)])
        return row
    parent=store._retry(advance)
    if not more:
        parent=store.close(session,parent['manifestRevision'],'abandoned',abandoned=True)
    return dict(parent,abandonmentNeedsMore=more)


def reconcile_supersession(store,metadata,row):
    backlog=json.loads(row.get('waveSupersessionBacklog') or '[]')
    if not backlog:return
    successor=admit_reprocess(store,row)
    for revision in backlog:
        key='supersession!'+digest([row['anonymousImageId'],revision])
        def commit():
            if store._get(key):return
            control=store._required('control')
            store._commit(control,[('create',store._row(key,photoId=row['anonymousImageId'],
                oldRevision=revision,newRevision=row['processingRevision'],successorWaveId=successor))])
        store._retry(commit)
    # Lost acknowledgement only replays the deterministic admission/proofs.
    current=metadata.get_entity(store.library,row['RowKey'])
    if current.get('processingRevision')!=row['processingRevision']:return
    from azure.core import MatchConditions
    from azure.data.tables import UpdateMode
    remaining=[r for r in json.loads(current.get('waveSupersessionBacklog') or '[]') if r not in backlog]
    metadata.update_entity(dict(PartitionKey=store.library,RowKey=row['RowKey'],waveSupersessionBacklog=json.dumps(remaining)),
        mode=UpdateMode.MERGE,etag=current.metadata['etag'],match_condition=MatchConditions.IfNotModified)


def recover_supersessions(store,wave,metadata):
    if metadata is None:return
    # Revisited on each scan; bounded source reads. A durable cursor covers large waves.
    after=wave.get('supersessionAfter','');page=store._page(f'e!{wave["waveId"]}!m!',after)
    for member in page:
        try:row=metadata.get_entity(store.library,member['filename'])
        except ResourceNotFoundError:continue
        if row.get('waveSupersessionBacklog'):reconcile_supersession(store,metadata,row)
    def checkpoint():
        current=store._required(store.wave_key(wave['waveId']));control=store._required('control')
        store._commit(control,[store._change(current,dict(current,
            supersessionAfter=page[-1]['RowKey'] if len(page)==20 else ''))],advance=False)
    store._retry(checkpoint)
