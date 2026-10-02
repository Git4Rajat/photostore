"""Versioned filesystem checkpoints for LiveFaceIndex (trusted mounted share).

SQLite backup and native serialization happen on caller-owned LOCAL disk under
the runtime lock. Only closed files are streamed to the share. Unique immutable
generation directories and a small atomic CURRENT replacement publish LAST.
Concurrent writers are last-publisher-wins; freshness still requires a caller
revision covering EVERY latest completed durable assignment batch (including
assignment publication/main journal), not merely curation or a lease token.
Never save mid-publication. Equal revision must mean equal authoritative data.

No generation is deleted here, even after failed publication: a concurrent
reader may hold its name. Cleanup requires an externally enforced reader grace
period/quiescence, not just writer ownership. Schedule saves/rebuild explicitly;
background rebuild is deferred because sibling training + replay have no proven
aggregate memory/disk bound. Caller provisions enough local/share disk space.

The share must support same-directory atomic replace and flush (Azure Files SMB
does); server durability follows its fsync guarantees. Workdir MUST be local:
portable Python cannot reliably detect network mounts. Overlapping trees and
symlinks are rejected. SHA256 detects accidental damage, NOT hostile writers;
FAISS deserialization requires a trusted, access-controlled checkpoint share.
Library identity is the hash of the caller's canonical checkpoint directory,
never the transient owner token or workdir. Moving that directory invalidates it.
"""
from __future__ import annotations

from dataclasses import asdict
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import uuid

import numpy as np
import clustering_index as ci

FORMAT_VERSION = 1
CHUNK_BYTES = 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
_GENERATION = re.compile(r'gen-[0-9a-f]{32}\Z')


class CorruptCheckpoint(ValueError):
    """Invalid or incompatible data, distinct from storage availability errors."""


def _require(condition, message='Invalid checkpoint'):
    if not condition:
        raise CorruptCheckpoint(message)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True, allow_nan=False).encode('utf-8')


def _revision(value):
    """Canonical JSON scalar or nested lists/tuples; reject objects and NaN."""
    if isinstance(value, (list, tuple)):
        return [_revision(item) for item in value]
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise ValueError('source_revision must be a finite JSON scalar/list/tuple')


def _identity(directory):
    return hashlib.sha256(os.fsencode(str(directory.resolve()))).hexdigest()


def _separate(local, share):
    local, share = local.resolve(), share.resolve()
    if local == share or local in share.parents or share in local.parents:
        raise ValueError('Active SQLite/local staging and checkpoint trees must not overlap')


def _regular(path):
    # stat errors (permissions, disconnected share) must not become corruption.
    import stat
    _require(not path.is_symlink(), 'Symlink in checkpoint')
    _require(stat.S_ISREG(path.stat().st_mode), 'Not a regular checkpoint file')


def _stream_file(source, destination=None):
    """Bounded streaming hash/copy; never read a full SQLite/FAISS file in RAM."""
    digest, size = hashlib.sha256(), 0
    with open(source, 'rb') as src:
        if destination is None:
            while chunk := src.read(CHUNK_BYTES):
                digest.update(chunk)
                size += len(chunk)
        else:
            with open(destination, 'xb') as dst:
                while chunk := src.read(CHUNK_BYTES):
                    digest.update(chunk)
                    size += len(chunk)
                    dst.write(chunk)
                dst.flush()
                os.fsync(dst.fileno())
    return {'size': size, 'sha256': digest.hexdigest()}


def _write(path, content):
    with open(path, 'xb') as file:
        file.write(content)
        file.flush()
        os.fsync(file.fileno())


def _read_json(path, limit):
    _regular(path)
    with open(path, 'rb') as file:
        raw = file.read(limit + 1)
    _require(len(raw) <= limit, 'Oversized checkpoint metadata')
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise CorruptCheckpoint('Invalid JSON') from exc
    _require(type(result) is dict)
    return result, raw


def _sqlite_error(exc):
    # SQLITE_ERROR indicates malformed/missing schema in this private snapshot.
    # IOERR, CANTOPEN, FULL, BUSY, READONLY, permissions etc MUST propagate.
    if getattr(exc, 'sqlite_errorcode', None) in (
            sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB, sqlite3.SQLITE_ERROR):
        raise CorruptCheckpoint('Invalid SQLite snapshot') from exc
    raise exc


