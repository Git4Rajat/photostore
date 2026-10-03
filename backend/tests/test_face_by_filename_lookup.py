"""Unit tests for the keyed photofacebyfilename lookup table.

_store_client_face_entities (storage_utils.py) and
_face_ids_awaiting_person_assignment (app.py) used to find "every face row
this photo has" by filtering a whole-partition face scan in memory --
Table Storage has no secondary index on filename, so that scaled with the
size of the whole library, not the one photo being processed (confirmed live
2026-10-01: ~100-200 QueryEntities calls per photo on a 99k-row face
partition). photofacebyfilename is a keyed PartitionKey=userId,
RowKey=filename table, dual-written alongside every photofaces write, that
turns that into a bounded point read.

Only schema-valid complete generations are authoritative. Missing, legacy,
dirty, and expired/incomplete rows require fresh filename queries. Confirmed
zero is a complete row containing [], never a deleted lookup row. Tests use
real Azure exception types and an ETag-aware fake, not FakeTable's permissive
updates or non-Azure not-found exception.
"""
from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from azure.core import MatchConditions
from azure.core.exceptions import ResourceExistsError, ResourceModifiedError, ResourceNotFoundError
from azure.data.tables import UpdateMode

import storage_utils
from fakes import FakeTable, ResourceNotFound


class AzureFaceTable(FakeTable):
    def __init__(self):
        super().__init__()
        self.gets = []
        self.queries = []
        self.writes = []

    def get_entity(self, partition_key, row_key):
        self.gets.append((partition_key, row_key))
        try:
            return super().get_entity(partition_key, row_key)
        except ResourceNotFound as exc:
            raise ResourceNotFoundError(str(exc)) from exc

    def query_entities(self, filter_str, select=None):
        self.queries.append(filter_str)
        return super().query_entities(filter_str, select)

    def upsert_entity(self, entity):
        self.writes.append(dict(entity))
        super().upsert_entity(entity)


class ETaggedEntity(dict):
    def __init__(self, entity, etag):
        super().__init__(entity)
        self.metadata = {'etag': etag}


class FilenameLookupTable(AzureFaceTable):
    """Atomic create and CAS replace, with genuine out-of-band ETag metadata."""
    def __init__(self):
        super().__init__()
        self.lock = threading.RLock()
        self.versions = {}
        self.updates = []

    def upsert_entity(self, entity):
        with self.lock:
            super().upsert_entity(entity)
            key = (entity['PartitionKey'], entity['RowKey'])
            self.versions[key] = self.versions.get(key, 0) + 1

    def get_entity(self, partition_key, row_key):
        with self.lock:
            entity = super().get_entity(partition_key, row_key)
            return ETaggedEntity(entity, str(self.versions[(partition_key, row_key)]))

    def create_entity(self, entity):
        with self.lock:
            if (entity['PartitionKey'], entity['RowKey']) in self.rows:
                raise ResourceExistsError('Already exists')
            self.upsert_entity(entity)

    def update_entity(self, entity, mode=None, *, etag=None, match_condition=None):
        assert mode == UpdateMode.REPLACE
        assert match_condition == MatchConditions.IfNotModified
        assert etag and etag != '*'
        with self.lock:
            key = (entity['PartitionKey'], entity['RowKey'])
            if key not in self.rows:
                raise ResourceNotFoundError('Missing')
            if etag != str(self.versions[key]):
                raise ResourceModifiedError('ETag conflict')
            self.updates.append(dict(entity))
            self.upsert_entity(entity)


@pytest.fixture
def lookup_ctx(monkeypatch):
    face_table = AzureFaceTable()
    lookup_table = FilenameLookupTable()
    monkeypatch.setitem(storage_utils._CTX, 'face_table_client', face_table)
    monkeypatch.setitem(storage_utils._CTX, 'face_by_filename_table_client', lookup_table)
    monkeypatch.setitem(storage_utils._CTX, 'face_summary_lookup', None)
    monkeypatch.setitem(storage_utils._CTX, 'face_summary_cache_writer', None)
    monkeypatch.setitem(storage_utils._CTX, 'person_table_client', None)
    monkeypatch.setitem(storage_utils._CTX, 'face_embeddings_table_client', None)
    monkeypatch.setitem(storage_utils._CTX, 'person_members_table_client', None)
    return face_table, lookup_table


def _face(left, confidence=0.9):
    return {
        'embedding': [0.1, 0.2, 0.3],
        'confidence': confidence,
        'bbox': {'left': left, 'top': 0, 'width': 10, 'height': 10},
        'imageWidth': 200, 'imageHeight': 200,
    }


def test_get_face_ids_for_filename_returns_none_when_table_unconfigured(monkeypatch):
    monkeypatch.setitem(storage_utils._CTX, 'face_by_filename_table_client', None)
    assert storage_utils.get_face_ids_for_filename('u1', 'photo.jpg') is None


def _complete_row(ids=None, **overrides):
    return {
        'PartitionKey': 'u1', 'RowKey': 'photo.jpg',
        'schemaVersion': storage_utils._FACE_FILENAME_SCHEMA_VERSION,
        'generation': 'a' * 32, 'state': 'complete', 'leaseExpiresAt': '',
        'faceIds': json.dumps(ids or []), **overrides,
    }


