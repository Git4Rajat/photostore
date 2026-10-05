"""Index builds must never run in a serving process (backend/extras/admin/upload/
tools), and the builders themselves must hold flat memory.

Production incident: `extras` (0.5 vCPU / 1Gi) OOM-crash-looped because the first
People request ran the people-index build in-process -- a full download of every
person AND face row including their ~8KB embedding columns."""
from __future__ import annotations

import gc
import json
import threading
import tracemalloc

import numpy as np
import pytest

import storage_utils as su


class _ExplodingTable:
    """Any scan is a failure: serving processes must not touch the table to build."""

    def query_entities(self, *a, **k):
        raise AssertionError('a serving process scanned a table to build an index')

    def get_entity(self, *a, **k):
        raise AssertionError('a serving process read the table to build an index')


@pytest.fixture
def serving_process(monkeypatch):
    monkeypatch.setattr(su, '_ROLE_MAY_BUILD_INDEXES', False)
    requested = []
    monkeypatch.setattr(su, 'INDEX_BUILD_REQUEST_HOOK', lambda uid: requested.append(uid))
    for client in ('metadata_table_client', 'person_table_client', 'face_table_client', 'embeddings_table_client',
                   'albums_table_client'):
        monkeypatch.setitem(su._CTX, client, _ExplodingTable())
    started = []
    real_start = threading.Thread.start
    monkeypatch.setattr(threading.Thread, 'start', lambda self: started.append(self.name) or real_start(self))
    return requested, started


@pytest.mark.parametrize('getter', [
    'get_user_sort_index', 'get_user_access_index', 'get_user_albums_index', 'get_user_people_index',
])
def test_json_index_getters_request_a_worker_build_instead_of_scanning(serving_process, getter):
    requested, started = serving_process
    # no blob exists, sync build allowed (the default): a build role would scan here
    assert getattr(su, getter)('lib-1', allow_refresh=True) is None
    assert requested == ['lib-1']
    assert not [name for name in started if 'rebuild' in name or 'index' in name]


@pytest.mark.parametrize('getter', ['get_user_vector_index', 'get_user_tag_embedding_index', 'get_user_people_embedding_index'])
def test_embedding_index_getters_request_a_worker_build_instead_of_scanning(serving_process, getter):
    requested, _ = serving_process
    assert getattr(su, getter)('lib-1', allow_refresh=True) is None
    assert requested == ['lib-1']


@pytest.mark.parametrize('kicker', [
    '_rebuild_lexical_index_in_background', '_rebuild_sort_index_in_background', '_rebuild_access_index_in_background',
    '_rebuild_albums_index_in_background', '_rebuild_people_index_in_background', '_rebuild_people_embedding_index_in_background',
])
def test_background_kickers_become_build_requests(serving_process, kicker):
    requested, started = serving_process
    getattr(su, kicker)('lib-9', {'sourceVersion': 'v1'})
    assert requested == ['lib-9'] and not [n for n in started if 'rebuild' in n]


def test_build_roles_still_build(monkeypatch):
    monkeypatch.setattr(su, '_ROLE_MAY_BUILD_INDEXES', True)
    requested = []
    monkeypatch.setattr(su, 'INDEX_BUILD_REQUEST_HOOK', lambda uid: requested.append(uid))
    ran = []
    monkeypatch.setattr(su, 'refresh_user_sort_index', lambda key, **k: ran.append(key))
    monkeypatch.setattr(su, '_load_sort_index_manifest', lambda key: {})
    monkeypatch.setattr(su, '_load_sort_index_blob', lambda key: None)
    su.get_user_sort_index('lib-2', allow_refresh=True)
    assert ran == ['lib-2'] and requested == []


def test_role_defaults_only_worker_and_ipworker_may_build():
    for role, expected in (('worker', True), ('ipworker', True), ('backend', False), ('extras', False),
                           ('admin', False), ('upload', False), ('tools', False)):
        assert (role in su._INDEX_BUILD_ROLES) is expected, role


def test_request_hook_failures_never_break_a_request(monkeypatch):
    monkeypatch.setattr(su, 'INDEX_BUILD_REQUEST_HOOK', lambda uid: (_ for _ in ()).throw(RuntimeError('queue down')))
    su.request_index_build('lib-1')   # must not raise
    su.request_index_build('')        # no-op


# --- builders hold flat memory ---------------------------------------------------------

_EMB_BASE = json.dumps([0.12345678] * 512)


