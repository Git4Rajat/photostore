"""Explicit failure/exclusion witnesses; these are never processing-success receipts."""
import json
from azure.core.exceptions import ResourceNotFoundError
from execution_lease import guard_operations
from upload_waves import Conflict,digest

MAX_SOURCE_ATTEMPTS=1
MAX_INSPECTION_FAILURES=3


def settle(store,wave_id,member,outcome,reason,*,witness,maintenance=False,pending_only=False):
    if outcome not in ('deleted','superseded','failed_exhausted','excluded'):
        raise ValueError('Not a terminal settlement outcome')
    payload=dict(waveId=wave_id,photoId=member['photoId'],revision=member['processingRevision'],
                 outcome=outcome,reason=reason,witnessDigest=digest(json.loads(json.dumps(witness,default=str))))
    identity=digest(payload);key='settlement!'+identity
    def commit():
        control=store._required('control');wave=store._required(store.wave_key(wave_id))
        old=store._required(member['RowKey'])
        if pending_only and old['outcome'] != 'pending':return
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


def _proof(store, key, proofs):
    return proofs[key] if proofs is not None and key in proofs else store._get(key)


def verified(store,member, *, proofs=None):
    proof=_proof(store, 'settlement!'+member.get('evidenceId',''), proofs)
    if not proof:return False
    body=json.loads(proof['payload'])
    return (digest(body)==member['evidenceId'] and body.get('outcome')==member['outcome']
        and body.get('photoId')==member['photoId'] and body.get('revision')==member['processingRevision']
        and body.get('waveId')==member['waveId'])


def deletion_key(photo,revision):return 'deleted-source!'+digest([photo,revision])


def source_state(store,wave_id,member,metadata, *, proofs=None):
    """Only durable deletion/successor proof can automatically exclude old input."""
    deleted=_proof(store, deletion_key(member['photoId'],member['processingRevision']), proofs)
    if deleted:
        return settle(store,wave_id,member,'deleted','permanent_deletion',witness=dict(deleted))
    try:row=metadata.get_entity(store.library,member['filename'])
    except ResourceNotFoundError:return None
    if row.get('anonymousImageId')==member['photoId'] and row.get('processing_state')=='deleted':
        return settle(store,wave_id,member,'deleted','source_tombstone',witness=dict(row))
    if row.get('anonymousImageId') and row.get('processingRevision') and (
            row['anonymousImageId']!=member['photoId'] or row['processingRevision']!=member['processingRevision']):
        admitted=_proof(store, 'admitted-source!'+digest([row['anonymousImageId'],row['processingRevision']]), proofs)
        if admitted and admitted['filename']==member['filename']:
            return settle(store,wave_id,member,'superseded','admitted_replacement',witness=dict(admitted))
    successor=_proof(store, 'supersession!'+digest([member['photoId'],member['processingRevision']]), proofs)
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


def inspection_failed(store, wave_id, member, metadata, error):
    """Bound pre-admission failures separately from expensive source attempts.

    Persist only a bounded count and exception type, never exception text.
    Ownership loss and failures to write this record escape to stage retries.
    A live source owner or changed revision must not be declared failed.
    """
    def record():
        control = store._required('control')
        wave = store._required(store.wave_key(wave_id))
        current = store._required(member['RowKey'])
        if current['outcome'] != 'pending':
            return current
        if wave['state'] not in ('processing_pending', 'processing'):
            raise Conflict('Inspection failures cannot alter frozen inputs')
        count = min(MAX_INSPECTION_FAILURES, int(current.get('inspectionFailures', 0)) + 1)
        updated = dict(current, inspectionFailures=count, inspectionError=type(error).__name__[:80])
        if updated != dict(current):
            store._commit(control, [store._change(current, updated)])
        return updated
    current = store._retry(record)
    if current['outcome'] != 'pending':
        return True
    if current['inspectionFailures'] < MAX_INSPECTION_FAILURES:
        return False
    # Bypass a possibly stale page snapshot for the terminal decision.
    raw = getattr(metadata, 'table', metadata)
    row = raw.get_entity(store.library, member['filename'])
    from storage_utils import _processing_is_expired
    if (row.get('anonymousImageId') != member['photoId']
            or row.get('processingRevision') != member['processingRevision']
            or (any(row.get(k) for k in ('processing_lease_owner', 'processing_lease',
                                        'processing_lease_expires_at')) and not _processing_is_expired(row))):
        return False
    # Reload after checking the source: a concurrently settled member wins.
    current = store._required(member['RowKey'])
    if current['outcome'] != 'pending':
        return True
    settle(store, wave_id, member, 'failed_exhausted', 'inspection_failures_exhausted',
           witness=dict(attempts=current['inspectionFailures'], error=current['inspectionError'],
                        generation=row.get('processingGeneration', '')), pending_only=True)
    return True


