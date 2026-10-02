"""Pure adapter tests with real FAISS and fault-injectable point-read tables."""
from collections import Counter
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
import threading
from types import SimpleNamespace

from azure.core import MatchConditions
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
from azure.data.tables import UpdateMode
import pytest

pytest.importorskip('faiss')
import faiss_assignment as assignment
from faiss_assignment import AssignmentConfig, FaissAssigner


class _EtagEntity(dict):
    """SDK-shaped point-read row: ETags live in metadata, not entity fields."""

    def __init__(self, row, etag):
        super().__init__(row)
        self.metadata = {'etag': etag}


class Table:
    def __init__(self, name, events):
        self.name, self.events = name, events
        self.rows = {}
        self.reads = Counter()
        self.queries = []
        self.fail_read = {}
        self.fail_query = None
        self.fail_write = None
        self.commit_then_fail = False
        self.etags = {}
        self.conditional_writes = []

    def seed(self, row):
        self.rows[row['PartitionKey'], row['RowKey']] = dict(row)

    def get_entity(self, *, partition_key, row_key):
        key = partition_key, row_key
        self.reads[key] += 1
        if key in self.fail_read:
            raise self.fail_read[key]
        if key not in self.rows:
            raise ResourceNotFoundError('not found')
        if key in self.etags:
            return _EtagEntity(self.rows[key], self.etags[key])
        return dict(self.rows[key])

    def query_entities(self, query_filter):
        self.queries.append(query_filter)
        assert query_filter.startswith("PartitionKey eq '")
        partition = query_filter[len("PartitionKey eq '"):-1].replace("''", "'")
        # Deliberately streaming; errors may occur during iteration, not call.
        for (user, _), row in self.rows.items():
            if user == partition:
                yield dict(row)
        if self.fail_query is not None:
            raise self.fail_query

    def upsert_entity(self, entity):
        assert self.events[-1] == 'check', 'lease check must immediately precede write'
        self.events.append(self.name)
        if self.fail_write is not None:
            error, self.fail_write = self.fail_write, None
            if self.commit_then_fail:
                self.seed(entity)
            raise error
        self.seed(entity)

    def update_entity(self, entity, **kwargs):
        self.conditional_writes.append((dict(entity), dict(kwargs)))
        assert kwargs == dict(mode=UpdateMode.MERGE,
                              etag=self.etags[entity['PartitionKey'], entity['RowKey']],
                              match_condition=MatchConditions.IfNotModified)
        assert '_assignment_etag' not in entity
        self.upsert_entity(entity)


class Harness:
    def __init__(self, *, config=None, members=True):
        self.events = []
        self.faces = Table('face', self.events)
        self.people = Table('person', self.events)
        self.embeddings = Table('embedding', self.events)
        self.members = Table('member', self.events)
        self.metadata = []
        self.metadata_error = None
        self.generation = None
        self.checks = 0
        self.fail_check = None
        self.assigner = FaissAssigner(
            face_table=self.faces, person_table=self.people,
            embedding_table=self.embeddings, member_table=self.members if members else None,
            metadata_callback=self.update_metadata,
            clusterable=lambda row: row.get('clusterable', True),
            eligible=lambda row: row.get('eligible', True),
            tier=lambda row: row['alignmentMethod'],
            version=lambda row: row.get('embeddingVersion', ''),
            lease=self.lease, config=config)

    @contextmanager
    def lease(self, user):
        self.events.append(('lease', user))
        harness = self

        class Guard:
            cache_generation = harness.generation

            def check(self):
                harness.checks += 1
                harness.events.append('check')
                if harness.checks == harness.fail_check:
                    raise RuntimeError('lost lease')

        try:
            yield Guard()  # An explicitly injected test lease, never a default.
        finally:
            self.events.append('release')

    def update_metadata(self, user, filename):
        self.events.append('metadata')
        self.metadata.append((user, filename))
        if self.metadata_error:
            raise self.metadata_error

    def face(self, face_id, vector=(1, 0), *, user='u', person='', tier='5pt',
             version='v1', **extra):
        row = dict(PartitionKey=user, RowKey=face_id, filename='photo.jpg',
                   personId=person, alignmentMethod=tier, embeddingVersion=version,
                   embedding=json.dumps(vector), **extra)
        self.faces.seed(row)
        return row

    def person(self, person_id, ids=(), *, user='u', rep=(0, 1), name='Alice', **extra):
        row = dict(PartitionKey=user, RowKey=person_id, faceIds=json.dumps(list(ids)),
                   repEmbedding=json.dumps(rep), name=name, **extra)
        self.people.seed(row)
        return row

    def assign(self, *ids, user='u'):
        return self.assigner.assign(user, 'photo.jpg', ids)


