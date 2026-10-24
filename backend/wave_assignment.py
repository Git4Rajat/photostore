"""Frozen, disk-ordered wave inputs and write-ahead per-face decisions.

The wave partition is the authority: input publication, decisions, applied
markers and final coverage are fenced by the stage lease in the SAME Table
transaction. People/membership/face tables are recoverable materializations of
those immutable decisions. They use create-only or genuine-ETag writes.

A private FAISS runtime is rebuilt from the frozen baseline on each attempt,
then committed decisions are replayed in order. No checkpoint can introduce
unrecorded candidates, and each face enters the private runtime at most once.
"""
from dataclasses import asdict
from datetime import datetime
import json
import sqlite3
import tempfile

from azure.core import MatchConditions
from azure.core.exceptions import ResourceNotFoundError
from azure.data.tables import UpdateMode

from clustering_wave import canonical_sort_key
from execution_lease import guard_operations
from faiss_assignment import FaissAssigner, _point, _vector, _rejected, invalidate, _LOCK
from clustering_runtime import LiveFaceIndex, RebuildNeeded
from upload_waves import digest


def encoded(value):
    def timestamp(item):
        if isinstance(item, datetime):
            return item.isoformat()
        raise TypeError('Unsupported snapshot value')
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False, default=timestamp)


def clean(row):
    return json.loads(encoded({k: v for k, v in row.items() if k != '_assignment_etag'}))


class Workspace:
    """SQLite owns the full input/candidate/person maps; Python holds one row."""
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.executescript('''
            PRAGMA cache_size=-512; PRAGMA temp_store=FILE; PRAGMA mmap_size=0;
            CREATE TABLE inputs(face TEXT PRIMARY KEY, payload TEXT, a TEXT, v TEXT,
                                d INTEGER, filename TEXT);
            CREATE TABLE candidates(face TEXT PRIMARY KEY, payload TEXT);
            CREATE TABLE people(person TEXT PRIMARY KEY, payload TEXT);
        ''')

    def close(self):
        self.db.close()

    def input(self, row):
        face = row['RowKey']
        key = canonical_sort_key(face, row)
        try:
            self.db.execute('INSERT INTO inputs VALUES (?,?,?,?,?,?)',
                (face, encoded(row), *key[:-1]))
        except sqlite3.IntegrityError as exc:
            raise ValueError('Duplicate face in frozen wave') from exc

    def inputs(self):
        for (payload,) in self.db.execute('SELECT payload FROM inputs ORDER BY a,v,d,filename,face'):
            yield json.loads(payload)

    def put(self, table, key, row):
        column = 'person' if table == 'people' else 'face'
        self.db.execute(f'INSERT OR REPLACE INTO {table}({column},payload) VALUES (?,?)', (key, encoded(row)))

    def get(self, table, key):
        column = 'person' if table == 'people' else 'face'
        result = self.db.execute(f'SELECT payload FROM {table} WHERE {column}=?', (key,)).fetchone()
        return json.loads(result[0]) if result else None

    def rows(self, table):
        column = 'person' if table == 'people' else 'face'
        for (payload,) in self.db.execute(f'SELECT payload FROM {table} ORDER BY {column}'):
            yield json.loads(payload)

    def count(self, table):
        return self.db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]


