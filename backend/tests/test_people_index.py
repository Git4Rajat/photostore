"""Unit tests for the people index (storage_utils.get_user_people_index and
friends), added so the People grid can render the whole list client-side
instead of routes/people.py:list_persons's hardcoded single page (the live
frontend calls faceService.listPersons(undefined, 0, 200) -- a person beyond
the 200th is silently never shown, not real pagination).

Mirrors test_albums_index.py's structure. Dirty-marking is tested at its real
hook point (app._invalidate_people_scan_cache, called by
_InvalidatingTableClient on every person/face write from ANY code path --
HTTP routes or the background clustering worker) rather than at individual
routes, since that's how it's actually wired -- see the module comment on the
people index in storage_utils.py.
"""
from __future__ import annotations

import gzip
import json
import threading
from datetime import datetime, timedelta, timezone
import time

import pytest

import app
from routes.people import people_index
import storage_utils
from tests.fakes import FakeTable


class _FakeBlob:
    def __init__(self, store: dict, key: str) -> None:
        self._store = store
        self._key = key

    def upload_blob(self, data, overwrite=True, content_settings=None):
        self._store[self._key] = data

    def download_blob(self):
        if self._key not in self._store:
            raise KeyError(self._key)
        return _Downloaded(self._store[self._key])

    def delete_blob(self):
        self._store.pop(self._key, None)


class _Downloaded:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def readall(self) -> bytes:
        return self._data


class _FakeBlobServiceClient:
    def __init__(self) -> None:
        self.blobs: dict = {}

    def get_blob_client(self, container, blob):
        return _FakeBlob(self.blobs, f'{container}/{blob}')


def _seed_person(table: FakeTable, user_id: str, person_id: str, **overrides) -> None:
    row = {
        'PartitionKey': user_id, 'RowKey': person_id,
        'name': overrides.pop('name', ''), 'faceIds': '[]',
        **overrides,
    }
    table.upsert_entity(row)


def _seed_face(table: FakeTable, user_id: str, face_id: str, **overrides) -> None:
    row = {'PartitionKey': user_id, 'RowKey': face_id, **overrides}
    table.upsert_entity(row)


@pytest.fixture
def people_ctx(monkeypatch):
    person_table = FakeTable()
    face_table = FakeTable()
    blob_service = _FakeBlobServiceClient()
    monkeypatch.setitem(storage_utils._CTX, 'person_table_client', person_table)
    monkeypatch.setitem(storage_utils._CTX, 'face_table_client', face_table)
    monkeypatch.setitem(storage_utils._CTX, 'blob_service_client', blob_service)
    monkeypatch.setitem(storage_utils._CTX, 'blob_lexical_index_container', 'lexical-index')
    yield person_table, face_table, blob_service


# --- _build_user_people_index_snapshot ---------------------------------------

def test_build_snapshot_projects_expected_fields_and_picks_best_cover(people_ctx):
    person_table, face_table, _ = people_ctx
    _seed_person(person_table, 'lib-A', 'p1', name='Alice', faceIds=json.dumps(['f1', 'f2']))
    _seed_face(face_table, 'lib-A', 'f1', filename='a.jpg', personId='p1', confidence=0.5, bbox='{}')
    _seed_face(face_table, 'lib-A', 'f2', filename='b.jpg', personId='p1', confidence=0.9, confirmedByUser=True, bbox='{"left":1,"top":2,"width":3,"height":4}')

    snapshot = storage_utils._build_user_people_index_snapshot('lib-A', 'v1')

    assert snapshot is not None
    assert len(snapshot.rows) == 1
    row = snapshot.rows[0]
    assert row['personId'] == 'p1'
    assert row['name'] == 'Alice'
    assert row['isNamed'] is True
    assert row['faceCount'] == 2
    assert row['coverFaceId'] == 'f2'  # confirmed + higher confidence wins
    assert row['coverFilename'] == 'b.jpg'
    assert row['coverBbox'] == {'left': 1, 'top': 2, 'width': 3, 'height': 4}