@pytest.fixture(autouse=True)
def clean_runtime():
    assignment.invalidate()
    yield
    assignment.invalidate()


def test_warm_no_scans_and_source_faces_not_person_representatives(monkeypatch):
    h = Harness()
    h.person('alice', ['a', 'aa'], rep=[0, 1])
    h.face('a', person='alice')
    h.face('aa', [.99, .01], person='alice')
    h.face('new')
    builds = []
    original = assignment.LiveFaceIndex.build

    def build(runtime, rows):
        assert iter(rows) is rows  # No materialized library list.
        builds.append(True)
        return original(runtime, rows)

    monkeypatch.setattr(assignment.LiveFaceIndex, 'build', build)
    assert h.assign('new') == ({'new': 'alice'}, set())
    h.face('newer')
    assert h.assign('newer') == ({'newer': 'alice'}, set())
    assert builds == [True]
    assert h.faces.queries == ["PartitionKey eq 'u'"]
    assert not h.people.queries and not h.embeddings.queries and not h.members.queries
    row = h.people.rows['u', 'alice']
    assert row['name'] == 'Alice' and json.loads(row['repEmbedding']) == [0, 1]
    assert json.loads(row['faceIds']) == ['a', 'aa', 'new', 'newer']
    assert h.people.reads['u', 'alice'] == 4  # Query-local existence + matched point read.
    writes = [event for event in h.events if event in ('person', 'member', 'face', 'metadata')]
    assert writes == ['person', 'member', 'face', 'metadata'] * 2


def test_candidate_cloud_reads_bounded_by_valid_top_two(caplog):
    h = Harness(config=AssignmentConfig(candidates=64))
    for i in range(100):
        h.person(str(i), [f'face-{i}'])
        h.face(f'face-{i}', [1, i / 100], person=str(i))
    h.face('new')
    with caplog.at_level('INFO', logger='faiss_assignment'):
        h.assign('new')
    candidate_reads = sum(count for (user, face), count in h.faces.reads.items()
                          if face.startswith('face-'))
    assert candidate_reads == 2
    assert 'candidate_face_reads=2 candidate_person_reads=2' in caplog.text
    assert 'faiss cold build' in caplog.text


@pytest.mark.parametrize('change', [dict(tier='2pt'), dict(version='v2'), dict(vector=[1, 0, 0])])
def test_tier_version_dimension_separation(change):
    h = Harness()
    h.person('alice', ['a'])
    h.face('a', person='alice')
    h.face('new', **change)
    mapping, created = h.assign('new')
    assert mapping['new'] != 'alice' and created == {mapping['new']}
    assert json.loads(h.people.rows['u', 'alice']['faceIds']) == ['a']


def test_exact_match_vs_unique_and_distinct_person_margin():
    h = Harness(config=AssignmentConfig(threshold=.9, margin=.15, candidates=1))
    h.person('alice', ['a', 'aa'])
    h.person('bob', ['b'])
    h.face('a', person='alice')
    h.face('aa', [.9999, .0001], person='alice')
    h.face('b', [.8, .6], person='bob')
    h.face('match')
    assert h.assign('match') == ({'match': 'alice'}, set())
    h.face('unique', [-1, 0])
    mapping, created = h.assign('unique')
    assert mapping['unique'] not in ('alice', 'bob') and created == {mapping['unique']}


def test_close_distinct_people_fail_margin_even_above_threshold():
    h = Harness(config=AssignmentConfig(threshold=.78, margin=.1))
    for pid, vector in [('alice', [1, 0]), ('bob', [.99, .1])]:
        h.person(pid, [pid])
        h.face(pid, vector, person=pid)
    h.face('new')
    mapping, created = h.assign('new')
    assert mapping['new'] not in ('alice', 'bob') and created == {mapping['new']}