class WaveAssignmentJournal:
    def __init__(self, store, wave_id, check):
        self.store, self.wave_id, self.check = store, wave_id, check
        self.prefix = 'assignment!' + wave_id + '!'

    def get(self, key):
        row = self.store._get(self.prefix + key)
        if row is None:
            return None
        body = json.loads(row['payload'])
        if digest(body) != row.get('digest'):
            raise ValueError('Corrupt wave assignment journal')
        return body

    def put(self, key, payload):
        body = encoded(payload)
        if len(body.encode('utf-16-le')) > 60_000:
            raise ValueError('Assignment journal row exceeds byte budget')
        row = self.store._row(self.prefix + key, payload=body, digest=digest(payload))
        def write():
            self.check()
            guards = guard_operations(self.store)
            if not guards:
                raise RuntimeError('Wave assignment requires transaction-fenced stage ownership')
            previous = self.store._get(row['RowKey'])
            if previous is not None:
                if previous.get('payload') != body:
                    raise ValueError('Immutable wave assignment journal rebound')
                return
            self.store.table.submit_transaction([('create', row), *guards])
        self.store._retry(write)
        return digest(payload)

    def rows(self, kind):
        prefix = self.prefix + kind + '!'
        after = ''
        while True:
            self.check()
            page = self.store._page(prefix, after)
            for row in page:
                payload = json.loads(row['payload'])
                if digest(payload) != row['digest']:
                    raise ValueError('Corrupt frozen assignment input')
                yield payload
            if len(page) < 20:
                return
            after = page[-1]['RowKey']

    def prepare(self, workspace, inputs, assigner, binding, baseline_revision):
        header = self.get('manifest')
        if header is not None:
            if header['binding'] != binding:
                raise ValueError('Frozen assignment manifest or matcher policy changed')
            for row in self.rows('input'):
                workspace.input(row)
            for row in self.rows('baseline'):
                workspace.put('candidates', row['faceId'], row)
            for row in self.rows('person'):
                workspace.put('people', row['RowKey'], row)
            if self._description(workspace, binding) != header:
                raise ValueError('Frozen assignment manifest coverage mismatch')
            return header
        # A failed initial capture cannot silently switch to a different
        # library baseline. No assignment effects exist before publication.
        self.put('capture', dict(binding=binding, baselineRevision=json.loads(encoded(baseline_revision))))
        # Input iterator reads only immutable receipt snapshots, never live rows.
        for row in inputs():
            self.check()
            workspace.input(clean(row))
        # UUID5 person rows can predate their face stamps after an interrupted
        # legacy write. Freeze those too: the reference reuses them rather than
        # treating them as a new-person collision on recovery.
        for face in workspace.inputs():
            self.check()
            person_id = assigner.person_id(self.store.library, face['RowKey'])
            person = _point(assigner.person_table, self.store.library, person_id)
            if person is not None:
                workspace.put('people', person_id, clean(person))
        # Empty waves need no candidate scan.
        if workspace.count('inputs'):
            for candidate in assigner._cold_rows(self.store.library):
                self.check()
                person = workspace.get('people', candidate['personId'])
                if person is None:
                    person = _point(assigner.person_table, self.store.library, candidate['personId'])
                    if person is not None:
                        workspace.put('people', candidate['personId'], clean(person))
                workspace.put('candidates', candidate['faceId'], dict(candidate,
                    embedding=candidate['embedding'].tolist()))
        # Deterministic keys also detect a changed partial manifest on retry.
        for position, row in enumerate(workspace.inputs()):
            self.put(f'input!{position:012d}', row)
        for row in workspace.rows('candidates'):
            self.put('baseline!' + digest(row['faceId']), row)
        for row in workspace.rows('people'):
            self.put('person!' + digest(row['RowKey']), row)
        header = self._description(workspace, binding)
        # Publish only after all referenced rows are durable. No decisions yet.
        self.put('manifest', header)
        return header

    @staticmethod
    def _description(workspace, binding):
        chain = digest(['assignment-manifest-v1', binding])
        for kind, rows in [('person', workspace.rows('people')),
                           ('baseline', workspace.rows('candidates')), ('input', workspace.inputs())]:
            for row in rows:
                chain = digest([chain, kind, row])
        return dict(schemaVersion=1, binding=binding, digest=chain,
            faceCount=workspace.count('inputs'), baselineCount=workspace.count('candidates'),
            personCount=workspace.count('people'))

    def decision(self, position):
        return self.get(f'decision!{position:012d}')

    def applied(self, position, decision_id):
        row = self.get(f'applied!{position:012d}')
        if row is not None and row != {'decisionId': decision_id}:
            raise ValueError('Applied marker does not cover this decision')
        return row is not None

    def finish(self, header, chain):
        receipt = dict(waveId=self.wave_id, libraryId=self.store.library,
            frozenRevision=header['binding']['frozenRevision'], manifestId=digest(header),
            faceCount=header['faceCount'], decisionDigest=chain)
        return self.put('complete', receipt)


def _runtime_rows(workspace, guard):
    for row in workspace.rows('candidates'):
        guard.check()
        yield row


