"""Explicit, bounded tag deltas; no automatic dirty-set discovery or full fallback.

Call storage_utils.refresh_user_tag_embedding_index(bootstrap_incremental=True)
once with the real model owner to seed this state during the existing streamed
scan. Then supply affected_filenames COMPLETE since previous_source_version,
including deletes and both sides of renames. Serialize metadata mutations with
publication: manifest CAS fences publishers, not unfenced source-table writes.
Default full builds deliberately do not seed contribution state.

SQLite stores each photo's old effective tags, reference counts and reusable
vectors (including zero-reference tags). Incremental RAM is independent of the
library/vocabulary: fixed SQLite cache, capped IDs/photo fields, and one capped
encoding batch. Disk and blob IO are O(sidecar + output); NPZ is the unchanged
consumer format. Immutable unreferenced generations require external GC.
"""
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
import tempfile
import uuid
import zipfile

import numpy as np
from azure.core import MatchConditions
from azure.core.exceptions import ResourceNotFoundError

from people_embedding_incremental import Deferred, PublicationFailed, _write_header, _write_json_member
import index_progress

MAX_IDS = 512
MAX_TAGS_PER_PHOTO = 128
MAX_TAG_CHARS = 1024
MAX_ITEM_CHARS = 262144
MAX_DIM = 4096
ENCODE_BATCH = 64
STATE_VERSION = 'tag-contributions-v1'


@dataclass(frozen=True)
class TagEmbeddingPublication:
    user_id: str
    source_version: str
    embedding_version: str
    updated_at: str
    tag_count: int


def bounded_ids(values):
    if getattr(values, 'has_more', False) or getattr(values, 'truncated', False):
        raise Deferred('incomplete affected filename capture')
    if isinstance(values, (str, bytes)):
        raise Deferred('affected filenames must not be scalar')
    ids = set()
    for index, value in enumerate(values):
        if index >= MAX_IDS:
            raise Deferred('affected filename budget exceeded')
        if not isinstance(value, str) or not value.strip() or len(value) > 1024:
            raise Deferred('invalid filename')
        ids.add(value)
    if not ids:
        raise Deferred('empty affected filename set')
    return sorted(ids)


def _tags(values):
    if not isinstance(values, list) or len(values) > MAX_TAGS_PER_PHOTO:
        raise Deferred('per-photo tag budget exceeded')
    if any(not isinstance(t, str) or not t.strip() or len(t) > MAX_TAG_CHARS for t in values):
        raise Deferred('invalid tag')
    return sorted(set(values))


def _photo_tags(row, columns, effective):
    # Project BEFORE computing effective_tags: the full scan uses precisely
    # these columns, including rows with a deleted flag (no deletion filter).
    projected = {name: row[name] for name in columns if name in row}
    size = 0
    for value in projected.values():
        if isinstance(value, str):
            size += len(value)
        elif value is not None and not isinstance(value, (int, float, bool)):
            raise Deferred('unexpected metadata field type')
    if size > MAX_ITEM_CHARS:
        raise Deferred('photo metadata budget exceeded')
    return _tags(effective(projected))


def _open(path):
    db = sqlite3.connect(str(path))
    db.set_progress_handler(index_progress.sqlite_progress, 10000)
    db.execute('PRAGMA cache_size = -2048')
    db.execute('PRAGMA temp_store = FILE')
    db.execute('PRAGMA trusted_schema = OFF')
    return db


def _create(db):
    db.execute('CREATE TABLE provenance (user TEXT, source TEXT, model TEXT, schema TEXT, version TEXT, dim INTEGER)')
    db.execute('CREATE TABLE photos (filename TEXT PRIMARY KEY, tags TEXT NOT NULL)')
    db.execute('CREATE TABLE tags (tag TEXT PRIMARY KEY, refs INTEGER NOT NULL CHECK(refs >= 0), vector BLOB)')


