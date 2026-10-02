"""Injected, library-local assignment adapter; no app/storage helper caches.

Callbacks ``clusterable/eligible/tier/version`` receive a face entity.
``metadata_callback(user_id, filename)`` runs after successful persistence (also
on owned-face retries). ``lease(user_id)`` MUST yield a guard with ``check()``;
there is no implicit no-op lease. Tests may explicitly supply one.

One process-wide RLock serializes the single active temporary LiveFaceIndex.
Only cold starts, library/adapter switches, and explicit invalidation stream
the authoritative face partition. No timer retrains the index. Curation and
cross-replica assignment wiring must call invalidate(), or provide guards with
a stable ``cache_generation`` that changes whenever another writer could have
changed the library. A fresh per-acquisition lease ID is NOT such a generation.
Without that integration, validation excludes stale candidates but cannot
discover another replica's new assignments. This module does not claim ANN
recall beyond LiveFaceIndex's bounded retrieval and exact candidate reranking.
"""
from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager, nullcontext
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import hashlib
import math
import logging
import os
from pathlib import Path
import tempfile
import threading
import time
import uuid

from azure.core.exceptions import ResourceNotFoundError
from azure.core import MatchConditions
from azure.data.tables import UpdateMode

from clustering_index import IndexConfig, normalized_vector
from clustering_runtime import LiveFaceIndex, RebuildNeeded


@dataclass(frozen=True)
class AssignmentConfig:
    threshold: float = 0.78
    margin: float = 0.05
    person_cache_size: int = 128
    index_config: IndexConfig = field(default_factory=IndexConfig)
    candidates: int = 64
    nprobe: int = 64
    delta_limit: int = 10000
    threads: int = 2
    cold_stream_embeddings: bool = False
    work_dir: str | None = None
    checkpoint_dir: str | None = None
    checkpoint_interval_seconds: float = 300
    io_concurrency: int = 4

    def __post_init__(self):
        if (not math.isfinite(self.threshold) or not -1 <= self.threshold <= 1
                or not math.isfinite(self.margin) or not 0 <= self.margin <= 2):
            raise ValueError('Invalid assignment threshold/margin')
        if type(self.person_cache_size) is not int or self.person_cache_size <= 0:
            raise ValueError('person_cache_size must be a positive integer')
        if type(self.cold_stream_embeddings) is not bool:
            raise ValueError('cold_stream_embeddings must be a bool')
        if type(self.io_concurrency) is not int or not 1 <= self.io_concurrency <= 16:
            raise ValueError('io_concurrency must be between 1 and 16')
        if (not math.isfinite(self.checkpoint_interval_seconds)
                or self.checkpoint_interval_seconds < 0):
            raise ValueError('Invalid checkpoint interval')
        if self.work_dir and self.checkpoint_dir:
            work, share = Path(self.work_dir).resolve(), Path(self.checkpoint_dir).resolve()
            if work == share or work in share.parents or share in work.parents:
                raise ValueError('Work and checkpoint directories must not overlap')


@dataclass
class _ActiveLibrary:
    owner: object
    user_id: str
    directory: object
    runtime: LiveFaceIndex
    generation: object
    last_checkpoint: float = 0


_LOCK = threading.RLock()
_ACTIVE = None
_LEGACY_MEMBERSHIP_BYTES = 60 * 1024
_LOGGER = logging.getLogger(__name__)
_COLD_BUILD_ATTEMPTS = 0


def invalidate(user_id=None):
    """Close/delete the active cache, optionally only for the specified user."""
    global _ACTIVE
    with _LOCK:
        if _ACTIVE is None or (user_id is not None and _ACTIVE.user_id != user_id):
            return
        active, _ACTIVE = _ACTIVE, None
        try:
            active.runtime.close()
        finally:
            active.directory.cleanup()


def _vector(value):
    """Strict numeric JSON array; malformed/nonfinite/zero vectors are invalid.

    Do not accept booleans, numeric strings, nested arrays, or JSON objects as
    embeddings. Invalid stored data is skipped; storage/transport errors are
    never interpreted as invalid data.
    """
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return None
    if (not isinstance(value, (list, tuple)) or not value
            or any(type(v) not in (int, float) for v in value)):
        return None
    return normalized_vector(value)


