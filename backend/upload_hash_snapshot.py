"""Private, prebuilt upload-dedup downloads; no table scan on the HTTP path."""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone

from azure.core.exceptions import ResourceNotFoundError
from azure.data.tables import TableTransactionError

import index_files
import perf_instrumentation
import storage_utils as su

SNAPSHOT_ROW = '!upload-hash-snapshot'
DELETE_EPOCH_ROW = '!upload-hash-deletes'
CONTROL_ROWS = {SNAPSHOT_ROW, DELETE_EPOCH_ROW}
REFRESH_SECONDS = 60


def covers_upload_activity(user_id, last_activity_at):
    """Avoid a second idle rebuild if an explicit end-of-upload build covered it."""
    table = su._CTX.get('hash_index_table_client')
    if table is None or not last_activity_at:
        return False
    manifest = _row(table, user_id, SNAPSHOT_ROW)
    try:
        covers = datetime.fromisoformat(str(manifest.get('sourceReadStartedAt') or '')) >= datetime.fromisoformat(str(last_activity_at))
    except (ValueError, TypeError):
        return False
    return (manifest.get('schemaVersion') == 1
            and str(manifest.get('blobName') or '').startswith(_prefix(user_id))
            and str(manifest.get('deleteEpoch') or '') == _epoch(table, user_id)
            and covers)


def _row(table, user_id, row_key):
    try:
        return table.get_entity(partition_key=user_id, row_key=row_key)
    except ResourceNotFoundError:
        return {}


def _prefix(user_id):
    return hashlib.sha256(user_id.encode('utf-8')).hexdigest() + '-upload-hashes-'


def _epoch(table, user_id):
    return str(_row(table, user_id, DELETE_EPOCH_ROW).get('epoch') or '')


def delete_hash(table, user_id, file_hash):
    """Invalidate old snapshots atomically with deletion of their source row."""
    if file_hash in CONTROL_ROWS:
        return
    marker = {'PartitionKey': user_id, 'RowKey': DELETE_EPOCH_ROW, 'epoch': uuid.uuid4().hex}
    transaction = getattr(table, 'submit_transaction', None)
    if transaction is None:
        # Older adapters/test doubles: invalidating first is the safe ordering.
        table.upsert_entity(marker)
        table.delete_entity(partition_key=user_id, row_key=file_hash)
        return
    try:
        transaction([
            ('upsert', marker),
            ('delete', {'PartitionKey': user_id, 'RowKey': file_hash}),
        ])
    except (ResourceNotFoundError, TableTransactionError) as error:
        if isinstance(error, TableTransactionError) and error.status_code != 404:
            raise
        # The row may already be gone; its hash can still be in an old snapshot.
        table.upsert_entity(marker)


def read_snapshot(user_id):
    """Return (compressed bytes or None, manifest, refresh needed), fail closed."""
    table = su._CTX.get('hash_index_table_client')
    if table is None:
        return None, {}, False
    try:
        manifest = _row(table, user_id, SNAPSHOT_ROW)
        blob_name = str(manifest.get('blobName') or '')
        if (not blob_name.startswith(_prefix(user_id))
                or manifest.get('schemaVersion') != 1
                or str(manifest.get('deleteEpoch') or '') != _epoch(table, user_id)):
            return None, {}, True
        updated = datetime.fromisoformat(str(manifest['updatedAt']))
        age = (datetime.now(timezone.utc) - updated).total_seconds()
        blob = su._get_blob_client(su._lexical_index_container_name(), blob_name)
        if blob is None:
            return None, {}, True
        payload = blob.download_blob().readall()
        # A deletion during the download must not turn into a known stale skip.
        if str(manifest.get('deleteEpoch') or '') != _epoch(table, user_id):
            return None, {}, True
        return payload, manifest, age >= REFRESH_SECONDS
    except Exception:
        su._LOGGER.warning('Upload hash snapshot unavailable for %s', user_id, exc_info=True)
        return None, {}, True


def rebuild_snapshot(user_id):
    """Stream one library's hash partition into a gzip file on the build worker."""
    table = su._CTX.get('hash_index_table_client')
    if table is None:
        return None
    container = su._lexical_index_container_name()
    epoch = _epoch(table, user_id)
    previous = _row(table, user_id, SNAPSHOT_ROW)
    blob_name = _prefix(user_id) + uuid.uuid4().hex + '.json.gz'
    blob = su._get_blob_client(container, blob_name)
    if blob is None:
        raise RuntimeError('upload hash snapshot storage is not configured')
    count = 0
    source_read_started_at = datetime.now(timezone.utc).isoformat()
    published = False
    try:
        with index_files.workspace() as workdir:
            path = os.path.join(workdir, 'upload-hashes.json.gz')
            with perf_instrumentation.step('upload.hashes.build', user=user_id):
                with gzip.open(path, 'wb', compresslevel=3) as out:
                    out.write(b'{"hashes":{')
                    rows = table.query_entities(
                        f"PartitionKey eq '{su._escape_odata(user_id)}'", select=['RowKey', 'filename'])
                    for row in rows:
                        file_hash = str(row.get('RowKey') or '')
                        filename = str(row.get('filename') or '')
                        if not re.fullmatch(r'[0-9a-f]{64}', file_hash) or not filename:
                            continue
                        if count:
                            out.write(b',')
                        out.write(json.dumps(file_hash).encode('ascii'))
                        out.write(b':')
                        out.write(json.dumps(filename, ensure_ascii=False).encode('utf-8'))
                        count += 1
                    out.write(b'}}')
                if epoch != _epoch(table, user_id):
                    raise RuntimeError('library hashes deleted during snapshot build; retry')
                su._upload_file_to_blob(blob, path, overwrite=True, content_settings=su.BlobContentSettings(
                    content_type='application/json', content_encoding='gzip'))
                compressed_bytes = os.path.getsize(path)
            # Publish only after the immutable data file is fully stored.
            manifest = {
                'PartitionKey': user_id, 'RowKey': SNAPSHOT_ROW, 'schemaVersion': 1,
                'blobName': blob_name, 'deleteEpoch': epoch, 'rows': count,
                'updatedAt': datetime.now(timezone.utc).isoformat(), 'compressedBytes': compressed_bytes,
                'sourceReadStartedAt': source_read_started_at,
            }
            table.upsert_entity(manifest)
            published = True
            perf_instrumentation.log_event('upload_hash_snapshot_built', user=user_id,
                                           rows=count, bytes=compressed_bytes)
        old_blob = str(previous.get('blobName') or '')
        if old_blob.startswith(_prefix(user_id)) and old_blob != blob_name:
            try:
                su._get_blob_client(container, old_blob).delete_blob()
            except Exception:
                su._LOGGER.debug('Could not remove superseded upload hash snapshot', exc_info=True)
        return manifest
    finally:
        if not published:
            try:
                blob.delete_blob()
            except Exception:
                pass
