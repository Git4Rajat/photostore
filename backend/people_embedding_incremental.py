"""Bounded-RAM people embedding merges; no whole-index numpy/JSON loads.

Temporary disk is O(base + output). RAM is bounded by one metadata item,
one representative vector, SQLite's fixed cache and the capped dirty set.
Legacy consumers still receive the same NPZ members. Extra rep_sizes/model
members certify merge compatibility, including mixed-width representatives.
The caller must supply every person affected since the expected base source
(both owners for a face move), and serialize source mutations appropriately.
"""
from dataclasses import dataclass
import codecs
import itertools
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

MAX_IDS = 512
MAX_ITEM_CHARS = 262144
MAX_FACES = 4096
MAX_FACE_READS = 16384
MAX_DIM = 4096
CHUNK = 65536


class Deferred(Exception):
    """Input/base cannot safely be merged within the incremental budget."""


class PublicationFailed(Exception):
    """An upload/CAS failed: never attempt a full fallback after this."""


@dataclass(frozen=True)
class PeopleEmbeddingPublication:
    user_id: str
    source_version: str
    embedding_version: str
    updated_at: str
    row_count: int
    model_version: str


def bounded_ids(values):
    if getattr(values, 'has_more', False) or getattr(values, 'truncated', False):
        raise Deferred('incomplete affected person capture')
    if isinstance(values, (str, bytes)):
        raise Deferred('affected IDs must be an iterable of IDs, not a scalar')
    ids = set()
    for index, value in enumerate(values):
        if index >= MAX_IDS:
            raise Deferred('affected ID budget exceeded')
        if not isinstance(value, str) or not value.strip() or len(value) > 1024:
            raise Deferred('invalid affected person ID')
        ids.add(value)
    if not ids:
        raise Deferred('empty affected ID set')
    return sorted(ids)


def _header(stream):
    version = np.lib.format.read_magic(stream)
    if version == (1, 0):
        return np.lib.format.read_array_header_1_0(stream)
    if version == (2, 0):
        return np.lib.format.read_array_header_2_0(stream)
    raise Deferred('unsupported NPY version')


def _text_chunks(archive, name):
    with archive.open(name + '.npy') as stream:
        shape, order, dtype = _header(stream)
        if shape not in ((), (1,)) or order or dtype.kind != 'U':
            raise Deferred('invalid JSON/scalar member')
        decoder = codecs.getincrementaldecoder('utf-32-be' if dtype.byteorder == '>' else 'utf-32-le')()
        remaining = dtype.itemsize
        while remaining:
            data = stream.read(min(CHUNK, remaining))
            if not data:
                raise Deferred('truncated text member')
            remaining -= len(data)
            yield decoder.decode(data)
        tail = decoder.decode(b'', final=True)
        if tail:
            yield tail
        if stream.read(1):
            raise Deferred('oversized text member')


def _scalar(archive, name):
    parts = []
    size = 0
    for part in _text_chunks(archive, name):
        size += len(part)
        if size > MAX_ITEM_CHARS:
            raise Deferred('oversized scalar')
        parts.append(part)
    return ''.join(parts).rstrip('\x00')


