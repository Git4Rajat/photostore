"""Wave assignment runner: canonical order, bounded assignment chunks, a durable
frozen-input/chunk journal, and resumability -- see docs/indexing-clustering-target-
state.md step 5 ("one-shot clustering").

Drives the SAME FaissAssigner.batch/assign API the existing (non-wave) chunk
orchestrator already uses -- this module adds scheduling/journaling around
real, unchanged assignment code, not a new matcher. canonical_order is the
one production-shared implementation of the ordering
backend/tests/clustering_reference_oracle.py's canonical_work also uses, so
a real wave job and that oracle can never silently drift apart.

No real dispatcher calls this yet -- see upload_wave_execution.py's module
docstring on the remaining scope of step 4. This module is itself the next,
independently parity-tested layer (test_wave_clustering_parity.py compares
its output to the oracle's run_single_pass_reference), not yet wired to a
queue consumer.
"""
import json
import hashlib

from azure.core import MatchConditions
from azure.data.tables import UpdateMode

from azure.core.exceptions import ResourceExistsError, ResourceModifiedError, ResourceNotFoundError

from index_work import etag

JOURNAL_PARTITION_PREFIX = 'wave-clustering-journal!'
DEFAULT_CHUNK_SIZE = 128


def canonical_sort_key(face_id, face_row):
    vector = json.loads(face_row.get('embedding') or '[]')
    return (str(face_row.get('alignmentMethod') or ''), str(face_row.get('embeddingVersion') or ''), len(vector),
            str(face_row.get('filename') or ''), face_id)


def canonical_order(faces):
    """`faces`: a mapping of face_id -> face row (dict-like with embedding,
    alignmentMethod, embeddingVersion, filename), or an iterable of
    (face_id, face_row) pairs. Returns face ids ordered by
    (alignmentMethod, embeddingVersion, len(vector), filename, face_id).
    """
    items = faces.items() if hasattr(faces, 'items') else list(faces)
    return [face_id for face_id, _ in sorted(items, key=lambda pair: canonical_sort_key(*pair))]


class ClusteringJournal:
    """Durable input/chunk ledger; individual assignment decisions are not logged.

    The future live caller must supply renewed stage ownership and fencing.
    This class binds the whole input and records chunk plans/commit markers;
    it does not implement that ownership protocol.
    A chunk row is written exactly once, before that chunk's assignment
    calls, and marked committed exactly once after they succeed. Retrying an
    interrupted chunk re-plans the SAME row (face set must match -- the
    canonical order is deterministic and must not change between retries)
    and re-runs assign() for every face in it; FaissAssigner.assign already
    treats an already-owned face as a no-op. The small oracle fixtures qualify
    those tested recovery cases; general decision-journal equivalence remains
    a release prerequisite.
    """

    def __init__(self, table, wave_id):
        self.table = table
        self.wave_id = wave_id
        self.partition = JOURNAL_PARTITION_PREFIX + wave_id

    def _key(self, chunk_index):
        return f'chunk-{chunk_index:08d}'

    def bind_input(self, library, input_digest, chunk_size, face_count):
        row = dict(PartitionKey=self.partition, RowKey='input-v1', libraryId=library,
                   inputDigest=input_digest, chunkSize=chunk_size, faceCount=face_count)
        try:
            existing = self.table.get_entity(self.partition, 'input-v1')
        except ResourceNotFoundError:
            existing = None
        if existing is None:
            try:
                self.table.get_entity(self.partition, self._key(0))
            except ResourceNotFoundError:
                pass
            else:
                raise RuntimeError('Legacy unbound chunk journal requires explicit migration')
            try:
                self.table.create_entity(row)
                return
            except ResourceExistsError:
                existing = self.table.get_entity(self.partition, 'input-v1')
        if any(existing.get(key) != value for key, value in row.items()):
            raise RuntimeError('Frozen wave input or chunk layout changed between retries')

    def plan(self, chunk_index, face_ids):
        key = self._key(chunk_index)
        row = dict(PartitionKey=self.partition, RowKey=key, waveId=self.wave_id,
                   chunkIndex=chunk_index, faceIds=json.dumps(list(face_ids)), status='planned')
        try:
            self.table.create_entity(row)
        except ResourceExistsError:
            existing = self.table.get_entity(self.partition, key)
            if json.loads(existing['faceIds']) != list(face_ids):
                raise RuntimeError('Journaled chunk face set changed between retries; '
                                   'canonical order must be stable')

    def committed(self, chunk_index):
        try:
            row = self.table.get_entity(self.partition, self._key(chunk_index))
        except ResourceNotFoundError:
            return False
        return row.get('status') == 'committed'

    def commit(self, chunk_index):
        key = self._key(chunk_index)
        for _ in range(12):
            row = self.table.get_entity(self.partition, key)
            if row.get('status') == 'committed':
                return
            try:
                version = etag(row)
                if not version:
                    raise RuntimeError('Clustering journal commit requires an ETag')
                self.table.update_entity(dict(row, status='committed'), etag=version,
                    mode=UpdateMode.REPLACE, match_condition=MatchConditions.IfNotModified)
                return
            except ResourceModifiedError:
                continue
        raise RuntimeError('Clustering journal commit contention')


def run_wave_assignment(user_id, assigner, journal, members, face_rows, *, chunk_size=DEFAULT_CHUNK_SIZE):
    """Runs one wave's frozen manifest through the existing FaissAssigner in
    canonical order, bounded chunks, with the durable journal above.

    `members`: {filename: [face_id, ...]} for every member in the frozen
    wave. `face_rows`: {face_id: face_row} across all members, used only
    for canonical ordering -- the actual assignment reads/writes go through
    `assigner` exactly as the existing chunk orchestrator's do.

    Returns the number of chunks newly committed by this call; 0 means every
    chunk was already committed on entry -- a safe, idempotent no-op, which
    is exactly what a recovered/retried job should see on its final call.
    """
    if type(chunk_size) is not int or not 0 < chunk_size <= 128:
        raise ValueError('chunk_size must be between 1 and 128')
    filename_by_face = {}
    for filename, face_ids in members.items():
        for face_id in face_ids:
            if face_id in filename_by_face:
                raise ValueError('Face appears more than once in frozen manifest')
            filename_by_face[face_id] = filename
    if set(filename_by_face) != set(face_rows):
        raise ValueError('Face rows do not exactly cover the frozen manifest')
    if any(row.get('filename') != filename_by_face[face_id] for face_id, row in face_rows.items()):
        raise ValueError('Face/photo identity differs from the frozen manifest')
    work = canonical_order(face_rows)
    fingerprint = hashlib.sha256()
    for face_id in work:
        row = face_rows[face_id]
        payload = [face_id, canonical_sort_key(face_id, row), json.loads(row.get('embedding') or '[]'),
                   row.get('processingRevision')]
        fingerprint.update(json.dumps(payload, separators=(',', ':'), allow_nan=False).encode())
        fingerprint.update(b'\n')
    journal.bind_input(user_id, fingerprint.hexdigest(), chunk_size, len(work))
    newly_committed = 0
    for chunk_index, offset in enumerate(range(0, len(work), chunk_size)):
        chunk = work[offset:offset + chunk_size]
        journal.plan(chunk_index, chunk)
        if journal.committed(chunk_index):
            continue
        with assigner.batch(user_id, chunk, staged=True) as assign:
            for face_id in chunk:
                assign(filename_by_face[face_id], [face_id])
        journal.commit(chunk_index)
        newly_committed += 1
    return newly_committed
