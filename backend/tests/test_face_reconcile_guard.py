"""Unit tests for the force_reconcile guard on _store_client_face_entities.

A forced backfill (Tools > Backfill all photos) passes force_reconcile=True
so a fresh detection pass can delete stale rows it didn't re-detect -- correct
when detection genuinely ran this pass, wrong when it didn't really run at all
(throttled, deferred for a not-yet-ready model, or a real failure such as an
ipworker step crashing). Without gating force_reconcile on that distinction, a
single bad pass during a forced backfill could silently delete
previously-detected/curated faces for a photo the detector never actually
examined this time. These tests spy on _store_client_face_entities to confirm
force_reconcile is only ever True when detection genuinely ran.
"""
from __future__ import annotations

import json

import pytest

import storage_utils


class _ResourceNotFound(Exception):
    pass


class _FakeMetadataTable:
    def __init__(self) -> None:
        self.rows: dict = {}

    def upsert_entity(self, entity):
        self.rows[(entity['PartitionKey'], entity['RowKey'])] = dict(entity)

    def get_entity(self, partition_key, row_key):
        key = (partition_key, row_key)
        if key not in self.rows:
            raise _ResourceNotFound(f'{key} not found')
        return dict(self.rows[key])


def _seed_row(metadata: _FakeMetadataTable, user_id: str, filename: str, *, forced: bool, **overrides) -> None:
    processing_metadata = {'face': {'forced': True}} if forced else {}
    row = {
        'PartitionKey': user_id,
        'RowKey': filename,
        'face_status': 'running',
        'processing_metadata': json.dumps(processing_metadata),
        **overrides,
    }
    metadata.upsert_entity(row)


class _Calls:
    def __init__(self) -> None:
        self.force_reconcile_calls: list = []


@pytest.fixture
def reconcile_ctx(monkeypatch):
    metadata = _FakeMetadataTable()
    monkeypatch.setitem(storage_utils._CTX, 'metadata_table_client', metadata)
    monkeypatch.setitem(storage_utils._CTX, 'blob_service_client', object())
    monkeypatch.setattr(storage_utils, 'download_media_bytes', lambda kind, name: b'irrelevant')
    monkeypatch.setattr(storage_utils, 'refresh_user_vector_index', lambda *a, **k: None)
    monkeypatch.setattr(storage_utils, '_refresh_semantic_fields', lambda *a, **k: None)
    calls = _Calls()

    def _fake_store(user_id, filename, faces, *, force_reconcile=False):
        calls.force_reconcile_calls.append(force_reconcile)
        return []

    monkeypatch.setattr(storage_utils, '_store_client_face_entities', _fake_store)
    yield metadata, calls


def _apply_face_result(user_id, filename, face_result):
    storage_utils.apply_client_processing_results_for_file(
        user_id, filename,
        client_processing={'face': face_result},
        client_processing_report=[],
        client_asset_id='ipworker:job-1',
        origin='ipworker',
    )


def test_reconcile_enabled_when_forced_and_faces_found(reconcile_ctx):
    metadata, calls = reconcile_ctx
    user_id, filename = 'lib-A', 'photo.jpg'
    _seed_row(metadata, user_id, filename, forced=True)

    _apply_face_result(user_id, filename, {
        'hasData': True,
        'faces': [{
            'bbox': {'left': 0, 'top': 0, 'width': 50, 'height': 50},
            'confidence': 0.9,
            'imageWidth': 200, 'imageHeight': 200,
            'embedding': [0.1] * 8,
        }],
    })

    assert calls.force_reconcile_calls == [True]
    assert metadata.get_entity(user_id, filename)['face_status'] == 'done'


def test_reconcile_enabled_when_forced_and_genuinely_zero_faces(reconcile_ctx):
    """A real detection attempt that confirms zero faces (no failure stage,
    not throttled/transient) is authoritative -- reconcile should proceed."""
    metadata, calls = reconcile_ctx
    user_id, filename = 'lib-A', 'photo.jpg'
    _seed_row(metadata, user_id, filename, forced=True)

    _apply_face_result(user_id, filename, {'hasData': False, 'faces': [], 'rawFaceCount': 0})

    assert calls.force_reconcile_calls == [True]
    assert metadata.get_entity(user_id, filename)['face_status'] == 'no_data'


def test_reconcile_disabled_when_forced_but_face_failure_stage_set(reconcile_ctx):
    """The exact risk this guard closes: an ipworker step crash (or any real
    failure) during a forced backfill must not be treated as 'confirmed zero
    faces' -- that would delete previously-stored faces for a photo the
    detector never actually got to examine this pass."""
    metadata, calls = reconcile_ctx
    user_id, filename = 'lib-A', 'photo.jpg'
    _seed_row(metadata, user_id, filename, forced=True)

    _apply_face_result(user_id, filename, {
        'hasData': False, 'faces': [], 'rawFaceCount': 0,
        'faceFailureStage': 'unsupported_runtime',
        'faceFailureDetail': 'ipworker_decode_failed: bad bytes',
    })

    assert calls.force_reconcile_calls == [False]
    assert metadata.get_entity(user_id, filename)['face_status'] == 'failed'


def test_reconcile_disabled_when_forced_but_background_throttled(reconcile_ctx):
    metadata, calls = reconcile_ctx
    user_id, filename = 'lib-A', 'photo.jpg'
    _seed_row(metadata, user_id, filename, forced=True)

    _apply_face_result(user_id, filename, {
        'hasData': False, 'faces': [], 'rawFaceCount': 0,
        'deferredReason': 'background_throttled',
    })

    assert calls.force_reconcile_calls == [False]
    assert metadata.get_entity(user_id, filename)['face_status'] == 'pending'


def test_reconcile_disabled_when_forced_but_transient_timeout(reconcile_ctx):
    metadata, calls = reconcile_ctx
    user_id, filename = 'lib-A', 'photo.jpg'
    _seed_row(metadata, user_id, filename, forced=True)

    _apply_face_result(user_id, filename, {
        'hasData': False, 'faces': [], 'rawFaceCount': 0,
        'faceFailureStage': 'timeout',
    })

    assert calls.force_reconcile_calls == [False]
    assert metadata.get_entity(user_id, filename)['face_status'] == 'pending'


def test_reconcile_always_disabled_when_not_forced(reconcile_ctx):
    """Sanity check: an ordinary (non-backfill) pass never reconciles,
    regardless of the detection outcome."""
    metadata, calls = reconcile_ctx
    user_id, filename = 'lib-A', 'photo.jpg'
    _seed_row(metadata, user_id, filename, forced=False)

    _apply_face_result(user_id, filename, {'hasData': False, 'faces': [], 'rawFaceCount': 0})

    assert calls.force_reconcile_calls == [False]
