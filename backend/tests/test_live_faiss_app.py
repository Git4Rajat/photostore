"""App dispatch, policy and persistence tests using the real live FAISS adapter.

Only the storage transport and library lease are faked. In particular, neither
the assignment algorithm nor the app's metadata projection is stubbed out.
"""
from collections import Counter
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

from azure.core.exceptions import ResourceNotFoundError
import pytest

import app


class _Table:
    """Point reads have SDK missing-row semantics; unexpected scans fail closed."""

    def __init__(self, name, events, *, allow_scan=False):
        self.name = name
        self.events = events
        self.allow_scan = allow_scan
        self.rows = {}
        self.reads = Counter()
        self.queries = []
        self.writes = []

    def seed(self, entity):
        self.rows[entity['PartitionKey'], entity['RowKey']] = dict(entity)

    def get_entity(self, partition_key, row_key):
        key = partition_key, row_key
        self.reads[key] += 1
        if key not in self.rows:
            raise ResourceNotFoundError('missing test entity')
        return dict(self.rows[key])

    def query_entities(self, query_filter, **kwargs):
        self.queries.append(query_filter)
        assert self.allow_scan, f'unexpected {self.name} scan: {query_filter}'
        assert query_filter == "PartitionKey eq 'lib-live'"
        for (partition, _), row in sorted(self.rows.items()):
            if partition == 'lib-live':
                yield dict(row)

    def upsert_entity(self, entity, **kwargs):
        if self.name in ('face', 'person', 'member'):
            assert self.events[-1] == 'check', 'write must follow a lease check'
        self.events.append(self.name)
        self.writes.append(dict(entity))
        self.seed(entity)

    def update_entity(self, entity, **kwargs):
        self.upsert_entity(entity, **kwargs)


def _forbid_legacy_helpers(monkeypatch):
    mocks = {}
    for name in (
        '_assign_faces_to_people_incrementally_legacy',
        '_load_people_embedding_index', '_load_user_face_summary_by_id',
        '_cached_person_rows_for_user', '_load_existing_people_for_matching',
        '_create_person_entity', '_add_face_to_person',
        '_ensure_face_embedding_present', '_update_person_rep_embedding',
    ):
        mock = Mock(side_effect=AssertionError(f'live path called {name}'))
        monkeypatch.setattr(app, name, mock)
        mocks[name] = mock
    return mocks


@pytest.fixture
def live(monkeypatch):
    pytest.importorskip('faiss')
    import clustering_lease
    import faiss_assignment

    faiss_assignment.invalidate()
    events = []
    h = SimpleNamespace(
        events=events, filenames={}, lease_users=[], generation='stable-test-revision',
        faces=_Table('face', events, allow_scan=True),
        people=_Table('person', events), embeddings=_Table('embedding', events, allow_scan=True),
        members=_Table('member', events), metadata=_Table('metadata', events),
    )

    @contextmanager
    def lease(user_id):
        h.lease_users.append(user_id)
        events.append('acquire')

        class Guard:
            cache_generation = h.generation

            def check(self):
                events.append('check')

        try:
            yield Guard()
        finally:
            events.append('release')

    monkeypatch.setattr(app, 'PEOPLE_ASSIGNMENT_ENGINE', 'faiss')
    monkeypatch.setattr(app, '_live_faiss_assigner', None)
    monkeypatch.setattr(app, '_live_faiss_clients', None)
    monkeypatch.setattr(app, 'blob_service_client', object())
    for name, table in (
        ('face_table_client', h.faces), ('person_table_client', h.people),
        ('face_embeddings_table_client', h.embeddings),
        ('person_members_table_client', h.members), ('metadata_table_client', h.metadata),
    ):
        monkeypatch.setattr(app, name, table)
    h.lease_factory = Mock(return_value=lease)
    monkeypatch.setattr(clustering_lease, 'BlobLibraryLeaseFactory', h.lease_factory)
    monkeypatch.setattr(app, 'get_face_ids_for_filename',
                        lambda user, filename: list(h.filenames[filename]))
    monkeypatch.setattr(app, 'touch_user_search_indexes_state', Mock())
    monkeypatch.setattr(app, 'touch_user_sort_index_dirty', Mock())
    for name in ('_person_scan_cache', '_face_summary_scan_cache',
                 '_people_embedding_index_cache', '_metadata_scan_cache'):
        monkeypatch.setattr(app, name, app._UserScanCache(20))
    h.forbidden = _forbid_legacy_helpers(monkeypatch)

    def face(face_id, filename='photo.jpg', vector=(1.0, 0.0), **extra):
        row = dict(PartitionKey='lib-live', RowKey=face_id, filename=filename,
                   personId='', alignmentMethod='landmark-5pt', confidence=.99,
                   embeddingVersion=app.IPWORKER_FACE_CLUSTER_EMBEDDING_VERSION)
        row.update(extra)
        h.faces.seed(row)
        if vector is not None:
            h.embeddings.seed(dict(PartitionKey='lib-live', RowKey=face_id,
                                   embedding=json.dumps(vector)))
        h.filenames.setdefault(filename, []).append(face_id)
        h.metadata.seed(dict(PartitionKey='lib-live', RowKey=filename,
                             processing_state='active', face_status='done',
                             unrelated='preserve-me'))

    h.face = face
    h.assign = lambda filename, ids: app._assign_faces_to_people_incrementally(
        'lib-live', filename, ids)
    try:
        yield h
    finally:
        faiss_assignment.invalidate()