def _replace(db, filename, tags):
    previous = db.execute('SELECT length(tags) FROM photos WHERE filename = ?', (filename,)).fetchone()
    if previous:
        if previous[0] > MAX_ITEM_CHARS:
            raise Deferred('old photo contribution budget exceeded')
        old = _tags(json.loads(db.execute('SELECT tags FROM photos WHERE filename = ?', (filename,)).fetchone()[0]))
        for tag in old:
            changed = db.execute('UPDATE tags SET refs = refs - 1 WHERE tag = ? AND refs > 0', (tag,))
            if changed.rowcount != 1:
                raise Deferred('invalid contribution refcount')
    db.execute('DELETE FROM photos WHERE filename = ?', (filename,))
    if tags is not None:
        db.execute('INSERT INTO photos VALUES (?, ?)', (filename, json.dumps(tags, ensure_ascii=False)))
        for tag in tags:
            db.execute('INSERT INTO tags VALUES (?, 1, NULL) ON CONFLICT(tag) DO UPDATE SET refs = refs + 1', (tag,))


def _manifest(client, allow_missing=False):
    if client is None:
        raise Deferred('missing manifest client')
    try:
        props = client.get_blob_properties()
    except ResourceNotFoundError as error:
        if allow_missing and getattr(error, 'error_code', None) in (None, 'BlobNotFound', 'ResourceNotFound'):
            return {}, None
        raise
    if props.size > MAX_ITEM_CHARS:
        raise Deferred('manifest budget exceeded')
    raw = client.download_blob(etag=props.etag, match_condition=MatchConditions.IfNotModified).readall()
    if len(raw) > MAX_ITEM_CHARS:
        raise Deferred('manifest budget exceeded')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise Deferred('invalid manifest')
    return value, props.etag


def _identity(user, source, model, schema):
    if any(not isinstance(v, str) or not v.strip() or len(v) > 1024 for v in (user, source, model, schema)):
        raise Deferred('missing or oversized source/model identity')


def _write_output(db, path, directory, source, model, dim):
    count = db.execute('SELECT COUNT(*) FROM tags WHERE refs > 0').fetchone()[0]
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        with archive.open('embeddings.npy', 'w', force_zip64=True) as member:
            _write_header(member, (count, dim if count else 0), '<f4')
            for vector, in db.execute('SELECT vector FROM tags WHERE refs > 0 ORDER BY tag'):
                if vector is None or len(vector) != dim * 4:
                    raise Deferred('invalid stored vector')
                if not np.isfinite(np.frombuffer(vector, dtype='<f4')).all():
                    raise Deferred('nonfinite stored vector')
                member.write(vector)
                index_progress.completed()
        _write_json_member(archive, 'tags', (r[0] for r in db.execute(
            'SELECT tag FROM tags WHERE refs > 0 ORDER BY tag')), directory,
                           progress=index_progress.completed)
        for name, value in [('source_version', source), ('embedding_version', model), ('updated_at', source)]:
            with archive.open(name + '.npy', 'w') as member:
                np.lib.format.write_array(member, np.asarray([value]), allow_pickle=False)
    return count


def _publish(db, state_path, directory, *, user, source, model, schema, dim,
             manifest_client, etag, blob_client, blob_prefix, content_settings):
    db.execute('UPDATE provenance SET source = ?, dim = ?', (source, dim))
    db.commit()
    output = Path(directory) / 'output.npz'
    count = _write_output(db, output, directory, source, model, dim)
    generation = blob_prefix + '.' + uuid.uuid4().hex
    manifest = {'userId': user, 'sourceVersion': source, 'embeddingVersion': model,
                'schemaVersion': schema, 'tagCount': count, 'dirty': False, 'updatedAt': source,
                'npzBlobName': generation + '.npz', 'contributionBlobName': generation + '.sqlite',
                'contributionVersion': STATE_VERSION}
    try:
        for path, name in [(output, manifest['npzBlobName']), (state_path, manifest['contributionBlobName'])]:
            with path.open('rb') as stream:
                blob_client(name).upload_blob(index_progress.Stream(stream), overwrite=False,
                                             content_settings=content_settings('application/octet-stream'))
        condition = {'overwrite': True, 'etag': etag, 'match_condition': MatchConditions.IfNotModified} if etag is not None else {'overwrite': False}
        manifest_client.upload_blob(json.dumps(manifest).encode(), content_settings=content_settings('application/json'), **condition)
    except Exception as error:
        raise PublicationFailed('tag embedding publication failed') from error
    return TagEmbeddingPublication(user, source, model, source, count)