def _decide(assigner, library, face, workspace, runtime, guard):
    face_id = face['RowKey']
    result = dict(faceId=face_id, inputDigest=digest(face), outcome='excluded', person=None,
                  newPerson=False, candidate=None)
    if _rejected(face):
        return result
    if face.get('personId'):
        result.update(outcome='preserved', personId=face['personId'])
        return result
    vector = _vector(face.get('embedding'))
    if not assigner._allowed(face) or vector is None:
        return result
    tier, version = assigner.tier(face), assigner.version(face)
    if runtime.needs_rebuild:
        guard.check()
        runtime.rebuild()
        guard.check()
    def valid(face_id, person_id):
        candidate = workspace.get('candidates', face_id)
        return (candidate is not None and candidate['personId'] == person_id
                and candidate['tier'] == tier and candidate['embeddingVersion'] == version
                and workspace.get('people', person_id) is not None)
    person, is_new = assigner.choose_person(library, face_id, vector, tier, version,
        runtime, valid, lambda pid: workspace.get('people', pid))
    person_id = person['RowKey']
    person['faceIds'] = assigner._membership(person, face_id)
    if _vector(person.get('repEmbedding')) is None:
        person.update(repEmbedding=json.dumps(vector.tolist()), repEmbeddingTier=tier, embeddingVersion=version)
    result.update(outcome='assigned', personId=person_id, person=clean(person), newPerson=is_new,
        candidate=dict(faceId=face_id, personId=person_id, embedding=vector.tolist(), tier=tier, embeddingVersion=version))
    return result


def _write(table, row, previous, guard):
    guard.check()
    if previous is None:
        table.create_entity(row)
    else:
        version = previous.get('_assignment_etag')
        if not version:
            raise ValueError('Wave materialization requires a genuine ETag')
        table.update_entity(row, mode=UpdateMode.MERGE, etag=version,
                            match_condition=MatchConditions.IfNotModified)


def _apply(assigner, library, face, decision, decision_id, guard, wave_id):
    if decision['outcome'] == 'excluded':
        return
    live = _point(assigner.face_table, library, face['RowKey'])
    if live is None:
        raise ValueError('Frozen face was deleted before assignment')
    # Source replacement and curation must not be overwritten by old work.
    for key in ('filename', 'sourcePhotoId', 'processingRevision', 'processingGeneration',
                'processingFence', 'embeddingVersion', 'alignmentMethod', 'rejected', 'reviewStatus'):
        if live.get(key) != face.get(key):
            raise ValueError('Frozen face source or curation changed')
    if live.get('assignmentDecisionId') == decision_id:
        return  # Face is written last; its stamp proves prerequisite effects landed.
    expected = face.get('personId') or ''
    current = live.get('personId') or ''
    if current not in (expected, decision['personId']):
        raise ValueError('Face ownership was curated after the manifest froze')
    if decision['outcome'] == 'preserved':
        if current != expected:
            raise ValueError('Preserved face ownership changed')
        return
    actual = assigner._embedding(library, live)
    frozen = _vector(face.get('embedding'))
    if actual is None or frozen is None or not (actual == frozen).all():
        raise ValueError('Frozen face embedding changed')
    target = decision['personId']
    person = _point(assigner.person_table, library, target)
    if person is None:
        if not decision['newPerson']:
            raise ValueError('Matched person was deleted; refusing resurrection')
        person_row = dict(decision['person'], assignmentOrigin=wave_id)
    else:
        if decision['newPerson'] and person.get('assignmentOrigin') != wave_id:
            raise ValueError('New person identity collides with another writer')
        person_row = clean(person)
        person_row['faceIds'] = assigner._membership(person, face['RowKey'])
        if _vector(person.get('repEmbedding')) is None:
            for key in ('repEmbedding', 'repEmbeddingTier', 'embeddingVersion'):
                person_row[key] = decision['person'][key]
    _write(assigner.person_table, person_row, person, guard)
    if assigner.member_table is not None:
        member = _point(assigner.member_table, target, face['RowKey'])
        if member is None:
            _write(assigner.member_table, dict(PartitionKey=target, RowKey=face['RowKey'],
                userId=library, addedAt=decision['person'].get('createdAt') or '',
                assignmentDecisionId=decision_id), None, guard)
        elif member.get('userId') != library:
            raise ValueError('Person membership belongs to another library')
    face_row = clean(live)
    face_row.update(personId=target, assignmentDecisionId=decision_id)
    _write(assigner.face_table, face_row, live, guard)