def test_unconfigured_app_defaults_to_live_faiss():
    """Check import-time configuration without changing this process's app."""
    env = dict(os.environ)
    env.pop('PEOPLE_ASSIGNMENT_ENGINE', None)
    result = subprocess.run(
        [sys.executable, '-c',
         'import app; print("assignment-default=" + app.PEOPLE_ASSIGNMENT_ENGINE)'],
        cwd=Path(app.__file__).parent, env=env, capture_output=True, text=True,
        timeout=60, check=True,
    )
    assert 'assignment-default=faiss' in result.stdout.splitlines()


def test_live_wrapper_dispatch_never_invokes_legacy_helpers(monkeypatch):
    monkeypatch.setattr(app, 'PEOPLE_ASSIGNMENT_ENGINE', 'faiss')
    forbidden = _forbid_legacy_helpers(monkeypatch)
    adapter = SimpleNamespace(assign=Mock(return_value=({'face-1': 'alice'}, set())))
    factory = Mock(return_value=adapter)
    monkeypatch.setattr(app, '_get_live_faiss_assigner', factory)

    assert app._assign_faces_to_people_incrementally(
        'lib-live', 'photo.jpg', ['face-1']) == ({'face-1': 'alice'}, set())
    factory.assert_called_once_with()
    adapter.assign.assert_called_once_with('lib-live', 'photo.jpg', ['face-1'])
    for mock in forbidden.values():
        mock.assert_not_called()


def test_live_empty_wrapper_does_not_acquire_adapter_or_lease(monkeypatch):
    monkeypatch.setattr(app, 'PEOPLE_ASSIGNMENT_ENGINE', 'faiss')
    factory = Mock(side_effect=AssertionError('empty dispatch acquired adapter'))
    monkeypatch.setattr(app, '_get_live_faiss_assigner', factory)
    assert app._assign_faces_to_people_incrementally('lib-live', 'photo.jpg', []) == ({}, set())
    factory.assert_not_called()


def test_live_worker_skips_queued_upload_dbscan_maintenance(monkeypatch):
    monkeypatch.setattr(app, 'PEOPLE_ASSIGNMENT_ENGINE', 'faiss')
    status = Mock()
    monkeypatch.setattr(app, '_upsert_job_status', status)
    monkeypatch.setattr(app, 'cluster_user_faces',
                        Mock(side_effect=AssertionError('automatic DBSCAN must not run')))
    app._handle_clustering_queue_payload(
        {'trigger': 'upload_face_ready'}, 'job-1', 'lib-live', 'people_cluster')
    assert status.call_args.args == ('job-1', 'lib-live', 'people_cluster', 'done')
    assert status.call_args.kwargs['result'] == {'skipped': 'live_faiss_assignment'}


