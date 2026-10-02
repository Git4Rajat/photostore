"""Live library-local FAISS retrieval, with disk-exact distinct-person reranking.

SQLite stores normalized float32 BLOBs, ownership and base slot mappings, not
Python embedding lists. Compatibility groups have flat/IVF-PQ bases and a small
exact mutable delta. Only one process/runtime may own a directory. Operations
are serialized; validator callbacks must not mutate the runtime.

Normal opening performs an explicit cold rebuild from durable SQLite. Explicit
save_checkpoint/restore_checkpoint preserve native base and live delta slots
without training; see clustering_checkpoint for publication/freshness contracts.
The caller must place active directories on LOCAL disk, never Azure Files/SMB.
SQLite transactions atomically publish builds and writes. Memory accounting is
conservative working-set accounting, NOT an RSS guarantee; caller input and
other process allocations are excluded. Cache only one library per process.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sqlite3
import sys
import threading

import numpy as np
import clustering_index as ci


class RebuildNeeded(RuntimeError):
    """Retryable admission failure: rebuild(), then retry the unchanged write."""


class LiveFaceIndex:
    """config is IndexConfig; delta_limit is global across compatibility groups.

    Writes exceeding the delta cap raise RebuildNeeded BEFORE mutation.
    The cap counts native slots, including obsolete revisions and tombstones.
    needs_rebuild is true at the cap or after admission rejection. rebuild()
    compacts the local SQLite snapshot, without needing the original input.
    Other errors propagate. Small groups are exact flat; larger ones use IVF-PQ.
    Returned scores are exact cosine scores of retrieved candidates, not an ANN
    recall guarantee. Missing results use 0.0/None; negative scores are preserved.
    Supply candidate_validator(face_id, person_id) for fresh ownership/rejection
    checks against the authoritative store. close() is idempotent.
    """

    _SQLITE_BYTES = 1024 * 1024
    _MAX_K = 1024

    def __init__(self, directory, *, config=None, candidates=64, nprobe=64,
                 delta_limit=10000, threads=2):
        self._initialize(config=config, candidates=candidates, nprobe=nprobe,
                         delta_limit=delta_limit, threads=threads)
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        self._directory = directory.resolve()
        self._db = sqlite3.connect(directory / 'faces.sqlite3', isolation_level=None,
                                   check_same_thread=False)
        try:
            self._initialize_database()
            self.rebuild()
        except BaseException:
            self._db.close()
            raise

    def _initialize(self, *, config=None, candidates=64, nprobe=64,
                    delta_limit=10000, threads=2):
        """Initialize state without opening SQLite or invoking native training."""
        ci._require_faiss()
        for name, value in (('candidates', candidates), ('nprobe', nprobe),
                            ('delta_limit', delta_limit), ('threads', threads)):
            if type(value) is not int or value <= 0:
                raise ValueError(f'{name} must be a positive integer')
        self.config = config or ci.IndexConfig()
        c = self.config
        if (c.training_sample_size <= 0 or c.training_points_per_centroid <= 0 or
                c.training_iterations <= 0 or not 1 <= c.pq_bits <= 8 or
                c.pq_subquantizers <= 0 or c.nlist_min <= 0 or
                c.nlist_max < c.nlist_min or c.flat_max_vectors < 0 or
                not 0 < c.memory_budget_bytes < 4 * 1024 ** 3):
            raise ValueError('Invalid index configuration/count')
        self.candidates = min(candidates, self._MAX_K)
        self.nprobe = nprobe
        self.delta_limit = delta_limit
        # macOS app dependencies load competing OpenMP runtimes: parallel FAISS
        # training can segfault, not raise. Use one thread there; at most two on
        # other platforms. This also protects cold rebuilds in an app process.
        self.threads = min(threads, 1 if sys.platform == 'darwin' else 2)
        self._lock = threading.RLock()
        self._base = {}
        self._base_bytes = 0
        self._delta = {}
        self._delta_count = 0
        self._requested_rebuild = False
        self._closed = False
        self._check_budget(self._SQLITE_BYTES)

    def _initialize_database(self):
        self._db.executescript('''
                PRAGMA journal_mode=DELETE;
                PRAGMA synchronous=FULL;
                PRAGMA cache_size=-512;
                PRAGMA temp_store=FILE;
                PRAGMA mmap_size=0;
                CREATE TABLE IF NOT EXISTS faces (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    face_id TEXT NOT NULL UNIQUE, person_id TEXT NOT NULL,
                    tier TEXT NOT NULL, version TEXT NOT NULL,
                    dimension INTEGER NOT NULL, vector BLOB NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1,
                    dirty INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS faces_compatibility
                    ON faces(tier, version, dimension, id);
                CREATE TABLE IF NOT EXISTS base_slots (
                    tier TEXT NOT NULL, version TEXT NOT NULL,
                    dimension INTEGER NOT NULL, slot INTEGER NOT NULL,
                    face_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    PRIMARY KEY(tier, version, dimension, slot)
                ) WITHOUT ROWID;
                CREATE TABLE IF NOT EXISTS delta_slots (
                    tier TEXT NOT NULL, version TEXT NOT NULL,
                    dimension INTEGER NOT NULL, slot INTEGER NOT NULL,
                    face_row INTEGER NOT NULL, revision INTEGER NOT NULL,
                    PRIMARY KEY(tier, version, dimension, slot)
                ) WITHOUT ROWID;
            ''')
        # Older directories have no delta revision. Cold rebuild below
        # discards their mappings; the default must never match a live row.
        columns = {row[1] for row in self._db.execute('PRAGMA table_info(delta_slots)')}
        if 'revision' not in columns:
            self._db.execute('ALTER TABLE delta_slots ADD COLUMN revision INTEGER NOT NULL DEFAULT 0')

    def save_checkpoint(self, directory, *, source_revision):
        """Explicit, locked snapshot; returns the published generation name.

        source_revision MUST cover the latest completed durable assignment batch,
        not just external edits or the current lease/ownership token. Schedule
        after durable publication; this API cannot verify the adapter's journal.
        """
        from clustering_checkpoint import save_checkpoint
        return save_checkpoint(self, directory, source_revision=source_revision)

    @classmethod
    def restore_checkpoint(cls, checkpointdir, workdir, *, source_revision,
                           config=None, **kwargs):
        """Restore into a unique LOCAL workdir child, without rebuilding/training.

        Missing/stale/corrupt returns None; filesystem/SQLite I/O errors propagate.
        Caller owns workdir and must ensure it is local disk (not an SMB mount).
        No active SQLite file is opened on checkpointdir. New owners may restore
        current data. Caller must close the runtime before removing its directory.
        """
        from clustering_checkpoint import restore_checkpoint
        return restore_checkpoint(cls, checkpointdir, workdir,
                                  source_revision=source_revision, config=config, **kwargs)

    def _ready(self):
        if self._closed:
            raise RuntimeError('LiveFaceIndex is closed')
        # OpenMP settings are thread-local: cap on every query/build/write entry.
        ci.faiss.omp_set_num_threads(self.threads)

    def _check_budget(self, amount):
        if amount > self.config.memory_budget_bytes:
            raise ValueError('Aggregate index working set exceeds memory budget')

    @staticmethod
    def _key(tier, version, dimension):
        if not isinstance(tier, str) or not tier:
            raise ValueError('Missing or invalid tier')
        if not isinstance(version, str):
            raise ValueError('Invalid embedding version')
        return tier, version, dimension

    @staticmethod
    def _identity(face_id, person_id):
        if not isinstance(face_id, str) or not face_id:
            raise ValueError('Missing or invalid faceId')
        if not isinstance(person_id, str) or not person_id:
            raise ValueError('Missing or invalid personId')

    def _rows(self, key):
        """Paged SQLite generator; no retained vector or face-ID collection."""
        cursor = self._db.execute('''SELECT face_id, vector FROM faces
            WHERE tier=? AND version=? AND dimension=? ORDER BY id''', key)
        try:
            while page := cursor.fetchmany(128):
                for face_id, blob in page:
                    yield {'faceId': face_id, 'embedding': np.frombuffer(blob, dtype='float32'),
                           'tier': key[0], 'embeddingVersion': key[1]}
        finally:
            cursor.close()

    @staticmethod
    def _compact_bytes(index):
        # FAISS capacity/workspace headroom, without full serialization copies.
        if isinstance(index, ci.faiss.IndexFlatIP):
            return 4096 + int(index.ntotal) * (index.d * 4 * 2 + 16)
        return (4096 + int(index.ntotal) * (index.pq.code_size + 8) * 2 +
                index.nlist * index.d * 4 * 4 +
                (1 << index.pq.nbits) * index.d * 4 * 4 + index.nlist * 256)

    def _delta_bytes(self, delta=None):
        delta = self._delta if delta is None else delta
        return sum(4096 + int(index.ntotal) * (key[2] * 4 * 4 + 256)
                   for key, index in delta.items())

    def _build_snapshot(self):
        """Train sequentially; stage all groups before publishing any of them."""
        staged, staged_bytes = {}, 0
        self._db.execute('DELETE FROM base_slots')
        groups = self._db.execute('''SELECT tier, version, dimension, COUNT(*)
            FROM faces GROUP BY tier, version, dimension''')
        try:
            for tier, version, dimension, count in groups:
                key = (tier, version, dimension)
                occupied = (self._SQLITE_BYTES + self._base_bytes + staged_bytes +
                            self._delta_bytes() + self._MAX_K * dimension * 4 * 4)
                remaining = self.config.memory_budget_bytes - occupied
                self._check_budget(occupied + 1)
                group_config = replace(self.config, memory_budget_bytes=remaining)
                batch = max(1, min(256, remaining // (dimension * 4 * 64)))
                build = ci.build_face_index(tier, self._rows(key), vector_count=count,
                                            batch_size=batch, config=group_config)
                index = build.index
                if isinstance(index, ci.faiss.IndexIVFPQ):
                    index.nprobe = min(index.nlist, self.nprobe)
                # Discard the shared builder's transient ID list after writing
                # the slot mapping: runtime keeps no library-wide Python ID map.
                self._db.executemany('''INSERT INTO base_slots
                    SELECT ?, ?, ?, ?, face_id, revision FROM faces WHERE face_id=?''',
                    ((*key, slot, face_id) for slot, face_id in enumerate(build.face_ids)))
                size = self._compact_bytes(index)
                self._check_budget(occupied + size)
                staged[key] = index
                staged_bytes += size
                del build, index
        finally:
            groups.close()
        self._db.execute('UPDATE faces SET dirty=0')
        self._db.execute('DELETE FROM delta_slots')
        return staged, staged_bytes

    def _replace(self, rows=None):
        self._ready()
        self._db.execute('BEGIN IMMEDIATE')
        try:
            if rows is not None:
                self._db.execute('DELETE FROM faces')
                for row in rows:
                    vector = ci.normalized_vector(row.get('embedding'))
                    if vector is None:
                        continue
                    face_id, person_id = row.get('faceId'), row.get('personId')
                    self._identity(face_id, person_id)
                    key = self._key(row.get('tier'), row.get('embeddingVersion') or '', len(vector))
                    self._db.execute('''INSERT INTO faces
                        (face_id, person_id, tier, version, dimension, vector)
                        VALUES (?, ?, ?, ?, ?, ?)''', (face_id, person_id, *key, vector.tobytes()))
            staged, size = self._build_snapshot()
            self._db.execute('COMMIT')
        except BaseException:
            self._db.execute('ROLLBACK')
            raise
        self._base, self._base_bytes = staged, size
        self._delta = {}
        self._delta_count = 0
        self._requested_rebuild = False

    def build(self, rows):
        """Atomically replace the library from a single-pass iterable of rows."""
        if rows is None:
            raise TypeError('build requires an iterable; use rebuild for the local snapshot')
        with self._lock:
            self._replace(rows)

    def rebuild(self):
        """Compact the durable local snapshot; failure preserves the old runtime."""
        with self._lock:
            self._replace()

    @property
    def needs_rebuild(self):
        with self._lock:
            return self._requested_rebuild or self._delta_count >= self.delta_limit

    def _changed_delta(self, key, face_id, vector):
        """Append one slot; publish its revision mapping in the SQL transaction.

        Native appends cannot roll back. Account for ntotal even when add raises
        after appending, and never reuse those slots. SQL rollback makes failed
        appends unreachable; updates invalidate older mappings by revision.
        No native removal, cloning, dirty-row scan, or error-path rebuild.
        """
        index = self._delta.get(key)
        if index is None:
            index = ci.faiss.IndexFlatIP(key[2])
        slot = int(index.ntotal)
        self._db.execute('''INSERT INTO delta_slots
            (tier, version, dimension, slot, face_row, revision)
            SELECT ?, ?, ?, ?, id, revision FROM faces WHERE face_id=?''',
            (*key, slot, face_id))
        self._delta[key] = index
        try:
            index.add(vector.reshape(1, -1))
        except BaseException:
            self._requested_rebuild = True
            raise
        finally:
            self._delta_count += int(index.ntotal) - slot

    def _old(self, face_id):
        return self._db.execute('''SELECT id, person_id, tier, version, dimension, dirty
            FROM faces WHERE face_id=?''', (face_id,)).fetchone()

    def upsert(self, face_id, person_id, embedding, tier, version):
        """Admit a valid vector to the exact delta, or raise before mutation."""
        with self._lock:
            self._ready()
            vector = ci.normalized_vector(embedding)
            if vector is None:
                return
            self._identity(face_id, person_id)
            key = self._key(tier, version, len(vector))
            count = self._delta_count + 1
            if count > self.delta_limit:
                self._requested_rebuild = True
                raise RebuildNeeded('Delta cap exceeded; call rebuild(), then retry upsert')
            # Reserve append capacity plus bounded query/rerank workspace BEFORE
            # proposed FAISS allocations or SQLite mutations.
            extra = vector.nbytes * 4 + 4096 + 256
            dimension = max([key[2], *(k[2] for k in self._base), *(k[2] for k in self._delta)])
            workspace = self._MAX_K * dimension * 4 * 4
            self._check_budget(self._SQLITE_BYTES + self._base_bytes +
                               self._delta_bytes() + extra + workspace)
            self._db.execute('BEGIN IMMEDIATE')
            try:
                self._db.execute('''INSERT INTO faces
                    (face_id, person_id, tier, version, dimension, vector, dirty)
                    VALUES (?, ?, ?, ?, ?, ?, 1)
                    ON CONFLICT(face_id) DO UPDATE SET
                        person_id=excluded.person_id, tier=excluded.tier,
                        version=excluded.version, dimension=excluded.dimension,
                        vector=excluded.vector, revision=faces.revision+1, dirty=1''',
                    (face_id, person_id, *key, vector.tobytes()))
                self._changed_delta(key, face_id, vector)
                self._db.execute('COMMIT')
            except BaseException:
                if self._db.in_transaction:
                    self._db.execute('ROLLBACK')
                raise

    def remove(self, face_id):
        """Delete only SQLite ownership; native slots remain unreachable until rebuild."""
        with self._lock:
            self._ready()
            with self._db:
                self._db.execute('BEGIN IMMEDIATE')
                self._db.execute('DELETE FROM faces WHERE face_id=?', (face_id,))

    def best_two(self, embedding, tier, version, candidate_validator=None):
        """Double k until two validated distinct people, exhaustion, or k=1024.

        The bound applies per source (base/delta). Scores are max exact cosine
        per distinct person. A runner-up behind >1024 same-person faces may be
        missed. Validator decisions are fresh each query, once per retrieved face.
        """
        with self._lock:
            self._ready()
            vector = ci.normalized_vector(embedding)
            if vector is None:
                return 0.0, 0.0, None
            key = self._key(tier, version, len(vector))
            sources = [(self._base.get(key), False), (self._delta.get(key), True)]
            sources = [(index, delta) for index, delta in sources if index is not None]
            if not sources:
                return 0.0, 0.0, None
            self._check_budget(self._SQLITE_BYTES + self._base_bytes + self._delta_bytes() +
                               self._MAX_K * len(vector) * 4 * 4)
            people, visited = {}, set()
            k = self.candidates
            while True:
                exhausted = True
                candidates = []
                for index, delta in sources:
                    limit = min(k, int(index.ntotal))
                    exhausted &= limit == index.ntotal
                    _, ids = index.search(vector.reshape(1, -1), limit)
                    for slot in ids[0]:
                        if slot < 0 or (delta, int(slot)) in visited:
                            continue
                        visited.add((delta, int(slot)))
                        if delta:
                            row = self._db.execute('''SELECT f.face_id, f.person_id, f.vector
                                FROM delta_slots s JOIN faces f ON f.id=s.face_row AND f.dirty=1
                                AND f.revision=s.revision
                                AND f.tier=s.tier AND f.version=s.version AND f.dimension=s.dimension
                                WHERE s.tier=? AND s.version=? AND s.dimension=? AND s.slot=?''',
                                (*key, int(slot))).fetchone()
                        else:
                            row = self._db.execute('''SELECT f.face_id, f.person_id, f.vector
                                FROM base_slots b JOIN faces f ON f.face_id=b.face_id
                                AND f.revision=b.revision AND f.dirty=0
                                AND f.tier=b.tier AND f.version=b.version AND f.dimension=b.dimension
                                WHERE b.tier=? AND b.version=? AND b.dimension=? AND b.slot=?''',
                                (*key, int(slot))).fetchone()
                        if row is None:
                            continue
                        face_id, person_id, blob = row
                        score = float(np.clip(np.dot(vector, np.frombuffer(blob, dtype='float32')), -1, 1))
                        candidates.append((score, face_id, person_id))
                # Exact-score order permits stopping once the two best VALID
                # distinct identities are known. Validating every ANN hit first
                # turns a fast local search into 64..1024 serial cloud reads.
                for score, face_id, person_id in sorted(candidates, reverse=True):
                    if person_id in people and score <= people[person_id]:
                        continue
                    if len(people) >= 2:
                        runner_up = sorted(people.values(), reverse=True)[1]
                        if score <= runner_up:
                            break
                    if candidate_validator is not None and not candidate_validator(face_id, person_id):
                        continue
                    people[person_id] = max(score, people.get(person_id, -float('inf')))
                if len(people) >= 2 or exhausted or k >= self._MAX_K:
                    break
                k = min(k * 2, self._MAX_K)
            ranked = sorted(people.items(), key=lambda item: (-item[1], item[0]))
            if not ranked:
                return 0.0, 0.0, None
            # Hitting the overfetch cap with just one identity is not proof
            # there is no runner-up. Suppress automatic merges conservatively.
            second = ranked[1][1] if len(ranked) > 1 else (0.0 if exhausted else ranked[0][1])
            return ranked[0][1], second, ranked[0][0]

    def close(self):
        with self._lock:
            if not self._closed:
                self._db.close()
                self._base.clear()
                self._delta.clear()
                self._closed = True