def test_build_snapshot_unnamed_person_gets_placeholder_name(people_ctx):
    person_table, face_table, _ = people_ctx
    _seed_person(person_table, 'lib-A', 'p1', name='', faceIds=json.dumps(['f1']))
    _seed_face(face_table, 'lib-A', 'f1', filename='a.jpg', personId='p1')

    snapshot = storage_utils._build_user_people_index_snapshot('lib-A', 'v1')

    row = snapshot.rows[0]
    assert row['isNamed'] is False
    assert row['name'] == 'Unnamed 1'


def test_build_snapshot_excludes_rejected_and_reassigned_faces(people_ctx):
    person_table, face_table, _ = people_ctx
    _seed_person(person_table, 'lib-A', 'p1', name='Alice', faceIds=json.dumps(['f1', 'f2', 'f3']))
    _seed_face(face_table, 'lib-A', 'f1', filename='a.jpg', personId='p1', rejected=True)
    _seed_face(face_table, 'lib-A', 'f2', filename='b.jpg', personId='p2')  # reassigned to someone else
    _seed_face(face_table, 'lib-A', 'f3', filename='c.jpg', personId='p1')

    snapshot = storage_utils._build_user_people_index_snapshot('lib-A', 'v1')

    row = snapshot.rows[0]
    assert row['faceCount'] == 1
    assert row['coverFilename'] == 'c.jpg'


def test_build_snapshot_auto_deletes_empty_unnamed_cluster(people_ctx):
    """Mirrors list_persons's Phase B cleanup: an unnamed cluster whose faces
    are all definitively inactive gets deleted, not just excluded."""
    person_table, face_table, _ = people_ctx
    _seed_person(person_table, 'lib-A', 'p-empty', name='', faceIds=json.dumps(['f1']))
    _seed_face(face_table, 'lib-A', 'f1', filename='a.jpg', personId='p1')  # reassigned away

    snapshot = storage_utils._build_user_people_index_snapshot('lib-A', 'v1')

    assert snapshot.rows == []
    assert ('lib-A', 'p-empty') not in person_table.rows  # actually deleted


def test_build_snapshot_keeps_empty_named_cluster_without_deleting(people_ctx):
    person_table, face_table, _ = people_ctx
    _seed_person(person_table, 'lib-A', 'p-named', name='Alice', faceIds=json.dumps(['f1']))
    _seed_face(face_table, 'lib-A', 'f1', filename='a.jpg', personId='someone-else')

    snapshot = storage_utils._build_user_people_index_snapshot('lib-A', 'v1')

    assert len(snapshot.rows) == 1
    assert snapshot.rows[0]['personId'] == 'p-named'
    assert snapshot.rows[0]['faceCount'] == 0
    assert ('lib-A', 'p-named') in person_table.rows  # not deleted


def test_build_snapshot_indeterminate_face_lookup_prevents_auto_delete(people_ctx, monkeypatch):
    """A face missing from both the bulk map and a direct point-read (a
    genuine lookup failure, not confirmed deletion) must never cause an
    unnamed cluster to be auto-deleted -- same "indeterminate" guard
    list_persons uses."""
    person_table, face_table, _ = people_ctx
    _seed_person(person_table, 'lib-A', 'p1', name='', faceIds=json.dumps(['missing-face']))
    # Deliberately do not seed 'missing-face' in face_table.

    snapshot = storage_utils._build_user_people_index_snapshot('lib-A', 'v1')

    assert len(snapshot.rows) == 1  # kept, not deleted
    assert ('lib-A', 'p1') in person_table.rows


def test_build_snapshot_sorts_named_first(people_ctx):
    person_table, face_table, _ = people_ctx
    _seed_person(person_table, 'lib-A', 'p-unnamed', name='', faceIds=json.dumps(['f1']))
    _seed_face(face_table, 'lib-A', 'f1', filename='a.jpg', personId='p-unnamed')
    _seed_person(person_table, 'lib-A', 'p-named', name='Alice', faceIds=json.dumps(['f2']))
    _seed_face(face_table, 'lib-A', 'f2', filename='b.jpg', personId='p-named')

    snapshot = storage_utils._build_user_people_index_snapshot('lib-A', 'v1')

    assert [row['personId'] for row in snapshot.rows] == ['p-named', 'p-unnamed']


def test_build_snapshot_returns_none_when_tables_unconfigured(monkeypatch):
    monkeypatch.setitem(storage_utils._CTX, 'person_table_client', None)
    monkeypatch.setitem(storage_utils._CTX, 'face_table_client', None)
    assert storage_utils._build_user_people_index_snapshot('lib-A', 'v1') is None


