"""Explicit bounded vector merges. IDs must be COMPLETE since the base source.

RAM: a capped ID set, one row/vector, streaming buffers and fixed SQLite cache.
Disk: O(base + output). No implicit full fallback; manifest CAS commits output.
"""
from dataclasses import dataclass
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import uuid
import zipfile

import numpy as np
from azure.core import MatchConditions
from azure.core.exceptions import ResourceNotFoundError

# Format-generic bounded Unicode-NPY JSON helpers; no people-module changes.
from people_embedding_incremental import (
    Deferred, _header, _json_items, _scalar, _write_header, _write_json_member,
)

MAX_IDS = 4096
MAX_ID_CHARS = 1024
MAX_ITEM_CHARS = 262144
MAX_DIM = 4096
CHUNK = 65536
SCHEMA_VERSION = 1


def _stored(name):
    info = zipfile.ZipInfo(name)
    info.compress_type = zipfile.ZIP_STORED
    return info


@dataclass(frozen=True)
class VectorPublication:
    """Receipt only: deliberately no keys or matrix."""
    user_id: str
    source_version: str
    embedding_version: str
    updated_at: str
    row_count: int
    blob_name: str


def bounded_ids(values):
    if getattr(values, 'has_more', False) or getattr(values, 'truncated', False):
        raise Deferred('incomplete affected filename capture')
    if isinstance(values, (str, bytes)):
        raise Deferred('affected filenames must be an iterable, not a scalar')
    ids = set()
    for index, value in enumerate(values):
        if index >= MAX_IDS:
            raise Deferred('affected filename budget exceeded')
        if not isinstance(value, str) or not value.strip() or len(value) > MAX_ID_CHARS:
            raise Deferred('invalid affected filename')
        ids.add(value)
    if not ids:
        raise Deferred('empty affected filename set')
    return sorted(ids)


def _entity(table, user, filename):
    try:
        return table.get_entity(partition_key=user, row_key=filename)
    except ResourceNotFoundError as error:
        # Table/account/network failures are NOT deletion evidence.
        if getattr(error, 'error_code', None) not in (None, 'ResourceNotFound', 'EntityNotFound'):
            raise
        return None


def _check_source_row(row):
    # Azure entities have bounded property counts/sizes. Enforce a string
    # budget before JSON embedding/text parsing as well (including test or
    # alternate table implementations that do not enforce Azure's limits).
    if any(isinstance(value, str) and len(value) > MAX_ITEM_CHARS for value in row.values()):
        raise Deferred('source field budget exceeded')


def _load_base(db, archive, directory, manifest, expected, version, dim):
    members = archive.namelist()
    if len(members) != len(set(members)):
        raise Deferred('duplicate NPZ members')
    if _scalar(archive, 'source_version') != expected or _scalar(archive, 'embedding_version') != version:
        raise Deferred('incompatible NPZ source/model')
    _scalar(archive, 'updated_at')
    path = Path(directory) / 'matrix.npy'
    with archive.open('embeddings.npy') as source, path.open('wb') as target:
        shutil.copyfileobj(source, target, CHUNK)
    with path.open('rb') as stream:
        shape, order, dtype = _header(stream)
        offset = stream.tell()
    if (len(shape) != 2 or order or dtype != np.dtype('<f4') or
            shape[0] < 0 or shape[1] != (dim if shape[0] else 0)):
        raise Deferred('invalid base matrix schema/dimension')
    count = shape[0]
    if type(manifest.get('rowCount')) is not int or manifest['rowCount'] != count:
        raise Deferred('incompatible manifest row count')
    if path.stat().st_size != offset + count * shape[1] * 4:
        raise Deferred('truncated/oversized base matrix')
    seen = 0
    with path.open('rb') as matrix:
        matrix.seek(offset)
        for position, filename in enumerate(_json_items(archive, 'row_keys')):
            if (position >= count or not isinstance(filename, str) or
                    not filename.strip() or len(filename) > MAX_ID_CHARS):
                raise Deferred('invalid base row keys')
            vector = matrix.read(dim * 4)
            if len(vector) != dim * 4 or not np.isfinite(np.frombuffer(vector, dtype='<f4')).all():
                raise Deferred('invalid base vector')
            db.execute('INSERT INTO rows VALUES (?, ?, ?)', (filename, position, vector))
            seen += 1
        if seen != count:
            raise Deferred('base row alignment mismatch')
    db.commit()


def _apply(db, metadata_table, embeddings_table, user, ids, compute, dim):
    next_position = db.execute('SELECT COALESCE(MAX(position), -1) + 1 FROM rows').fetchone()[0]
    for filename in ids:
        row = _entity(metadata_table, user, filename)
        if row is None or str(row.get('processing_state') or '').strip().lower() == 'deleted':
            db.execute('DELETE FROM rows WHERE filename = ?', (filename,))
            continue
        _check_source_row(row)
        embedding_row = _entity(embeddings_table, user, filename) if embeddings_table is not None else None
        if embedding_row is not None:
            _check_source_row(embedding_row)
        # An existing unusable table embedding means text fallback, not inline.
        vector = compute(filename, row, embedding_row)
        if vector is None or vector.size != dim:
            db.execute('DELETE FROM rows WHERE filename = ?', (filename,))
            continue
        if vector.ndim != 1 or not np.isfinite(vector).all():
            raise Deferred('invalid computed vector')
        previous = db.execute('SELECT position FROM rows WHERE filename = ?', (filename,)).fetchone()
        position = previous[0] if previous else next_position
        next_position += not bool(previous)
        db.execute('INSERT OR REPLACE INTO rows VALUES (?, ?, ?)',
                   (filename, position, np.ascontiguousarray(vector, dtype='<f4').tobytes()))
    db.commit()