@pytest.fixture
def mutation_hook(monkeypatch):
    import clustering_lease

    h = SimpleNamespace(events=[], markers=[], rows={}, marker_error_at=None,
                        mutation_error=None, blob=object())

    def mark(blob, container, user):
        assert blob is h.blob
        assert container == app.BLOB_PEOPLE_EMBEDDING_INDEX_CONTAINER
        assert user == 'lib-live'
        h.events.append('marker')
        h.markers.append({key: dict(row) for key, row in h.rows.items()})
        if len(h.markers) == h.marker_error_at:
            raise RuntimeError('revision marker unavailable')

    def mutate(entity, **kwargs):
        h.events.append('mutation')
        h.rows[entity['RowKey']] = dict(entity)
        if h.mutation_error:
            raise h.mutation_error
        return 'mutation-result'

    h.raw = SimpleNamespace(upsert_entity=Mock(side_effect=mutate),
                            create_entity=Mock(side_effect=mutate))
    h.callback = Mock(side_effect=lambda user: h.events.append(('invalidate', user)))
    h.client = app._InvalidatingTableClient(h.raw, h.callback)
    monkeypatch.setattr(app, 'PEOPLE_ASSIGNMENT_ENGINE', 'faiss')
    monkeypatch.setattr(app, 'blob_service_client', h.blob)
    monkeypatch.setattr(app, 'face_table_client', h.client)
    monkeypatch.setattr(clustering_lease, 'mark_library_changed', mark)
    return h


@pytest.mark.parametrize('policy', [
    {'personId': 'alice'}, {'personId': '', 'rejected': True},
    {'personId': '', 'reviewStatus': 'rejected'},
])
def test_face_curation_marks_revision_before_and_after_upsert(mutation_hook, policy):
    h = mutation_hook
    row = dict(PartitionKey='lib-live', RowKey='face-1', **policy)
    assert h.client.upsert_entity(entity=row) == 'mutation-result'
    assert h.events == ['marker', ('invalidate', 'lib-live'), 'mutation', 'marker']
    assert h.markers == [{}, {'face-1': row}]
    h.callback.assert_called_once_with('lib-live')


@pytest.mark.parametrize('method', ['upsert_entity', 'create_entity'])
@pytest.mark.parametrize('review_status', ['', 'suspicious'])
def test_new_unowned_face_insert_does_not_mark_revision(mutation_hook, method, review_status):
    h = mutation_hook
    row = dict(PartitionKey='lib-live', RowKey='new-face', personId='', reviewStatus=review_status)
    assert getattr(h.client, method)(row) == 'mutation-result'
    assert not h.markers
    assert h.events == [('invalidate', 'lib-live'), 'mutation']
    assert h.rows == {'new-face': row}


def test_revision_marker_failure_before_upsert_fails_closed(mutation_hook):
    h = mutation_hook
    h.marker_error_at = 1
    with pytest.raises(RuntimeError, match='revision marker unavailable'):
        h.client.upsert_entity(dict(PartitionKey='lib-live', RowKey='face-1', personId='alice'))
    assert h.events == ['marker'] and not h.rows
    h.raw.upsert_entity.assert_not_called()
    h.callback.assert_not_called()


def test_revision_marker_failure_after_upsert_propagates_committed_ambiguity(mutation_hook):
    h = mutation_hook
    h.marker_error_at = 2
    row = dict(PartitionKey='lib-live', RowKey='face-1', personId='alice')
    with pytest.raises(RuntimeError, match='revision marker unavailable'):
        h.client.upsert_entity(row)
    assert h.rows == {'face-1': row}
    assert h.markers == [{}, {'face-1': row}]
    assert h.events == ['marker', ('invalidate', 'lib-live'), 'mutation', 'marker']
    h.raw.upsert_entity.assert_called_once_with(row)


def test_ambiguous_upsert_error_still_marks_revision_after_mutation(mutation_hook):
    h = mutation_hook
    h.mutation_error = RuntimeError('ambiguous face write')
    row = dict(PartitionKey='lib-live', RowKey='face-1', rejected=True)
    with pytest.raises(RuntimeError, match='ambiguous face write') as exc:
        h.client.upsert_entity(row)
    assert exc.value is h.mutation_error
    assert h.markers == [{}, {'face-1': row}]
    assert h.events == ['marker', ('invalidate', 'lib-live'), 'mutation', 'marker']