def _json_items(archive, name):
    """Parse the legacy single Unicode JSON-array member one bounded item at a time."""
    chunks = iter(_text_chunks(archive, name))
    buffer = ''
    eof = False
    decoder = json.JSONDecoder()

    def fill():
        nonlocal buffer, eof
        try:
            buffer += next(chunks)
        except StopIteration:
            eof = True

    def token():
        nonlocal buffer
        buffer = buffer.lstrip()
        while not buffer and not eof:
            fill()
            buffer = buffer.lstrip()
        if not buffer:
            raise Deferred('truncated JSON array')
        char, buffer = buffer[0], buffer[1:]
        return char

    if token() != '[':
        raise Deferred('expected JSON array')
    buffer = buffer.lstrip()
    while not buffer and not eof:
        fill()
        buffer = buffer.lstrip()
    if buffer.startswith(']'):
        buffer = buffer[1:]
    else:
        while True:
            while True:
                buffer = buffer.lstrip()
                try:
                    item, end = decoder.raw_decode(buffer)
                    # Numeric values can end at a chunk boundary; wait for delimiter.
                    if end == len(buffer) and not eof:
                        fill()
                        continue
                    if end > MAX_ITEM_CHARS:
                        raise Deferred('JSON item budget exceeded')
                    break
                except json.JSONDecodeError as error:
                    if eof or len(buffer) > MAX_ITEM_CHARS + CHUNK:
                        raise Deferred('invalid or oversized JSON item') from error
                    fill()
            buffer = buffer[end:]
            yield item
            delimiter = token()
            if delimiter == ']':
                break
            if delimiter != ',':
                raise Deferred('invalid JSON delimiter')
    for part in chunks:
        if (buffer + part).strip(' \r\n\t\x00'):
            raise Deferred('trailing JSON data')
        buffer = ''
    if buffer.strip(' \r\n\t\x00'):
        raise Deferred('trailing JSON data')


def _write_header(stream, shape, dtype):
    np.lib.format.write_array_header_2_0(stream, {
        'descr': np.lib.format.dtype_to_descr(np.dtype(dtype)),
        'fortran_order': False, 'shape': shape,
    })


def _write_json_member(archive, name, values, directory, *, progress=None):
    # UTF-32 NPY needs character count in its header: spool encoded JSON first.
    path = Path(directory) / (name + '.text')
    count = 0
    with path.open('wb') as stream:
        def write(text):
            nonlocal count
            count += len(text)
            stream.write(text.encode('utf-32-le'))
            if progress is not None:
                progress()
        write('[')
        first = True
        for value in values:
            if not first:
                write(',')
            first = False
            write(json.dumps(value, ensure_ascii=False, separators=(',', ':')))
        write(']')
    with archive.open(name + '.npy', 'w', force_zip64=True) as member:
        _write_header(member, (1,), '<U' + str(count))
        with path.open('rb') as stream:
            while chunk := stream.read(CHUNK):
                member.write(chunk)
                if progress is not None:
                    progress()
    path.unlink()


def _load_base(db, archive, directory, manifest, expected, model, schema):
    for name, value in [('source_version', expected), ('embedding_version', schema),
                        ('model_version', model)]:
        if _scalar(archive, name) != value:
            raise Deferred('incompatible base ' + name)
    matrix_path = Path(directory) / 'matrix.npy'
    with archive.open('embeddings.npy') as source, matrix_path.open('wb') as target:
        shutil.copyfileobj(source, target, CHUNK)
    with matrix_path.open('rb') as stream:
        shape, order, dtype = _header(stream)
        offset = stream.tell()
    if (len(shape) != 2 or order or dtype != np.dtype('float32') or
            shape[0] < 0 or not 0 <= shape[1] <= MAX_DIM):
        raise Deferred('invalid base matrix')
    count, dim = shape
    if matrix_path.stat().st_size != offset + count * dim * 4 or manifest.get('rowCount') != count:
        raise Deferred('invalid base row count')
    # Read one row via file seek, not an mmap that can accumulate resident pages.
    sentinel = object()
    with matrix_path.open('rb') as matrix:
        matrix.seek(offset)
        rows = itertools.zip_longest(_json_items(archive, 'person_ids'),
                                    _json_items(archive, 'people_meta'),
                                    _json_items(archive, 'rep_sizes'), fillvalue=sentinel)
        seen = 0
        for index, (pid, meta, width) in enumerate(rows):
            if sentinel in (pid, meta, width) or not isinstance(pid, str) or not pid or len(pid) > 1024:
                raise Deferred('invalid base keys')
            if not isinstance(meta, dict) or not isinstance(meta.get('faceIds'), list):
                raise Deferred('invalid base metadata')
            if type(width) is not int or not 0 <= width <= dim or index >= count:
                raise Deferred('invalid base widths')
            vector = matrix.read(dim * 4)
            if len(vector) != dim * 4 or any(np.frombuffer(vector, dtype=np.float32)[width:] != 0):
                raise Deferred('invalid base padding')
            db.execute('INSERT INTO rows VALUES (?, ?, ?, ?, ?)',
                       (pid, index, json.dumps(meta, ensure_ascii=False), vector[:width * 4], width))
            seen += 1
        if seen != count:
            raise Deferred('base member alignment mismatch')
    db.commit()


