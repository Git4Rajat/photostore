"""Bounded control-only advancement. Never calls FAISS or a catalog builder."""
import json
from upload_waves import digest,Conflict,MAX_PAGE


def publication_key(member):return 'published-source!'+digest([member['photoId'],member['processingRevision']])


def record_publication_members(store,wave,check):
    after=''
    while True:
        page=store._page(f'e!{wave["waveId"]}!m!',after)
        for member in page:
            if member['outcome']!='settled':continue
            check();key=publication_key(member)
            def commit():
                old=store._get(key);control=store._required('control')
                row=store._row(key,waveId=wave['waveId'],photoId=member['photoId'],
                    processingRevision=member['processingRevision'],evidenceId=member['evidenceId'])
                if old and all(old.get(k)==v for k,v in row.items()):return
                store._commit(control,[store._change(old,row)],advance=False)
            store._retry(commit)
        if len(page)<MAX_PAGE:return
        after=page[-1]['RowKey']


def advance_empty(store,wave_id):
    def commit():
        wave=store._required(store.wave_key(wave_id))
        if wave['state'] not in ('processing_pending','processing') or wave.get('noWorkImpossible'):return wave
        if not store.predecessor_published(wave):return wave
        control=store._required('control')
        probe=json.loads(wave.get('noWorkProbe') or '{}')
        if probe.get('revision')!=control['revision']:
            probe=dict(revision=control['revision'],after='',digest=digest(['no-work',wave_id]),members=0,sessions=0)
        page=store._page(f'e!{wave_id}!',probe['after'])
        for member in page:
            if member['kind']=='session':
                if member['state'] not in ('closed','abandoned'):return wave
                probe['sessions']+=1;witness=dict(member)
            else:
                if member.get('needsProjection',True):
                    row=dict(wave,noWorkImpossible=True,noWorkProbe='')
                    store._commit(control,[store._change(wave,row)],advance=False);return row
                published=store._get(publication_key(member))
                prior=store._get(store.wave_key(published['waveId'])) if published else None
                if not prior or prior['state']!='complete':return wave
                witness=dict(member=member['RowKey'],publication=published['waveId'],receipt=prior['completionReceiptId'])
                probe['members']+=1
            probe['digest']=digest([probe['digest'],witness]);probe['after']=member['RowKey']
        if len(page)==MAX_PAGE:
            row=dict(wave,noWorkProbe=json.dumps(probe))
            store._commit(control,[store._change(wave,row)],advance=False);return row
        if probe['members']!=wave['memberCount'] or probe['sessions']!=wave['sessionCount']:
            raise Conflict('No-work proof does not cover sealed intake')
        revision=digest(['no_work',wave_id,probe['digest']])
        payload=dict(libraryId=store.library,waveId=wave_id,outcome='no_work',
            frozenRevision=revision,requiredSetId='',
            witnessDigest=probe['digest'],members=probe['members'],sessions=probe['sessions'],
            stages=['vision','cluster','index'])
        identity=digest(payload)
        row=dict(wave,state='complete',completionReceiptId=identity,completionOutcome='no_work',
            frozenRevision=revision,requiredPublicationId='',manifestDigest=probe['digest'],noWorkProbe='',
            stateEnteredAt=store.clock(),lastProgressAt=store.clock(),updatedAt=store.clock())
        new_control=dict(control,revision=identity,updatedAt=store.clock())
        if new_control.get('collectingWaveId')==wave_id:new_control['collectingWaveId']=''
        from execution_lease import guard_operations
        store.table.submit_transaction([store._change(control,new_control),store._change(wave,row),
            ('create',store._row('completion!'+wave_id,payload=json.dumps(payload),receiptId=identity,
                frozenRevision=revision,requiredSetId='',outcome='no_work')),
            *store._policy_guard(),*guard_operations(store)])
        return row
    return store._retry(commit)


def advance_zero_faces(store,wave_id):
    wave=store._required(store.wave_key(wave_id))
    if wave['state']!='ready' or wave.get('hasFaceWork'):return wave
    if not store.predecessor_published(wave):return wave
    from upload_wave_execution import _completion
    from wave_settlement import verified
    # One frozen page per call, with a durable proof cursor.
    after=wave.get('zeroFaceAfter','');page=store._page(f'e!{wave_id}!m!',after)
    for member in page:
        if member['outcome']!='settled':
            if not verified(store,member):return wave
            continue
        completion=_completion(store,member,member['evidenceId'])
        if not completion or completion[1]['faceCount']!=0:
            def positive():
                current=store._required(store.wave_key(wave_id));control=store._required('control')
                row=dict(current,hasFaceWork=True,zeroFaceProbePending=False)
                store._commit(control,[store._change(current,row)],advance=False);return row
            return store._retry(positive)
    if len(page)==MAX_PAGE:
        def checkpoint():
            current=store._required(store.wave_key(wave_id));control=store._required('control')
            row=dict(current,zeroFaceAfter=page[-1]['RowKey'],zeroFaceProbePending=True)
            store._commit(control,[store._change(current,row)],advance=False);return row
        return store._retry(checkpoint)
    # The control fast path and a delayed Cluster message must contend on
    # the same lease. An expired applied journal belongs to Cluster recovery.
    existing=store._get(f'assignment!{wave_id}!manifest')
    if existing and json.loads(existing['payload']).get('outcome')!='no_work':return wave
    from execution_lease import ExecutionLeases,OwnershipLost,execution_scope
    leases=ExecutionLeases(store.table,partition=store.partition,clock=store.clock)
    try:ownership=leases.claim(store.library,wave_id,'cluster',wave['frozenRevision'])
    except OwnershipLost:return wave
    if ownership.status=='succeeded':return store.status(wave_id)
    try:
        with execution_scope(leases,ownership,store,lambda:leases.check(ownership)):
            result=_commit_zero_faces(store,wave_id,wave)
        leases.complete(ownership,result['assignmentReceiptId'])
        return result
    finally:
        current=leases._get(ownership.key)
        if current and current.get('owner')==ownership.owner:leases.release(ownership)


def _commit_zero_faces(store,wave_id,wave):
    manifest=dict(libraryId=store.library,waveId=wave_id,frozenRevision=wave['frozenRevision'],
        manifestDigest=wave['manifestDigest'],outcome='no_work',faceCount=0)
    receipt=dict(libraryId=store.library,waveId=wave_id,frozenRevision=wave['frozenRevision'],
        manifestId=digest(manifest),outcome='no_work',faceCount=0)
    def record():
        control=store._required('control');changes=[]
        for suffix,payload in [('manifest',manifest),('complete',receipt)]:
            key=f'assignment!{wave_id}!{suffix}';old=store._get(key)
            if old:
                if old.get('digest')!=digest(payload):raise Conflict('No-work assignment proof changed')
            else:changes.append(('create',store._row(key,payload=json.dumps(payload),digest=digest(payload))))
        if changes:store._commit(control,changes,advance=False)
    store._retry(record)
    from wave_catalog import SCOPES,PREREQUISITES
    from wave_publication import ObligationLedger,compute_obligations
    ledger=ObligationLedger(store.table,wave_id,library=store.library,
        frozen_revision=wave['frozenRevision'],assignment_revision=digest(receipt))
    obligations=compute_obligations(store.library,wave_id,wave['frozenRevision'],SCOPES,
        prerequisite_scopes=PREREQUISITES,assignment_revision=digest(receipt))
    required=ledger.record_required(obligations,prerequisite_scopes=PREREQUISITES)
    return store.commit_assignment(wave_id,required,manifest_digest=wave['manifestDigest'],assignment_receipt=digest(receipt))