def test_app_assign_persists_and_second_photo_is_warm_with_point_read_metadata(live):
    from faiss_assignment import FaissAssigner

    live.face('first')
    pid = FaissAssigner.person_id('lib-live', 'first')
    assert live.assign('photo.jpg', ['first']) == ({'first': pid}, {pid})
    adapter = app._get_live_faiss_assigner()
    assert isinstance(adapter, FaissAssigner)
    live.lease_factory.assert_called_once_with(
        app.blob_service_client, app.BLOB_PEOPLE_EMBEDDING_INDEX_CONTAINER)
    assert live.faces.queries == ["PartitionKey eq 'lib-live'"]
    assert json.loads(live.people.rows['lib-live', pid]['faceIds']) == ['first']
    assert live.people.rows['lib-live', pid]['name'] == ''
    assert json.loads(live.people.rows['lib-live', pid]['repEmbedding']) == [1.0, 0.0]
    assert live.faces.rows['lib-live', 'first']['personId'] == pid
    member = live.members.rows[pid, 'first']
    assert member['userId'] == 'lib-live' and member['addedAt']

    # Naming is a storage mutation here, not a mocked metadata callback. The
    # second projection must freshly point-read the person to discover it.
    live.people.rows['lib-live', pid]['name'] = 'Alice'
    live.face('second', 'second.jpg', (.99, .01))
    live.faces.allow_scan = False  # Any warm scan now fails, not just a count.
    before_reads = live.people.reads['lib-live', pid]
    assert live.assign('second.jpg', ['second']) == ({'second': pid}, set())
    assert app._get_live_faiss_assigner() is adapter
    assert live.faces.queries == ["PartitionKey eq 'lib-live'"]
    assert not live.people.queries
    assert live.embeddings.queries == ["PartitionKey eq 'lib-live'"]
    assert not live.members.queries and not live.metadata.queries
    assert live.people.reads['lib-live', pid] > before_reads
    assert live.embeddings.reads == Counter({('lib-live', 'first'): 1,
                                           ('lib-live', 'second'): 1})
    assert live.metadata.reads == Counter({('lib-live', 'photo.jpg'): 1,
                                         ('lib-live', 'second.jpg'): 1})
    assert json.loads(live.people.rows['lib-live', pid]['faceIds']) == ['first', 'second']
    assert live.faces.rows['lib-live', 'second']['personId'] == pid
    assert live.members.rows[pid, 'second']['userId'] == 'lib-live'
    first = live.metadata.rows['lib-live', 'photo.jpg']
    second = live.metadata.rows['lib-live', 'second.jpg']
    assert json.loads(first['faces'])[0]['personId'] == pid
    assert first['faceCount'] == 1
    assert json.loads(second['peopleIds']) == [pid]
    assert second['faceCount'] == 1 and second['unrelated'] == 'preserve-me'
    assert json.loads(second['faces'])[0]['personId'] == pid
    assert json.loads(second['faces'])[0]['faceId'] == 'second'
    assert [e for e in live.events if e in ('person', 'member', 'face', 'metadata')] == (
        ['person', 'member', 'face', 'metadata'] * 2)
    assert live.lease_users == ['lib-live', 'lib-live']
    assert live.events.count('release') == 2
    for mock in live.forbidden.values():
        mock.assert_not_called()


@pytest.mark.parametrize('policy', [
    {'rejected': True}, {'rejected': 'true'}, {'reviewStatus': 'rejected'},
    {'rejected': True, 'confirmedByUser': True},
    {'rejected': True, 'personId': 'existing-owner'},
    {'reviewStatus': 'suspicious'}, {'confidence': 0.0},
    {'alignmentMethod': 'none'}, {'embeddingVersion': 'unsupported-test-version'},
])
def test_app_policy_skips_rejected_or_ineligible_faces_without_writes(live, policy):
    live.face('blocked', **policy)
    assert live.assign('photo.jpg', ['blocked']) == ({}, set())
    assert live.faces.rows['lib-live', 'blocked']['personId'] == policy.get('personId', '')
    assert not live.faces.writes and not live.people.writes and not live.members.writes
    assert not live.metadata.writes and not live.faces.queries
    assert not live.embeddings.reads