def test_build_snapshot_returns_none_on_query_exception(monkeypatch):
    class _BoomTable:
        def query_entities(self, filter_str):
            raise RuntimeError('table storage hiccup')

    monkeypatch.setitem(storage_utils._CTX, 'person_table_client', _BoomTable())
    monkeypatch.setitem(storage_utils._CTX, 'face_table_client', FakeTable())
    assert storage_utils._build_user_people_index_snapshot('lib-A', 'v1') is None


# --- serialize / round trip ---------------------------------------------------

def test_serialize_round_trip_via_gzip_json():
    snapshot = storage_utils.LexicalIndexSnapshot(
        user_id='lib-A', source_version='v1', schema_version='v1', updated_at='v1',
        rows=[{'personId': 'p1', 'name': 'Alice', 'isNamed': True, 'faceCount': 1, 'coverFaceId': 'f1', 'coverFilename': 'a.jpg', 'coverBbox': {}, 'updatedAt': 'v1'}],
    )

    raw = storage_utils._serialize_people_index(snapshot)
    parsed = json.loads(gzip.decompress(raw).decode('utf-8'))

    assert parsed['sourceVersion'] == 'v1'
    assert parsed['rows'][0]['personId'] == 'p1'


def test_refresh_then_get_round_trips_through_the_fake_blob_service(people_ctx):
    person_table, face_table, _ = people_ctx
    _seed_person(person_table, 'lib-B', 'p1', name='Alice')

    refreshed = storage_utils.refresh_user_people_index('lib-B', source_version='v1')
    assert refreshed is not None
    storage_utils.invalidate_user_people_index_cache('lib-B')  # force the blob path

    result = storage_utils.get_user_people_index('lib-B', allow_refresh=False)

    assert result is not None
    assert len(result['rows']) == 1
    assert result['rows'][0]['personId'] == 'p1'


def test_get_user_people_index_returns_none_without_refresh_when_never_built(people_ctx):
    assert storage_utils.get_user_people_index('lib-never-built', allow_refresh=False) is None


def test_get_user_people_index_builds_on_first_call_with_allow_refresh(people_ctx):
    person_table, face_table, _ = people_ctx
    _seed_person(person_table, 'lib-C', 'p1', name='Alice')

    result = storage_utils.get_user_people_index('lib-C', allow_refresh=True)

    assert result is not None
    assert [row['personId'] for row in result['rows']] == ['p1']


def test_get_user_people_index_returned_rows_are_a_copy_not_the_shared_cache(people_ctx):
    person_table, face_table, _ = people_ctx
    _seed_person(person_table, 'lib-D', 'p1', name='Original')

    first = storage_utils.get_user_people_index('lib-D', allow_refresh=True)
    first['rows'][0]['name'] = 'mutated'
    second = storage_utils.get_user_people_index('lib-D', allow_refresh=True)

    assert second['rows'][0]['name'] == 'Original'


# --- cold + non-blocking (allow_sync_build=False) -----------------------------

def test_get_user_people_index_cold_non_blocking_returns_none_then_builds_in_background(people_ctx):
    person_table, face_table, _ = people_ctx
    _seed_person(person_table, 'lib-cold', 'p1', name='Alice')

    result = storage_utils.get_user_people_index('lib-cold', allow_refresh=True, allow_sync_build=False)
    assert result is None  # did NOT block on a synchronous build

    lock = storage_utils._PEOPLE_INDEX_REBUILD_LOCKS.lock_for('lib-cold')
    for _ in range(50):
        if lock.acquire(blocking=False):
            lock.release()
            break
        time.sleep(0.05)
    else:
        pytest.fail('background people index build never completed')

    built = storage_utils.get_user_people_index('lib-cold', allow_refresh=False)
    assert built is not None
    assert [row['personId'] for row in built['rows']] == ['p1']


def test_get_user_people_index_cold_sync_build_default_still_builds_inline(people_ctx):
    person_table, face_table, _ = people_ctx
    _seed_person(person_table, 'lib-sync', 'p1', name='Alice')

    result = storage_utils.get_user_people_index('lib-sync', allow_refresh=True)
    assert result is not None