@pytest.mark.parametrize('change', [dict(rejected=True), dict(rejected='true'),
                                    dict(reviewStatus='rejected'), dict(clusterable=False),
                                    dict(eligible=False)])
def test_rejected_and_ineligible_targets_skip_without_build(change):
    h = Harness()
    h.face('new', **change)
    assert h.assign('new', 'missing') == ({}, set())
    assert not h.faces.queries and not h.people.reads and not h.metadata
    assert assignment._ACTIVE is None


@pytest.mark.parametrize('change', ['rejected', 'owner', 'tier', 'version', 'eligible', 'missing-person'])
def test_fresh_candidate_validation_on_warm_queries(change):
    h = Harness()
    h.person('alice', ['a'])
    h.face('a', person='alice')
    h.face('first')
    assert h.assign('first')[0]['first'] == 'alice'
    # Remove the locally added candidate as well, so only 'a' can match.
    h.faces.rows['u', 'first']['rejected'] = True
    face = h.faces.rows['u', 'a']
    if change == 'missing-person':
        del h.people.rows['u', 'alice']
    else:
        key, value = {'rejected': ('rejected', True), 'owner': ('personId', 'bob'),
                      'tier': ('alignmentMethod', '2pt'), 'version': ('embeddingVersion', 'v2'),
                      'eligible': ('eligible', False)}[change]
        face[key] = value
    h.face('new')
    mapping, _ = h.assign('new')
    assert mapping['new'] != 'alice'
    assert len(h.faces.queries) == 1


def test_owned_faces_no_reassignment_or_persistence_and_rejected_owned_skip():
    h = Harness()
    h.face('owned', person='existing', eligible=False)
    h.face('rejected', person='existing', rejected=True)
    assert h.assign('owned', 'rejected') == ({'owned': 'existing'}, set())
    assert not h.people.reads and not h.faces.queries
    assert h.metadata == [('u', 'photo.jpg')]
    assert not any(event in ('person', 'face', 'member') for event in h.events)


@pytest.mark.parametrize('stage', ['person', 'member', 'face'])
def test_deterministic_crash_replay_after_authoritative_writes(stage):
    h = Harness()
    h.face('new')
    table = {'person': h.people, 'member': h.members, 'face': h.faces}[stage]
    table.fail_write = HttpResponseError('write failed')
    table.commit_then_fail = stage == 'person'
    with pytest.raises(HttpResponseError, match='write failed'):
        h.assign('new')
    deterministic = h.assigner.person_id('u', 'new')
    assert ('u', deterministic) in h.people.rows
    assert not h.faces.rows['u', 'new']['personId']
    if stage == 'face':
        assert assignment._ACTIVE is None
    else:
        assert assignment._ACTIVE.runtime._old('new') is None
    h.assigner.invalidate()  # Simulate a process restart with no helper caches.
    assert h.assign('new') == ({'new': deterministic}, set())
    assert len(h.people.rows) == 1
    assert json.loads(h.people.rows['u', deterministic]['faceIds']) == ['new']
    assert (deterministic, 'new') in h.members.rows


def test_face_stamp_failure_retry_does_not_duplicate_person():
    h = Harness()
    h.face('new')
    h.faces.fail_write = RuntimeError('stamp failed')
    with pytest.raises(RuntimeError, match='stamp failed'):
        h.assign('new')
    assert assignment._ACTIVE is None
    deterministic = h.assigner.person_id('u', 'new')
    assert h.assign('new') == ({'new': deterministic}, set())
    assert len(h.faces.queries) == 2 and len(h.people.rows) == 1
    assert assignment._ACTIVE.runtime._old('new')[1] == deterministic