def test_app_missing_face_or_embedding_is_a_no_op_not_a_transport_error(live):
    live.face('no-vector', vector=None)
    assert live.assign('photo.jpg', ['missing-face', 'no-vector']) == ({}, set())
    assert live.faces.reads['lib-live', 'missing-face'] == 1
    assert live.embeddings.reads['lib-live', 'no-vector'] == 1
    assert not live.people.writes and not live.faces.writes and not live.metadata.writes


def test_app_point_read_transport_errors_propagate(live, monkeypatch):
    live.face('first')
    monkeypatch.setattr(live.embeddings, 'get_entity',
                        Mock(side_effect=RuntimeError('embedding transport unavailable')))
    with pytest.raises(RuntimeError, match='embedding transport unavailable'):
        live.assign('photo.jpg', ['first'])
    assert not live.people.writes and not live.faces.writes
    assert live.events[-1] == 'release'


@pytest.mark.parametrize('mutation', ['reject-face', 'delete-person'])
def test_app_warm_candidate_validation_uses_fresh_authoritative_rows(live, mutation):
    live.face('first')
    mapping, _ = live.assign('photo.jpg', ['first'])
    old_pid = mapping['first']
    if mutation == 'reject-face':
        live.faces.rows['lib-live', 'first']['rejected'] = True
    else:
        del live.people.rows['lib-live', old_pid]
    live.face('second', 'second.jpg')
    live.faces.allow_scan = False
    mapping, created = live.assign('second.jpg', ['second'])
    assert mapping['second'] != old_pid
    assert created == {mapping['second']}
    assert live.faces.reads['lib-live', 'first'] >= 3
    assert live.faces.queries == ["PartitionKey eq 'lib-live'"]
    assert live.members.rows[mapping['second'], 'second']['userId'] == 'lib-live'


def test_live_metadata_projection_skips_missing_rejected_and_unnamed_rows(live):
    live.face('named', personId='alice')
    live.face('unnamed', personId='unnamed')
    live.face('rejected', personId='alice', rejected=True)
    live.face('orphan', personId='deleted-person')
    live.filenames['photo.jpg'].append('deleted-face')
    live.people.seed(dict(PartitionKey='lib-live', RowKey='alice', name='Alice'))
    live.people.seed(dict(PartitionKey='lib-live', RowKey='unnamed',
                          name='Unnamed 1'))
    live.faces.allow_scan = False

    app._live_faiss_metadata_update('lib-live', 'photo.jpg')

    row = live.metadata.rows['lib-live', 'photo.jpg']
    assert row['faceCount'] == 3
    assert json.loads(row['peopleIds']) == ['alice']
    assert [face['faceId'] for face in json.loads(row['faces'])] == ['named', 'orphan', 'unnamed']
    assert live.faces.reads['lib-live', 'deleted-face'] == 1
    assert live.people.reads['lib-live', 'deleted-person'] == 1
    assert not live.faces.queries and not live.people.queries and not live.metadata.queries


def test_metadata_face_and_person_reads_overlap_with_bounded_concurrency(live, monkeypatch):
    import threading

    monkeypatch.setenv('PEOPLE_FAISS_IO_CONCURRENCY', '2')
    for fid, pid in [('d', 'alice'), ('b', 'alice'), ('c', 'bob'), ('a', 'bob')]:
        live.face(fid, personId=pid)
    for pid in ('alice', 'bob'):
        live.people.seed(dict(PartitionKey='lib-live', RowKey=pid, name=pid.title()))
    original_face = live.faces.get_entity
    original_person = live.people.get_entity
    face_barrier, person_barrier = threading.Barrier(2), threading.Barrier(2)
    state = {'active': 0, 'peak': 0}
    lock = threading.Lock()

    def read(original, barrier, **kwargs):
        with lock:
            state['active'] += 1
            state['peak'] = max(state['peak'], state['active'])
        try:
            barrier.wait(timeout=5)
            return original(**kwargs)
        finally:
            with lock:
                state['active'] -= 1

    monkeypatch.setattr(live.faces, 'get_entity', lambda **kw: read(original_face, face_barrier, **kw))
    monkeypatch.setattr(live.people, 'get_entity', lambda **kw: read(original_person, person_barrier, **kw))
    app._live_faiss_metadata_update('lib-live', 'photo.jpg')
    assert state['peak'] == 2
    row = live.metadata.rows['lib-live', 'photo.jpg']
    assert [face['faceId'] for face in json.loads(row['faces'])] == ['a', 'b', 'c', 'd']
    assert json.loads(row['peopleIds']) == ['bob', 'alice']
    assert live.people.reads == Counter({('lib-live', 'alice'): 1, ('lib-live', 'bob'): 1})


