"""Generation-aware cleanup for the processing-artifact namespaces: staging/
published derivative blobs and immutable face-snapshot/manifest rows -- see
docs/indexing-clustering-target-state.md step 4 ("cleanup integration").

Live-owned libraries wire these primitives into permanent deletion, library
clean and purge. A durable policy fence stops source writers; reference surveys
retain shared artifacts and unfinished-wave inputs. Physical deletes are bounded
and retryable through the existing library-operations queue.
"""
import json
import uuid

from azure.core.exceptions import ResourceNotFoundError

from processing_artifacts import staging_name
from upload_waves import digest


def delete_staging_attempt(storage, library, photo, revision, attempt, *, current_attempt):
    """Deletes one SPECIFIC abandoned attempt's staging blobs (thumbnail and
    preview). Refuses to delete the row's currently active attempt -- the
    caller must pass the row's live processingFence as current_attempt, not
    just assume whatever attempt it holds is stale."""
    if attempt == current_attempt:
        raise ValueError('Cannot delete staging for the currently active processing attempt')
    container = storage._CTX.get('blob_thumbnail_container')
    if not container:
        return 0
    deleted = 0
    for kind in ('thumbnail', 'preview'):
        name = staging_name(library, photo, revision, attempt, kind)
        blob = storage._get_blob_client(container, name)
        if blob is None:
            continue
        try:
            blob.delete_blob()
            deleted += 1
        except ResourceNotFoundError:
            pass
    return deleted


def _parse_json(raw, description):
    try:
        return json.loads(raw)
    except (TypeError, ValueError) as exc:
        # Fail closed: corrupt/unparseable completion evidence must block
        # cleanup, not be silently skipped and undercount what a row/inbox
        # entry still references -- an undercount is exactly what would
        # make a still-needed manifest look orphaned.
        raise ValueError(f'Corrupt or unreadable {description}') from exc


def referenced_manifest_ids(metadata_rows, inbox_rows=()):
    """Given an iterable of live metadata rows and an iterable of durable
    source-completion inbox rows (upload_waves.ShadowWaves's
    `source-completion!...` partition -- pass every row for the library, not
    a sample), returns the set of artifact manifest IDs still referenced via
    a row's `processingCompletionOutbox` backlog, its committed
    `artifactReceipt` pointer, or an inbox entry's own `artifactReceipt`.

    Deliberately does NOT treat `processingCompletionAck` as a manifest
    reference -- it is a completion-INTENT identity (processing_completion.
    completion_identity), a different identity space entirely; treating it
    as one was a bug (it happened to never collide with a real manifest ID,
    but asserted a reference that doesn't exist).

    Callers must survey every live metadata row and every inbox row for the
    library, not a sample, before treating anything outside this set as
    orphaned -- this function only aggregates what it is given. Raises on
    any entry whose completion evidence cannot be parsed, rather than
    silently skipping it (see `_parse_json`).
    """
    referenced = set()
    for row in metadata_rows:
        backlog = row.get('processingCompletionOutbox')
        if backlog:
            for item in _parse_json(backlog, f"processingCompletionOutbox for {row.get('RowKey')!r}"):
                receipt = item.get('artifactReceipt') if isinstance(item, dict) else None
                if isinstance(receipt, dict) and isinstance(receipt.get('id'), str):
                    referenced.add(receipt['id'])
        receipt_raw = row.get('artifactReceipt')
        if receipt_raw:
            parsed = _parse_json(receipt_raw, f"artifactReceipt for {row.get('RowKey')!r}")
            if isinstance(parsed, dict) and isinstance(parsed.get('id'), str):
                referenced.add(parsed['id'])
    for row in inbox_rows:
        payload_raw = row.get('payload')
        if not payload_raw:
            continue
        payload = _parse_json(payload_raw, f"source-completion inbox payload for {row.get('RowKey')!r}")
        receipt = payload.get('artifactReceipt') if isinstance(payload, dict) else None
        if isinstance(receipt, dict) and isinstance(receipt.get('id'), str):
            referenced.add(receipt['id'])
    return referenced


def _manifest_rows(table, library):
    partition = 'processing-artifacts-' + digest(library)
    for row in table.query_entities(f"PartitionKey eq '{partition}'", results_per_page=1000):
        if row['RowKey'].startswith('manifest!'):
            yield row