def EMB_FOR(i: int) -> str:
    """A DISTINCT ~6KB embedding string per row, like real data. (A single shared
    string would make every row 'free' and hide whether a builder retains rows.)
    str.replace allocates a fresh copy each call, and is far cheaper than dumps."""
    return _EMB_BASE.replace('0.12345678', f'0.{i % 100000000:08d}', 1)


EMB = EMB_FOR(0)   # for tests that only need a valid embedding value


class _HeavyTable:
    """Yields heavy rows lazily and IGNORES `select` (worst case): the builder must
    still keep only the light columns."""

    def __init__(self, make_rows):
        self.make_rows = make_rows
        self.select_seen = None

    def query_entities(self, query, select=None, **kwargs):
        self.select_seen = select
        return self.make_rows()

    def get_entity(self, partition_key, row_key):
        raise KeyError(row_key)


def _peak(fn):
    gc.collect()
    tracemalloc.start()
    try:
        result = fn()
        return result, tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def test_people_index_build_does_not_retain_embeddings(monkeypatch):
    persons, faces_per = 4000, 5

    def person_rows():
        for p in range(persons):
            yield {'PartitionKey': 'lib', 'RowKey': f'p{p}', 'name': f'P{p}' if p % 2 else '', 'repEmbedding': EMB_FOR(p),
                   'faceIds': json.dumps([f'p{p}-f{f}' for f in range(faces_per)]), 'createdAt': 'x'}

    def face_rows():
        for p in range(persons):
            for f in range(faces_per):
                yield {'PartitionKey': 'lib', 'RowKey': f'p{p}-f{f}', 'personId': f'p{p}', 'confidence': 0.5 + f / 10,
                       'filename': f'img{p}.jpg', 'bbox': '{"x":1}', 'embedding': EMB_FOR(p * 10 + f)}

    person_table, face_table = _HeavyTable(person_rows), _HeavyTable(face_rows)
    monkeypatch.setitem(su._CTX, 'person_table_client', person_table)
    monkeypatch.setitem(su._CTX, 'face_table_client', face_table)

    snapshot, peak = _peak(lambda: su._build_user_people_index_snapshot('lib', 'v1'))
    assert len(snapshot.rows) == persons
    assert 'repEmbedding' not in person_table.select_seen and 'embedding' not in face_table.select_seen
    # 4,000 persons + 20,000 faces with ~6KB embeddings is ~144MB if they were retained.
    assert peak < 25 * 1024 * 1024, f'peak {peak / 1048576:.1f}MB: embeddings are being retained'
    assert snapshot.rows[0]['coverFaceId'].endswith('-f4')   # best (highest-confidence) face is the cover


def test_vector_index_build_keeps_compact_vectors_not_embedding_rows(monkeypatch):
    n, dim = 6000, 512
    monkeypatch.setattr(su, 'PHOTO_EMBEDDING_DIMENSION', dim)
    monkeypatch.setattr(su.vision_utils, 'get_text_embedding_dimension', lambda: dim)
    monkeypatch.setattr(su.vision_utils, 'get_text_embedding_version', lambda: 'v')

    def meta_rows():
        for i in range(n):
            yield {'PartitionKey': 'lib', 'RowKey': f'img{i}.jpg', 'tags': '["a"]'}

    def embedding_rows():
        for i in range(n):
            yield {'PartitionKey': 'lib', 'RowKey': f'img{i}.jpg', 'photoEmbedding': EMB_FOR(i),
                   'photoEmbeddingVersion': su.PHOTO_EMBEDDING_MODEL_VERSION}

    monkeypatch.setitem(su._CTX, 'metadata_table_client', _HeavyTable(meta_rows))
    monkeypatch.setitem(su._CTX, 'embeddings_table_client', _HeavyTable(embedding_rows))
    snapshot, peak = _peak(lambda: su._build_user_vector_index_snapshot('lib', 'v1'))
    assert snapshot.embeddings.shape == (n, dim) and len(snapshot.row_keys) == n
    assert np.allclose(np.linalg.norm(snapshot.embeddings, axis=1), 1.0, atol=1e-4)
    index_bytes = n * dim * 4                       # ~12MB: the unavoidable size of the result
    assert peak < index_bytes * 3.2, f'peak {peak / 1048576:.1f}MB vs index {index_bytes / 1048576:.1f}MB'   # dict-of-rows approach was ~40MB of JSON on top