def test_get_user_people_index_never_blocks_once_a_stale_snapshot_exists(people_ctx, monkeypatch):
    person_table, face_table, _ = people_ctx
    _seed_person(person_table, 'lib-I', 'p1', name='Alice')
    storage_utils.get_user_people_index('lib-I', allow_refresh=True)  # cold-start build, synchronous
    storage_utils.touch_user_people_index_state('lib-I')

    entered = threading.Event()
    release = threading.Event()

    def slow_build(user_id, source_version):
        entered.set()
        release.wait(timeout=5)
        return storage_utils.LexicalIndexSnapshot(
            user_id=user_id, source_version=source_version,
            schema_version=storage_utils._PEOPLE_INDEX_SCHEMA_VERSION,
            updated_at=source_version,
            rows=[{'personId': 'p2', 'name': 'Bob', 'isNamed': True, 'faceCount': 0, 'coverFaceId': '', 'coverFilename': '', 'coverBbox': {}, 'updatedAt': source_version}],
        )

    monkeypatch.setattr(storage_utils, '_build_user_people_index_snapshot', slow_build)

    start = time.monotonic()
    result = storage_utils.get_user_people_index('lib-I', allow_refresh=True)
    elapsed = time.monotonic() - start

    assert elapsed < 1.0, f'get_user_people_index blocked for {elapsed:.2f}s on a warm-but-dirty index'
    assert [row['personId'] for row in result['rows']] == ['p1']  # stale snapshot, served immediately
    assert entered.wait(timeout=5), 'background rebuild never started'

    release.set()
    lock = storage_utils._PEOPLE_INDEX_REBUILD_LOCKS.lock_for('lib-I')
    for _ in range(50):
        if lock.acquire(blocking=False):
            lock.release()
            break
        time.sleep(0.05)
    else:
        pytest.fail('background people index rebuild never completed')


# --- dirty-marking wiring: the real hook point --------------------------------

def test_invalidate_people_scan_cache_marks_people_index_dirty(monkeypatch):
    """The real coverage-completeness guarantee: _invalidate_people_scan_cache
    is called by _InvalidatingTableClient on EVERY write to
    person_table_client/face_table_client, from any code path (HTTP routes
    or the background clustering worker) -- so hooking it here once must
    dirty the people index regardless of which function triggered the
    write."""
    calls = []
    monkeypatch.setattr(app, 'touch_user_people_index_state', lambda user_id: calls.append(user_id))

    app._invalidate_people_scan_cache('lib-E')

    assert calls == ['lib-E']


def test_invalidate_people_scan_cache_noop_for_empty_user_id(monkeypatch):
    calls = []
    monkeypatch.setattr(app, 'touch_user_people_index_state', lambda user_id: calls.append(user_id))

    app._invalidate_people_scan_cache('')

    assert calls == []


def test_touch_user_people_index_state_debounces_repeated_calls(people_ctx):
    """_invalidate_people_scan_cache fires on every write to
    person_table_client/face_table_client -- during upload face detection
    that's once per detected FACE, not once per photo. Repeated calls in a
    burst must not rewrite the manifest blob every time. See
    _manifest_already_marked_dirty."""
    first = storage_utils.touch_user_people_index_state('lib-Z')
    second = storage_utils.touch_user_people_index_state('lib-Z')

    assert first != ''
    assert second == ''
    assert storage_utils._INDEX_MANIFEST_DIRTY_FLAGS[('lib-Z', 'people')] is True


def test_people_manifest_dirty_flag_clears_after_a_real_rebuild(people_ctx):
    storage_utils.touch_user_people_index_state('lib-Z')
    assert storage_utils._INDEX_MANIFEST_DIRTY_FLAGS.get(('lib-Z', 'people')) is True

    storage_utils.refresh_user_people_index('lib-Z', source_version='v1')

    assert ('lib-Z', 'people') not in storage_utils._INDEX_MANIFEST_DIRTY_FLAGS
    assert storage_utils.touch_user_people_index_state('lib-Z') != ''