def orphaned_manifest_candidates(table, library, referenced_ids):
    """Read-only scan of this library's artifact partition for manifest rows
    not in `referenced_ids`. Returns candidates for the caller to delete
    only after it has independently confirmed `referenced_ids` reflects
    every live metadata row AND every inbox row -- a stale or partial
    survey here would make a still-needed manifest look orphaned.
    """
    return [row for row in _manifest_rows(table, library)
            if row['RowKey'][len('manifest!'):] not in referenced_ids]


def retained_manifest_rows(table, library, referenced_ids):
    """The complement of `orphaned_manifest_candidates`: manifest rows that
    ARE still referenced. Feed these into `protected_artifacts` to compute
    the blob/snapshot sets `delete_orphaned_manifest` must never delete,
    even when they also happen to appear inside an orphaned manifest.
    """
    return [row for row in _manifest_rows(table, library)
            if row['RowKey'][len('manifest!'):] in referenced_ids]


def protected_artifacts(retained_rows):
    """Aggregates every derivative blob name and face-snapshot ID referenced
    by manifests the caller has confirmed are still live (`retained_manifest_
    rows`). A derivative blob is content-addressed by its bytes, not by
    which manifest captured it -- two distinct manifests (different
    generations, different face snapshots) can reference the identical
    blob if a recapture produced byte-identical thumbnail/preview output.
    Deleting an orphaned manifest must never delete a blob or snapshot that
    a RETAINED manifest also points to.
    """
    blobs, snapshots = set(), set()
    for row in retained_rows:
        payload = _parse_json(row['payload'], f"manifest payload for {row.get('RowKey')!r}")
        blobs.update(receipt['blob'] for receipt in payload.get('derivatives', {}).values())
        snapshots.update(payload.get('faceSnapshots', []))
    return blobs, snapshots


def delete_orphaned_manifest(table, storage, manifest_row, *, protected_blobs=frozenset(), protected_snapshots=frozenset()):
    """Permanently deletes one confirmed-orphaned manifest: its published
    derivative blobs (except any in `protected_blobs`), then its face
    snapshot rows (except any in `protected_snapshots`), then the manifest
    row itself -- blobs first, so a crash after partial deletion leaves the
    manifest row as evidence of what's still outstanding rather than
    orphaning blobs with no remaining record of them.

    The caller must have already confirmed this manifest is not in the
    referenced set (orphaned_manifest_candidates) and computed
    `protected_blobs`/`protected_snapshots` from every RETAINED manifest
    (protected_artifacts(retained_manifest_rows(...))); this function
    trusts those decisions rather than re-deriving them.
    """
    payload = _parse_json(manifest_row['payload'], f"manifest payload for {manifest_row.get('RowKey')!r}")
    container = storage._CTX.get('blob_thumbnail_container')
    deleted_blobs = skipped_blobs = 0
    for receipt in payload.get('derivatives', {}).values():
        if receipt['blob'] in protected_blobs:
            skipped_blobs += 1
            continue
        if not container:
            continue
        blob = storage._get_blob_client(container, receipt['blob'])
        if blob is None:
            continue
        try:
            blob.delete_blob()
            deleted_blobs += 1
        except ResourceNotFoundError:
            pass
    deleted_snapshots = skipped_snapshots = 0
    for snapshot_id in payload.get('faceSnapshots', []):
        if snapshot_id in protected_snapshots:
            skipped_snapshots += 1
            continue
        # Explicit existence check before delete: some table fakes (and not
        # every real backend) raise on deleting an absent key, so this must
        # not rely on that to decide whether a deletion actually happened.
        try:
            table.get_entity(manifest_row['PartitionKey'], 'face!' + snapshot_id)
        except ResourceNotFoundError:
            continue
        table.delete_entity(manifest_row['PartitionKey'], 'face!' + snapshot_id)
        deleted_snapshots += 1
    table.delete_entity(manifest_row['PartitionKey'], manifest_row['RowKey'])
    return dict(deletedBlobs=deleted_blobs, deletedSnapshots=deleted_snapshots,
                skippedBlobs=skipped_blobs, skippedSnapshots=skipped_snapshots)


def _wave_store(table,library):
    from wave_policy import owned
    from upload_waves import ShadowWaves
    return ShadowWaves(table,library) if owned(table,library) else None


