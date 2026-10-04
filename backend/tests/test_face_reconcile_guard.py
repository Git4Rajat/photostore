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

    def update_entity(self, entity, mode=None, *, etag=None, match_condition=None):
        key = (entity['PartitionKey'], entity['RowKey'])
        if key not in self.rows:
            raise _ResourceNotFound(f'{key} not found')
        self.rows[key] = dict(entity)

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

    assert calls.force_reconcile_calls == []
    assert metadata.get_entity(user_id, filename)['face_status'] == 'failed'


def test_reconcile_disabled_when_forced_but_background_throttled(reconcile_ctx):
    metadata, calls = reconcile_ctx
    user_id, filename = 'lib-A', 'photo.jpg'
    _seed_row(metadata, user_id, filename, forced=True)

    _apply_face_result(user_id, filename, {
        'hasData': False, 'faces': [], 'rawFaceCount': 0,
        'deferredReason': 'background_throttled',
    })

    assert calls.force_reconcile_calls == []
    assert metadata.get_entity(user_id, filename)['face_status'] == 'pending'


def test_reconcile_disabled_when_forced_but_transient_timeout(reconcile_ctx):
    metadata, calls = reconcile_ctx
    user_id, filename = 'lib-A', 'photo.jpg'
    _seed_row(metadata, user_id, filename, forced=True)

    _apply_face_result(user_id, filename, {
        'hasData': False, 'faces': [], 'rawFaceCount': 0,
        'faceFailureStage': 'timeout',
    })

    assert calls.force_reconcile_calls == []
    assert metadata.get_entity(user_id, filename)['face_status'] == 'pending'


def test_reconcile_always_disabled_when_not_forced(reconcile_ctx):
    """Sanity check: an ordinary (non-backfill) pass never reconciles,
    regardless of the detection outcome."""
    metadata, calls = reconcile_ctx
    user_id, filename = 'lib-A', 'photo.jpg'
    _seed_row(metadata, user_id, filename, forced=False)

    _apply_face_result(user_id, filename, {'hasData': False, 'faces': [], 'rawFaceCount': 0})

    assert calls.force_reconcile_calls == [False]


def _valid_face(left=0):
    return {'bbox': {'left': left, 'top': 0, 'width': 10, 'height': 10},
            'confidence': 0.9, 'imageWidth': 200, 'imageHeight': 200, 'embedding': [0.1] * 8}


@pytest.mark.parametrize('extra,expected_stage', [
    ({}, 'postprocessing_failed'),
    ({'filteredReason': 'quality_filter_rejected', 'filteredFaceCount': 2}, 'quality_filter_rejected'),
    ({'faceFailureStage': 'postprocessing_failed'}, 'postprocessing_failed'),
])
def test_detected_empty_failure_preserves_previous_photo_faces(reconcile_ctx, extra, expected_stage):
    metadata, calls = reconcile_ctx
    prior_faces = json.dumps([{'bbox': _valid_face()['bbox'], 'personId': 'curated-person'}])
    _seed_row(metadata, 'lib-A', 'photo.jpg', forced=True, faces=prior_faces, faceCount=1,
              processing_lease_owner='worker', processing_lease='lease', processing_lease_expires_at='later')
    _apply_face_result('lib-A', 'photo.jpg', {'faces': [], 'rawFaceCount': 2, 'hasData': False, **extra})
    row = metadata.get_entity('lib-A', 'photo.jpg')
    assert row['faces'] == prior_faces
    assert row['faceCount'] == 1
    assert row['face_status'] == 'failed'
    assert row['processing_lease_owner'] == row['processing_lease'] == row['processing_lease_expires_at'] == ''
    assert calls.force_reconcile_calls == []
    summary = json.loads(row['processing_metadata'])['client_face']
    assert summary['faceFailureStage'] == expected_stage
    assert summary['rawFaceCount'] == 2
    assert summary['embeddingMissing'] is True


@pytest.mark.parametrize('extra', [
    {'faceFailureStage': 'postprocessing_failed'},
    {'filteredFaceCount': 1},
    {'faceDiagnostics': {'failureCount': 1}},
    {},
])
def test_partial_outcome_never_forced_deletes_and_preserves_metadata(reconcile_ctx, extra):
    metadata, calls = reconcile_ctx
    previous = [{'bbox': _valid_face()['bbox'], 'personId': 'matched'},
                {'bbox': _valid_face(100)['bbox'], 'personId': 'absent-curated'}]
    _seed_row(metadata, 'lib-A', 'photo.jpg', forced=True, faces=json.dumps(previous), faceCount=2)
    _apply_face_result('lib-A', 'photo.jpg', {'faces': [_valid_face()], 'rawFaceCount': 2, 'hasData': True, **extra})
    row = metadata.get_entity('lib-A', 'photo.jpg')
    assert calls.force_reconcile_calls == [False]
    assert row['face_status'] == 'failed'
    assert row['faceCount'] == 2
    assert len(json.loads(row['faces'])) == 2
    assert json.loads(row['faces'])[1]['personId'] == 'absent-curated'