def test_people_rebuild_cooldown_skips_a_second_trigger_right_after_the_first(people_ctx, monkeypatch):
    calls = []
    monkeypatch.setattr(
        storage_utils, 'refresh_user_people_index',
        lambda key, source_version=None: calls.append(key) or None,
    )
    manifest = {'sourceVersion': 'v1'}

    storage_utils._rebuild_people_index_in_background('lib-Y', manifest)
    for thread in list(threading.enumerate()):
        if thread.name == 'people-index-rebuild':
            thread.join(timeout=2)
    assert calls == ['lib-Y']

    storage_utils._rebuild_people_index_in_background('lib-Y', manifest)
    for thread in list(threading.enumerate()):
        if thread.name == 'people-index-rebuild':
            thread.join(timeout=2)
    assert calls == ['lib-Y']


# --- cleanup -------------------------------------------------------------

def test_delete_user_people_index_data_removes_blobs_and_cache(people_ctx):
    person_table, face_table, blob_service = people_ctx
    _seed_person(person_table, 'lib-K', 'p1', name='Alice')
    storage_utils.get_user_people_index('lib-K', allow_refresh=True)
    assert storage_utils._PEOPLE_INDEX_CACHE.get('lib-K') is not None

    storage_utils.delete_user_people_index_data('lib-K')

    assert storage_utils._PEOPLE_INDEX_CACHE.get('lib-K') is None
    container, blob_name = storage_utils.get_people_index_blob_location('lib-K')
    assert f'{container}/{blob_name}' not in blob_service.blobs


# --- GET /api/persons/index route ---------------------------------------------

@pytest.fixture
def people_index_route_ctx(monkeypatch):
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('owner', None))
    monkeypatch.setattr(app, '_people_features_available', lambda: True)


def test_people_index_returns_200_available_false_when_features_unavailable(monkeypatch):
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('owner', None))
    monkeypatch.setattr(app, '_people_features_available', lambda: False)

    with app.app.test_request_context('/api/persons/index'):
        response = people_index()

    assert not isinstance(response, tuple)
    assert response.status_code == 200
    assert response.get_json() == {'available': False}


def test_people_index_returns_200_available_false_when_index_none(monkeypatch, people_index_route_ctx):
    monkeypatch.setattr(app, 'get_user_people_index', lambda *a, **k: None)

    with app.app.test_request_context('/api/persons/index'):
        response = people_index()

    assert not isinstance(response, tuple)
    assert response.status_code == 200
    assert response.get_json() == {'available': False}


def test_people_index_passes_allow_sync_build_false(monkeypatch, people_index_route_ctx):
    captured = {}

    def _spy(user_id, **kwargs):
        captured.update(kwargs)
        return None

    monkeypatch.setattr(app, 'get_user_people_index', _spy)

    with app.app.test_request_context('/api/persons/index'):
        people_index()

    assert captured.get('allow_sync_build') is False


def test_people_index_returns_200_available_false_on_sas_mint_failure(monkeypatch, people_index_route_ctx):
    monkeypatch.setattr(app, 'get_user_people_index', lambda *a, **k: {'source_version': 'v1', 'updated_at': 'v1', 'rows': []})

    def _boom(*a, **k):
        raise RuntimeError('storage account not configured')

    monkeypatch.setattr(app, 'get_people_index_blob_location', _boom)

    with app.app.test_request_context('/api/persons/index'):
        response = people_index()

    assert response.status_code == 200
    assert response.get_json() == {'available': False}


def test_people_index_returns_available_true_with_sas_url(monkeypatch, people_index_route_ctx):
    monkeypatch.setattr(app, 'get_user_people_index', lambda *a, **k: {'source_version': 'v2', 'updated_at': '2026-01-01', 'rows': []})
    monkeypatch.setattr(app, 'get_people_index_blob_location', lambda *a, **k: ('lexical-index', 'abc-people.json.gz'))
    monkeypatch.setattr(app, '_create_stable_read_sas_url', lambda *a, **k: ('https://example.invalid/abc-people.json.gz?sas', '2026-01-02'))

    with app.app.test_request_context('/api/persons/index'):
        response = people_index()

    assert response.status_code == 200
    payload = response.get_json()
    assert payload['available'] is True
    assert payload['indexUrl'] == 'https://example.invalid/abc-people.json.gz?sas'
    assert payload['sourceVersion'] == 'v2'


# --- incremental refresh -------------------------------------------------------

def _refresh(user, version):
    return storage_utils.refresh_user_people_index(user, source_version=version)