def check_source_policy(table,library):
    """Called before source claims and commits; durable cleanup never falls back."""
    store=_wave_store(table,library)
    if not store:return
    policy=store._required('policy')
    if policy.get('cleanupState') or policy.get('gcUntil',0)>store.clock():
        raise RuntimeError('Processing is fenced by library cleanup')


def _all_pages(store,prefix):
    after=''
    while True:
        rows=store._page(prefix,after)
        yield from rows
        if len(rows)<20:return
        after=rows[-1]['RowKey']


def fence_photo_deletion(table,metadata,library,filenames):
    """Keep a retryable metadata tombstone until strict face/blob deletion succeeds.
    Frozen assignment/index work must finish before deleting its live inputs.
    """
    store=_wave_store(table,library)
    if not store:return
    from upload_waves import Conflict
    owner=uuid.uuid4().hex
    def acquire():
        policy=store._required('policy');control=store._get('control')
        if policy.get('cleanupState') or policy.get('gcUntil',0)>store.clock():
            raise Conflict('Library cleanup is already claimed')
        store._commit(control,[store._change(policy,dict(policy,gcOwner=owner,gcUntil=store.clock()+600))],maintenance=True)
    store._retry(acquire)
    def check():
        policy=store._required('policy')
        if policy.get('gcOwner')!=owner or policy.get('gcUntil',0)<=store.clock():
            raise Conflict('Deletion preparation lease expired; retry')
    try:
        _fence_photo_deletion(store,metadata,filenames,check)
    finally:
        def release():
            policy=store._required('policy')
            if policy.get('gcOwner')==owner:
                store._commit(store._get('control'),[store._change(policy,dict(policy,gcOwner='',gcUntil=0))],maintenance=True)
        store._retry(release)
    request_artifact_cleanup(table,library)


def _fence_photo_deletion(store,metadata,filenames,check):
    library=store.library
    from azure.core import MatchConditions
    from azure.data.tables import UpdateMode
    from storage_utils import _processing_is_expired
    from wave_settlement import deletion_key
    from upload_waves import Conflict
    targets={}
    for filename in filenames:
        try:row=metadata.get_entity(library,filename)
        except ResourceNotFoundError:continue
        if row.get('anonymousImageId') and row.get('processingRevision'):
            targets[filename]=row
    for wave in _all_pages(store,'wave!'):
        if wave['state'] not in ('ready','indexing'):continue
        for row in targets.values():
            key=f'e!{wave["waveId"]}!m!'+digest([row['anonymousImageId'],row['processingRevision']])
            if store._get(key):raise Conflict('Photo is in frozen wave work; retry deletion after publication')
    for filename,row in targets.items():
        if row.get('processing_lease_owner') and not _processing_is_expired(row):
            raise Conflict('Photo processing is leased; retry deletion after its lease ends')
        check()
        metadata.update_entity(dict(PartitionKey=library,RowKey=filename,processing_state='deleted',
            processingDeletedAttempt=row.get('processingFence') or row.get('processingDeletedAttempt',''),
            processingFence='',processing_lease_owner='',processing_lease='',processing_lease_expires_at=''),
            mode=UpdateMode.MERGE,etag=row.metadata['etag'],match_condition=MatchConditions.IfNotModified)
        key=deletion_key(row['anonymousImageId'],row['processingRevision'])
        def record():
            if store._get(key):return
            control=store._get('control')
            proof=store._row(key,photoId=row['anonymousImageId'],processingRevision=row['processingRevision'],
                filename=filename,processingAttempt=row.get('processingFence') or row.get('processingDeletedAttempt',''),deletedAt=store.clock())
            policy=store._required('policy')
            store._commit(control,[('create',proof),store._change(policy,dict(policy,gcRequested=True))],maintenance=True)
        store._retry(record)
        # Clear pre-freeze obligations before releasing the policy pause.
        # A concurrent evaluator cannot freeze between the survey and tombstone.
        from wave_settlement import settle
        for wave in _all_pages(store,'wave!'):
            if wave['state'] in ('complete','cancelled'):continue
            member=store._get(f'e!{wave["waveId"]}!m!'+digest([row['anonymousImageId'],row['processingRevision']]))
            if member:
                check()
                settle(store,wave['waveId'],member,'deleted','permanent_deletion',
                    witness=dict(store._required(key)),maintenance=True)