def _write_output(db, path, directory, source, version, dim):
    count = db.execute('SELECT COUNT(*) FROM rows').fetchone()[0]
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        # Float32 embeddings barely deflate (~7%) but cost most of the merge CPU.
        with archive.open(_stored('embeddings.npy'), 'w', force_zip64=True) as member:
            _write_header(member, (count, dim if count else 0), '<f4')
            for vector, in db.execute('SELECT vector FROM rows ORDER BY position'):
                member.write(vector)
        _write_json_member(archive, 'row_keys',
                           (row[0] for row in db.execute('SELECT filename FROM rows ORDER BY position')),
                           directory)
        for name, value in [('source_version', source), ('embedding_version', version), ('updated_at', source)]:
            with archive.open(name + '.npy', 'w') as member:
                np.lib.format.write_array(member, np.asarray([value]), allow_pickle=False)
    return count


def merge_and_publish(*, user, ids, source, expected, version, dim, metadata_table,
                      embeddings_table, manifest_client, blob_client, blob_prefix,
                      compute, content_settings):
    """Immutable data upload then ETag-conditional manifest commit.

    Caller must serialize source mutations, including writes while already
    dirty. CAS fences manifest publishers, not completeness of the given IDs.
    """
    if (not expected or not source or source == expected or not version or
            max(len(expected), len(source), len(version)) > MAX_ITEM_CHARS or
            not 0 < dim <= MAX_DIM or metadata_table is None or manifest_client is None):
        raise Deferred('missing/incompatible source, version, dimension or storage')
    props = manifest_client.get_blob_properties()
    if props.size > MAX_ITEM_CHARS:
        raise Deferred('manifest budget exceeded')
    payload = manifest_client.download_blob(etag=props.etag, match_condition=MatchConditions.IfNotModified).readall()
    if len(payload) > MAX_ITEM_CHARS:
        raise Deferred('manifest budget exceeded')
    manifest = json.loads(payload)
    base_source = manifest.get('baseSourceVersion') if manifest.get('dirty') else manifest.get('sourceVersion')
    if (manifest.get('userId') != user or manifest.get('embeddingVersion') != version or
            base_source != expected or manifest.get('schemaVersion', SCHEMA_VERSION) != SCHEMA_VERSION):
        raise Deferred('incompatible source manifest')
    base_name = manifest.get('blobName', blob_prefix + '.npz')
    if not isinstance(base_name, str) or not base_name.startswith(blob_prefix + '.') or not base_name.endswith('.npz'):
        raise Deferred('invalid manifest blob pointer')
    with tempfile.TemporaryDirectory(prefix='vector-merge-') as directory:
        base_path = Path(directory) / 'base.npz'
        base_client = blob_client(base_name)
        base_props = base_client.get_blob_properties()
        download = base_client.download_blob(etag=base_props.etag, match_condition=MatchConditions.IfNotModified)
        with base_path.open('wb') as stream:
            download.readinto(stream)  # Never readall an NPZ.
        db = sqlite3.connect(str(Path(directory) / 'rows.sqlite'))
        try:
            db.execute('PRAGMA cache_size = -2048')
            db.execute('PRAGMA temp_store = FILE')
            db.execute('CREATE TABLE rows (filename TEXT PRIMARY KEY, position INTEGER, vector BLOB)')
            db.execute('CREATE INDEX row_order ON rows(position)')
            with zipfile.ZipFile(base_path) as archive:
                _load_base(db, archive, directory, manifest, expected, version, dim)
            _apply(db, metadata_table, embeddings_table, user, ids, compute, dim)
            output = Path(directory) / 'output.npz'
            count = _write_output(db, output, directory, source, version, dim)
            name = blob_prefix + '.' + uuid.uuid4().hex + '.npz'
            committed = {'userId': user, 'sourceVersion': source, 'embeddingVersion': version,
                         'rowCount': count, 'dirty': False, 'updatedAt': source,
                         'blobName': name, 'schemaVersion': SCHEMA_VERSION}
            with output.open('rb') as stream:
                blob_client(name).upload_blob(stream, overwrite=False,
                                             content_settings=content_settings('application/octet-stream'))
            manifest_client.upload_blob(json.dumps(committed).encode('utf-8'), overwrite=True,
                                        etag=props.etag, match_condition=MatchConditions.IfNotModified,
                                        content_settings=content_settings('application/json'))
        finally:
            db.close()
    return VectorPublication(user, source, version, source, count, name)