def test_incremental_refresh_rederives_only_changed_clusters(people_ctx, monkeypatch):
    person_table, face_table, _ = people_ctx
    _seed_person(person_table, 'lib-A', 'p1', name='Alice', faceIds=json.dumps(['f1']))
    _seed_person(person_table, 'lib-A', 'p2', name='', faceIds=json.dumps(['f2']))
    _seed_person(person_table, 'lib-A', 'p3', name='', faceIds=json.dumps(['f3']))
    for fid, pid in (('f1', 'p1'), ('f2', 'p2'), ('f3', 'p3')):
        _seed_face(face_table, 'lib-A', fid, filename=f'{fid}.jpg', personId=pid)
    old = datetime.now(timezone.utc) - timedelta(days=1)
    person_table.clock = face_table.clock = lambda: old
    for t in (person_table, face_table):
        t.stamps = {k: old for k in t.rows}
    first = _refresh('lib-A', 'v1')
    assert [r['name'] for r in first.rows] == ['Alice', 'Unnamed 1', 'Unnamed 2']

    # p2 is rejected-away (face rejected), p4 appears, p1 is renamed; p3 is untouched.
    person_table.clock = face_table.clock = None
    _seed_face(face_table, 'lib-A', 'f2', filename='f2.jpg', personId='p2', rejected=True)
    _seed_person(person_table, 'lib-A', 'p1', name='Alicia', faceIds=json.dumps(['f1']))
    _seed_person(person_table, 'lib-A', 'p4', name='', faceIds=json.dumps(['f4']))
    _seed_face(face_table, 'lib-A', 'f4', filename='f4.jpg', personId='p4')
    lookups = []
    original = face_table.get_entity
    face_table.get_entity = lambda partition_key, row_key: (lookups.append(row_key), original(partition_key, row_key))[1]
    bulk = []
    original_query = face_table.query_entities
    face_table.query_entities = lambda f, select=None, **kw: (bulk.append(f), original_query(f, select=select, **kw))[1]

    second = _refresh('lib-A', 'v2')
    rows = {r['personId']: r for r in second.rows}
    assert set(rows) == {'p1', 'p3', 'p4'}                      # p2 emptied -> removed
    assert rows['p1']['name'] == 'Alicia'
    assert [rows['p3']['name'], rows['p4']['name']] == ['Unnamed 1', 'Unnamed 2']   # renumbered
    assert 'f3' not in lookups                                  # the untouched cluster's faces were never read
    assert all('Timestamp ge' in f for f in bulk)               # no full face scan


def test_incremental_refresh_falls_back_to_full_build_when_too_much_changed(people_ctx, monkeypatch):
    person_table, face_table, _ = people_ctx
    _seed_person(person_table, 'lib-A', 'p1', name='A', faceIds=json.dumps(['f1']))
    _seed_face(face_table, 'lib-A', 'f1', filename='a.jpg', personId='p1')
    _refresh('lib-A', 'v1')
    monkeypatch.setattr(storage_utils, '_PEOPLE_INCREMENTAL_MAX_CHANGED', 0)
    built = []
    real = storage_utils._build_user_people_index_snapshot
    monkeypatch.setattr(storage_utils, '_build_user_people_index_snapshot', lambda *a: (built.append(1), real(*a))[1])
    _seed_face(face_table, 'lib-A', 'f1', filename='a.jpg', personId='p1', rejected=False)
    _refresh('lib-A', 'v2')
    assert built == [1]


def test_incremental_rederives_cluster_whose_cover_face_moved_without_its_row_changing(people_ctx):
    person_table, face_table, _ = people_ctx
    _seed_person(person_table, 'lib-A', 'p1', name='A', faceIds=json.dumps(['f1', 'f2']))
    _seed_person(person_table, 'lib-A', 'p2', name='B', faceIds=json.dumps(['f3']))
    _seed_face(face_table, 'lib-A', 'f1', filename='1.jpg', personId='p1', confidence=0.9)
    _seed_face(face_table, 'lib-A', 'f2', filename='2.jpg', personId='p1', confidence=0.1)
    _seed_face(face_table, 'lib-A', 'f3', filename='3.jpg', personId='p2')
    old = datetime.now(timezone.utc) - timedelta(days=1)
    for t in (person_table, face_table):
        t.stamps = {k: old for k in t.rows}
    first = _refresh('lib-A', 'v1')
    assert {r['personId']: r['faceCount'] for r in first.rows} == {'p1': 2, 'p2': 1}
    for t in (person_table, face_table):
        t.stamps = {k: old for k in t.rows}
    # f1 moves to p2 but p1's person row is (wrongly) left untouched.
    _seed_face(face_table, 'lib-A', 'f1', filename='1.jpg', personId='p2', confidence=0.9)
    second = _refresh('lib-A', 'v2')
    rows = {r['personId']: r for r in second.rows}
    assert rows['p1']['faceCount'] == 1 and rows['p1']['coverFaceId'] == 'f2'


