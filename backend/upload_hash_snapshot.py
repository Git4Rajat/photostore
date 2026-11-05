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
import index_publication
from index_ownership import catalog_publisher
import perf_instrumentation
import storage_utils as su

SNAPSHOT_ROW = '!upload-hash-snapshot'
DELETE_EPOCH_ROW = '!upload-hash-deletes'
CONTROL_ROWS = {SNAPSHOT_ROW, DELETE_EPOCH_ROW}
REFRESH_SECONDS = 60
INCREMENTAL_MAX_HASHES = 512
# Bound both the targeted delta and each untrusted JSON string. The existing
# full builder is deliberately unchanged; oversized legacy rows defer a merge.
INCREMENTAL_MAX_FILENAME_BYTES = 64 * 1024
_JSON_CHUNK_BYTES = 16 * 1024


class _SnapshotJSON:
    """Small strict parser for the legacy single-line hashes object, not a map.

    index_files.iter_rows has a whole-document legacy fallback, and ijson is
    not a backend dependency. Only one bounded string and one chunk live here.
    """

    def __init__(self, stream):
        self.stream = stream
        self.chunk = b''
        self.position = 0

    def byte(self):
        if self.position == len(self.chunk):
            self.chunk = self.stream.read(_JSON_CHUNK_BYTES)
            self.position = 0
            if not self.chunk:
                return None
        value = self.chunk[self.position]
        self.position += 1
        return value

    def token(self):
        value = self.byte()
        while value in (9, 10, 13, 32):
            value = self.byte()
        return value

    def expect(self, character):
        if self.token() != ord(character):
            raise RuntimeError('invalid upload hash snapshot JSON schema')

    def string(self, limit, first=None):
        if (self.token() if first is None else first) != ord('"'):
            raise RuntimeError('upload hash snapshot requires string keys and filenames')
        encoded = bytearray(b'"')
        escaped = False
        while True:
            value = self.byte()
            if value is None:
                raise RuntimeError('truncated upload hash snapshot JSON')
            encoded.append(value)
            if len(encoded) > 6 * limit + 2:
                raise RuntimeError('oversized upload hash snapshot string')
            if value == ord('"') and not escaped:
                break
            escaped = not escaped if value == ord('\\') else False
        try:
            decoded = json.loads(encoded)
            if len(decoded.encode('utf-8')) > limit:
                raise RuntimeError('oversized upload hash snapshot string')
            return decoded
        except (ValueError, UnicodeError) as error:
            raise RuntimeError('invalid upload hash snapshot string') from error

    def hashes(self):
        self.expect('{')
        if self.string(6) != 'hashes':
            raise RuntimeError('invalid upload hash snapshot JSON schema')
        self.expect(':')
        self.expect('{')
        first = self.token()
        if first != ord('}'):
            while True:
                file_hash = self.string(64, first)
                if not re.fullmatch(r'[0-9a-f]{64}', file_hash):
                    raise RuntimeError('invalid upload hash snapshot hash')
                self.expect(':')
                filename = self.string(INCREMENTAL_MAX_FILENAME_BYTES)
                if not filename:
                    raise RuntimeError('invalid upload hash snapshot filename')
                yield file_hash, filename
                separator = self.token()
                if separator == ord('}'):
                    break
                if separator != ord(','):
                    raise RuntimeError('invalid upload hash snapshot JSON schema')
                first = self.token()
        self.expect('}')
        if self.token() is not None:
            raise RuntimeError('trailing upload hash snapshot JSON')


def _snapshot_replace(row):
    from azure.core import MatchConditions
    from azure.data.tables import UpdateMode
    version = getattr(row, 'metadata', {}).get('etag')
    if not version or version == '*':
        raise RuntimeError('Snapshot publication requires native ETags')
    return dict(mode=UpdateMode.REPLACE, etag=version,
                match_condition=MatchConditions.IfNotModified)