def begin_library_cleanup(table,metadata,library):
    store=_wave_store(table,library)
    if not store:return False
    from upload_waves import Conflict
    for wave in _all_pages(store,'wave!'):
        if wave['state']=='collecting':
            raise Conflict('Upload intake is open; stop or abandon it before library cleanup')
    def mark():
        policy=store._required('policy');control=store._get('control')
        if policy.get('cleanupState')=='cleaning':return
        store._commit(control,[store._change(policy,dict(policy,cleanupState='cleaning',
            cleanupBefore=store.clock(),gcRequested=True,gcRequestId=uuid.uuid4().hex))],maintenance=True)
    store._retry(mark)
    # Existing workers observe the fence, then their renewable leases drain.
    for lease in _all_pages(store,library+'!'):
        if lease.get('status')=='running' and lease.get('leaseUntil',0)>store.clock():
            raise Conflict('Wave stage is draining for library cleanup; retry')
    for wave in _all_pages(store,'wave!'):
        if wave['state'] in ('complete','cancelled'):continue
        def cancel():
            current=store._required(wave['RowKey']);control=store._required('control')
            store._commit(control,[store._change(current,dict(current,state='cancelled',
                cancellationReason='library_cleanup',lastProgressAt=store.clock(),stateEnteredAt=store.clock()))],maintenance=True)
        store._retry(cancel)
    def detach():
        control=store._get('control')
        if control:
            store._commit(control,[],maintenance=True)
            latest=store._required('control')
            store.table.submit_transaction([store._change(latest,dict(latest,collectingWaveId='',tailWaveId=''))])
    store._retry(detach)
    # Invalidate source attempts before the caller starts physical deletion.
    query="PartitionKey eq '"+library.replace("'","''")+"'"
    from azure.core import MatchConditions
    from azure.data.tables import UpdateMode
    for row in metadata.query_entities(query):
        if row.get('anonymousImageId') and row.get('processingRevision'):
            from wave_settlement import deletion_key
            key=deletion_key(row['anonymousImageId'],row['processingRevision'])
            def record():
                if store._get(key):return
                store._commit(store._get('control'),[('create',store._row(key,
                    photoId=row['anonymousImageId'],processingRevision=row['processingRevision'],filename=row['RowKey'],
                    processingAttempt=row.get('processingFence') or row.get('processingDeletedAttempt',''),deletedAt=store.clock()))],maintenance=True)
            store._retry(record)
        metadata.update_entity(dict(PartitionKey=library,RowKey=row['RowKey'],processing_state='deleted',
            processingDeletedAttempt=row.get('processingFence') or row.get('processingDeletedAttempt',''),
            processingFence='',processing_lease_owner='',processing_lease='',processing_lease_expires_at=''),
            mode=UpdateMode.MERGE,etag=row.metadata['etag'],match_condition=MatchConditions.IfNotModified)
    return True