def test_metadata_deduplicates_unnamed_and_missing_people_reads(live):
    for fid, pid in [('a', 'unnamed'), ('b', 'unnamed'), ('c', 'missing'), ('d', 'missing')]:
        live.face(fid, personId=pid)
    live.filenames['photo.jpg'].append('a')
    live.people.seed(dict(PartitionKey='lib-live', RowKey='unnamed', name='Unnamed 1'))
    app._live_faiss_metadata_update('lib-live', 'photo.jpg')
    row = live.metadata.rows['lib-live', 'photo.jpg']
    assert row['faceCount'] == 4
    assert json.loads(row['peopleIds']) == []
    assert live.faces.reads['lib-live', 'a'] == 1
    assert live.people.reads['lib-live', 'unnamed'] == 1
    assert live.people.reads['lib-live', 'missing'] == 1


@pytest.mark.parametrize('stage', ['face', 'person'])
def test_metadata_parallel_transport_failure_does_not_write_partial_projection(live, monkeypatch, stage):
    from azure.core.exceptions import ServiceRequestError

    live.face('a', personId='alice')
    live.face('b', personId='alice')
    live.people.seed(dict(PartitionKey='lib-live', RowKey='alice', name='Alice'))
    table = live.faces if stage == 'face' else live.people
    original = table.get_entity

    def fail(**kwargs):
        if kwargs['row_key'] in ('a', 'alice'):
            raise ServiceRequestError('read offline')
        return original(**kwargs)

    monkeypatch.setattr(table, 'get_entity', fail)
    with pytest.raises(ServiceRequestError, match='read offline'):
        app._live_faiss_metadata_update('lib-live', 'photo.jpg')
    assert not live.metadata.writes


def test_metadata_missing_filename_index_keeps_filtered_query_fallback(live, monkeypatch):
    live.face('a', personId='alice')
    live.people.seed(dict(PartitionKey='lib-live', RowKey='alice', name='Alice'))
    monkeypatch.setattr(app, 'get_face_ids_for_filename', lambda *args: None)

    def query(filter):
        assert filter == "PartitionKey eq 'lib-live' and filename eq 'photo.jpg'"
        yield live.faces.rows['lib-live', 'a']

    monkeypatch.setattr(live.faces, 'query_entities', query)
    app._live_faiss_metadata_update('lib-live', 'photo.jpg')
    assert json.loads(live.metadata.rows['lib-live', 'photo.jpg']['peopleIds']) == ['alice']


@pytest.mark.parametrize('value', ['0', '17', 'not-an-int'])
def test_metadata_invalid_concurrency_fails_before_storage(live, monkeypatch, value):
    monkeypatch.setenv('PEOPLE_FAISS_IO_CONCURRENCY', value)
    with pytest.raises(ValueError):
        app._live_faiss_metadata_update('lib-live', 'photo.jpg')
    assert not live.faces.reads and not live.metadata.writes


def test_app_factory_delta_writes_bypass_mutation_wrappers(live, monkeypatch):
    on_write = Mock(side_effect=AssertionError('assignment invalidated its own runtime'))
    monkeypatch.setattr(app, 'face_table_client',
                        app._InvalidatingTableClient(live.faces, on_write))
    monkeypatch.setattr(app, 'person_table_client',
                        app._InvalidatingTableClient(live.people, on_write))
    monkeypatch.setattr(app, 'face_embeddings_table_client',
                        app._InvalidatingTableClient(live.embeddings, on_write))
    monkeypatch.setattr(app, 'person_members_table_client',
                        app._InvalidatingTableClient(live.members, on_write))
    live.face('first')
    mapping, created = live.assign('photo.jpg', ['first'])
    assert created == {mapping['first']}
    adapter = app._get_live_faiss_assigner()
    assert adapter.face_table is live.faces and adapter.person_table is live.people
    assert adapter.embedding_table is live.embeddings and adapter.member_table is live.members
    live.face('second', 'second.jpg')
    live.faces.allow_scan = False
    assert live.assign('second.jpg', ['second']) == ({'second': mapping['first']}, set())
    on_write.assert_not_called()
    assert live.faces.queries == ["PartitionKey eq 'lib-live'"]