def test_conditional_persistence_uses_original_metadata_etags_and_merge():
    h = Harness()
    h.face('new')
    pid = h.assigner.person_id('u', 'new')
    h.person(pid, name='Preserved')
    h.faces.etags['u', 'new'] = 'W/"face-original"'
    h.people.etags['u', pid] = 'W/"person-original"'

    assert h.assign('new') == ({'new': pid}, set())
    for table, row_key, etag in ((h.faces, 'new', 'W/"face-original"'),
                                 (h.people, pid, 'W/"person-original"')):
        assert len(table.conditional_writes) == 1
        entity, kwargs = table.conditional_writes[0]
        assert (entity['PartitionKey'], entity['RowKey']) == ('u', row_key)
        assert kwargs == dict(mode=UpdateMode.MERGE, etag=etag,
                              match_condition=MatchConditions.IfNotModified)
        assert '_assignment_etag' not in entity
        assert '_assignment_etag' not in table.rows['u', row_key]
    assert h.people.rows['u', pid]['name'] == 'Preserved'
    assert h.faces.rows['u', 'new']['personId'] == pid
    assert h.metadata == [('u', 'photo.jpg')]


@pytest.mark.parametrize('stage', ['person', 'face'])
def test_stale_conditional_write_propagates_without_overwriting_face(stage, monkeypatch):
    h = Harness()
    h.person('alice', ['a'])
    h.face('a', person='alice')
    h.face('first')
    assert h.assign('first') == ({'first': 'alice'}, set())
    active = assignment._ACTIVE
    directory = Path(active.directory.name)
    h.face('new')
    h.faces.etags['u', 'new'] = 'W/"face-original"'
    h.people.etags['u', 'alice'] = 'W/"person-original"'
    table, key = (h.people, ('u', 'alice')) if stage == 'person' else (h.faces, ('u', 'new'))
    error = HttpResponseError('stale ETag', response=SimpleNamespace(
        status_code=412, reason='Precondition Failed', headers={}))
    table.fail_write = error
    original_update = table.update_entity

    def concurrent_curation(entity, **kwargs):
        if stage == 'face':
            table.rows[key]['personId'] = 'curated-owner'
            table.rows[key]['confirmedByUser'] = True
        else:
            table.rows[key]['name'] = 'Curated name'
        return original_update(entity, **kwargs)

    monkeypatch.setattr(table, 'update_entity', concurrent_curation)
    with pytest.raises(HttpResponseError, match='stale ETag') as exc:
        h.assign('new')
    assert exc.value is error and exc.value.status_code == 412
    assert len(table.conditional_writes) == 1
    assert h.faces.rows['u', 'new']['personId'] == ('curated-owner' if stage == 'face' else '')
    assert h.metadata == [('u', 'photo.jpg')]
    assert h.events[-1] == 'release'
    if stage == 'face':
        assert h.faces.rows['u', 'new']['confirmedByUser'] is True
        assert assignment._ACTIVE is None
        assert active.runtime._closed and not directory.exists()
    else:
        assert h.people.rows['u', 'alice']['name'] == 'Curated name'
        assert json.loads(h.people.rows['u', 'alice']['faceIds']) == ['a', 'first']
        assert not h.faces.conditional_writes
        assert ('alice', 'new') not in h.members.rows
        assert assignment._ACTIVE is active and directory.exists()
        assert active.runtime._old('new') is None


def test_local_write_failure_invalidates_and_retry_rebuilds(monkeypatch):
    h = Harness()
    h.face('new')
    original = assignment.LiveFaceIndex.upsert
    directories = []

    def fail(runtime, *args):
        # Partial local success must not survive either.
        original(runtime, *args)
        directories.append(Path(assignment._ACTIVE.directory.name))
        raise RuntimeError('local failure')

    monkeypatch.setattr(assignment.LiveFaceIndex, 'upsert', fail)
    with pytest.raises(RuntimeError, match='local failure'):
        h.assign('new')
    deterministic = h.assigner.person_id('u', 'new')
    assert h.faces.rows['u', 'new']['personId'] == deterministic
    assert assignment._ACTIVE is None and not directories[0].exists()
    monkeypatch.setattr(assignment.LiveFaceIndex, 'upsert', original)
    h.face('next')
    assert h.assign('new', 'next') == ({'new': deterministic, 'next': deterministic}, set())
    assert len(h.faces.queries) == 2 and len(h.people.rows) == 1


def test_legacy_membership_overflow_fails_before_any_authoritative_write():
    h = Harness()
    huge_ids = ['x' * 31000, 'a']
    before = h.person('alice', huge_ids)
    h.face('a', person='alice')
    h.face('new')
    with pytest.raises(ValueError, match='60 KiB'):
        h.assign('new')
    assert h.people.rows['u', 'alice'] == before
    assert h.faces.rows['u', 'new']['personId'] == '' and not h.members.rows
    assert not any(event in ('person', 'face', 'member') for event in h.events)