def exhausted(store,wave_id,member,metadata,count):
    """Force a terminal settlement once a source has had its full retry budget,
    regardless of whether a required step ever recorded 'failed'.

    A recorded step failure used to be the only accepted evidence, but a
    source with no required steps (browser-owned video) or a retry attempt
    that fails before producing any step result (e.g. an exception in
    completion/receipt capture, unrelated to any single step) can repeat
    forever under that rule -- this exact gap let a video source retry
    unbounded in production (wave_runtime.process_source -> an uncaught
    receipt-capture exception, with no required step to ever show 'failed').
    Attempt count alone is now sufficient: every source reaches a terminal
    state after MAX_SOURCE_ATTEMPTS, no matter what kept failing.
    """
    if count<MAX_SOURCE_ATTEMPTS:return False
    row=metadata.get_entity(store.library,member['filename'])
    from storage_utils import _processing_is_expired
    if (row.get('anonymousImageId')!=member['photoId'] or row.get('processingRevision')!=member['processingRevision']
        or (row.get('processing_lease_owner') and not _processing_is_expired(row))):return False
    steps=json.loads(member['requiredSteps'])
    statuses={step:row.get(step+'_status','pending') for step in steps}
    reason=('required_step_retries_exhausted' if any(status=='failed' for status in statuses.values())
            else 'source_retries_exhausted')
    settle(store,wave_id,member,'failed_exhausted',reason,
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


def fence_discard(store,session,*,reason,resumed_from=''):
    """Persist cancellation even when a timed-out admission has not committed.

    Creating the session key competes atomically with open_session's create.
    If open wins, retry fences its real parent; if cancellation wins, late
    opens see a terminal tombstone and cannot add an open session to a wave.
    """
    from upload_waves import identifier
    import uuid
    identifier(session)
    if resumed_from:
        identifier(resumed_from)
        if resumed_from==session:raise ValueError('A session cannot resume itself')
    if not isinstance(reason,str) or not reason.strip() or len(reason)>256:
        raise ValueError('Abandonment reason required')
    def commit():
        old=store._get(store.session_key(session))
        if old is None:
            row=store._row(store.session_key(session),sessionId=session,waveId='',
                state='abandoned',discardAbsent=True,discardRequestedAt=store.clock(),
                declaredCount=0,registeredCount=0,resolvedCount=0,
                manifestRevision=uuid.uuid4().hex,resumedFromSessionId=resumed_from)
        elif old.get('discardAbsent'):
            previous=old.get('resumedFromSessionId','')
            if previous and resumed_from and previous!=resumed_from:
                raise Conflict('Discard lineage cannot be rebound')
            if previous or not resumed_from:return dict(old)
            row=dict(old,resumedFromSessionId=resumed_from)
        else:
            if resumed_from and old.get('resumedFromSessionId','')!=resumed_from:
                raise Conflict('Discard lineage differs from admitted session')
            if old['state']!='open' or old.get('abandoning'):return dict(old)
            row=dict(old,abandoning=True,abandonReason=reason,discardRequestedAt=store.clock())
        store.table.submit_transaction([store._change(old,row),*store._policy_guard()])
        return row
    return store._retry(commit)


def abandon(store,session,manifest_revision,*,reason):
    """Operator-confirmed stop, one bounded reservation page per call.
    Resolved sources remain members; unknown/missing reservations are cancelled,
    not counted as successfully processed. A stale revision fails before changes.
    """
    if not isinstance(reason,str) or not reason.strip() or len(reason)>256:raise ValueError('Abandonment reason required')
    parent=store._required(store.session_key(session))
    if parent['manifestRevision']!=manifest_revision:raise Conflict('Abandonment revision is stale')
    if parent['state']!='open':return dict(parent)
    def advance():
        old=store._required(store.session_key(session))
        if old['state']!='open':return dict(old)
        if old['manifestRevision']!=manifest_revision:raise Conflict('Abandonment revision is stale')
        cursor=old.get('abandonCursor','')
        items=store._page('item!'+session+'!',cursor)
        reserved=[item for item in items if item['outcome']=='reserved']
        if any(item.get('finalizeLeaseUntil',0)>store.clock() for item in reserved):
            raise Conflict('A finalization is still in flight; retry abandonment later')
        # Cancel the bounded page atomically. One transaction per item used to
        # monopolize the shared wave/control rows while another tab registered.
        changes=[store._change(item,dict(item,outcome='cancelled',photoId='',
            processingRevision='',acceptedFilename='',requiredSteps='[]')) for item in reserved]
        more=len(items)==20
        row=dict(old,abandoning=True,abandonReason=reason,
            abandonCursor=items[-1]['RowKey'] if items else cursor,abandonScanComplete=not more,
            resolvedCount=old['resolvedCount']+len(reserved),
            abandonCancelledCount=old.get('abandonCancelledCount',0)+len(reserved))
        if reserved:
            import uuid
            row['manifestRevision']=uuid.uuid4().hex
            wave=store._required(store.wave_key(old['waveId']))
            changes.append(store._change(wave,dict(wave,lastProgressAt=store.clock(),updatedAt=store.clock())))
        # Read the global fence after the page, minimizing its conflict window.
        control=store._required('control')
        store._commit(control,[*changes,store._change(old,row)])
        return row
    parent=store._retry(advance)
    more=not parent.get('abandonScanComplete',False)
    if parent['state']=='open' and not more:
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