@catalog_publisher
def upgrade_snapshot(user_id, affected_hashes, *, previous_source_version=None,
                     expected_blob_name=None, publication_check=None,
                     complete_dirty_hashes=False, expected_delete_epoch=None,
                     strict_source_reads=False):
    """Opt-in bounded merge; never scan, track dirtiness, or rebuild implicitly.

    At least one expected base is required. Since schema 1 has no sourceVersion,
    previous_source_version is an alias for its immutable blobName. The caller
    supplies ALL changes since that base and serializes hash additions/updates
    through publication (those writes do not advance deleteEpoch). A callback
    may assert the caller's lease before reads and before the transaction.

    Crossing deleteEpoch additionally requires complete_dirty_hashes=True and
    expected_delete_epoch: the caller's certification that *every* deletion
    since the expected base through that captured epoch is included. Matching
    this epoch also fences deletions before our initial epoch read. There is no
    durable dirty log here to infer completeness. Unresolved reads then defer,
    rather than making old deleted hashes readable again.
    With the same epoch, transient reads retain the base entry, never delete it.
    Validation/CAS failures raise, leaving the old pointer and base untouched.
    """
    expected = expected_blob_name or previous_source_version
    if (not isinstance(expected, str) or not expected
            or (previous_source_version is not None and previous_source_version != expected)
            or (expected_blob_name is not None and expected_blob_name != expected)):
        raise ValueError('a consistent expected_blob_name/previous_source_version is required')
    if isinstance(affected_hashes, (str, bytes)) or affected_hashes is None:
        raise ValueError('affected_hashes must be a bounded iterable of hashes')
    if (getattr(affected_hashes, 'has_more', False)
            or getattr(affected_hashes, 'truncated', False)):
        raise ValueError('affected_hashes capture is incomplete; defer')
    dirty = set()
    # Bound consumption too, including duplicate input and infinite iterables.
    for index, file_hash in enumerate(affected_hashes):
        if index >= INCREMENTAL_MAX_HASHES:
            raise ValueError('incremental upload hash snapshot is limited to 512 hashes')
        if not isinstance(file_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', file_hash):
            raise ValueError('affected_hashes must contain lowercase SHA-256 hashes')
        dirty.add(file_hash)
    table = su._CTX.get('hash_index_table_client')
    if table is None or not callable(getattr(table, 'submit_transaction', None)):
        raise RuntimeError('incremental snapshot requires transactional table storage')
    if publication_check is not None:
        publication_check()
    previous = _row(table, user_id, SNAPSHOT_ROW)
    if (previous.get('PartitionKey') != user_id or previous.get('RowKey') != SNAPSHOT_ROW
            or previous.get('schemaVersion') != 1
            or previous.get('blobName') != expected or not expected.startswith(_prefix(user_id))
            or type(previous.get('rows')) is not int or previous['rows'] < 0
            or not isinstance(previous.get('deleteEpoch'), str)):
        raise RuntimeError('stale or invalid expected upload hash snapshot base; defer')
    previous_cas = _snapshot_replace(previous)
    epoch_row = _row(table, user_id, DELETE_EPOCH_ROW)
    if not epoch_row:
        from azure.core.exceptions import ResourceExistsError
        try:
            # Preserve the legacy empty epoch while creating a row we can CAS.
            table.create_entity({'PartitionKey': user_id, 'RowKey': DELETE_EPOCH_ROW, 'epoch': ''})
        except ResourceExistsError:
            pass
        epoch_row = _row(table, user_id, DELETE_EPOCH_ROW)
    if (epoch_row.get('PartitionKey') != user_id or epoch_row.get('RowKey') != DELETE_EPOCH_ROW
            or not isinstance(epoch_row.get('epoch'), str)):
        raise RuntimeError('invalid upload hash delete epoch; defer')
    epoch_cas = _snapshot_replace(epoch_row)
    epoch = epoch_row['epoch']
    crossing_epoch = epoch != previous['deleteEpoch']
    if expected_delete_epoch is not None and expected_delete_epoch != epoch:
        raise RuntimeError('certified delete epoch changed; defer')
    if crossing_epoch and (complete_dirty_hashes is not True or expected_delete_epoch != epoch):
        raise RuntimeError('delete epoch changed; complete dirty hashes and epoch certification required; defer')
    delta = {}
    for file_hash in sorted(dirty):
        try:
            row = table.get_entity(partition_key=user_id, row_key=file_hash)
        except ResourceNotFoundError:
            delta[file_hash] = None  # ONLY an authoritative 404 confirms removal.
            continue
        except Exception:
            if crossing_epoch or strict_source_reads:
                raise RuntimeError('unresolved hash across delete epoch; defer')
            su._LOGGER.warning('Retaining unresolved upload hash %s for %s', file_hash, user_id)
            continue
        filename = row.get('filename')
        if (row.get('PartitionKey') != user_id or row.get('RowKey') != file_hash
                or not isinstance(filename, str) or not filename):
            raise RuntimeError('invalid authoritative upload hash row; defer')
        if len(filename) > INCREMENTAL_MAX_FILENAME_BYTES:
            raise RuntimeError('oversized authoritative upload hash filename; defer')
        try:
            size = len(filename.encode('utf-8'))
        except UnicodeError as error:
            raise RuntimeError('invalid authoritative upload hash filename; defer') from error
        if size > INCREMENTAL_MAX_FILENAME_BYTES:
            raise RuntimeError('oversized authoritative upload hash filename; defer')
        delta[file_hash] = filename
    container = su._lexical_index_container_name()
    base = su._get_blob_client(container, expected)
    blob_name = _prefix(user_id) + uuid.uuid4().hex + '.json.gz'
    blob = su._get_blob_client(container, blob_name)
    if base is None or blob is None:
        raise RuntimeError('upload hash snapshot storage is not configured')
    published = False
    try:
        with index_files.workspace() as workdir:
            base_path = os.path.join(workdir, 'base.json.gz')
            path = os.path.join(workdir, 'upload-hashes.json.gz')
            downloader = base.download_blob(decompress=False, max_concurrency=1)
            with open(base_path, 'wb') as target:
                # No readall fallback: legacy adapters must implement streaming.
                downloader.readinto(target)
            count = 0
            base_count = 0
            seen = set()  # Only affected keys, at most 512, never the base map.
            with gzip.open(base_path, 'rb') as source, gzip.open(path, 'wb', compresslevel=3) as out:
                out.write(b'{"hashes":{')

                def write(file_hash, filename):
                    nonlocal count
                    if count:
                        out.write(b',')
                    out.write(json.dumps(file_hash).encode('ascii'))
                    out.write(b':')
                    out.write(json.dumps(filename, ensure_ascii=False).encode('utf-8'))
                    count += 1

                for file_hash, filename in _SnapshotJSON(source).hashes():
                    base_count += 1
                    if file_hash in dirty:
                        if file_hash in seen:
                            raise RuntimeError('duplicate affected hash in base; defer')
                        seen.add(file_hash)
                    if file_hash in delta:
                        filename = delta[file_hash]
                    if filename is not None:
                        write(file_hash, filename)
                if base_count != previous['rows']:
                    raise RuntimeError('upload hash snapshot base row count mismatch; defer')
                for file_hash, filename in delta.items():
                    if file_hash not in seen and filename is not None:
                        write(file_hash, filename)
                out.write(b'}}')
            settings = su.BlobContentSettings(content_type='application/json', content_encoding='gzip')
            # Stream even for non-native adapters; the generic upload helper
            # has a bytes fallback that is unsuitable for an unbounded base.
            if callable(getattr(blob, 'upload_file', None)):
                blob.upload_file(path, overwrite=False, content_settings=settings)
            else:
                with open(path, 'rb') as source:
                    blob.upload_blob(source, overwrite=False, content_settings=settings)
            manifest = {
                'PartitionKey': user_id, 'RowKey': SNAPSHOT_ROW, 'schemaVersion': 1,
                'blobName': blob_name, 'deleteEpoch': epoch, 'rows': count,
                'updatedAt': datetime.now(timezone.utc).isoformat(),
                'compressedBytes': os.path.getsize(path),
                # A partial merge (especially transient failures) must not claim
                # a full source scan covered more recent upload activity.
                'sourceReadStartedAt': previous.get('sourceReadStartedAt', ''),
            }
            if publication_check is not None:
                publication_check()
            index_publication.check()
            table.submit_transaction([
                ('update', dict(epoch_row), epoch_cas),
                ('update', manifest, previous_cas),
            ])
            published = True
            index_publication.check()
            perf_instrumentation.log_event('upload_hash_snapshot_upgraded', user=user_id,
                                           rows=count, bytes=manifest['compressedBytes'])
            return manifest
    finally:
        # An ambiguous Table commit can have succeeded before a transport
        # error. Never delete a possibly referenced upload here; worker GC
        # checks current references and retirement grace under the real lock.
        if not published:
            su._LOGGER.debug('Retained unconfirmed upload hash generation for guarded GC')


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
        # Preserve stored gzip bytes for HTTP Content-Encoding; the SDK otherwise decodes them.
        payload = blob.download_blob(decompress=False).readall()
        # A deletion during the download must not turn into a known stale skip.
        if str(manifest.get('deleteEpoch') or '') != _epoch(table, user_id):
            return None, {}, True
        return payload, manifest, age >= REFRESH_SECONDS
    except Exception:
        su._LOGGER.warning('Upload hash snapshot unavailable for %s', user_id, exc_info=True)
        return None, {}, True


@catalog_publisher
def rebuild_snapshot(user_id, *, publication_check=None):
    """Stream one library's hash partition into a gzip file on the build worker."""
    table = su._CTX.get('hash_index_table_client')
    if table is None:
        return None
    epoch_row = None
    if publication_check is not None:
        publication_check()
        epoch_row = _row(table, user_id, DELETE_EPOCH_ROW)
        if not epoch_row:
            from azure.core.exceptions import ResourceExistsError
            try:
                table.create_entity({'PartitionKey': user_id, 'RowKey': DELETE_EPOCH_ROW, 'epoch': uuid.uuid4().hex})
            except ResourceExistsError:
                pass
            epoch_row = _row(table, user_id, DELETE_EPOCH_ROW)
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
                su._upload_file_to_blob(blob, path, overwrite=publication_check is None, content_settings=su.BlobContentSettings(
                    content_type='application/json', content_encoding='gzip'))
                compressed_bytes = os.path.getsize(path)
            # Publish only after the immutable data file is fully stored.
            manifest = {
                'PartitionKey': user_id, 'RowKey': SNAPSHOT_ROW, 'schemaVersion': 1,
                'blobName': blob_name, 'deleteEpoch': epoch, 'rows': count,
                'updatedAt': datetime.now(timezone.utc).isoformat(), 'compressedBytes': compressed_bytes,
                'sourceReadStartedAt': source_read_started_at,
            }
            index_publication.check()
            if publication_check is None:
                table.upsert_entity(manifest)
            else:
                from azure.core import MatchConditions
                from azure.data.tables import UpdateMode
                def replace(row):
                    version = getattr(row, 'metadata', {}).get('etag')
                    if not version:raise RuntimeError('Snapshot publication requires native ETags')
                    return dict(mode=UpdateMode.REPLACE, etag=version, match_condition=MatchConditions.IfNotModified)
                publication_check()
                table.submit_transaction([
                    ('update', dict(epoch_row), replace(epoch_row)),
                    ('update', manifest, replace(previous)) if previous else ('create', manifest),
                ])
            published = True
            index_publication.check()
            perf_instrumentation.log_event('upload_hash_snapshot_built', user=user_id,
                                           rows=count, bytes=compressed_bytes)
        return manifest
    finally:
        if not published:
            su._LOGGER.debug('Retained unconfirmed upload hash generation for guarded GC')
