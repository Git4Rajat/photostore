"""Opt-in lexical JSON merges, bounded by the dirty set and one JSON row.

The caller certifies ALL filenames changed since ``expected`` and serializes
source mutations. Manifest CAS fences publishers, not source-table writes.
Legacy single-document gzip JSON is parsed incrementally (never the fallback
whole-file parse in index_files.iter_rows). Disk usage is O(base + output).
"""
from dataclasses import dataclass
import gzip
import json
from pathlib import Path
import sqlite3
import uuid

from azure.core import MatchConditions
from azure.core.exceptions import ResourceNotFoundError

from index_files import RowsWriter, iter_rows, workspace

MAX_IDS = 4096
MAX_ID_CHARS = 1024
MAX_ITEM_CHARS = 1048576
MAX_DIRTY_BYTES = 8388608
MAX_MANIFEST_BYTES = 65536
CHUNK = 65536


class Deferred(Exception):
    """Cannot safely merge; never a request for a full rebuild."""


@dataclass(frozen=True)
class LexicalPublication:
    """Publication receipt only, deliberately without snapshot rows."""
    user_id: str
    source_version: str
    schema_version: str
    updated_at: str
    row_count: int
    blob_name: str


def bounded_ids(values):
    if values is None or isinstance(values, (str, bytes)):
        raise Deferred('complete affected filenames must be explicitly supplied')
    if getattr(values, 'has_more', False) or getattr(values, 'truncated', False):
        raise Deferred('affected filenames are incomplete')
    ids = set()
    for index, value in enumerate(values):
        if index >= MAX_IDS:
            raise Deferred('affected filename budget exceeded')
        if (not isinstance(value, str) or not value.strip() or
                value != value.strip() or len(value) > MAX_ID_CHARS):
            raise Deferred('invalid affected filename')
        ids.add(value)
    return sorted(ids)


def effective_snapshot_version(manifest):
    """Snapshot provenance, NOT the shared SQLite/listing freshness token.

    Legacy dirty manifests keep the last published snapshot in baseSourceVersion.
    Once present, the independent field is authoritative even while shared dirty.
    """
    if 'lexicalSnapshotSourceVersion' in manifest:
        return manifest['lexicalSnapshotSourceVersion']
    return manifest.get('baseSourceVersion') if manifest.get('dirty') else manifest.get('sourceVersion')


def snapshot_row_count(manifest):
    return manifest.get('lexicalSnapshotRowCount', manifest.get('rowCount'))


def capture_manifest(client):
    """Read the exact CAS revision; only actual absence permits creation."""
    if client is None:
        raise Deferred('manifest storage unavailable')
    try:
        props = client.get_blob_properties()
    except ResourceNotFoundError:
        return {}, None
    if not props.etag or props.size > MAX_MANIFEST_BYTES:
        raise Deferred('manifest ETag unavailable or budget exceeded')
    payload = client.download_blob(
        etag=props.etag, match_condition=MatchConditions.IfNotModified).readall()
    if len(payload) > MAX_MANIFEST_BYTES:
        raise Deferred('manifest budget exceeded')
    manifest = json.loads(payload)
    if not isinstance(manifest, dict):
        raise Deferred('invalid manifest')
    return manifest, props