def _json_source(row, field):
    text = row.get(field, '[]') or '[]'
    if not isinstance(text, str) or len(text) > MAX_ITEM_CHARS:
        raise Deferred('source field budget exceeded')
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return []  # Same malformed-JSON behavior as the full builder.


def _entity(table, user, key):
    try:
        return table.get_entity(partition_key=user, row_key=key)
    except ResourceNotFoundError as error:
        # A missing table/account is not evidence that an entity was deleted.
        if getattr(error, 'error_code', None) not in (None, 'ResourceNotFound', 'EntityNotFound'):
            raise
        return None


def _apply(db, person_table, face_table, user, ids):
    next_order = db.execute('SELECT COALESCE(MAX(position), -1) + 1 FROM rows').fetchone()[0]
    face_reads = 0
    for pid in ids:
        row = _entity(person_table, user, pid)
        if row is None:
            db.execute('DELETE FROM rows WHERE pid = ?', (pid,))
            continue
        face_ids = _json_source(row, 'faceIds')
        if not isinstance(face_ids, list) or len(face_ids) > MAX_FACES:
            raise Deferred('source membership budget/shape')
        active = []
        for fid in face_ids:
            fid = str(fid)
            if len(fid) > 1024:
                raise Deferred('invalid face ID')
            face_reads += 1
            if face_reads > MAX_FACE_READS:
                raise Deferred('face point-read budget exceeded')
            face = _entity(face_table, user, fid)
            if face is None:
                continue
            if (not face.get('rejected') and
                    str(face.get('reviewStatus') or '').strip().lower() != 'rejected' and
                    str(face.get('personId') or '') == pid):
                active.append(fid)
        rep = _json_source(row, 'repEmbedding')
        if not isinstance(rep, list) or len(rep) > MAX_DIM:
            raise Deferred('source embedding budget/shape')
        if not active or not rep:
            db.execute('DELETE FROM rows WHERE pid = ?', (pid,))
            continue
        vector = np.asarray([v for v in rep if isinstance(v, (int, float))], dtype=np.float32)
        name = row.get('name', '')
        if not isinstance(name, str) or len(name) > MAX_ITEM_CHARS:
            raise Deferred('source name budget/shape')
        previous = db.execute('SELECT position FROM rows WHERE pid = ?', (pid,)).fetchone()
        position = previous[0] if previous else next_order
        next_order += not bool(previous)
        metadata = json.dumps({'name': name, 'faceIds': active}, ensure_ascii=False)
        if len(metadata) > MAX_ITEM_CHARS:
            raise Deferred('combined metadata budget exceeded')
        db.execute('INSERT OR REPLACE INTO rows VALUES (?, ?, ?, ?, ?)',
                   (pid, position, metadata, vector.tobytes(), vector.size))
    db.commit()


def _write_output(db, path, directory, source, schema, model):
    count, dim = db.execute('SELECT COUNT(*), COALESCE(MAX(width), 0) FROM rows').fetchone()
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        with archive.open('embeddings.npy', 'w', force_zip64=True) as member:
            _write_header(member, (count, dim), '<f4')
            for vector, width in db.execute('SELECT vector, width FROM rows ORDER BY position'):
                member.write(vector)
                member.write(b'\x00' * ((dim - width) * 4))
        for name, column in [('person_ids', 'pid'), ('people_meta', 'meta'), ('rep_sizes', 'width')]:
            values = (json.loads(row[0]) if name == 'people_meta' else row[0]
                      for row in db.execute('SELECT ' + column + ' FROM rows ORDER BY position'))
            _write_json_member(archive, name, values, directory)
        for name, value in [('source_version', source), ('embedding_version', schema),
                            ('model_version', model), ('updated_at', source)]:
            with archive.open(name + '.npy', 'w') as member:
                np.lib.format.write_array(member, np.asarray([value]), allow_pickle=False)
    return count