@pytest.mark.parametrize('value', ['not json', '{}', '[true,0]', '["1",0]',
                                   '[[1],0]', '[NaN,1]', '[Infinity,1]', '[0,0]'])
def test_strict_embedding_validation(value):
    h = Harness()
    h.face('invalid')
    h.faces.rows['u', 'invalid']['embedding'] = value
    assert h.assign('invalid') == ({}, set())
    assert not h.people.rows and not h.faces.queries


def test_missing_inline_embeddings_point_read_only_and_safe_escaping():
    h = Harness()
    user = "u' or PartitionKey ne 'u"
    h.person('alice', ['a'], user=user)
    h.face('a', person='alice', user=user)
    h.face('new', user=user)
    for fid in ('a', 'new'):
        del h.faces.rows[user, fid]['embedding']
        h.embeddings.seed(dict(PartitionKey=user, RowKey=fid, embedding='[1,0]'))
    assert h.assign('new', user=user) == ({'new': 'alice'}, set())
    assert h.faces.queries == ["PartitionKey eq 'u'' or PartitionKey ne ''u'"]
    assert h.embeddings.reads == Counter({(user, 'a'): 1, (user, 'new'): 1})
    assert 'embedding' not in h.faces.rows[user, 'new']


@pytest.mark.parametrize('stage', ['target', 'embedding', 'cold-embedding', 'cold-query',
                                    'candidate-face', 'candidate-person', 'matched-person'])
def test_storage_errors_propagate_per_query(stage, monkeypatch):
    h = Harness()
    h.person('alice', ['a'])
    h.face('a', person='alice')
    h.face('new')
    error = HttpResponseError('transport unavailable')
    if stage in ('candidate-face', 'candidate-person', 'matched-person'):
        h.face('first')
        h.assign('first')
    if stage == 'target':
        h.faces.fail_read['u', 'new'] = error
    elif stage == 'embedding':
        del h.faces.rows['u', 'new']['embedding']
        h.embeddings.fail_read['u', 'new'] = error
    elif stage == 'cold-embedding':
        del h.faces.rows['u', 'a']['embedding']
        h.embeddings.fail_read['u', 'a'] = error
    elif stage == 'cold-query':
        h.faces.fail_query = error
    elif stage == 'candidate-face':
        h.faces.fail_read['u', 'a'] = error
        h.faces.fail_read['u', 'first'] = error
    elif stage == 'candidate-person':
        h.people.fail_read['u', 'alice'] = error
    else:
        original = h.people.get_entity
        reads = []

        def fail_matched(**kwargs):
            if kwargs['row_key'] == 'alice':
                reads.append(True)
                if len(reads) == 2:
                    raise error
            return original(**kwargs)

        monkeypatch.setattr(h.people, 'get_entity', fail_matched)
    with pytest.raises(HttpResponseError, match='transport unavailable'):
        h.assign('new')
    assert h.faces.rows['u', 'new']['personId'] == ''
    if stage.startswith('cold'):
        assert assignment._ACTIVE is None


def test_bounded_lru_person_existence_cache_is_per_query():
    h = Harness(config=AssignmentConfig(person_cache_size=2))
    for pid in ('a', 'b', 'c'):
        h.person(pid, [pid])
        h.face(pid, person=pid)
    validate = h.assigner._validator('u', '5pt', 'v1')
    for pid in ('a', 'a', 'b', 'a', 'c', 'b'):
        assert validate(pid, pid)
    assert h.people.reads == Counter({('u', 'a'): 1, ('u', 'b'): 2, ('u', 'c'): 1})
    h.assigner._validator('u', '5pt', 'v1')('a', 'a')
    assert h.people.reads['u', 'a'] == 2


def test_metadata_error_propagates_and_owned_retry_updates_metadata():
    h = Harness()
    h.face('new')
    h.metadata_error = RuntimeError('metadata failed')
    with pytest.raises(RuntimeError, match='metadata failed'):
        h.assign('new')
    person = h.faces.rows['u', 'new']['personId']
    assert person and assignment._ACTIVE.runtime._old('new')[1] == person
    h.metadata_error = None
    assert h.assign('new') == ({'new': person}, set())
    assert len(h.people.rows) == 1 and len(h.metadata) == 2