@pytest.mark.parametrize('overrides', [
    {'schemaVersion': None}, {'schemaVersion': True}, {'schemaVersion': '1'},
    {'schemaVersion': 99}, {'generation': ''}, {'generation': 42},
    {'state': 'dirty'}, {'state': 'writing'}, {'leaseExpiresAt': 'bad'},
    {'faceIds': 'null'}, {'faceIds': '{}'}, {'faceIds': '[1]'},
    {'faceIds': '[""]'}, {'faceIds': '["f", "f"]'}, {'faceIds': 'garbage'},
])
def test_strict_schema_rejects_untrusted_ids(lookup_ctx, overrides):
    _, lookup = lookup_ctx
    lookup.upsert_entity(_complete_row(['hidden'], **overrides))
    assert storage_utils.get_face_ids_for_filename('u1', 'photo.jpg') is None


def test_legacy_row_is_unknown(lookup_ctx):
    _, lookup = lookup_ctx
    lookup.upsert_entity({'PartitionKey': 'u1', 'RowKey': 'photo.jpg', 'faceIds': '["f"]'})
    assert storage_utils.get_face_ids_for_filename('u1', 'photo.jpg') is None


def test_set_publishes_schema_generation_complete_zero(lookup_ctx):
    _, lookup = lookup_ctx
    storage_utils._set_face_ids_for_filename('u1', 'photo.jpg', [])
    assert storage_utils.get_face_ids_for_filename('u1', 'photo.jpg') == []
    row = lookup.rows[('u1', 'photo.jpg')]
    assert row['generation'] and row['leaseExpiresAt'] == ''
    assert row['schemaVersion'] == storage_utils._FACE_FILENAME_SCHEMA_VERSION


def _raise_outage(*args, **kwargs):
    raise OSError('storage outage')


def _storage_metrics(caplog):
    records = [record for record in caplog.records
               if record.msg.startswith('face storage timings')]
    assert len(records) == 1
    return json.loads(records[0].args[2])


@pytest.mark.parametrize('path', ['indexed', 'indexed_zero', 'query'])
def test_storage_phase_timings_cover_full_lazy_enumeration(lookup_ctx, monkeypatch, caplog, path):
    faces, _ = lookup_ctx
    clock = [0.0]
    monkeypatch.setattr(storage_utils, 'time', SimpleNamespace(monotonic=lambda: clock[0]))

    def advance(seconds):
        clock[0] += seconds

    existing = {'PartitionKey': 'u1', 'RowKey': 'rejected', 'filename': 'photo.jpg',
                'bbox': json.dumps(_face(0)['bbox']), 'rejected': True}

    def begin(*args):
        advance(.01)
        return 'generation', None if path == 'query' else ([] if path == 'indexed_zero' else ['rejected'])

    def get(**kwargs):
        advance(.03)
        return existing

    def query(*args):
        advance(.02)  # Creating the iterable is not its enumeration cost.

        def pages():
            advance(.03)
            yield existing
            advance(.04)
            advance(.05)  # Last page/exhaustion must be included too.

        return pages()

    monkeypatch.setattr(storage_utils, '_begin_face_filename_write', begin)
    monkeypatch.setattr(faces, 'get_entity', get)
    monkeypatch.setattr(faces, 'query_entities', query)
    monkeypatch.setattr(storage_utils, '_renew_face_filename_write', lambda *a: advance(.005))
    monkeypatch.setattr(faces, 'upsert_entity', lambda entity: advance(.04))
    published = []

    def finish(*args):
        advance(.06)
        published.append(args[-1])

    monkeypatch.setattr(storage_utils, '_finish_face_filename_write', finish)
    with caplog.at_level('INFO', logger='storage_utils'):
        result = storage_utils._store_client_face_entities('u1', 'photo.jpg', [_face(100)])
    metrics = _storage_metrics(caplog)
    retrieval_ms = {'indexed': 30, 'indexed_zero': 0, 'query': 140}[path]
    assert metrics['path'] == ('filename_query' if path == 'query' else 'indexed_point_reads')
    assert metrics['acquire_ms'] == 10
    assert metrics['existing_retrieval_ms'] == retrieval_ms
    assert metrics['query_enumeration_ms'] == (120 if path == 'query' else 0)
    assert metrics['mutation_ms'] == 50  # Both renewals remain in the mutation phase.
    assert metrics['finish_publication_ms'] == 60
    assert metrics['total_ms'] == 120 + retrieval_ms
    assert metrics['existing_retrieval_complete'] is True
    assert metrics['existing_row_count'] == (0 if path == 'indexed_zero' else 1)
    assert metrics['input_face_count'] == metrics['candidate_face_count'] == metrics['stored_face_count'] == 1
    assert metrics['deleted_face_count'] == 0
    assert metrics['outcome'] == 'done' and 'failure_phase' not in metrics
    assert set(published[0]) == set(result) | (set() if path == 'indexed_zero' else {'rejected'})