def test_full_rebuild_is_forced_once_the_last_full_build_is_old(people_ctx, monkeypatch):
    person_table, face_table, blobs = people_ctx
    _seed_person(person_table, 'lib-A', 'p1', name='A', faceIds=json.dumps(['f1']))
    _seed_face(face_table, 'lib-A', 'f1', filename='a.jpg', personId='p1')
    _refresh('lib-A', 'v1')
    monkeypatch.setattr(storage_utils, '_PEOPLE_FULL_REBUILD_HOURS', 0.0)
    built = []
    real = storage_utils._build_user_people_index_snapshot
    monkeypatch.setattr(storage_utils, '_build_user_people_index_snapshot', lambda *a: (built.append(1), real(*a))[1])
    _refresh('lib-A', 'v2')
    assert built == [1]


def test_people_page_route_pages_filters_and_looks_up_ids(monkeypatch):
    from routes import people as people_routes
    rows = [{'personId': f'p{i:03d}', 'name': ('Ann %d' % i) if i < 5 else f'Unnamed {i}', 'isNamed': i < 5,
             'faceCount': i, 'coverFaceId': f'f{i}', 'coverFilename': 'a.jpg'} for i in range(300)]
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('u', None))
    monkeypatch.setattr(app, '_people_features_available', lambda: True)
    seen = []
    monkeypatch.setattr(app, 'get_user_people_index', lambda uid, **kw: (seen.append(kw), {'rows': rows})[1])

    def call(qs):
        with app.app.test_request_context('/api/persons/page?' + qs):
            return people_routes.people_page().get_json()

    first = call('limit=120')
    assert first['total'] == 300 and len(first['rows']) == 120 and first['hasMore'] and first['namedCount'] == 5
    last = call('offset=240&limit=120')
    assert len(last['rows']) == 60 and not last['hasMore'] and last['rows'][0]['personId'] == 'p240'
    assert [r['personId'] for r in call('q=ann%202')['rows']] == ['p002']
    assert [r['personId'] for r in call('ids=p007,p250')['rows']] == ['p007', 'p250']
    assert all(kw.get('copy_rows') is False for kw in seen)        # no per-request copy of every row


def test_get_person_with_a_blank_name_returns_the_faces_instead_of_500(monkeypatch):
    from routes import people as people_routes
    person_table, face_table = FakeTable(), FakeTable()
    _seed_person(person_table, 'u', 'p1', name='', faceIds=json.dumps(['f1', 'f2']))
    _seed_face(face_table, 'u', 'f1', filename='a.jpg', personId='p1', confidence=0.9, bbox='{}')
    _seed_face(face_table, 'u', 'f2', filename='b.jpg', personId='p1', confidence=0.5, bbox='{}')
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('u', None))
    monkeypatch.setattr(app, '_people_features_available', lambda: True)
    monkeypatch.setattr(app, 'person_table_client', person_table)
    monkeypatch.setattr(app, 'face_table_client', face_table)
    monkeypatch.setattr(app, '_load_user_face_summary_by_id', lambda uid: {r['RowKey']: r for r in face_table.rows.values()})
    monkeypatch.setattr(app, '_face_thumbnail_url', lambda *a, **k: '')
    with app.app.test_request_context('/api/persons/p1?offset=0&limit=120'):
        resp = people_routes.get_person('p1')
    body = resp.get_json()
    assert resp.status_code == 200 and body['name'] == 'Unnamed' and [f['faceId'] for f in body['faces']] == ['f1', 'f2']
    assert person_table.rows[('u', 'p1')]['name'] == ''          # nothing was written back