def _rejected(face):
    value = face.get('rejected', False)
    return (value is True or value == 1
            or (isinstance(value, str) and value.strip().lower() in ('true', '1', 'yes'))
            or str(face.get('reviewStatus') or '').lower() == 'rejected')


def _point(table, partition, row):
    try:
        entity = table.get_entity(partition_key=partition, row_key=row)
    except ResourceNotFoundError:
        return None
    if entity.get('PartitionKey') != partition or entity.get('RowKey') != row:
        raise ValueError('Point-read entity has mismatched ownership/identity')
    result = dict(entity)
    etag = getattr(entity, 'metadata', {}).get('etag')
    if etag:
        result['_assignment_etag'] = etag
    return result


def _persist(table, entity, guard):
    """Don't overwrite curation that raced a candidate/ownership point read."""
    payload = dict(entity)
    etag = payload.pop('_assignment_etag', None)
    guard.check()
    if etag:
        table.update_entity(payload, mode=UpdateMode.MERGE,
                            etag=etag, match_condition=MatchConditions.IfNotModified)
    else:
        table.upsert_entity(payload)


class FaissAssigner:
    """assign(user_id, filename, face_ids) -> (face-to-person dict, created set).

    Required callbacks retain application quality/version policy; all writes
    use injected table clients directly. Existing owners are never reassigned.
    person -> optional membership -> face -> local runtime is the write order.
    UUID5 identities recover new-person writes interrupted before face stamps.
    Errors propagate; runtime mutation failure additionally invalidates cache.
    ``created`` contains only person rows first written by the successful call.
    """

    def __init__(self, *, face_table, person_table, embedding_table,
                 metadata_callback, clusterable, eligible, tier, version, lease,
                 member_table=None, config=None):
        self.face_table = face_table
        self.person_table = person_table
        self.embedding_table = embedding_table
        self.member_table = member_table
        self.metadata_callback = metadata_callback
        self.clusterable = clusterable
        self.eligible = eligible
        self.tier = tier
        self.version = version
        self.lease = lease
        self.config = config or AssignmentConfig()
        if face_table is None or person_table is None:
            raise ValueError('face_table and person_table are required')
        for callback in (metadata_callback, clusterable, eligible, tier, version, lease):
            if not callable(callback):
                raise TypeError('Assignment callbacks and lease must be callable')

    @staticmethod
    def person_id(user_id, face_id):
        """Collision-unambiguous UUID5 seed from the library and source face."""
        return str(uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(
            [user_id, face_id], ensure_ascii=True, separators=(',', ':'))))

    def invalidate(self, user_id=None):
        invalidate(user_id)

    @contextmanager
    def batch(self, user_id, face_ids=()):
        """One library lease/revision for a bounded group; ordered decisions.

        Prefetch only input faces/embeddings. Candidate validation remains fresh.
        A prefetched ID is consumed once, so duplicate deliveries re-read ownership.
        Checkpoint only after all calls succeed; queue callers retain failed jobs.
        """
        started = time.monotonic()
        with _LOCK, self.lease(user_id) as guard:
            lease_ms = int((time.monotonic() - started) * 1000)
            guard.check()
            prefetch_started = time.monotonic()
            faces = {}
            # Cap retained entities/vectors even for unusually face-dense photos.
            # Remaining IDs use the normal fresh sequential read path.
            ids = list(dict.fromkeys(face_ids))[:128]

            def read(face_id):
                face = _point(self.face_table, user_id, face_id)
                vector = (self._embedding(user_id, face) if face is not None
                          and not face.get('personId') and self._allowed(face) else None)
                return face_id, (face, vector)

            # Bound submitted work as well as active threads: queue batches are
            # bounded, but an individual photo can contain many faces.
            with ThreadPoolExecutor(max_workers=self.config.io_concurrency) as pool:
                for offset in range(0, len(ids), self.config.io_concurrency):
                    faces.update(pool.map(read, ids[offset:offset + self.config.io_concurrency]))
            guard.check()
            prefetch_ms = int((time.monotonic() - prefetch_started) * 1000)
            begun, failed = False, False
            assigner = self

            class BatchGuard:
                cache_generation = getattr(guard, 'cache_generation', None)

                @property
                def checkpoint_revision(self):
                    return getattr(guard, 'checkpoint_revision', None)

                def check(self):
                    guard.check()

                def begin_assignment(self):
                    nonlocal begun
                    if not begun:
                        begin = getattr(guard, 'begin_assignment', None)
                        if callable(begin):
                            begin()
                        elif assigner.config.checkpoint_dir:
                            raise TypeError('Checkpointing requires revision publication')
                        begun = True

            def assign(filename, ids):
                nonlocal failed
                try:
                    return self.assign(user_id, filename, ids, _guard=BatchGuard(),
                                       _prefetched=faces, _defer_checkpoint=True)
                except Exception:
                    failed = True
                    raise

            yield assign
            if not failed and _ACTIVE is not None and _ACTIVE.user_id == user_id and _ACTIVE.owner is self:
                self._checkpoint(user_id, guard)
            _LOGGER.info('faiss batch user=%s prefetched_faces=%d failed=%s lease_ms=%d '
                         'prefetch_ms=%d elapsed_ms=%d', user_id, len(ids), failed,
                         lease_ms, prefetch_ms, int((time.monotonic() - started) * 1000))

    def _allowed(self, face):
        return not _rejected(face) and self.clusterable(face) and self.eligible(face)

    def _embedding(self, user_id, face):
        value = face.get('embedding')
        if value is None or value == '' or value == [] or value == '[]':
            if self.embedding_table is None:
                return None
            row = _point(self.embedding_table, user_id, face['RowKey'])
            value = row.get('embedding') if row is not None else None
        return _vector(value)

    def _cold_rows(self, user_id):
        # Single-pass SDK paging: never list() the face partition or scan people.
        escaped = user_id.replace("'", "''")
        query = f"PartitionKey eq '{escaped}'"
        faces = self.face_table.query_entities(query)
        stream = self.config.cold_stream_embeddings

        def checked(rows, source):
            previous = None
            for row in rows:
                key = row.get('RowKey')
                if row.get('PartitionKey') != user_id:
                    raise ValueError(f'{source} stream has mismatched PartitionKey')
                if not isinstance(key, str) or not key:
                    raise ValueError(f'{source} stream has invalid RowKey')
                if previous is not None and key <= previous:
                    raise ValueError(f'{source} stream RowKeys must be strictly increasing')
                previous = key
                yield row

        if stream:
            faces = checked(faces, 'Face')
        embeddings = None
        embedding = None
        for face in faces:
            if stream and self.embedding_table is not None:
                # Open lazily: an empty face partition must not scan embeddings.
                # Azure Tables orders a partition by RowKey. Retain one join row,
                # advance even for dropped faces, and do not drain trailing rows.
                if embeddings is None:
                    embeddings = iter(checked(self.embedding_table.query_entities(
                        query, select=['RowKey', 'PartitionKey', 'embedding']), 'Embedding'))
                    embedding = next(embeddings, None)
                while embedding is not None and embedding['RowKey'] < face['RowKey']:
                    embedding = next(embeddings, None)
            if (face.get('PartitionKey') != user_id or not face.get('RowKey')
                    or not face.get('personId') or not self._allowed(face)):
                continue
            if stream:
                value = face.get('embedding')
                if value is None or value == '' or value == [] or value == '[]':
                    value = (embedding.get('embedding') if embedding is not None
                             and embedding['RowKey'] == face['RowKey'] else None)
                vector = _vector(value)
            else:
                vector = self._embedding(user_id, face)
            if vector is not None:
                yield {'faceId': face['RowKey'], 'personId': face['personId'],
                       'embedding': vector, 'tier': self.tier(face),
                       'embeddingVersion': self.version(face)}

    def _runtime(self, user_id, guard):
        global _ACTIVE, _COLD_BUILD_ATTEMPTS
        generation = getattr(guard, 'cache_generation', None)
        if (_ACTIVE is not None and (_ACTIVE.owner is not self
                or _ACTIVE.user_id != user_id or _ACTIVE.generation != generation)):
            invalidate()
        if _ACTIVE is None:
            if self.config.work_dir:
                Path(self.config.work_dir).mkdir(parents=True, exist_ok=True)
            directory = tempfile.TemporaryDirectory(prefix='photostore-faiss-', dir=self.config.work_dir)
            checkpoint = self._checkpoint_path(user_id, guard)
            runtime = None
            try:
                if checkpoint is not None:
                    started = time.monotonic()
                    try:
                        runtime = LiveFaceIndex.restore_checkpoint(
                            checkpoint, directory.name, source_revision=guard.checkpoint_revision,
                            config=self.config.index_config, candidates=self.config.candidates,
                            nprobe=self.config.nprobe, delta_limit=self.config.delta_limit,
                            threads=self.config.threads)
                    except OSError:
                        _LOGGER.warning('faiss checkpoint restore unavailable user=%s', user_id, exc_info=True)
                    guard.check()
                    if runtime is not None:
                        _ACTIVE = _ActiveLibrary(self, user_id, directory, runtime, generation,
                                                 time.monotonic())
                        _LOGGER.info('faiss checkpoint restored user=%s elapsed_ms=%d pid=%d',
                                     user_id, int((time.monotonic() - started) * 1000), os.getpid())
                        return runtime
                    _LOGGER.info('faiss checkpoint missing stale or invalid user=%s', user_id)
            except BaseException:
                if runtime is not None:
                    runtime.close()
                directory.cleanup()
                raise
            _COLD_BUILD_ATTEMPTS += 1
            started = time.monotonic()
            _LOGGER.info('faiss cold build started user=%s pid=%d build_attempt=%d '
                         'first_build_in_process=%s', user_id, os.getpid(),
                         _COLD_BUILD_ATTEMPTS, _COLD_BUILD_ATTEMPTS == 1)
            runtime = None
            ready = False
            try:
                runtime = LiveFaceIndex(directory.name, config=self.config.index_config,
                                        candidates=self.config.candidates,
                                        nprobe=self.config.nprobe,
                                        delta_limit=self.config.delta_limit,
                                        threads=self.config.threads)
                runtime.build(self._cold_rows(user_id))
                guard.check()
                _LOGGER.info('faiss cold build user=%s elapsed_ms=%d pid=%d build_attempt=%d', user_id,
                             int((time.monotonic() - started) * 1000), os.getpid(), _COLD_BUILD_ATTEMPTS)
                _ACTIVE = _ActiveLibrary(self, user_id, directory, runtime, generation)
                ready = True
            finally:
                if not ready:
                    try:
                        if runtime is not None:
                            runtime.close()
                    finally:
                        directory.cleanup()
        return _ACTIVE.runtime

    def _checkpoint_path(self, user_id, guard):
        if not self.config.checkpoint_dir:
            return None
        if (getattr(guard, 'checkpoint_revision', None) is None
                or not callable(getattr(guard, 'begin_assignment', None))):
            raise TypeError('Checkpointing requires a revision-publishing lease guard')
        return Path(self.config.checkpoint_dir) / hashlib.sha256(user_id.encode('utf-8')).hexdigest()

    def _checkpoint(self, user_id, guard):
        checkpoint = self._checkpoint_path(user_id, guard)
        if checkpoint is None or _ACTIVE is None:
            return
        now = time.monotonic()
        if (_ACTIVE.last_checkpoint and
                now - _ACTIVE.last_checkpoint < self.config.checkpoint_interval_seconds):
            return
        guard.check()
        try:
            _ACTIVE.runtime.save_checkpoint(checkpoint, source_revision=guard.checkpoint_revision)
        except OSError:
            # A stale snapshot stays stale: its revision cannot cover these writes.
            _LOGGER.warning('faiss checkpoint save unavailable user=%s', user_id, exc_info=True)
        else:
            _ACTIVE.last_checkpoint = time.monotonic()
            _LOGGER.info('faiss checkpoint saved user=%s elapsed_ms=%d', user_id,
                         int((time.monotonic() - now) * 1000))
        guard.check()

    def _validator(self, user_id, tier, version, stats=None):
        # Bounded LRU belongs to ONE best_two query, never the library/adapter.
        people = OrderedDict()

        def validate(face_id, person_id):
            if stats is not None:
                stats['candidate_face_reads'] += 1
            face = _point(self.face_table, user_id, face_id)
            if (face is None or face.get('personId') != person_id
                    or not self._allowed(face) or self.tier(face) != tier
                    or self.version(face) != version):
                return False
            if person_id not in people:
                if stats is not None:
                    stats['candidate_person_reads'] += 1
                people[person_id] = _point(self.person_table, user_id, person_id) is not None
                if len(people) > self.config.person_cache_size:
                    people.popitem(last=False)
            people.move_to_end(person_id)
            return people[person_id]

        return validate

    @staticmethod
    def _membership(person, face_id):
        ids = json.loads(person.get('faceIds') or '[]')
        if not isinstance(ids, list) or any(not isinstance(fid, str) for fid in ids):
            raise ValueError('Invalid legacy person faceIds')
        if face_id not in ids:
            ids.append(face_id)
        encoded = json.dumps(ids, ensure_ascii=True, separators=(',', ':'))
        # Azure Tables strings are UTF-16; ASCII JSON also bounds UTF-8 usage.
        if len(encoded.encode('utf-16-le')) > _LEGACY_MEMBERSHIP_BYTES:
            raise ValueError('Legacy person faceIds exceeds 60 KiB; refusing truncation')
        return encoded

    def assign(self, user_id, filename, face_ids, *, _guard=None,
               _prefetched=None, _defer_checkpoint=False):
        if not isinstance(user_id, str) or not user_id:
            raise ValueError('user_id must be a nonempty string')
        assignments, created = {}, set()
        started = time.monotonic()
        stats = {'candidate_face_reads': 0, 'candidate_person_reads': 0}
        search_ms = 0
        persistence_ms = 0
        metadata_ms = 0
        with _LOCK, (self.lease(user_id) if _guard is None else nullcontext(_guard)) as guard:
            if not callable(getattr(guard, 'check', None)):
                raise TypeError('lease must yield a guard with check()')
            guard.check()
            generation = getattr(guard, 'cache_generation', None)
            if (_ACTIVE is not None and (_ACTIVE.owner is not self
                    or _ACTIVE.user_id != user_id or _ACTIVE.generation != generation)):
                _LOGGER.info('faiss cache invalidated user=%s adapter_changed=%s '
                             'library_changed=%s generation_changed=%s pid=%d '
                             'ownership_changed=%s source_revision_changed=%s', user_id,
                             _ACTIVE.owner is not self, _ACTIVE.user_id != user_id,
                             _ACTIVE.generation != generation, os.getpid(),
                             (isinstance(_ACTIVE.generation, tuple) and isinstance(generation, tuple)
                              and len(_ACTIVE.generation) == len(generation) == 2
                              and _ACTIVE.generation[0] != generation[0]),
                             (isinstance(_ACTIVE.generation, tuple) and isinstance(generation, tuple)
                              and len(_ACTIVE.generation) == len(generation) == 2
                              and _ACTIVE.generation[1] != generation[1]))
                invalidate()
            runtime = None
            assignment_started = False
            for face_id in face_ids:
                guard.check()
                prefetched = _prefetched.pop(face_id, None) if _prefetched is not None else None
                face = prefetched[0] if prefetched is not None else _point(self.face_table, user_id, face_id)
                if face is None or _rejected(face):
                    continue
                if face.get('personId'):
                    assignments[face_id] = face['personId']
                    continue
                if not self._allowed(face):
                    continue
                vector = prefetched[1] if prefetched is not None else self._embedding(user_id, face)
                if vector is None:
                    continue
                tier, version = self.tier(face), self.version(face)
                if runtime is None:
                    runtime = self._runtime(user_id, guard)
                if runtime.needs_rebuild:
                    rebuild_started = time.monotonic()
                    runtime.rebuild()
                    _LOGGER.info('faiss delta compaction user=%s elapsed_ms=%d', user_id,
                                 int((time.monotonic() - rebuild_started) * 1000))
                    guard.check()
                deterministic_id = self.person_id(user_id, face_id)
                person = _point(self.person_table, user_id, deterministic_id)
                is_new = False
                if person is None:
                    search_started = time.monotonic()
                    best, second, matched_id = runtime.best_two(
                        vector, tier, version, self._validator(user_id, tier, version, stats))
                    search_ms += int((time.monotonic() - search_started) * 1000)
                    if (matched_id is not None and best >= self.config.threshold
                            and best - second >= self.config.margin):
                        # Read the matched row directly, not a helper/summary cache.
                        person = _point(self.person_table, user_id, matched_id)
                        if person is None:
                            raise RuntimeError('Matched person disappeared before persistence')
                    else:
                        is_new = True
                        person = {'PartitionKey': user_id, 'RowKey': deterministic_id,
                                  'name': '',
                                  'createdAt': datetime.now(timezone.utc).isoformat(),
                                  'faceIds': '[]'}
                person_id = person['RowKey']
                person['faceIds'] = self._membership(person, face_id)
                # Keep the first valid source exemplar, never mix/average tiers.
                if _vector(person.get('repEmbedding')) is None:
                    person['repEmbedding'] = json.dumps(vector.tolist(), allow_nan=False)
                    person['repEmbeddingTier'] = tier
                    person['embeddingVersion'] = version
                if not assignment_started:
                    # Enabled for all production guards, even without a share:
                    # another process may have checkpointing enabled later.
                    begin = getattr(guard, 'begin_assignment', None)
                    if callable(begin):
                        begin()
                    elif self.config.checkpoint_dir:
                        raise TypeError('Checkpointing requires revision publication')
                    assignment_started = True
                persistence_started = time.monotonic()
                _persist(self.person_table, person, guard)
                if self.member_table is not None:
                    member = {'PartitionKey': person_id, 'RowKey': face_id,
                              'userId': user_id,
                              'addedAt': datetime.now(timezone.utc).isoformat()}
                    guard.check()
                    self.member_table.upsert_entity(member)
                face['personId'] = person_id
                guard.check()
                persisted = False
                try:
                    _persist(self.face_table, face, guard)
                    persisted = True
                finally:
                    # A failed response may follow a committed remote stamp.
                    # Do not keep a local snapshot that cannot know its outcome.
                    if not persisted:
                        invalidate(user_id)
                updated = False
                try:
                    try:
                        runtime.upsert(face_id, person_id, vector, tier, version)
                    except RebuildNeeded:
                        runtime.rebuild()
                        guard.check()
                        runtime.upsert(face_id, person_id, vector, tier, version)
                    updated = True
                finally:
                    if not updated:
                        invalidate(user_id)
                persistence_ms += int((time.monotonic() - persistence_started) * 1000)
                assignments[face_id] = person_id
                if is_new:
                    created.add(person_id)
            if assignments:
                guard.check()
                metadata_started = time.monotonic()
                self.metadata_callback(user_id, filename)
                metadata_ms = int((time.monotonic() - metadata_started) * 1000)
            if runtime is not None and not _defer_checkpoint:
                self._checkpoint(user_id, guard)
            _LOGGER.info('faiss assignment user=%s filename=%s assigned=%d created=%d '
                     'candidate_face_reads=%d candidate_person_reads=%d search_ms=%d '
                     'persistence_ms=%d metadata_ms=%d total_ms=%d',
                     user_id, filename, len(assignments), len(created),
                     stats['candidate_face_reads'], stats['candidate_person_reads'], search_ms,
                     persistence_ms, metadata_ms,
                     int((time.monotonic() - started) * 1000))
        return assignments, created