"""Bounded, owner-requested cleanup of abandoned upload reservations.

Uses metadata as the durable cleanup manifest, so it also works after Core
removed a session's lifecycle-directory entry or the browser lost its checkpoint.
"""
import time
import uuid
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from itertools import islice

from azure.core import MatchConditions
from azure.core.exceptions import AzureError, ResourceNotFoundError
from azure.data.tables import UpdateMode

from upload_waves import Conflict, ShadowWaves, _etag

PAGE_SIZE = 25
QUIET_SECONDS = 300


def _time(value):
    if not value:
        return 0
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return parsed.replace(tzinfo=timezone.utc).timestamp() if parsed.tzinfo is None else parsed.timestamp()


def _idle(app, store):
    query = (f"PartitionKey eq '{store.partition}' and RowKey ge 'session!' "
             "and RowKey lt 'session!~' and state eq 'open'")
    if next(iter(store.table.query_entities(query, results_per_page=1)), None) is not None:
        raise Conflict('An upload is still open. Retry or discard it before running upload cleanup.')
    try:
        activity = app.metadata_table_client.get_entity('clustering_maintenance', store.library)
    except ResourceNotFoundError:
        activity = {}
    if _time(activity.get('lastUploadActivityAt')) > store.clock() - QUIET_SECONDS:
        raise Conflict('Uploads were active recently. Wait five minutes, then run upload cleanup again.')
    from execution_lease import ExecutionLeases
    if ExecutionLeases(store.table, partition=store.partition, clock=store.clock).active(store.library):
        raise Conflict('Photo processing is still running. Run upload cleanup after it finishes.')


@contextmanager
def _fence(app, store):
    owner = uuid.uuid4().hex
    def acquire():
        policy = store._required('policy')
        if policy.get('cleanupState') or policy.get('gcUntil', 0) > store.clock():
            raise Conflict('Another cleanup is running. Try upload cleanup again shortly.')
        store._commit(store._get('control'), [store._change(policy,
            dict(policy, gcOwner=owner, gcUntil=store.clock()+120))], advance=False, maintenance=True)
    _idle(app, store)
    store._retry(acquire)
    def check():
        policy = store._required('policy')
        if policy.get('gcOwner') != owner or policy.get('gcUntil', 0) <= store.clock():
            raise Conflict('Upload cleanup ownership expired. Run cleanup again to continue.')
    try:
        # Admission and stage ownership can race the initial survey. The policy
        # transaction fences new writers; repeat the survey after acquiring it.
        _idle(app, store)
        yield check
    finally:
        def release():
            policy = store._required('policy')
            if policy.get('gcOwner') == owner:
                store.table.submit_transaction([store._change(policy, dict(policy, gcOwner='', gcUntil=0))])
        store._retry(release)