def bootstrap_and_publish(*, user, source, model, schema, build, columns, effective,
                          manifest_client, blob_client, blob_prefix, content_settings):
    """Use a contribution hook in the ORIGINAL full scan; never a second scan."""
    _identity(user, source, model, schema)
    _, etag = _manifest(manifest_client, allow_missing=True)
    with tempfile.TemporaryDirectory(prefix='tag-embedding-bootstrap-') as directory:
        path = Path(directory) / 'state.sqlite'
        db = _open(path)
        try:
            _create(db)
            def hook(row):
                filename = row.get('RowKey')
                if not isinstance(filename, str) or not filename.strip() or len(filename) > 1024:
                    raise Deferred('invalid bootstrap filename')
                tags = _photo_tags(row, columns, effective)
                # Duplicate source keys cannot certify a complete contribution base.
                if db.execute('SELECT 1 FROM photos WHERE filename = ?', (filename,)).fetchone():
                    raise Deferred('duplicate bootstrap filename')
                _replace(db, filename, tags)
                index_progress.completed()
            snapshot = build(hook)
            if snapshot is None or snapshot.embedding_version != model:
                raise Deferred('bootstrap builder failed or model changed')
            matrix = snapshot.embeddings
            if matrix.ndim != 2 or matrix.shape[0] != len(snapshot.tags) or matrix.shape[1] > MAX_DIM:
                raise Deferred('bootstrap vector shape')
            dim = matrix.shape[1]
            if snapshot.tags and not dim:
                raise Deferred('empty vector')
            for tag, vector in zip(snapshot.tags, matrix):
                if not np.isfinite(vector).all():
                    raise Deferred('nonfinite vector')
                changed = db.execute('UPDATE tags SET vector = ? WHERE tag = ?', (vector.astype('<f4').tobytes(), tag))
                if changed.rowcount != 1:
                    raise Deferred('bootstrap contributions mismatch')
                index_progress.completed()
            db.execute('INSERT INTO provenance VALUES (?, ?, ?, ?, ?, ?)', (user, source, model, schema, STATE_VERSION, dim))
            _publish(db, path, directory, user=user, source=source, model=model, schema=schema, dim=dim,
                     manifest_client=manifest_client, etag=etag, blob_client=blob_client,
                     blob_prefix=blob_prefix, content_settings=content_settings)
            return snapshot
        finally:
            db.close()