def test_failed_vector_scan_is_not_persisted_as_an_empty_index(monkeypatch):
    monkeypatch.setitem(su._CTX, 'metadata_table_client', _ExplodingTable())
    monkeypatch.setitem(su._CTX, 'embeddings_table_client', None)
    monkeypatch.setattr(su.vision_utils, 'get_text_embedding_version', lambda: 'v')
    assert su._build_user_vector_index_snapshot('lib', 'v1') is None


def test_people_embedding_build_uses_float32_arrays_and_aligns_dimensions(monkeypatch):
    def person_rows():
        yield {'RowKey': 'a', 'name': 'A', 'faceIds': '["fa"]', 'repEmbedding': json.dumps([1.0, 2.0, 3.0])}
        yield {'RowKey': 'b', 'name': 'B', 'faceIds': '["fb"]', 'repEmbedding': json.dumps([1.0, 2.0])}       # shorter: zero-padded
        yield {'RowKey': 'c', 'name': 'C', 'faceIds': '["fc"]', 'repEmbedding': json.dumps([1.0, 2.0, 3.0, 4.0])}  # longer: sets dim

    def face_rows():
        for pid in 'abc':
            yield {'RowKey': f'f{pid}', 'personId': pid}

    monkeypatch.setitem(su._CTX, 'person_table_client', _HeavyTable(person_rows))
    monkeypatch.setitem(su._CTX, 'face_table_client', _HeavyTable(face_rows))
    snap = su._build_user_people_embedding_index_snapshot('lib', 'v1')
    assert snap.embeddings.dtype == np.float32 and snap.embeddings.shape == (3, 4)
    assert snap.embeddings[1].tolist() == [1.0, 2.0, 0.0, 0.0] and snap.person_ids == ['a', 'b', 'c']


def test_serving_process_does_not_scan_person_rows_to_build_the_assignment_index(monkeypatch):
    import app
    monkeypatch.setattr(su, '_ROLE_MAY_BUILD_INDEXES', False)
    requested = []
    monkeypatch.setattr(su, 'INDEX_BUILD_REQUEST_HOOK', lambda uid: requested.append(uid))
    monkeypatch.setattr(app, 'person_table_client', _ExplodingTable())
    monkeypatch.setattr(app, 'get_user_people_embedding_index', lambda uid, allow_refresh=True: None)  # no durable blob yet
    app._people_embedding_index_cache.invalidate('lib-3')
    assert app._load_people_embedding_index('lib-3') == []
    assert requested == ['lib-3']


# --- a failed download / manifest blip must never start a full rebuild ------------------------

def test_open_library_db_requests_a_build_only_when_none_exists(monkeypatch):
    import app
    import search_db
    triggered = []
    monkeypatch.setattr(app, '_trigger_tools_index_rebuild', lambda uid, **kw: triggered.append(kw.get('reason')))
    monkeypatch.setattr(search_db, 'open_database', lambda uid, **kw: None)

    monkeypatch.setattr(search_db, 'needs_build', lambda uid: False)        # manifest exists / transient error
    assert app._open_library_db('u1') is None and triggered == []

    monkeypatch.setattr(search_db, 'needs_build', lambda uid: True)         # genuinely never built
    assert app._open_library_db('u1') is None and triggered == ['no-search-db']


def test_needs_build_distinguishes_absent_from_error(monkeypatch):
    import search_db

    class _Missing(Exception):
        pass
    _Missing.__name__ = 'ResourceNotFoundError'

    class _Client:
        def __init__(self, exc=None, body=b''):
            self.exc, self.body = exc, body

        def download_blob(self):
            if self.exc:
                raise self.exc
            return type('D', (), {'readall': lambda s: self.body})()

    monkeypatch.setattr(search_db, '_blob_client', lambda name: _Client(_Missing()))
    assert search_db.needs_build('u1') is True                               # no manifest at all
    monkeypatch.setattr(search_db, '_blob_client', lambda name: _Client(RuntimeError('timeout')))
    assert search_db.needs_build('u1') is False                              # storage blip
    monkeypatch.setattr(search_db, '_blob_client',
                        lambda name: _Client(body=('{"sourceVersion":"v1","schemaVersion":"%s"}' % search_db.SCHEMA_VERSION).encode()))
    assert search_db.needs_build('u1') is False                              # current
    monkeypatch.setattr(search_db, '_blob_client', lambda name: _Client(body=b'{"sourceVersion":"v1","schemaVersion":"old"}'))
    assert search_db.needs_build('u1') is True                               # older schema