def _replay(workspace, runtime, decision, guard):
    candidate = decision.get('candidate')
    if candidate is None:
        return
    # Primary-keyed state protects even an accidental second replay in one run.
    previous = workspace.get('candidates', candidate['faceId'])
    if previous != candidate:
        if runtime.needs_rebuild:
            guard.check()
            runtime.rebuild()
            guard.check()
        try:
            runtime.upsert(candidate['faceId'], candidate['personId'], candidate['embedding'],
                           candidate['tier'], candidate['embeddingVersion'])
        except RebuildNeeded:
            guard.check()
            runtime.rebuild()
            guard.check()
            runtime.upsert(candidate['faceId'], candidate['personId'], candidate['embedding'],
                           candidate['tier'], candidate['embeddingVersion'])
        workspace.put('candidates', candidate['faceId'], candidate)
    workspace.put('people', decision['personId'], decision['person'])


def run_frozen_assignment(store, wave, assigner, inputs, check, *, page_size=128, progress=lambda count: None):
    """One logical pass; page_size changes neither decisions nor recovery IDs.

    inputs is a callable yielding frozen face dictionaries, used only while
    publishing the initial manifest. Resume loads the durable manifest instead.
    Metadata/index publication belongs to the downstream index stage.
    """
    if type(page_size) is not int or not 1 <= page_size <= 128:
        raise ValueError('Assignment page size must be between 1 and 128')
    if not isinstance(assigner, FaissAssigner):
        raise TypeError('Wave execution requires the snapshot-aware FaissAssigner path')
    config = assigner.config
    binding = dict(libraryId=store.library, waveId=wave['waveId'],
        frozenRevision=wave['frozenRevision'], manifestDigest=wave['manifestDigest'],
        algorithm='canonical-faiss-v1', threshold=config.threshold, margin=config.margin,
        candidates=config.candidates, nprobe=config.nprobe, deltaLimit=config.delta_limit,
        threads=config.threads, indexConfig=asdict(config.index_config))
    journal = WaveAssignmentJournal(store, wave['waveId'], check)
    # Keep the existing library assignment lease/lock: legacy assignments cannot
    # race baseline capture or a wave's materialization within the same owner.
    with _LOCK, assigner._assignment_lease(store.library) as library_guard:
        class Guard:
            def check(self):
                check(); library_guard.check()
        guard = Guard()
        guard.check()
        invalidate(store.library)  # A wave never publishes a partial legacy cache.
        with tempfile.TemporaryDirectory(prefix='photostore-wave-', dir=config.work_dir) as directory:
            workspace = Workspace(directory + '/manifest.sqlite3')
            runtime = None
            try:
                header = journal.prepare(workspace, inputs, assigner, binding,
                    getattr(library_guard, 'checkpoint_revision', None))
                guard.check()
                runtime = LiveFaceIndex(directory + '/runtime', config=config.index_config,
                    candidates=config.candidates, nprobe=config.nprobe,
                    delta_limit=config.delta_limit, threads=config.threads)
                runtime.build(_runtime_rows(workspace, guard))
                guard.check()
                chain = digest(['wave-decisions-v1', digest(header)])
                begun = False
                for position, face in enumerate(workspace.inputs()):
                    guard.check()
                    if position % page_size == 0:
                        workspace.db.commit()
                    decision = journal.decision(position)
                    if decision is None:
                        decision = _decide(assigner, store.library, face, workspace, runtime, guard)
                        decision.update(manifestId=digest(header), position=position, previousDigest=chain)
                        journal.put(f'decision!{position:012d}', decision)
                    if (decision['faceId'] != face['RowKey'] or decision['inputDigest'] != digest(face)
                            or decision.get('manifestId') != digest(header)
                            or decision.get('position') != position or decision.get('previousDigest') != chain):
                        raise ValueError('Decision is bound to another frozen face')
                    identity = digest(decision)
                    if not journal.applied(position, identity):
                        if not begun:
                            library_guard.begin_assignment()
                            begun = True
                        _apply(assigner, store.library, face, decision, identity, guard, wave['waveId'])
                        journal.put(f'applied!{position:012d}', {'decisionId': identity})
                    _replay(workspace, runtime, decision, guard)
                    chain = digest([chain, position, identity])
                    if (position + 1) % page_size == 0 or position + 1 == header['faceCount']:
                        progress(position + 1)
                guard.check()
                return journal.finish(header, chain)
            finally:
                if runtime is not None:
                    runtime.close()
                workspace.close()
                invalidate(store.library)
