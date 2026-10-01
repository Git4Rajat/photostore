"""Coverage for the people-embedding-index blob: a durable counterpart to
app.py's in-process _load_people_embedding_index/_people_embedding_index_cache
-- a person-level rep-embedding index for worker's clustering matcher. Not
to be confused with the photo-level vector index (search) or the People-page
listing index (touch_user_people_index_state, names/cover photos, no
embeddings). Deliberately simpler than the photo vector index: a full
rebuild every time, no incremental per-person dirty-set merge, since a full
scan is already cheap at real-world person-count scale.
"""
from __future__ import annotations

import json
import threading

import pytest

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


def _seed_person(table, user_id: str, person_id: str, face_ids, rep, name: str = '') -> None:
    table.upsert_entity({
        'PartitionKey': user_id, 'RowKey': person_id,
        'name': name, 'faceIds': json.dumps(face_ids), 'repEmbedding': json.dumps(rep),
    })


def _seed_face(table, user_id: str, face_id: str, person_id: str, *, rejected=False) -> None:
    table.upsert_entity({
        'PartitionKey': user_id, 'RowKey': face_id,
        'personId': person_id, 'rejected': rejected,
    })


@pytest.fixture
def ctx(monkeypatch):
    person_table = FakeTable()
    face_table = FakeTable()
    blob_service = _FakeBlobServiceClient()
    monkeypatch.setitem(storage_utils._CTX, 'person_table_client', person_table)
    monkeypatch.setitem(storage_utils._CTX, 'face_table_client', face_table)
    monkeypatch.setitem(storage_utils._CTX, 'blob_service_client', blob_service)
    monkeypatch.setitem(storage_utils._CTX, 'blob_people_embedding_index_container', 'people-embedding-index')
    yield person_table, face_table, blob_service


# --- snapshot build -----------------------------------------------------

def test_build_snapshot_includes_person_with_active_face(ctx):
    person_table, face_table, _blobs = ctx
    _seed_person(person_table, 'u1', 'p1', ['f1'], [1.0, 0.0], name='Alice')
    _seed_face(face_table, 'u1', 'f1', 'p1')

    snapshot = storage_utils._build_user_people_embedding_index_snapshot('u1', 'v1')

    assert snapshot.person_ids == ['p1']
    assert snapshot.people_meta == [{'name': 'Alice', 'faceIds': ['f1']}]
    assert snapshot.embeddings.shape == (1, 2)
    assert list(snapshot.embeddings[0]) == [1.0, 0.0]


def test_build_snapshot_excludes_person_with_zero_active_faces(ctx):
    """A face claims personId='p1' but is rejected -- not a real active
    face, so p1 must not appear (mirrors app.py's _active_face_ids_for_person
    cross-verification)."""
    person_table, face_table, _blobs = ctx
    _seed_person(person_table, 'u1', 'p1', ['f1'], [1.0, 0.0])
    _seed_face(face_table, 'u1', 'f1', 'p1', rejected=True)

    snapshot = storage_utils._build_user_people_embedding_index_snapshot('u1', 'v1')
    assert snapshot.person_ids == []


def test_build_snapshot_excludes_person_whose_face_points_to_someone_else(ctx):
    """Person row claims f1, but f1's own personId says otherwise -- the
    same orphan-detection shape as app.py's cross-verification."""
    person_table, face_table, _blobs = ctx
    _seed_person(person_table, 'u1', 'p1', ['f1'], [1.0, 0.0])
    _seed_face(face_table, 'u1', 'f1', 'p2')  # points elsewhere

    snapshot = storage_utils._build_user_people_embedding_index_snapshot('u1', 'v1')
    assert snapshot.person_ids == []


def test_build_snapshot_excludes_person_with_no_rep_embedding(ctx):
    person_table, face_table, _blobs = ctx
    _seed_person(person_table, 'u1', 'p1', ['f1'], [])
    _seed_face(face_table, 'u1', 'f1', 'p1')

    snapshot = storage_utils._build_user_people_embedding_index_snapshot('u1', 'v1')
    assert snapshot.person_ids == []


def test_build_snapshot_empty_library_returns_empty_snapshot(ctx):
    snapshot = storage_utils._build_user_people_embedding_index_snapshot('u1', 'v1')
    assert snapshot.person_ids == []
    assert snapshot.embeddings.shape == (0, 0)


def test_build_snapshot_aligns_mixed_embedding_dimensions(ctx):
    person_table, face_table, _blobs = ctx
    _seed_person(person_table, 'u1', 'p1', ['f1'], [1.0, 2.0, 3.0])
    _seed_face(face_table, 'u1', 'f1', 'p1')
    _seed_person(person_table, 'u1', 'p2', ['f2'], [4.0])
    _seed_face(face_table, 'u1', 'f2', 'p2')

    snapshot = storage_utils._build_user_people_embedding_index_snapshot('u1', 'v1')
    assert snapshot.embeddings.shape == (2, 3)


# --- serialize / round trip ----------------------------------------------

def test_serialize_and_load_npz_round_trips(ctx):
    person_table, face_table, _blobs = ctx
    _seed_person(person_table, 'u1', 'p1', ['f1'], [1.0, 0.5], name='Alice')
    _seed_face(face_table, 'u1', 'f1', 'p1')
    snapshot = storage_utils._build_user_people_embedding_index_snapshot('u1', 'v1')

    blob_client = storage_utils._people_embedding_index_blob_client(
        storage_utils._people_embedding_index_npz_blob_name('u1')
    )
    blob_client.upload_blob(storage_utils._serialize_people_embedding_index(snapshot))

    loaded = storage_utils._load_people_embedding_index_npz('u1')
    assert loaded is not None
    assert loaded.person_ids == ['p1']
    assert loaded.people_meta == [{'name': 'Alice', 'faceIds': ['f1']}]
    assert list(loaded.embeddings[0]) == [1.0, 0.5]