@pytest.mark.parametrize('failure', ['acquire', 'indexed', 'query_create', 'query_page', 'mutation', 'publication'])
def test_storage_failure_diagnostics_and_dirty_release(lookup_ctx, monkeypatch, caplog, failure):
    faces, lookup = lookup_ctx
    clock = [0.0]
    monkeypatch.setattr(storage_utils, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    original_release = storage_utils._release_failed_face_filename_write

    def timed_release(*args):
        clock[0] += .1
        return original_release(*args)

    monkeypatch.setattr(storage_utils, '_release_failed_face_filename_write', timed_release)

    def fail(*args, **kwargs):
        clock[0] += .25
        raise OSError('timed outage')

    if failure == 'acquire':
        monkeypatch.setattr(lookup, 'create_entity', fail)
    elif failure == 'indexed':
        lookup.upsert_entity(_complete_row(['f']))
        monkeypatch.setattr(faces, 'get_entity', fail)
    elif failure == 'query_create':
        monkeypatch.setattr(faces, 'query_entities', fail)
    elif failure == 'query_page':
        def pages(*args):
            yield {'PartitionKey': 'u1', 'RowKey': 'partial', 'filename': 'photo.jpg'}
            fail()
        monkeypatch.setattr(faces, 'query_entities', pages)
    elif failure == 'mutation':
        original_upsert = faces.upsert_entity

        def fail_second(entity):
            if faces.writes:
                fail()
            original_upsert(entity)

        monkeypatch.setattr(faces, 'upsert_entity', fail_second)
    else:
        original_update = lookup.update_entity

        def fail_publication(entity, *args, **kwargs):
            if entity['state'] == 'complete':
                fail()
            return original_update(entity, *args, **kwargs)

        monkeypatch.setattr(lookup, 'update_entity', fail_publication)

    with caplog.at_level('INFO', logger='storage_utils'), pytest.raises(OSError, match='timed outage'):
        storage_utils._store_client_face_entities('u1', 'photo.jpg', [_face(0), _face(100)])
    metrics = _storage_metrics(caplog)
    phase = {'acquire': 'acquire', 'indexed': 'existing_retrieval', 'query_create': 'existing_retrieval',
             'query_page': 'existing_retrieval', 'mutation': 'mutation', 'publication': 'finish_publication'}[failure]
    assert metrics['failure_phase'] == phase
    assert metrics[phase + '_ms'] == 250
    assert metrics['failure_cleanup_ms'] == (0 if failure == 'acquire' else 100)
    assert metrics['total_ms'] == (250 if failure == 'acquire' else 350)
    assert metrics['query_enumeration_ms'] == (250 if failure == 'query_page' else 0)
    assert metrics['outcome'] == 'error'
    assert metrics['existing_retrieval_complete'] == (failure in {'mutation', 'publication'})
    assert metrics['existing_row_count'] == (None if failure == 'acquire' else (1 if failure == 'query_page' else 0))
    assert metrics['stored_face_count'] == {'mutation': 1, 'publication': 2}.get(failure, 0)
    if failure != 'acquire':
        assert lookup.rows[('u1', 'photo.jpg')]['state'] == 'dirty'
    if phase in {'acquire', 'existing_retrieval'}:
        assert faces.writes == []


@pytest.mark.parametrize('prior', ['missing', 'complete'])
def test_lookup_invalidation_outage_aborts_before_any_mutation(lookup_ctx, monkeypatch, prior):
    faces, lookup = lookup_ctx
    embeddings = AzureFaceTable()
    monkeypatch.setitem(storage_utils._CTX, 'face_embeddings_table_client', embeddings)
    if prior == 'complete':
        lookup.upsert_entity(_complete_row())
        monkeypatch.setattr(lookup, 'update_entity', _raise_outage)
    else:
        monkeypatch.setattr(lookup, 'create_entity', _raise_outage)
    with pytest.raises(OSError, match='outage'):
        storage_utils._store_client_face_entities('u1', 'photo.jpg', [_face(0)])
    assert faces.writes == embeddings.writes == []
    assert faces.queries == []


def test_exact_outage_hidden_rejection_reproduce(lookup_ctx, monkeypatch):
    """An unindexed rejected bbox may not be resurrected after completion fails."""
    faces, lookup = lookup_ctx
    first = storage_utils._store_client_face_entities('u1', 'photo.jpg', [_face(0)])[0]
    # A legacy/dirty lookup must not hide the curated row from recovery.
    hidden = {'PartitionKey': 'u1', 'RowKey': 'legacy-rejected', 'filename': 'photo.jpg',
              'bbox': json.dumps(_face(100)['bbox']), 'rejected': True, 'reviewStatus': 'rejected'}
    faces.upsert_entity(hidden)
    lookup.upsert_entity(_complete_row([first], state='dirty'))
    stale_summary = {first: dict(faces.rows[('u1', first)])}
    monkeypatch.setitem(storage_utils._CTX, 'face_summary_lookup', lambda user: stale_summary)
    original_update = lookup.update_entity

    def completion_outage(entity, *args, **kwargs):
        if entity['state'] == 'complete':
            raise OSError('completion outage')
        return original_update(entity, *args, **kwargs)

    monkeypatch.setattr(lookup, 'update_entity', completion_outage)
    with pytest.raises(OSError, match='completion outage'):
        storage_utils._store_client_face_entities('u1', 'photo.jpg', [_face(0), _face(100)])
    assert storage_utils.get_face_ids_for_filename('u1', 'photo.jpg') is None
    monkeypatch.setattr(lookup, 'update_entity', original_update)
    returned = storage_utils._store_client_face_entities('u1', 'photo.jpg', [_face(0), _face(100)])
    assert returned == [first]
    assert faces.rows[('u1', 'legacy-rejected')] == hidden
    assert len(faces.rows) == 2
    assert set(storage_utils.get_face_ids_for_filename('u1', 'photo.jpg')) == {first, 'legacy-rejected'}


@pytest.mark.parametrize('state', ['dirty', 'legacy', 'expired'])
def test_unknown_generation_queries_fresh_not_cache(lookup_ctx, monkeypatch, state):
    faces, lookup = lookup_ctx
    fid = storage_utils._store_client_face_entities('u1', 'photo.jpg', [_face(0)])[0]
    faces.rows[('u1', fid)]['rejected'] = True
    row = _complete_row([], state='dirty')
    if state == 'legacy':
        row.pop('schemaVersion')
    elif state == 'expired':
        row.update(state='writing', leaseExpiresAt=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
    lookup.upsert_entity(row)
    monkeypatch.setitem(storage_utils._CTX, 'face_summary_lookup', lambda _: pytest.fail('stale summary read'))
    monkeypatch.setitem(storage_utils._CTX, 'face_summary_cache_writer', lambda *_: pytest.fail('partial cache publish'))
    faces.queries.clear()
    faces.writes.clear()
    assert storage_utils._store_client_face_entities('u1', 'photo.jpg', [_face(1)]) == []
    assert len(faces.queries) == 1 and "filename eq 'photo.jpg'" in faces.queries[0]
    assert faces.writes == []
    assert storage_utils.get_face_ids_for_filename('u1', 'photo.jpg') == [fid]


def test_keyed_reads_are_fresh_bounded_without_whole_summary(lookup_ctx, monkeypatch):
    faces, lookup = lookup_ctx
    fid = storage_utils._store_client_face_entities('u1', 'photo.jpg', [_face(0)])[0]
    faces.rows[('u1', fid)]['rejected'] = True
    # Missing keyed rows are genuine not-found, not transport failures.
    storage_utils._set_face_ids_for_filename('u1', 'photo.jpg', [fid, 'missing'])
    monkeypatch.setitem(storage_utils._CTX, 'face_summary_lookup', lambda _: pytest.fail('whole summary scan'))
    monkeypatch.setitem(storage_utils._CTX, 'face_summary_cache_writer', lambda *_: pytest.fail('partial cache publish'))
    faces.gets.clear()
    faces.queries.clear()
    assert storage_utils._store_client_face_entities('u1', 'photo.jpg', [_face(1)]) == []
    assert set(faces.gets) == {('u1', fid), ('u1', 'missing')}
    assert faces.queries == []


@pytest.mark.parametrize('source', ['lookup_get', 'face_get', 'query'])
def test_transport_read_failure_aborts_without_mutating(lookup_ctx, monkeypatch, source):
    faces, lookup = lookup_ctx
    if source == 'face_get':
        lookup.upsert_entity(_complete_row(['f']))
        monkeypatch.setattr(faces, 'get_entity', _raise_outage)
    elif source == 'lookup_get':
        monkeypatch.setattr(lookup, 'get_entity', _raise_outage)
    else:
        monkeypatch.setattr(faces, 'query_entities', _raise_outage)
    with pytest.raises(OSError, match='outage'):
        storage_utils._store_client_face_entities('u1', 'photo.jpg', [_face(0)])
    assert faces.writes == []
    if source != 'lookup_get':
        assert storage_utils.get_face_ids_for_filename('u1', 'photo.jpg') is None


def test_lookup_reader_distinguishes_transport_from_missing(lookup_ctx, monkeypatch):
    _, lookup = lookup_ctx
    monkeypatch.setattr(lookup, 'get_entity', _raise_outage)
    with pytest.raises(OSError):
        storage_utils.get_face_ids_for_filename('u1', 'photo.jpg')


@pytest.mark.parametrize('target', ['faces', 'embeddings'])
def test_failed_mutation_propagates_and_leaves_dirty(lookup_ctx, monkeypatch, target):
    faces, lookup = lookup_ctx
    embeddings = AzureFaceTable()
    monkeypatch.setitem(storage_utils._CTX, 'face_embeddings_table_client', embeddings)
    monkeypatch.setattr(faces if target == 'faces' else embeddings, 'upsert_entity', _raise_outage)
    with pytest.raises(OSError, match='outage'):
        storage_utils._store_client_face_entities('u1', 'photo.jpg', [_face(0)])
    assert lookup.rows[('u1', 'photo.jpg')]['state'] == 'dirty'
    assert storage_utils.get_face_ids_for_filename('u1', 'photo.jpg') is None


def test_completion_and_release_outage_remains_incomplete(lookup_ctx, monkeypatch):
    faces, lookup = lookup_ctx
    original_update = lookup.update_entity

    def fail_finish(entity, *args, **kwargs):
        if entity['state'] != 'writing':
            raise OSError('finish outage')
        return original_update(entity, *args, **kwargs)

    monkeypatch.setattr(lookup, 'update_entity', fail_finish)
    with pytest.raises(OSError, match='finish outage'):
        storage_utils._store_client_face_entities('u1', 'photo.jpg', [_face(0)])
    assert len(faces.rows) == 1
    assert lookup.rows[('u1', 'photo.jpg')]['state'] == 'writing'
    assert storage_utils.get_face_ids_for_filename('u1', 'photo.jpg') is None
    with pytest.raises(storage_utils.FaceFilenameLookupRetryableError, match='active'):
        storage_utils._store_client_face_entities('u1', 'photo.jpg', [_face(100)])


@pytest.mark.parametrize('preexisting', [False, True])
def test_concurrent_begin_has_one_winner(lookup_ctx, monkeypatch, preexisting):
    faces, lookup = lookup_ctx
    if preexisting:
        lookup.upsert_entity(_complete_row())
    barrier = threading.Barrier(2)
    original_get = lookup.get_entity
    local = threading.local()

    def simultaneous_get(*args, **kwargs):
        try:
            result = original_get(*args, **kwargs)
        except ResourceNotFoundError:
            result = None
        if not getattr(local, 'first_read', False):
            local.first_read = True
            barrier.wait(timeout=5)
        if result is None:
            raise ResourceNotFoundError('Missing')
        return result

    monkeypatch.setattr(lookup, 'get_entity', simultaneous_get)

    def begin():
        try:
            return storage_utils._begin_face_filename_write('u1', 'photo.jpg')
        except storage_utils.FaceFilenameLookupRetryableError:
            return 'conflict'

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(begin), pool.submit(begin)]
        results = [f.result(timeout=10) for f in futures]
    assert results.count('conflict') == 1
    assert faces.writes == []
    assert lookup.rows[('u1', 'photo.jpg')]['state'] == 'writing'


def test_acquisition_cas_retries_are_bounded(lookup_ctx, monkeypatch):
    _, lookup = lookup_ctx
    lookup.upsert_entity(_complete_row())
    calls = []

    def always_conflict(*args, **kwargs):
        calls.append(kwargs)
        raise ResourceModifiedError('conflict')

    monkeypatch.setattr(lookup, 'update_entity', always_conflict)
    with pytest.raises(storage_utils.FaceFilenameLookupRetryableError):
        storage_utils._begin_face_filename_write('u1', 'photo.jpg')
    assert len(calls) == storage_utils._FACE_FILENAME_CAS_ATTEMPTS


def test_old_writer_cannot_complete_or_release_new_generation(lookup_ctx):
    _, lookup = lookup_ctx
    old_generation, _ = storage_utils._begin_face_filename_write('u1', 'photo.jpg')
    row = dict(lookup.rows[('u1', 'photo.jpg')])
    row['leaseExpiresAt'] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    lookup.upsert_entity(row)
    new_generation, ids = storage_utils._begin_face_filename_write('u1', 'photo.jpg')
    assert ids is None and new_generation != old_generation
    for face_ids in (['old'], None):
        with pytest.raises(storage_utils.FaceFilenameLookupRetryableError, match='generation lost'):
            storage_utils._finish_face_filename_write('u1', 'photo.jpg', old_generation, face_ids)
    assert lookup.rows[('u1', 'photo.jpg')]['generation'] == new_generation
    storage_utils._finish_face_filename_write('u1', 'photo.jpg', new_generation, ['new'])
    assert storage_utils.get_face_ids_for_filename('u1', 'photo.jpg') == ['new']


def test_set_completion_failure_propagates(lookup_ctx, monkeypatch):
    _, lookup = lookup_ctx
    original_update = lookup.update_entity

    def fail_complete(entity, *args, **kwargs):
        if entity['state'] == 'complete':
            raise OSError('complete outage')
        return original_update(entity, *args, **kwargs)

    monkeypatch.setattr(lookup, 'update_entity', fail_complete)
    with pytest.raises(OSError, match='complete outage'):
        storage_utils._set_face_ids_for_filename('u1', 'photo.jpg', [])
    assert storage_utils.get_face_ids_for_filename('u1', 'photo.jpg') is None


def test_configure_storage_accepts_optional_member_client(monkeypatch):
    monkeypatch.setattr(storage_utils, '_CTX', {})
    members = AzureFaceTable()
    storage_utils.configure_storage(metadata_table_client=None, person_members_table_client=members)
    assert storage_utils._CTX['person_members_table_client'] is members
    storage_utils.configure_storage(metadata_table_client=None)
    assert storage_utils._CTX['person_members_table_client'] is None


@pytest.mark.parametrize('missing_shadows', [False, True])
def test_reconcile_deletes_embedding_and_member_shadows(lookup_ctx, monkeypatch, missing_shadows):
    faces, lookup = lookup_ctx
    members, embeddings, people = AzureFaceTable(), AzureFaceTable(), AzureFaceTable()
    monkeypatch.setitem(storage_utils._CTX, 'person_members_table_client', members)
    monkeypatch.setitem(storage_utils._CTX, 'face_embeddings_table_client', embeddings)
    monkeypatch.setitem(storage_utils._CTX, 'person_table_client', people)
    fid = storage_utils._store_client_face_entities('u1', 'photo.jpg', [_face(0)])[0]
    faces.rows[('u1', fid)]['personId'] = 'p1'
    people.upsert_entity({'PartitionKey': 'u1', 'RowKey': 'p1', 'faceIds': json.dumps([fid])})
    members.upsert_entity({'PartitionKey': 'p1', 'RowKey': fid})
    if missing_shadows:
        members.rows.clear()
        embeddings.rows.clear()
        monkeypatch.setattr(members, 'delete_entity', lambda **_: (_ for _ in ()).throw(ResourceNotFoundError('missing')))
        monkeypatch.setattr(embeddings, 'delete_entity', lambda **_: (_ for _ in ()).throw(ResourceNotFoundError('missing')))
    assert storage_utils._store_client_face_entities('u1', 'photo.jpg', [], force_reconcile=True) == []
    assert faces.rows == members.rows == embeddings.rows == people.rows == {}
    assert storage_utils.get_face_ids_for_filename('u1', 'photo.jpg') == []


@pytest.mark.parametrize('curation', ['rejected', 'named', 'confirmed', 'propagated'])
def test_reconcile_preserves_curated_identity_and_shadows(lookup_ctx, monkeypatch, curation):
    faces, lookup = lookup_ctx
    members, embeddings, people = AzureFaceTable(), AzureFaceTable(), AzureFaceTable()
    for key, table in [('person_members_table_client', members), ('face_embeddings_table_client', embeddings),
                       ('person_table_client', people)]:
        monkeypatch.setitem(storage_utils._CTX, key, table)
    fid = storage_utils._store_client_face_entities('u1', 'photo.jpg', [_face(0)])[0]
    faces.rows[('u1', fid)]['personId'] = 'p1'
    if curation == 'rejected':
        faces.rows[('u1', fid)]['reviewStatus'] = 'rejected'
    elif curation == 'confirmed':
        faces.rows[('u1', fid)]['confirmedByUser'] = True
    elif curation == 'propagated':
        faces.rows[('u1', fid)]['assignedByPropagation'] = True
    people.upsert_entity({'PartitionKey': 'u1', 'RowKey': 'p1', 'faceIds': json.dumps([fid]),
                         'name': 'Curated' if curation == 'named' else ''})
    members.upsert_entity({'PartitionKey': 'p1', 'RowKey': fid})
    before = [dict(table.rows) for table in (faces, members, embeddings, people)]
    storage_utils._store_client_face_entities('u1', 'photo.jpg', [], force_reconcile=True)
    assert [table.rows for table in (faces, members, embeddings, people)] == before
    assert storage_utils.get_face_ids_for_filename('u1', 'photo.jpg') == [fid]


@pytest.mark.parametrize('target', ['member', 'embedding', 'face', 'person_get', 'person_write'])
def test_reconcile_transport_failure_is_not_success(lookup_ctx, monkeypatch, target):
    faces, lookup = lookup_ctx
    members, embeddings, people = AzureFaceTable(), AzureFaceTable(), AzureFaceTable()
    for key, table in [('person_members_table_client', members), ('face_embeddings_table_client', embeddings),
                       ('person_table_client', people)]:
        monkeypatch.setitem(storage_utils._CTX, key, table)
    fid = storage_utils._store_client_face_entities('u1', 'photo.jpg', [_face(0)])[0]
    faces.rows[('u1', fid)]['personId'] = 'p1'
    people.upsert_entity({'PartitionKey': 'u1', 'RowKey': 'p1', 'faceIds': json.dumps([fid])})
    table, method = {
        'member': (members, 'delete_entity'), 'embedding': (embeddings, 'delete_entity'),
        'face': (faces, 'delete_entity'), 'person_get': (people, 'get_entity'),
        'person_write': (people, 'delete_entity'),
    }[target]
    monkeypatch.setattr(table, method, _raise_outage)
    with pytest.raises(OSError, match='outage'):
        storage_utils._store_client_face_entities('u1', 'photo.jpg', [], force_reconcile=True)
    assert storage_utils.get_face_ids_for_filename('u1', 'photo.jpg') is None


def test_reviewer_outage_then_rejected_unpublished_face_never_resurrects(lookup_ctx, monkeypatch):
    faces, lookup = lookup_ctx
    first = storage_utils._store_client_face_entities('u1', 'photo.jpg', [_face(0)])[0]
    original_update = lookup.update_entity

    def fail_complete(entity, *args, **kwargs):
        if entity['state'] == 'complete':
            raise OSError('lookup outage after successful face write')
        return original_update(entity, *args, **kwargs)

    monkeypatch.setattr(lookup, 'update_entity', fail_complete)
    with pytest.raises(OSError):
        storage_utils._store_client_face_entities('u1', 'photo.jpg', [_face(0), _face(100)])
    hidden = next(fid for (user, fid) in faces.rows if fid != first)
    faces.rows[('u1', hidden)].update(rejected=True, reviewStatus='rejected')
    # Simulate the obsolete cached summary still hiding the new rejected row.
    monkeypatch.setitem(storage_utils._CTX, 'face_summary_lookup', lambda _: {first: faces.rows[('u1', first)]})
    monkeypatch.setattr(lookup, 'update_entity', original_update)
    faces.queries.clear()
    assert storage_utils._store_client_face_entities('u1', 'photo.jpg', [_face(0), _face(100)]) == [first]
    assert len(faces.queries) == 1
    assert faces.rows[('u1', hidden)]['rejected'] is True
    assert set(storage_utils.get_face_ids_for_filename('u1', 'photo.jpg')) == {first, hidden}


def test_partial_face_failure_retries_from_authoritative_query(lookup_ctx, monkeypatch):
    faces, lookup = lookup_ctx
    original_upsert = faces.upsert_entity
    calls = 0

    def fail_second(entity):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError('second face outage')
        assert storage_utils.get_face_ids_for_filename('u1', 'photo.jpg') is None
        original_upsert(entity)

    monkeypatch.setattr(faces, 'upsert_entity', fail_second)
    with pytest.raises(OSError, match='second face outage'):
        storage_utils._store_client_face_entities('u1', 'photo.jpg', [_face(0), _face(100)])
    assert len(faces.rows) == 1
    assert lookup.rows[('u1', 'photo.jpg')]['state'] == 'dirty'
    monkeypatch.setattr(faces, 'upsert_entity', original_upsert)
    faces.queries.clear()
    assert len(storage_utils._store_client_face_entities('u1', 'photo.jpg', [_face(0), _face(100)])) == 2
    assert len(faces.queries) == 1 and len(faces.rows) == 2


def test_missing_etag_fails_closed(lookup_ctx, monkeypatch):
    faces, lookup = lookup_ctx
    lookup.upsert_entity(_complete_row())
    original_get = lookup.get_entity
    monkeypatch.setattr(lookup, 'get_entity', lambda **kwargs: dict(original_get(**kwargs)))
    with pytest.raises(storage_utils.FaceFilenameLookupRetryableError, match='ETag'):
        storage_utils._store_client_face_entities('u1', 'photo.jpg', [_face(0)])
    assert faces.writes == []


def test_completion_etag_conflict_cannot_overwrite_new_writer(lookup_ctx, monkeypatch):
    _, lookup = lookup_ctx
    generation, _ = storage_utils._begin_face_filename_write('u1', 'photo.jpg')
    original_update = lookup.update_entity
    new_generation = 'b' * 32

    def replace_before_cas(entity, *args, **kwargs):
        successor = dict(lookup.rows[('u1', 'photo.jpg')])
        successor.update(generation=new_generation, state='writing')
        lookup.upsert_entity(successor)
        return original_update(entity, *args, **kwargs)

    monkeypatch.setattr(lookup, 'update_entity', replace_before_cas)
    with pytest.raises(ResourceModifiedError):
        storage_utils._finish_face_filename_write('u1', 'photo.jpg', generation, ['old'])
    storage_utils._release_failed_face_filename_write('u1', 'photo.jpg', generation)
    assert lookup.rows[('u1', 'photo.jpg')]['generation'] == new_generation
    assert lookup.rows[('u1', 'photo.jpg')]['state'] == 'writing'


def test_expired_writer_cannot_renew_or_complete(lookup_ctx):
    _, lookup = lookup_ctx
    generation, _ = storage_utils._begin_face_filename_write('u1', 'photo.jpg')
    row = dict(lookup.rows[('u1', 'photo.jpg')])
    row['leaseExpiresAt'] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    lookup.upsert_entity(row)
    with pytest.raises(storage_utils.FaceFilenameLookupRetryableError):
        storage_utils._renew_face_filename_write('u1', 'photo.jpg', generation)
    with pytest.raises(storage_utils.FaceFilenameLookupRetryableError):
        storage_utils._finish_face_filename_write('u1', 'photo.jpg', generation, [])
    assert storage_utils.get_face_ids_for_filename('u1', 'photo.jpg') is None


def test_completion_committed_but_response_failed_is_conditionally_dirtied(lookup_ctx, monkeypatch):
    _, lookup = lookup_ctx
    original_update = lookup.update_entity

    def lost_response(entity, *args, **kwargs):
        original_update(entity, *args, **kwargs)
        if entity['state'] == 'complete':
            raise OSError('completion response lost')

    monkeypatch.setattr(lookup, 'update_entity', lost_response)
    with pytest.raises(OSError, match='response lost'):
        storage_utils._store_client_face_entities('u1', 'photo.jpg', [_face(0)])
    assert lookup.rows[('u1', 'photo.jpg')]['state'] == 'dirty'
    assert storage_utils.get_face_ids_for_filename('u1', 'photo.jpg') is None


def test_failed_person_cleanup_keeps_source_for_retry(lookup_ctx, monkeypatch):
    faces, lookup = lookup_ctx
    people, members, embeddings = AzureFaceTable(), AzureFaceTable(), AzureFaceTable()
    for key, table in [('person_table_client', people), ('person_members_table_client', members),
                       ('face_embeddings_table_client', embeddings)]:
        monkeypatch.setitem(storage_utils._CTX, key, table)
    fid = storage_utils._store_client_face_entities('u1', 'photo.jpg', [_face(0)])[0]
    faces.rows[('u1', fid)]['personId'] = 'p1'
    people.upsert_entity({'PartitionKey': 'u1', 'RowKey': 'p1', 'faceIds': json.dumps([fid])})
    members.upsert_entity({'PartitionKey': 'p1', 'RowKey': fid})
    original_delete = people.delete_entity
    monkeypatch.setattr(people, 'delete_entity', _raise_outage)
    with pytest.raises(OSError):
        storage_utils._store_client_face_entities('u1', 'photo.jpg', [], force_reconcile=True)
    assert ('u1', fid) in faces.rows
    monkeypatch.setattr(people, 'delete_entity', original_delete)
    storage_utils._store_client_face_entities('u1', 'photo.jpg', [], force_reconcile=True)
    assert faces.rows == people.rows == members.rows == embeddings.rows == {}
    assert storage_utils.get_face_ids_for_filename('u1', 'photo.jpg') == []


def test_get_face_ids_for_filename_returns_none_for_missing_row_not_empty_list(lookup_ctx):
    """A missing row must fall back (None), not be mistaken for a
    photo confirmed to have zero faces -- see module docstring."""
    _face_table, lookup_table = lookup_ctx
    assert storage_utils.get_face_ids_for_filename('u1', 'never-touched.jpg') is None


def test_store_writes_lookup_row_for_new_filename(lookup_ctx):
    face_table, lookup_table = lookup_ctx
    stored_ids = storage_utils._store_client_face_entities(
        'u1', 'photo.jpg', [_face(0), _face(100)],
    )
    assert len(stored_ids) == 2

    looked_up = storage_utils.get_face_ids_for_filename('u1', 'photo.jpg')
    assert looked_up is not None
    assert sorted(looked_up) == sorted(stored_ids)


def test_store_reuses_lookup_on_second_call_without_duplicating(lookup_ctx):
    face_table, lookup_table = lookup_ctx
    first_ids = storage_utils._store_client_face_entities('u1', 'photo.jpg', [_face(0)])
    # Same bbox again -- IoU-matches the existing row, reuses its face_id.
    second_ids = storage_utils._store_client_face_entities('u1', 'photo.jpg', [_face(0)])
    assert first_ids == second_ids

    looked_up = storage_utils.get_face_ids_for_filename('u1', 'photo.jpg')
    assert looked_up == first_ids


def test_force_reconcile_removes_stale_id_from_lookup(lookup_ctx):
    face_table, lookup_table = lookup_ctx
    initial_ids = storage_utils._store_client_face_entities('u1', 'photo.jpg', [_face(0), _face(100)])
    assert len(initial_ids) == 2

    # A forced re-run that only re-detects one of the two faces -- the other
    # has genuinely vanished from this photo.
    remaining_ids = storage_utils._store_client_face_entities(
        'u1', 'photo.jpg', [_face(0)], force_reconcile=True,
    )
    assert remaining_ids == [initial_ids[0]]

    looked_up = storage_utils.get_face_ids_for_filename('u1', 'photo.jpg')
    assert looked_up == [initial_ids[0]]


def test_lookup_row_complete_zero_when_all_faces_removed(lookup_ctx):
    face_table, lookup_table = lookup_ctx
    storage_utils._store_client_face_entities('u1', 'photo.jpg', [_face(0)])
    assert storage_utils.get_face_ids_for_filename('u1', 'photo.jpg') is not None

    storage_utils._store_client_face_entities('u1', 'photo.jpg', [], force_reconcile=True)
    assert lookup_table.rows[('u1', 'photo.jpg')]['state'] == 'complete'
    assert storage_utils.get_face_ids_for_filename('u1', 'photo.jpg') == []


def test_falls_back_to_scan_when_lookup_table_not_configured(monkeypatch):
    """An unconfigured lookup still uses authoritative filename queries."""
    face_table = AzureFaceTable()
    monkeypatch.setitem(storage_utils._CTX, 'face_table_client', face_table)
    monkeypatch.setitem(storage_utils._CTX, 'face_by_filename_table_client', None)
    monkeypatch.setitem(storage_utils._CTX, 'face_summary_lookup', None)
    monkeypatch.setitem(storage_utils._CTX, 'face_summary_cache_writer', None)
    monkeypatch.setitem(storage_utils._CTX, 'person_table_client', None)
    monkeypatch.setitem(storage_utils._CTX, 'face_embeddings_table_client', None)
    monkeypatch.setitem(storage_utils._CTX, 'person_members_table_client', None)

    stored_ids = storage_utils._store_client_face_entities('u1', 'photo.jpg', [_face(0)])
    assert len(stored_ids) == 1
    assert storage_utils.get_face_ids_for_filename('u1', 'photo.jpg') is None