def _counts(db):
    return {table: db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
            for table in ('faces', 'base_slots', 'delta_slots')}


def _description(runtime, key, index, kind, filename):
    table = 'base_slots' if kind == 'base' else 'delta_slots'
    mapped = runtime._db.execute(f'''SELECT COUNT(*) FROM {table}
        WHERE tier=? AND version=? AND dimension=?''', key).fetchone()[0]
    result = dict(key=list(key), kind=kind, file=filename, count=int(index.ntotal),
                  mapped=mapped, type='flat' if isinstance(index, ci.faiss.IndexFlatIP)
                  else 'ivfpq', trained=bool(index.is_trained),
                  metric=int(index.metric_type), bytes=(runtime._compact_bytes(index)
                  if kind == 'base' else 4096 + int(index.ntotal) * (key[2] * 16 + 256)))
    if result['type'] == 'ivfpq':
        result.update(nlist=int(index.nlist), pq_m=int(index.pq.M), pq_bits=int(index.pq.nbits))
    return result


def save_checkpoint(runtime, directory, *, source_revision):
    revision = _revision(source_revision)
    share = Path(directory)
    _separate(runtime._directory, share)
    if share.is_symlink():
        raise ValueError('Checkpoint directory must not be a symlink')
    with runtime._lock:
        runtime._ready()
        generation = 'gen-' + uuid.uuid4().hex
        # Never sqlite3.connect a path on the share, even a temporary one.
        with tempfile.TemporaryDirectory(prefix='checkpoint-', dir=runtime._directory) as temp:
            local = Path(temp)
            with sqlite3.connect(local / 'faces.sqlite3') as backup:
                runtime._db.backup(backup)
            manifest = dict(format=FORMAT_VERSION, faiss_version=ci.faiss.__version__,
                            identity=_identity(share), source_revision=revision,
                            config=asdict(runtime.config),
                            config_sha256=hashlib.sha256(_json(asdict(runtime.config))).hexdigest(),
                            counts=_counts(runtime._db), delta_count=runtime._delta_count,
                            requested_rebuild=runtime._requested_rebuild, groups=[], files={})
            for kind, indexes in (('base', runtime._base), ('delta', runtime._delta)):
                for key, index in sorted(indexes.items()):
                    filename = f'index-{len(manifest["groups"])}.faiss'
                    ci.faiss.write_index(index, str(local / filename))
                    manifest['groups'].append(_description(runtime, key, index, kind, filename))
            share.mkdir(parents=True, exist_ok=True)
            target = share / generation
            target.mkdir()  # exclusive UUID generation; never mutate a published one
            for filename in ['faces.sqlite3', *(g['file'] for g in manifest['groups'])]:
                manifest['files'][filename] = _stream_file(local / filename, target / filename)
            content = _json(manifest)
            if len(content) > MAX_MANIFEST_BYTES:
                raise ValueError('Checkpoint manifest too large')
            _write(target / 'manifest.json', content)
            pointer = share / ('.CURRENT-' + uuid.uuid4().hex)
            try:
                _write(pointer, _json(dict(generation=generation,
                                          sha256=hashlib.sha256(content).hexdigest())))
                os.replace(pointer, share / 'CURRENT')  # the only publication step, LAST
            finally:
                pointer.unlink(missing_ok=True)
    return generation