def collect_library_artifacts(table,storage,metadata,library,*,limit=50,purge=False,lease_check=lambda:None):
    """Full reference survey, bounded physical deletion. Retain unfinished waves.
    Source claims are paused during this short GC; existing leases cause retry.
    """
    store=_wave_store(table,library)
    if not store:return dict(deletedManifests=0)
    if not 1<=limit<=1000:raise ValueError('Cleanup bound exceeded')
    import uuid
    from upload_waves import Conflict
    owner=uuid.uuid4().hex
    requested=[None]
    def acquire():
        policy=store._required('policy');control=store._get('control')
        if policy.get('gcUntil',0)>store.clock():raise Conflict('Artifact cleanup already claimed')
        requested[0]=policy.get('gcRequestId')
        store._commit(control,[store._change(policy,dict(policy,gcOwner=owner,gcUntil=store.clock()+600))],advance=False,maintenance=True)
    store._retry(acquire)
    def check():
        lease_check()
        policy=store._required('policy')
        if policy.get('gcOwner')!=owner or policy.get('gcUntil',0)<=store.clock():
            raise Conflict('Artifact cleanup ownership expired')
    try:
        query="PartitionKey eq '"+library.replace("'","''")+"'"
        rows=list(metadata.query_entities(query,select=['PartitionKey','RowKey','anonymousImageId','processingRevision',
            'processing_state','processing_lease_owner','processing_lease_expires_at','artifactReceipt','processingCompletionOutbox']))
        from storage_utils import _processing_is_expired
        if any(row.get('processing_lease_owner') and not _processing_is_expired(row) for row in rows):
            raise Conflict('Active source leases prevent artifact cleanup')
        protected_sources=set()
        from wave_settlement import verified
        for wave in _all_pages(store,'wave!'):
            if wave['state'] in ('complete','cancelled'):continue
            for member in _all_pages(store,f'e!{wave["waveId"]}!m!'):
                if member['outcome']=='settled' or not verified(store,member):
                    protected_sources.add((member['photoId'],member['processingRevision']))
        live={(row.get('anonymousImageId'),row.get('processingRevision')) for row in rows
              if row.get('processing_state')!='deleted'}
        # Old inbox references can be released only after every dependent wave
        # has finished (or acquired an explicit exclusion witness).
        inbox=[]
        for entry in _all_pages(store,'source-completion!'):
            source=(entry['photoId'],entry['processingRevision'])
            if source not in live and source not in protected_sources:
                check();table.delete_entity(store.partition,entry['RowKey'])
            else:inbox.append(entry)
        referenced=referenced_manifest_ids([row for row in rows if row.get('processing_state')!='deleted'
            or (row.get('anonymousImageId'),row.get('processingRevision')) in protected_sources],inbox)
        retained=retained_manifest_rows(table,library,referenced)
        blobs,snapshots=protected_artifacts(retained)
        # Exact deleted attempts are recoverable even after metadata is gone.
        # Never enumerate or delete another generation's staging namespace.
        policy=store._required('policy')
        staging=store._page('deleted-source!',policy.get('stagingGcAfter',''))
        staging_blocked=False
        for deleted_source in staging:
            source=(deleted_source['photoId'],deleted_source['processingRevision'])
            if source in protected_sources:
                staging_blocked=True;continue
            attempt=deleted_source.get('processingAttempt')
            if attempt:
                check()
                delete_staging_attempt(storage,library,*source,attempt,current_attempt='')
        deleted=0
        candidates=orphaned_manifest_candidates(table,library,referenced)
        for candidate in candidates[:limit]:
            check()
            delete_orphaned_manifest(table,storage,candidate,protected_blobs=blobs,protected_snapshots=snapshots)
            deleted+=1
        policy=store._required('policy')
        projection_pending=False
        if policy.get('cleanupBefore'):
            _,projection_pending=_cleanup_projection_history(store,storage,policy['cleanupBefore'],max(0,limit-deleted),check)
        # Capture can crash between writing a face snapshot and its manifest.
        # A durable cursor prevents protected first pages from starving orphans.
        partition='processing-artifacts-'+digest(library)
        cursor=policy.get('snapshotGcAfter','')
        query=f"PartitionKey eq '{partition}' and RowKey ge 'face!' and RowKey lt 'face!~'"
        if cursor:query+=f" and RowKey gt '{cursor}'"
        from itertools import islice
        page=list(islice(table.query_entities(query,results_per_page=20),20))
        for snapshot in page:
            if snapshot['RowKey'][len('face!'):] not in snapshots:
                check();table.delete_entity(partition,snapshot['RowKey'])
        def progress():
            policy=store._required('policy');control=store._get('control')
            # A retained deleted source will be collected after its wave settles.
            pending=(len(candidates)>limit or bool(protected_sources-live) or projection_pending
                or len(page)==20 or len(staging)==20 or staging_blocked or policy.get('gcRequestId')!=requested[0])
            store._commit(control,[store._change(policy,dict(policy,gcRequested=pending,
                stagingGcAfter=staging[-1]['RowKey'] if len(staging)==20 else '',
                snapshotGcAfter=page[-1]['RowKey'] if len(page)==20 else ''))],advance=False,maintenance=True)
        store._retry(progress)
        return dict(deletedManifests=deleted)
    finally:
        def release():
            policy=store._required('policy')
            if policy.get('gcOwner')!=owner:return
            control=store._get('control')
            store._commit(control,[store._change(policy,dict(policy,gcOwner='',gcUntil=0))],advance=False,maintenance=True)
        store._retry(release)


def finish_library_cleanup(table,library,*,purged=False):
    store=_wave_store(table,library)
    if not store:return
    def finish():
        policy=store._required('policy');control=store._get('control')
        store._commit(control,[store._change(policy,dict(policy,cleanupState='deleted' if purged else ''))],maintenance=True)
    store._retry(finish)