@pytest.mark.parametrize('fail_check,expected_writes', [(3, []), (4, ['person']),
                                                       (5, ['person', 'member'])])
def test_lease_checked_immediately_before_every_write(fail_check, expected_writes):
    h = Harness()
    h.face('new')
    h.fail_check = fail_check
    with pytest.raises(RuntimeError, match='lost lease'):
        h.assign('new')
    assert [event for event in h.events if event in ('person', 'member', 'face')] == expected_writes
    assert not h.faces.rows['u', 'new']['personId']


def test_lease_and_callbacks_are_required_and_invalid_guard_fails():
    h = Harness()
    h.assigner.lease = lambda _: nullcontext(None)
    with pytest.raises(TypeError, match='guard'):
        h.assign('new')
    with pytest.raises(TypeError, match='lease'):
        FaissAssigner(face_table=h.faces, person_table=h.people, embedding_table=h.embeddings,
                      metadata_callback=h.update_metadata, clusterable=lambda _: True,
                      eligible=lambda _: True, tier=lambda _: '5pt', version=lambda _: 'v1')


def test_deterministic_row_ownership_mismatch_is_not_overwritten():
    h = Harness()
    h.face('new')
    deterministic = h.assigner.person_id('u', 'new')
    h.people.rows['u', deterministic] = dict(PartitionKey='another-user', RowKey=deterministic,
                                           faceIds='["new"]')
    with pytest.raises(ValueError, match='ownership'):
        h.assign('new')
    assert h.people.rows['u', deterministic]['PartitionKey'] == 'another-user'
    assert not h.faces.rows['u', 'new']['personId']
    assert h.assigner.person_id('ab', 'c') != h.assigner.person_id('a', 'bc')


def test_new_person_fields_and_missing_rep_uses_first_source_exemplar():
    h = Harness(members=False)
    h.face('new', [3, 4])
    mapping, created = h.assign('new')
    pid = mapping['new']
    person = h.people.rows['u', pid]
    assert created == {pid} and person['name'] == ''
    assert datetime.fromisoformat(person['createdAt']).tzinfo is not None
    assert json.loads(person['repEmbedding']) == pytest.approx([.6, .8])
    assert person['repEmbeddingTier'] == '5pt' and person['embeddingVersion'] == 'v1'
    assert not h.members.rows
    person['repEmbedding'] = 'invalid'
    h.face('next', [3, 4])
    assert h.assign('next')[0]['next'] == pid
    assert json.loads(h.people.rows['u', pid]['repEmbedding']) == pytest.approx([.6, .8])


def test_switching_and_explicit_invalidation_close_delete_runtime():
    h = Harness()
    h.face('a')
    h.assign('a')
    old = assignment._ACTIVE
    directory = Path(old.directory.name)
    h.assigner.invalidate('other')
    assert assignment._ACTIVE is old and directory.exists()
    h.face('owned', person='p', user='other')
    h.assign('owned', user='other')  # Even an owned-only switch evicts the old library.
    assert old.runtime._closed and not directory.exists() and assignment._ACTIVE is None
    h.face('b')
    h.assign('b')
    active = assignment._ACTIVE
    assignment.invalidate('u')
    assert active.runtime._closed and not Path(active.directory.name).exists()
    assignment.invalidate()


def test_other_adapter_switches_cache_even_same_user():
    h = Harness()
    h.face('a')
    h.assign('a')
    old = assignment._ACTIVE
    other = Harness()
    other.face('b')
    other.assign('b')
    assert old.runtime._closed and assignment._ACTIVE.owner is other.assigner
    assert len(other.people.rows) == 1


def test_lease_generation_change_discovers_other_replica_assignment():
    h = Harness()
    h.face('a')
    h.assign('a')
    old = assignment._ACTIVE
    h.person('remote', ['remote-face'])
    h.face('remote-face', [0, 1], person='remote')
    h.face('new', [0, 1])
    h.generation = 'another-writer-revision'
    assert h.assign('new') == ({'new': 'remote'}, set())
    assert old.runtime._closed and len(h.faces.queries) == 2