# --- dirty-mark dedup -----------------------------------------------------

def test_touch_writes_manifest_once_per_dirty_period(ctx):
    _person_table, _face_table, blobs = ctx
    storage_utils.touch_user_people_embedding_index_state('u1')
    manifest_blob = storage_utils._people_embedding_index_blob_client(
        storage_utils._people_embedding_index_manifest_blob_name('u1')
    )
    first = dict(json.loads(blobs.blobs[list(blobs.blobs.keys())[0]]))
    assert first['dirty'] is True

    # A second touch before any refresh clears the flag must be a no-op --
    # same _manifest_already_marked_dirty dedup the photo vector index uses.
    result = storage_utils.touch_user_people_embedding_index_state('u1')
    assert result == ''


# --- refresh / get full lifecycle -----------------------------------------

def test_refresh_writes_blob_and_manifest_and_clears_dirty(ctx):
    person_table, face_table, blobs = ctx
    _seed_person(person_table, 'u1', 'p1', ['f1'], [1.0, 0.0], name='Alice')
    _seed_face(face_table, 'u1', 'f1', 'p1')
    storage_utils.touch_user_people_embedding_index_state('u1')

    snapshot = storage_utils.refresh_user_people_embedding_index('u1')

    assert snapshot.person_ids == ['p1']
    manifest = storage_utils._load_people_embedding_index_manifest('u1')
    assert manifest['dirty'] is False
    assert manifest['rowCount'] == 1
    npz = storage_utils._load_people_embedding_index_npz('u1')
    assert npz.person_ids == ['p1']


def test_get_user_people_embedding_index_uses_warm_cache_without_reloading_blob(ctx):
    person_table, face_table, _blobs = ctx
    _seed_person(person_table, 'u1', 'p1', ['f1'], [1.0, 0.0])
    _seed_face(face_table, 'u1', 'f1', 'p1')
    storage_utils.refresh_user_people_embedding_index('u1')

    # Delete the underlying table data -- a cache hit must not need to
    # rescan to answer the next call.
    person_table.rows.clear()
    result = storage_utils.get_user_people_embedding_index('u1')
    assert result is not None
    assert result['person_ids'] == ['p1']


def test_get_user_people_embedding_index_serves_stale_snapshot_when_dirty_without_blocking(ctx):
    person_table, face_table, _blobs = ctx
    _seed_person(person_table, 'u1', 'p1', ['f1'], [1.0, 0.0])
    _seed_face(face_table, 'u1', 'f1', 'p1')
    storage_utils.refresh_user_people_embedding_index('u1')
    storage_utils.touch_user_people_embedding_index_state('u1')
    storage_utils.invalidate_user_people_embedding_index_cache('u1')
    _seed_person(person_table, 'u1', 'p2', ['f2'], [0.0, 1.0])
    _seed_face(face_table, 'u1', 'f2', 'p2')

    # allow_refresh=False must serve the last-known-good snapshot immediately
    # instead of forcing a synchronous rebuild when merely dirty -- that
    # synchronous rebuild (a full person+face scan) is exactly what used to
    # run inline in the clustering worker's message loop on every cache-TTL
    # expiry, since real traffic keeps this dirty nearly continuously.
    result = storage_utils.get_user_people_embedding_index('u1', allow_refresh=False)
    assert result is not None
    assert set(result['person_ids']) == {'p1'}  # stale: p2 not folded in yet

    for thread in threading.enumerate():
        if thread.name == 'people-embedding-index-rebuild':
            thread.join(timeout=5)


def test_get_user_people_embedding_index_background_refresh_picks_up_new_person(ctx):
    person_table, face_table, _blobs = ctx
    _seed_person(person_table, 'u1', 'p1', ['f1'], [1.0, 0.0])
    _seed_face(face_table, 'u1', 'f1', 'p1')
    storage_utils.refresh_user_people_embedding_index('u1')
    storage_utils.touch_user_people_embedding_index_state('u1')
    storage_utils.invalidate_user_people_embedding_index_cache('u1')
    _seed_person(person_table, 'u1', 'p2', ['f2'], [0.0, 1.0])
    _seed_face(face_table, 'u1', 'f2', 'p2')

    storage_utils.get_user_people_embedding_index('u1', allow_refresh=False)
    for thread in threading.enumerate():
        if thread.name == 'people-embedding-index-rebuild':
            thread.join(timeout=5)

    result = storage_utils.get_user_people_embedding_index('u1', allow_refresh=False)
    assert result is not None
    assert set(result['person_ids']) == {'p1', 'p2'}


def test_delete_user_people_embedding_index_data_clears_blobs_and_cache(ctx):
    person_table, face_table, blobs = ctx
    _seed_person(person_table, 'u1', 'p1', ['f1'], [1.0, 0.0])
    _seed_face(face_table, 'u1', 'f1', 'p1')
    storage_utils.refresh_user_people_embedding_index('u1')
    assert blobs.blobs

    storage_utils.delete_user_people_embedding_index_data('u1')
    assert blobs.blobs == {}
    assert storage_utils.get_user_people_embedding_index('u1', allow_refresh=False) is None