def merge_and_publish(*, user, ids, source, expected, model, schema, person_table,
                      face_table, manifest_client, blob_client, blob_prefix, content_settings):
    """Fail closed; publish immutable data then CAS the manifest as commit point."""
    if (not expected or not model or source == expected or
            max(len(expected), len(model), len(source)) > MAX_ITEM_CHARS or
            person_table is None or face_table is None or manifest_client is None):
        raise Deferred('missing source/model/tables or unchanged source')
    props = manifest_client.get_blob_properties()
    if props.size > MAX_ITEM_CHARS:
        raise Deferred('manifest budget exceeded')
    payload = manifest_client.download_blob(etag=props.etag, match_condition=MatchConditions.IfNotModified).readall()
    if len(payload) > MAX_ITEM_CHARS:
        raise Deferred('manifest budget exceeded')
    manifest = json.loads(payload)
    base_source = manifest.get('baseSourceVersion') if manifest.get('dirty') else manifest.get('sourceVersion')
    if (manifest.get('userId') != user or manifest.get('embeddingVersion') != schema or
            manifest.get('modelVersion') != model or base_source != expected):
        raise Deferred('incompatible source manifest')
    base_name = manifest.get('npzBlobName') or blob_prefix + '.npz'
    if not isinstance(base_name, str) or not base_name.startswith(blob_prefix) or not base_name.endswith('.npz'):
        raise Deferred('invalid base blob pointer')
    with tempfile.TemporaryDirectory(prefix='people-embedding-merge-') as directory:
        base_path = Path(directory) / 'base.npz'
        base_client = blob_client(base_name)
        base_props = base_client.get_blob_properties()
        download = base_client.download_blob(etag=base_props.etag, match_condition=MatchConditions.IfNotModified)
        with base_path.open('wb') as stream:
            # Azure readinto streams blocks; never fall back to readall.
            download.readinto(stream)
        db = sqlite3.connect(str(Path(directory) / 'rows.sqlite'))
        try:
            db.execute('PRAGMA cache_size = -2048')
            db.execute('PRAGMA temp_store = FILE')
            db.execute('CREATE TABLE rows (pid TEXT PRIMARY KEY, position INTEGER, meta TEXT, vector BLOB, width INTEGER)')
            db.execute('CREATE INDEX row_order ON rows(position)')
            with zipfile.ZipFile(base_path) as archive:
                _load_base(db, archive, directory, manifest, expected, model, schema)
            _apply(db, person_table, face_table, user, ids)
            output = Path(directory) / 'output.npz'
            count = _write_output(db, output, directory, source, schema, model)
            name = blob_prefix + '.' + uuid.uuid4().hex + '.npz'
            committed = {'userId': user, 'sourceVersion': source, 'embeddingVersion': schema,
                         'modelVersion': model, 'rowCount': count, 'dirty': False,
                         'updatedAt': source, 'npzBlobName': name}
            try:
                with output.open('rb') as stream:
                    blob_client(name).upload_blob(stream, overwrite=False, content_settings=content_settings('application/octet-stream'))
                manifest_client.upload_blob(json.dumps(committed).encode('utf-8'), overwrite=True,
                                            etag=props.etag, match_condition=MatchConditions.IfNotModified,
                                            content_settings=content_settings('application/json'))
            except Exception as error:
                raise PublicationFailed('people embedding publication failed') from error
        finally:
            db.close()
    return PeopleEmbeddingPublication(user, source, schema, source, count, model)