def test_global_lock_serializes_across_adapter_instances():
    h, other = Harness(), Harness()
    h.face('a')
    other.face('b')
    entered, release = threading.Event(), threading.Event()
    original_metadata = h.assigner.metadata_callback

    def hold(*args):
        entered.set()
        assert release.wait(10)
        original_metadata(*args)

    h.assigner.metadata_callback = hold
    errors = []
    attempting = threading.Event()

    def call(harness, fid, signal=False):
        try:
            if signal:
                attempting.set()
            harness.assign(fid)
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=call, args=(h, 'a'))
    second = threading.Thread(target=call, args=(other, 'b', True))
    first.start()
    try:
        assert entered.wait(10)
        second.start()
        assert attempting.wait(10)
        assert not other.events  # Other process-local adapter cannot even enter its lease.
    finally:
        release.set()
        first.join(10)
        if second.ident is not None:
            second.join(10)
    assert not errors and not first.is_alive() and not second.is_alive()
    assert assignment._ACTIVE.owner is other.assigner


@pytest.mark.parametrize('config', [dict(threshold=float('nan')), dict(margin=-1),
                                     dict(person_cache_size=0)])
def test_invalid_config_fails(config):
    with pytest.raises(ValueError):
        replace(AssignmentConfig(), **config)


def test_cold_stream_only_includes_eligible_assigned_library_faces():
    h = Harness()
    h.person('alice', ['a'])
    h.face('a', person='alice')
    h.face('rejected', person='alice', rejected=True)
    h.face('ineligible', person='alice', eligible=False)
    h.face('suspicious', person='alice', clusterable=False)
    h.face('unassigned', [0, 1])
    h.face('foreign', person='alice', user='other')
    h.face('new')
    assert h.assign('new')[0] == {'new': 'alice'}
    stored = assignment._ACTIVE.runtime._db.execute(
        'SELECT face_id FROM faces ORDER BY face_id').fetchall()
    assert stored == [('a',), ('new',)]


def test_missing_embedding_row_is_skipped_but_not_found_is_only_ignored_error():
    h = Harness()
    h.face('missing-vector')
    del h.faces.rows['u', 'missing-vector']['embedding']
    assert h.assign('missing-vector') == ({}, set())
    assert h.embeddings.reads['u', 'missing-vector'] == 1
    h.faces.fail_read['u', 'missing-vector'] = PermissionError('forbidden')
    with pytest.raises(PermissionError, match='forbidden'):
        h.assign('missing-vector')


def test_delta_cap_compacts_locally_without_authoritative_rescan():
    h = Harness(config=AssignmentConfig(delta_limit=1))
    h.face('a')
    pid = h.assign('a')[0]['a']
    h.face('b')
    assert h.assign('b') == ({'b': pid}, set())
    assert h.faces.rows['u', 'b']['personId'] == pid
    assert assignment._ACTIVE is not None
    h.face('c')
    assert h.assign('b', 'c') == ({'b': pid, 'c': pid}, set())
    assert len(h.faces.queries) == 1


def test_face_write_commits_then_errors_retry_keeps_owner_and_rebuild_can_find_it():
    h = Harness()
    h.face('a')
    h.faces.fail_write = HttpResponseError('ambiguous write')
    h.faces.commit_then_fail = True
    with pytest.raises(HttpResponseError, match='ambiguous write'):
        h.assign('a')
    pid = h.faces.rows['u', 'a']['personId']
    assert assignment._ACTIVE is None
    h.face('b')
    assert h.assign('a', 'b') == ({'a': pid, 'b': pid}, set())
    assert len(h.people.rows) == 1


def test_uuid_retry_retains_existing_names_and_first_exemplar_across_tiers():
    h = Harness()
    h.face('new', tier='2pt')
    pid = h.assigner.person_id('u', 'new')
    h.person(pid, ['new'], rep=[0, 1], name='Preserved',
             repEmbeddingTier='5pt', embeddingVersion='old-version')
    assert h.assign('new') == ({'new': pid}, set())
    person = h.people.rows['u', pid]
    assert person['name'] == 'Preserved'
    assert json.loads(person['repEmbedding']) == [0, 1]
    assert person['repEmbeddingTier'] == '5pt' and person['embeddingVersion'] == 'old-version'