def merge_and_publish(*, user, ids, source, expected, model, schema, table, columns, effective,
                      encode, manifest_client, blob_client, blob_prefix, content_settings):
    ids = bounded_ids(ids)
    _identity(user, source, model, schema)
    _identity(user, expected, model, schema)
    if source == expected or table is None:
        raise Deferred('unchanged source or missing metadata table')
    manifest, etag = _manifest(manifest_client)
    base_source = manifest.get('baseSourceVersion') if manifest.get('dirty') else manifest.get('sourceVersion')
    if (manifest.get('userId') != user or base_source != expected or
            manifest.get('schemaVersion') != schema or manifest.get('embeddingVersion') != model or
            manifest.get('contributionVersion') != STATE_VERSION):
        raise Deferred('incompatible manifest')
    for field, suffix in [('contributionBlobName', '.sqlite'), ('npzBlobName', '.npz')]:
        name = manifest.get(field)
        if (not isinstance(name, str) or len(name) > 1024 or not name.startswith(blob_prefix + '.') or
                not name.endswith(suffix) or '/' in name):
            raise Deferred('invalid provenance pointer')
    # Require the published pair to exist; sidecar provenance certifies model
    # and source without hydrating the existing NPZ matrix or vocabulary.
    blob_client(manifest['npzBlobName']).get_blob_properties()
    with tempfile.TemporaryDirectory(prefix='tag-embedding-merge-') as directory:
        path = Path(directory) / 'state.sqlite'
        client = blob_client(manifest['contributionBlobName'])
        props = client.get_blob_properties()
        with path.open('wb') as stream:
            client.download_blob(etag=props.etag, match_condition=MatchConditions.IfNotModified).readinto(index_progress.Stream(stream))
        db = _open(path)
        try:
            provenance = db.execute('SELECT user, source, model, schema, version, dim FROM provenance').fetchmany(2)
            if len(provenance) != 1 or provenance[0][:5] != (user, expected, model, schema, STATE_VERSION):
                raise Deferred('incompatible sidecar')
            dim = provenance[0][5]
            if type(dim) is not int or not 0 <= dim <= MAX_DIM:
                raise Deferred('vector dimension budget exceeded')
            if db.execute('SELECT COUNT(*) FROM tags WHERE refs > 0').fetchone()[0] != manifest.get('tagCount'):
                raise Deferred('sidecar count mismatch')
            if db.execute('SELECT 1 FROM tags WHERE refs < 0 OR length(tag) > ? OR length(vector) != ? OR vector IS NULL LIMIT 1',
                          (MAX_TAG_CHARS, dim * 4)).fetchone():
                raise Deferred('invalid sidecar vectors/refcounts')
            if db.execute('''SELECT 1 FROM photos WHERE length(filename) > 1024 OR length(tags) > ?
                          OR json_type(tags) != 'array' OR json_array_length(tags) > ? LIMIT 1''',
                          (MAX_ITEM_CHARS, MAX_TAGS_PER_PHOTO)).fetchone():
                raise Deferred('invalid sidecar photo budget')
            if db.execute('''SELECT 1 FROM photos p, json_each(p.tags) j
                          WHERE j.type != 'text' OR length(j.value) > ? OR length(trim(j.value)) = 0 LIMIT 1''',
                          (MAX_TAG_CHARS,)).fetchone():
                raise Deferred('invalid sidecar tag')
            if db.execute('''SELECT 1 FROM photos p, json_each(p.tags) j
                          GROUP BY p.filename, j.value HAVING COUNT(*) > 1 LIMIT 1''').fetchone():
                raise Deferred('duplicate sidecar contribution')
            # All aggregation spills to disk. Certify exact refs, including
            # corruption of a shared tag that would otherwise remove it early.
            if db.execute('''WITH contributions AS (
                    SELECT j.value AS tag, COUNT(*) AS refs FROM photos p, json_each(p.tags) j GROUP BY j.value)
                SELECT 1 FROM tags t LEFT JOIN contributions c ON t.tag = c.tag
                WHERE t.refs != COALESCE(c.refs, 0)
                UNION ALL SELECT 1 FROM contributions c LEFT JOIN tags t ON c.tag = t.tag WHERE t.tag IS NULL
                LIMIT 1''').fetchone():
                raise Deferred('sidecar reference mismatch')
            # Point reads only. Missing entities are removals, but a missing
            # table/account and transient failures are NOT evidence of deletion.
            for filename in ids:
                try:
                    row = table.get_entity(partition_key=user, row_key=filename)
                except ResourceNotFoundError as error:
                    if getattr(error, 'error_code', None) not in (None, 'ResourceNotFound', 'EntityNotFound'):
                        raise
                    row = None
                tags = None if row is None else _photo_tags(row, columns, effective)
                _replace(db, filename, tags)
                index_progress.completed()
            # Stream pending tags in bounded batches; retain zero-ref vectors
            # so a later re-add never has to re-encode an existing tag.
            while True:
                pending = [r[0] for r in db.execute('SELECT tag FROM tags WHERE refs > 0 AND vector IS NULL ORDER BY tag LIMIT ?',
                                                   (ENCODE_BATCH,))]
                if not pending:
                    break
                raw = encode(pending)
                if raw is None or len(raw) != len(pending):
                    raise Deferred('encoding failed')
                # Validate width before numpy can allocate a ragged/huge matrix.
                if any(not isinstance(v, (list, tuple, np.ndarray)) or not 0 < len(v) <= MAX_DIM for v in raw):
                    raise Deferred('encoding dimension budget exceeded')
                if any(any(not isinstance(x, (int, float, np.number)) for x in v) for v in raw):
                    raise Deferred('invalid encoding values')
                matrix = np.asarray(raw, dtype=np.float32)
                if matrix.ndim != 2 or not np.isfinite(matrix).all():
                    raise Deferred('invalid encoding')
                if dim and matrix.shape[1] != dim:
                    raise Deferred('model vector dimension changed')
                dim = matrix.shape[1]
                norms = np.linalg.norm(matrix, axis=1, keepdims=True)
                norms[norms == 0] = 1.0
                matrix = (matrix / norms).astype('<f4', copy=False)
                for tag, vector in zip(pending, matrix):
                    db.execute('UPDATE tags SET vector = ? WHERE tag = ?', (vector.tobytes(), tag))
                index_progress.completed()
            return _publish(db, path, directory, user=user, source=source, model=model, schema=schema, dim=dim,
                            manifest_client=manifest_client, etag=etag, blob_client=blob_client,
                            blob_prefix=blob_prefix, content_settings=content_settings)
        finally:
            db.close()