def _clean_row(app, store, row, check):
    if not any(field in row for field in app.UPLOAD_TRACKING_FIELDS):
        return 'skipped'
    last = max(_time(row.get(field)) for field in ('upload_started_at', 'upload_last_chunk_at'))
    if last > store.clock() - QUIET_SECONDS:
        return 'skipped'
    lease_until = _time(row.get('processing_lease_expires_at'))
    if row.get('processing_lease_owner') and lease_until > store.clock():
        return 'skipped'
    pending = str(row.get('pendingAnonymousBlob') or '')
    blobs = json.loads(row.get('uploadCleanupBlobs') or '[]')
    if not isinstance(blobs, list) or any(not isinstance(name, str) for name in blobs):
        raise ValueError('Invalid upload cleanup record')
    if pending and pending != row.get('anonymousImageId') and pending not in blobs:
        blobs.append(pending)
        # Persist cleanup identities before deletion. A later new upload must
        # allocate a fresh blob instead of reusing a partly deleted reservation.
        updated = dict(row, uploadCleanupBlobs=json.dumps(blobs))
        check()
        app.metadata_table_client.update_entity(updated, mode=UpdateMode.REPLACE,
            etag=_etag(row), match_condition=MatchConditions.IfNotModified)
        row = app.metadata_table_client.get_entity(store.library, row['RowKey'])
        if max(_time(row.get(field)) for field in ('upload_started_at', 'upload_last_chunk_at')) > store.clock()-QUIET_SECONDS:
            return 'skipped'
    for pending in blobs:
        if not pending or pending == row.get('anonymousImageId'):
            continue
        library = store.library.replace("'", "''")
        escaped = pending.replace("'", "''")
        # A rename may have promoted this reservation under another filename.
        # Never delete a blob referenced by any finalized row in this library.
        owners = app.metadata_table_client.query_entities(
            f"PartitionKey eq '{library}' and anonymousImageId eq '{escaped}'", select=['RowKey'])
        if next(iter(owners), None) is None:
            for container, name in (
                    (app.BLOB_IMAGE_CONTAINER, pending),
                    (app.BLOB_THUMBNAIL_CONTAINER, pending),
                    (app.BLOB_THUMBNAIL_CONTAINER, f'preview/{pending}.jpg'),
                    (app.BLOB_THUMBNAIL_CONTAINER, f'metadata/{pending}.bin')):
                check()
                if app._delete_blob_if_present(container, name):
                    raise AzureError('Could not remove a staged upload blob')
            if app.image_names_table_client is not None:
                check()
                try:
                    app.image_names_table_client.delete_entity(store.library, pending)
                except ResourceNotFoundError:
                    pass
    check()
    options = dict(etag=_etag(row), match_condition=MatchConditions.IfNotModified)
    completed = bool(any(row.get(field) for field in (
        'anonymousImageId', 'fileHash', 'perceptualHash', 'mimeType', 'verification_status')))
    if completed:
        updated = {key: value for key, value in row.items() if key not in app.UPLOAD_TRACKING_FIELDS}
        app.metadata_table_client.update_entity(updated, mode=UpdateMode.REPLACE, **options)
        return 'trackingCleared'
    app.metadata_table_client.delete_entity(store.library, row['RowKey'], **options)
    return 'deleted'


def cleanup(app, library, *, after='', clock=time.time):
    if not isinstance(after, str) or len(after) > 1024 or any(c in after for c in '\r\n\x00'):
        raise ValueError('Invalid cleanup cursor')
    if app.metadata_table_client is None or app.blob_service_client is None or app.jobs_table_client is None:
        raise AzureError('Upload cleanup storage unavailable')
    from wave_policy import owned, require_capability
    if not owned(app.jobs_table_client, library):
        raise Conflict('Upload cleanup requires this library to use upload waves.')
    store = ShadowWaves(app.jobs_table_client, library, clock=clock)
    require_capability(store, 'upload')
    result = dict(scanned=0, deleted=0, trackingCleared=0, skipped=0, errors=[])
    with _fence(app, store) as check:
        escaped = library.replace("'", "''")
        query = (f"PartitionKey eq '{escaped}' and (pendingAnonymousBlob ne '' "
                 "or upload_total_size ge 0 or upload_started_at ne '' "
                 "or upload_last_chunk_at ne '' or received_ranges ne '' or uploadCleanupBlobs ne '')")
        if after:
            query += " and RowKey gt '" + after.replace("'", "''") + "'"
        rows = list(islice(app.metadata_table_client.query_entities(query, results_per_page=PAGE_SIZE), PAGE_SIZE))
        for row in rows:
            check()
            result['scanned'] += 1
            try:
                action = _clean_row(app, store, row, check)
                result[action] += 1
            except (AzureError, ValueError, TypeError):
                app.app.logger.exception('Abandoned upload cleanup could not finish')
                result['errors'].append(dict(filename=row['RowKey'], error='Cleanup failed; run upload cleanup again to retry.'))
        result.update(nextCursor=rows[-1]['RowKey'] if len(rows)==PAGE_SIZE else '',
                      more=len(rows)==PAGE_SIZE, success=not result['errors'])
    return result