def test_live_metadata_rejects_filename_index_pointing_at_another_photo(live):
    live.face('other-face', 'other.jpg', personId='alice')
    live.filenames['photo.jpg'] = ['other-face']
    with pytest.raises(ValueError, match='another photo'):
        app._live_faiss_metadata_update('lib-live', 'photo.jpg')
    assert not live.metadata.writes and not live.faces.queries


def test_live_upload_queue_schedules_assignment_but_never_maintenance(monkeypatch):
    monkeypatch.setattr(app, 'PEOPLE_ASSIGNMENT_ENGINE', 'faiss')
    monkeypatch.setattr(app, '_people_features_available', lambda: True)
    enqueue = Mock()
    due = Mock(side_effect=AssertionError('live engine checked DBSCAN maintenance'))
    maintenance = Mock(side_effect=AssertionError('live engine queued DBSCAN maintenance'))
    monkeypatch.setattr(app, '_enqueue_incremental_assign_job', enqueue)
    monkeypatch.setattr(app, '_clustering_maintenance_due', due)
    monkeypatch.setattr(app, '_enqueue_clustering_job', maintenance)

    for filename in ('first.jpg', 'second.jpg'):
        assert app._queue_people_clustering_after_face_processing(
            'lib-live', filename,
            {'processing_state': 'active', 'face_status': 'done', 'faceCount': 1},
        ) == {'status': 'incremental_only'}
    assert [call.args for call in enqueue.call_args_list] == [
        ('lib-live', 'first.jpg'), ('lib-live', 'second.jpg')]
    due.assert_not_called()
    maintenance.assert_not_called()


@pytest.mark.parametrize('projection_fails', [False, True])
def test_live_queue_metadata_retry_without_pending_faces_never_assigns(live, monkeypatch,
                                                                     projection_fails):
    live.face('owned', personId='alice')
    live.people.seed(dict(PartitionKey='lib-live', RowKey='alice', name='Alice'))
    monkeypatch.setattr(app, '_people_features_available', lambda: True)
    metadata = Mock(return_value=live.metadata.rows['lib-live', 'photo.jpg'])
    pending = Mock(return_value=[])
    assign = Mock(side_effect=AssertionError('metadata retry attempted assignment'))
    error = RuntimeError('projection retry unavailable')
    projection = Mock(side_effect=error if projection_fails else app._live_faiss_metadata_update)
    monkeypatch.setattr(app, '_get_metadata_entity', metadata)
    monkeypatch.setattr(app, '_face_ids_awaiting_person_assignment', pending)
    monkeypatch.setattr(app, '_assign_faces_to_people_incrementally', assign)
    monkeypatch.setattr(app, '_live_faiss_metadata_update', projection)
    payload = {'filename': 'photo.jpg'}
    if projection_fails:
        with pytest.raises(RuntimeError, match='projection retry unavailable') as exc:
            app._handle_clustering_queue_payload(payload, None, 'lib-live', 'people_incremental_assign')
        assert exc.value is error
        assert not live.metadata.writes
    else:
        app._handle_clustering_queue_payload(payload, None, 'lib-live', 'people_incremental_assign')
        assert json.loads(live.metadata.rows['lib-live', 'photo.jpg']['peopleIds']) == ['alice']
        assert live.metadata.rows['lib-live', 'photo.jpg']['faceCount'] == 1
    metadata.assert_called_once_with('lib-live', 'photo.jpg')
    pending.assert_called_once_with('lib-live', 'photo.jpg')
    projection.assert_called_once_with('lib-live', 'photo.jpg')
    assign.assert_not_called()
    assert not live.lease_users and not live.faces.queries
    assert not live.faces.writes and not live.people.writes and not live.members.writes