@pytest.mark.parametrize('face', [
    _valid_face() | {'embedding': None},
    _valid_face() | {'embedding': [float('nan')]},
    _valid_face() | {'embedding': [float('inf')]},
    _valid_face() | {'embedding': [0, 0]},
    _valid_face() | {'confidence': 0.1},
])
def test_backend_rejected_candidates_are_failed_not_zero(reconcile_ctx, face):
    metadata, calls = reconcile_ctx
    _seed_row(metadata, 'lib-A', 'photo.jpg', forced=True, faceCount=3, faces='[{"personId":"prior"}]')
    _apply_face_result('lib-A', 'photo.jpg', {'faces': [face], 'rawFaceCount': 1})
    row = metadata.get_entity('lib-A', 'photo.jpg')
    assert calls.force_reconcile_calls == []
    assert row['face_status'] == 'failed'
    assert row['faceCount'] == 3
    assert row['faces'] == '[{"personId":"prior"}]'
    summary = json.loads(row['processing_metadata'])['client_face']
    assert summary['faceFailureStage'] == ('quality_filter_rejected' if face['confidence'] == 0.1 else 'postprocessing_failed')
    assert summary['backendRejectDiagnostic']
    assert summary['faceDiagnostics']['backendAcceptedCount'] == 0


def test_diagnostics_persist_only_bounded_allowlist(reconcile_ctx):
    metadata, calls = reconcile_ctx
    _seed_row(metadata, 'lib-A', 'photo.jpg', forced=True)
    _apply_face_result('lib-A', 'photo.jpg', {
        'faces': [], 'rawFaceCount': 1,
        'faceDiagnostics': {
            'failureCount': 10**100, 'embeddedCount': -10, 'imageWidth': 64,
            'detectorModelReady': True, 'embeddingModelReady': False,
            'detectorOutputShape': [1, 5, 8400, 7, 8],
            'reasonCounts': {'embedding_exception': 2, 'arbitrary-secret': 999},
            'stageTimingsMs': {'total': 10**20, 'embed': float('nan'), 'decode': 1.23456, 'arbitrary': 2},
            'vectors': [[1] * 512], 'perFace': ['unbounded'] * 5000,
        },
        'debugStages': ['x' * 100] * 100,
        'faceFailureDetail': 'detail' * 1000,
    })
    summary = json.loads(metadata.get_entity('lib-A', 'photo.jpg')['processing_metadata'])['client_face']
    metrics = summary['faceDiagnostics']
    assert metrics['failureCount'] == 1000000
    assert metrics['embeddedCount'] == 0
    assert metrics['detectorOutputShape'] == [1, 5, 8400, 7]
    assert metrics['reasonCounts'] == {'embedding_exception': 2}
    assert metrics['stageTimingsMs'] == {'total': 86400000, 'decode': 1.235}
    assert 'vectors' not in metrics and 'perFace' not in metrics
    assert len(summary['faceFailureDetail']) <= 512
    assert len(summary['debugStages']) == 16
    assert all(len(v) <= 64 for v in summary['debugStages'])


@pytest.mark.parametrize('result', [
    {'rawFaceCount': 2, 'hasData': False},
    {'faces': None, 'rawFaceCount': 2},
    {'faces': [], 'rawFaceCount': 0, 'faceModelReady': False},
])
def test_missing_face_list_or_unready_detector_cannot_confirm_zero(reconcile_ctx, result):
    metadata, calls = reconcile_ctx
    _seed_row(metadata, 'lib-A', 'photo.jpg', forced=True, faceCount=1, faces='[{"personId":"saved"}]')
    _apply_face_result('lib-A', 'photo.jpg', result)
    row = metadata.get_entity('lib-A', 'photo.jpg')
    assert row['face_status'] == 'failed'
    assert row['faceCount'] == 1
    assert row['faces'] == '[{"personId":"saved"}]'
    assert calls.force_reconcile_calls == []