GC_PARTITION='upload-wave-cleanup-pending'


def request_artifact_cleanup(table,library):
    store=_wave_store(table,library)
    if not store:return
    def record():
        policy=store._required('policy');control=store._get('control')
        import uuid
        store._commit(control,[store._change(policy,dict(policy,gcRequested=True,gcRequestId=uuid.uuid4().hex))],maintenance=True)
    store._retry(record)


def relay_artifact_cleanup(table,library,send):
    """Recovery only sends a durable wake; the full reference survey stays off it."""
    store=_wave_store(table,library)
    if not store:return False
    policy=store._required('policy')
    if not policy.get('gcRequested') or policy.get('nextGcAt',0)>store.clock():return False
    from index_work import WorkStore
    work=WorkStore(table,partition=GC_PARTITION)
    token=work.request(library,'processing-artifacts',advance=False)
    return work.notify(token,send)


def send_artifact_cleanup(library,scope,revision,delay=0):
    import app
    if app.library_ops_queue_client is None:raise RuntimeError('Cleanup queue unavailable')
    app.library_ops_queue_client.send_message(json.dumps(dict(type='processing_artifact_cleanup',
        userId=library,scope=scope,intentRevision=revision)),visibility_timeout=delay or None)


def run_artifact_cleanup(payload):
    import app
    from index_work import WorkStore
    from index_publication import publication
    from wave_policy import owned,require_capability
    library=payload['userId']
    store=_wave_store(app.jobs_table_client,library)
    if not store:return
    require_capability(store,'cluster',allow_cleanup=True)
    work=WorkStore(store.table,partition=GC_PARTITION)
    claim=work.claim(work.key(library,'processing-artifacts'))
    if claim is None:return
    if claim.revision!=payload.get('intentRevision'):
        work.release(claim,deferred=True);return
    try:
        with publication(work,claim) as guard:
            result=collect_library_artifacts(store.table,app.storage_utils_module,app.metadata_table_client,
                library,lease_check=guard.check)
        if store._required('policy').get('gcRequested'):
            work.release(claim,deferred=True)
        else:work.finish(claim)
        return result
    except Exception:
        work.release(claim,deferred=True)
        raise


def opportunistic_artifact_cleanup(table,storage,metadata,library):
    """Deletion success is independent of a retryable bounded GC failure."""
    try:return collect_library_artifacts(table,storage,metadata,library)
    except Exception:
        storage._LOGGER.warning('Processing artifact cleanup deferred for %s',library,exc_info=True)
        return dict(deferred=True)


def _cleanup_projection_history(store,storage,cutoff,budget,check):
    """Full-library cleanup only: old immutable outputs and completed journals.
    Keep the wave row/receipt as the final retry anchor until its children go.
    """
    used=0
    from wave_publication import PARTITION_PREFIX
    for wave in _all_pages(store,'wave!'):
        if wave['state'] not in ('complete','cancelled') or wave['createdAt']>cutoff:continue
        wave_id=wave['waveId']
        for proof in _all_pages(store,f'publication!{wave_id}!'):
            for output in json.loads(proof['payload'])['outputs']:
                if used>=budget:return used,True
                check()
                client=storage._get_blob_client(output['container'],output['blob'])
                if client is None:raise RuntimeError('Projection cleanup storage unavailable')
                try:client.delete_blob();used+=1
                except ResourceNotFoundError:pass
            check();store.table.delete_entity(store.partition,proof['RowKey']);used+=1
            if used>=budget:return used,True
        # The obligation partition belongs only to this immutable wave ID.
        partition=PARTITION_PREFIX+wave_id
        for row in store.table.query_entities("PartitionKey eq '"+partition+"'",results_per_page=20):
            if used>=budget:return used,True
            check();store.table.delete_entity(partition,row['RowKey']);used+=1
        # Journals include frozen faces and old baseline embeddings. Their
        # resumability is intentionally retired by a full library wipe.
        for prefix in (f'assignment!{wave_id}!',f'e!{wave_id}!'):
            for row in _all_pages(store,prefix):
                if used>=budget:return used,True
                check();store.table.delete_entity(store.partition,row['RowKey']);used+=1
    return used,False