def commit_manifest(client, manifest, props, content_settings):
    payload = json.dumps(manifest, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    if len(payload) > MAX_MANIFEST_BYTES:
        raise Deferred('manifest budget exceeded')
    client.upload_blob(payload, overwrite=props is not None,
                       **({'etag': props.etag, 'match_condition': MatchConditions.IfNotModified}
                          if props is not None else {}),
                       content_settings=content_settings)


class _JSONStream:
    """Bounded raw_decode parser for both legacy and RowsWriter JSON files."""
    def __init__(self, stream):
        self.stream = stream
        self.buffer = ''
        self.eof = False
        self.decoder = json.JSONDecoder()

    def fill(self):
        data = self.stream.read(CHUNK)
        self.eof = not data
        self.buffer += data

    def ready(self):
        self.buffer = self.buffer.lstrip()
        while not self.buffer and not self.eof:
            self.fill()
            self.buffer = self.buffer.lstrip()

    def token(self, expected):
        self.ready()
        if not self.buffer.startswith(expected):
            raise Deferred('invalid JSON delimiter')
        self.buffer = self.buffer[len(expected):]

    def value(self):
        while True:
            self.ready()
            try:
                value, end = self.decoder.raw_decode(self.buffer)
                if end == len(self.buffer) and not self.eof:
                    self.fill()
                    continue
                if end > MAX_ITEM_CHARS:
                    raise Deferred('JSON item budget exceeded')
                self.buffer = self.buffer[end:]
                return value
            except json.JSONDecodeError as error:
                if self.eof or len(self.buffer) > MAX_ITEM_CHARS + CHUNK:
                    raise Deferred('invalid or oversized JSON item') from error
                self.fill()

    def rows(self, header):
        self.token('{')
        seen = set()
        while True:
            key = self.value()
            if not isinstance(key, str) or key in seen or len(seen) >= 16:
                raise Deferred('invalid/duplicate JSON header key')
            seen.add(key)
            self.token(':')
            if key == 'rows':
                self.token('[')
                self.ready()
                if not self.buffer.startswith(']'):
                    while True:
                        yield self.value()
                        self.ready()
                        if self.buffer.startswith(']'):
                            break
                        self.token(',')
                self.token(']')
            else:
                header[key] = self.value()
            self.ready()
            if self.buffer.startswith('}'):
                break
            self.token(',')
        self.token('}')
        self.ready()
        if self.buffer or 'rows' not in seen:
            raise Deferred('trailing data or missing rows')


def _affected_rows(table, user, ids, excluded):
    dirty = {}
    total = 0
    for filename in ids:
        try:
            row = table.get_entity(partition_key=user, row_key=filename)
        except ResourceNotFoundError as error:
            # Missing tables/accounts are not evidence of an entity deletion.
            if getattr(error, 'error_code', None) not in (None, 'ResourceNotFound', 'EntityNotFound'):
                raise
            row = None
        if row is not None:
            if row.get('RowKey') != filename or row.get('PartitionKey') != user:
                raise Deferred('point-read identity mismatch')
            if (row.get('processing_complete') is not True or
                    str(row.get('processing_state') or '').strip().lower() == 'deleted'):
                row = None
            else:
                row = {k: v for k, v in dict(row).items() if k not in excluded}
                if any(isinstance(value, (str, bytes)) and len(value) > MAX_ITEM_CHARS
                       for value in row.values()):
                    raise Deferred('source field budget exceeded')
                text = json.dumps(row, ensure_ascii=False, separators=(',', ':'), default=str)
                if len(text) > MAX_ITEM_CHARS:
                    raise Deferred('source row budget exceeded')
                total += len(text.encode('utf-8'))
                if total > MAX_DIRTY_BYTES:
                    raise Deferred('dirty row memory budget exceeded')
        dirty[filename] = row
    return dirty


def _merge_file(base, spool, db, dirty, manifest, user, expected, schema):
    header = {}
    base_count = 0
    with gzip.open(base, 'rt', encoding='utf-8') as stream, RowsWriter(spool, {}) as writer:
        for row in _JSONStream(stream).rows(header):
            if not isinstance(row, dict):
                raise Deferred('invalid base row')
            filename = row.get('RowKey')
            if (not isinstance(filename, str) or not filename.strip() or
                    filename != filename.strip() or len(filename) > MAX_ID_CHARS):
                raise Deferred('invalid base filename')
            # An on-disk uniqueness check, not an O(library) Python set.
            db.execute('INSERT INTO keys VALUES (?)', (filename,))
            base_count += 1
            if filename in dirty:
                row = dirty.pop(filename)
            if row is not None:
                writer.add(row)
        if (header.get('userId') != user or header.get('sourceVersion') != expected or
                header.get('schemaVersion') != schema or
                type(header.get('rowCount')) is not int or header['rowCount'] != base_count or
                snapshot_row_count(manifest) != base_count):
            raise Deferred('incompatible base header/count')
        for row in dirty.values():
            if row is not None:
                writer.add(row)
        return writer.count


def merge_and_publish(*, user, ids, source, expected, schema, metadata_table,
                      manifest_client, blob_client, legacy_name, excluded,
                      content_settings):
    """Upload an immutable compatible gzip JSON blob, then CAS the pointer.

    No cache hydration, derived listing/search rebuild, or dirty-set drain.
    Failures leave the previous manifest/data readable; uncommitted immutable
    blobs may remain for retention/GC by the owner outside this implementation.
    """
    if (not isinstance(expected, str) or not expected or not source or source == expected or
            max(len(expected), len(source)) > MAX_MANIFEST_BYTES or
            metadata_table is None or manifest_client is None):
        raise Deferred('expected base source and configured storage are required')
    manifest, props = capture_manifest(manifest_client)
    base_source = effective_snapshot_version(manifest)
    if (manifest.get('userId') != user or base_source != expected or
            manifest.get('schemaVersion') != schema or
            type(snapshot_row_count(manifest)) is not int or snapshot_row_count(manifest) < 0):
        raise Deferred('incompatible source manifest')
    prefix = legacy_name[:-len('.json.gz')]
    name = manifest.get('blobName', legacy_name)
    if (not isinstance(name, str) or not name.endswith('.json.gz') or
            (name != legacy_name and not name.startswith(prefix + '.'))):
        raise Deferred('invalid manifest pointer')
    dirty = _affected_rows(metadata_table, user, ids, excluded)
    with workspace() as directory, workspace(sqlite=True) as sql_directory:
        base, spool, output = (Path(directory) / name for name in ('base.gz', 'spool.gz', 'output.gz'))
        client = blob_client(name)
        base_props = client.get_blob_properties()
        with base.open('wb') as stream:
            client.download_blob(etag=base_props.etag,
                                 match_condition=MatchConditions.IfNotModified).readinto(stream)
        db = sqlite3.connect(str(Path(sql_directory) / 'keys.sqlite'))
        try:
            db.execute('PRAGMA cache_size = -2048')
            db.execute('PRAGMA temp_store = FILE')
            db.execute('CREATE TABLE keys (filename TEXT PRIMARY KEY) WITHOUT ROWID')
            count = _merge_file(base, spool, db, dirty, manifest, user, expected, schema)
        finally:
            db.close()
        header = {'userId': user, 'sourceVersion': source, 'schemaVersion': schema,
                  'updatedAt': source, 'rowCount': count}
        with RowsWriter(output, header, compresslevel=6) as writer:
            # This spool is ours: capped rows, guaranteed line format. Never
            # expose legacy input to iter_rows's whole-document fallback.
            for row in iter_rows(spool):
                writer.add(row)
        name = prefix + '.' + uuid.uuid4().hex + '.json.gz'
        # This is a snapshot-only commit. Never advance the shared catalog
        # token/row count/timestamp or clear its dirty flag/base provenance.
        committed = {**manifest, 'lexicalSnapshotSourceVersion': source,
                 'lexicalSnapshotDirty': False, 'lexicalSnapshotRowCount': count,
                 'lexicalSnapshotUpdatedAt': source, 'blobName': name}
        import index_publication
        index_publication.check()
        with output.open('rb') as stream:
            blob_client(name).upload_blob(stream, overwrite=False,
                                         content_settings=content_settings(True))
        index_publication.check()
        commit_manifest(manifest_client, committed, props, content_settings(False))
    return LexicalPublication(user, source, schema, source, count, name)