def _validate_manifest(manifest, runtime, share, revision):
    _require(manifest['format'] == FORMAT_VERSION)
    _require(manifest['faiss_version'] == ci.faiss.__version__)
    _require(manifest['identity'] == _identity(share))
    _require(_json(manifest['source_revision']) == _json(revision), 'Stale checkpoint')
    config = asdict(runtime.config)
    _require(manifest['config'] == config and manifest['config_sha256'] ==
             hashlib.sha256(_json(config)).hexdigest(), 'Configuration mismatch')
    _require(type(manifest['groups']) is list and type(manifest['files']) is dict)
    _require(type(manifest['delta_count']) is int and manifest['delta_count'] >= 0)
    _require(type(manifest['requested_rebuild']) is bool)
    expected, keys, occupied, delta = {'faces.sqlite3'}, set(), runtime._SQLITE_BYTES, 0
    for number, group in enumerate(manifest['groups']):
        _require(type(group) is dict)
        key = group['key']
        _require(type(key) is list and len(key) == 3 and type(key[0]) is str and
                 bool(key[0]) and type(key[1]) is str and type(key[2]) is int and key[2] > 0)
        kind = group['kind']
        _require(kind in ('base', 'delta') and (kind, tuple(key)) not in keys)
        keys.add((kind, tuple(key)))
        filename = f'index-{number}.faiss'
        _require(group['file'] == filename)  # no arbitrary paths, traversal or duplicates
        expected.add(filename)
        _require(type(group['count']) is int and group['count'] >= 0 and
                 type(group['mapped']) is int and 0 <= group['mapped'] <= group['count'])
        _require(group['type'] in ('flat', 'ivfpq') and
                 (kind != 'delta' or group['type'] == 'flat'))
        _require(group['trained'] is True and group['metric'] == ci.faiss.METRIC_INNER_PRODUCT)
        if group['type'] == 'flat':
            size = (4096 + group['count'] * (key[2] * 8 + 16) if kind == 'base'
                    else 4096 + group['count'] * (key[2] * 16 + 256))
        else:
            nlist, m, bits = group['nlist'], group['pq_m'], group['pq_bits']
            _require(type(nlist) is int and nlist > 0 and type(m) is int and
                     0 < m <= key[2] and key[2] % m == 0 and type(bits) is int and 1 <= bits <= 8)
            size = (4096 + group['count'] * ((m * bits + 7) // 8 + 8) * 2 +
                    nlist * key[2] * 16 + (1 << bits) * key[2] * 16 + nlist * 256)
        _require(group['bytes'] == size)
        occupied += size
        if kind == 'delta':
            delta += group['count']
    _require(set(manifest['files']) == expected)
    _require(delta == manifest['delta_count'] and delta <= runtime.delta_limit)
    dimension = max((g['key'][2] for g in manifest['groups']), default=0)
    _require(occupied + runtime._MAX_K * dimension * 16 <= runtime.config.memory_budget_bytes,
             'Checkpoint exceeds memory budget')
    for info in manifest['files'].values():
        _require(type(info) is dict and type(info['size']) is int and info['size'] >= 0 and
                 type(info['sha256']) is str and re.fullmatch('[0-9a-f]{64}', info['sha256']))


def _validate_database(db, manifest):
    _require(db.execute('PRAGMA quick_check').fetchall() == [('ok',)], 'SQLite quick_check failed')
    _require(_counts(db) == manifest['counts'])
    # Native orphan slots and SQL tombstones are legal; mapped slots cannot be
    # out of range. Base mapping is dense, delta can have failed-append gaps.
    for kind, table in (('base', 'base_slots'), ('delta', 'delta_slots')):
        groups = {tuple(g['key']): g for g in manifest['groups'] if g['kind'] == kind}
        for tier, version, dimension, count, low, high in db.execute(f'''
                SELECT tier, version, dimension, COUNT(*), MIN(slot), MAX(slot)
                FROM {table} GROUP BY tier, version, dimension'''):
            group = groups.pop((tier, version, dimension), None)
            _require(group is not None and group['mapped'] == count and
                     low >= 0 and high < group['count'])
            if kind == 'base':
                _require(count == group['count'] and low == 0 and high == count - 1)
        _require(all(g['mapped'] == 0 for g in groups.values()))
    keys = {tuple(g['key']) for g in manifest['groups']}
    for face_id, person, tier, version, dimension, blob, rev, dirty in db.execute('''
            SELECT face_id, person_id, tier, version, dimension, vector, revision, dirty FROM faces'''):
        _require(type(face_id) is str and bool(face_id) and type(person) is str and bool(person))
        _require((tier, version, dimension) in keys and type(blob) is bytes and len(blob) == dimension * 4)
        vector = np.frombuffer(blob, dtype='float32')
        _require(np.isfinite(vector).all() and abs(float(np.linalg.norm(vector)) - 1) < 1e-4)
        _require(type(rev) is int and rev > 0 and dirty in (0, 1))
    # Every live face must be reachable via its current revision and compatibility.
    _require(db.execute('''SELECT COUNT(*) FROM faces f WHERE
        (dirty=0 AND NOT EXISTS (SELECT 1 FROM base_slots s WHERE
         s.face_id=f.face_id AND s.revision=f.revision AND s.tier=f.tier AND
         s.version=f.version AND s.dimension=f.dimension)) OR
        (dirty=1 AND NOT EXISTS (SELECT 1 FROM delta_slots s WHERE
         s.face_row=f.id AND s.revision=f.revision AND s.tier=f.tier AND
         s.version=f.version AND s.dimension=f.dimension))''').fetchone()[0] == 0)


def _load_index(path, group, runtime):
    # All network reads/checksums finished before native deserialization. Native
    # parse failures here are corruption, not disguised SMB availability errors.
    try:
        index = ci.faiss.read_index(str(path))
    except RuntimeError as exc:
        raise CorruptCheckpoint('Invalid native index') from exc
    expected = ci.faiss.IndexFlatIP if group['type'] == 'flat' else ci.faiss.IndexIVFPQ
    _require(type(index) is expected and index.d == group['key'][2] and
             index.ntotal == group['count'] and bool(index.is_trained) and
             index.metric_type == ci.faiss.METRIC_INNER_PRODUCT)
    if group['type'] == 'ivfpq':
        _require(index.nlist == group['nlist'] and index.pq.M == group['pq_m'] and
                 index.pq.nbits == group['pq_bits'] and index.quantizer.is_trained and
                 index.quantizer.d == index.d and index.quantizer.ntotal == index.nlist and
                 index.quantizer.metric_type == ci.faiss.METRIC_INNER_PRODUCT and
                 index.use_precomputed_table == -1)
        index.nprobe = min(index.nlist, runtime.nprobe)
    return index


def restore_checkpoint(cls, checkpointdir, workdir, *, source_revision, config=None, **kwargs):
    revision = _revision(source_revision)
    share, work = Path(checkpointdir), Path(workdir)
    _separate(work, share)
    if work.is_symlink():
        raise ValueError('Local workdir must not be a symlink')
    runtime = cls.__new__(cls)
    runtime._initialize(config=config, **kwargs)  # invalid caller options propagate
    local = None
    success = False
    try:
        _require(not share.is_symlink())
        pointer, _ = _read_json(share / 'CURRENT', 4096)
        generation = pointer['generation']
        _require(type(generation) is str and _GENERATION.fullmatch(generation))
        target = share / generation
        _require(not target.is_symlink())
        manifest, raw = _read_json(target / 'manifest.json', MAX_MANIFEST_BYTES)
        _require(hashlib.sha256(raw).hexdigest() == pointer['sha256'])
        _validate_manifest(manifest, runtime, share, revision)
        work.mkdir(parents=True, exist_ok=True)
        local = Path(tempfile.mkdtemp(prefix='restored-', dir=work))
        # Verify ALL files before even opening SQLite or loading native indexes.
        for filename, expected in manifest['files'].items():
            _regular(target / filename)
            _require(_stream_file(target / filename, local / filename) == expected,
                     'Checkpoint checksum mismatch')
        runtime._directory = local.resolve()
        runtime._db = sqlite3.connect(local / 'faces.sqlite3', isolation_level=None,
                                     check_same_thread=False)
        try:
            _validate_database(runtime._db, manifest)
            runtime._db.executescript('''PRAGMA journal_mode=DELETE;
                PRAGMA synchronous=FULL; PRAGMA cache_size=-512;
                PRAGMA temp_store=FILE; PRAGMA mmap_size=0;''')
        except sqlite3.DatabaseError as exc:
            _sqlite_error(exc)
        runtime._ready()
        for group in manifest['groups']:
            key = tuple(group['key'])
            index = _load_index(local / group['file'], group, runtime)
            if group['kind'] == 'base':
                runtime._base[key] = index
                runtime._base_bytes += runtime._compact_bytes(index)
            else:
                runtime._delta[key] = index
        runtime._delta_count = manifest['delta_count']
        runtime._requested_rebuild = manifest['requested_rebuild']
        success = True
        return runtime
    except (CorruptCheckpoint, KeyError, TypeError, RecursionError):
        return None
    except OSError as exc:
        if exc.errno in (errno.ENOENT, errno.ENOTDIR, errno.ELOOP, errno.EISDIR):
            return None
        raise
    finally:
        if not success:
            if hasattr(runtime, '_db'):
                runtime.close()
            if local is not None:
                shutil.rmtree(local)