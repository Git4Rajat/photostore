"""Unit tests for _run_ipwork_steps's failure-path shapes (app.py).

storage_utils's face block only resolves face_status to a terminal state when
the payload includes 'faces' (even empty) -- see _step_locked_done/the
isinstance(faces, list) gate, and ipwork_face.process_face's own except
blocks (test_ipwork_face.py) for the same fix at the per-processor level.
These tests cover the three ways _run_ipwork_steps itself can produce a
failure shape for the 'face' step specifically: no processor registered, the
processor raising, and the processor returning something that isn't a dict.
"""
from __future__ import annotations

import pytest

import app


@pytest.fixture
def steps_ctx(monkeypatch):
    monkeypatch.setattr(app, '_get_metadata_entity', lambda user_id, filename: {})
    monkeypatch.setattr(app, 'download_media_bytes', lambda kind, name: b'fake-image-bytes')
    monkeypatch.setitem(app.IPWORK_STEP_PROCESSORS, 'face', None)
    yield


def test_missing_processor_produces_diagnosable_face_shape(monkeypatch, steps_ctx):
    monkeypatch.delitem(app.IPWORK_STEP_PROCESSORS, 'face', raising=False)

    result = app._run_ipwork_steps('lib-A', 'photo.jpg', ['face'])

    assert result['face']['hasData'] is False
    assert result['face']['faces'] == []
    assert result['face']['faceFailureStage'] == 'unsupported_runtime'
    assert result['face']['error'] == 'not_implemented'


def test_processor_exception_produces_diagnosable_face_shape(monkeypatch, steps_ctx):
    def _boom(user_id, filename, image_bytes):
        raise RuntimeError('face model crashed')

    monkeypatch.setitem(app.IPWORK_STEP_PROCESSORS, 'face', _boom)

    result = app._run_ipwork_steps('lib-A', 'photo.jpg', ['face'])

    assert result['face']['hasData'] is False
    assert result['face']['faces'] == []
    assert result['face']['faceFailureStage'] == 'unsupported_runtime'
    assert 'face model crashed' in result['face']['error']


def test_non_dict_result_produces_diagnosable_face_shape(monkeypatch, steps_ctx):
    monkeypatch.setitem(app.IPWORK_STEP_PROCESSORS, 'face', lambda user_id, filename, image_bytes: None)

    result = app._run_ipwork_steps('lib-A', 'photo.jpg', ['face'])

    assert result['face']['hasData'] is False
    assert result['face']['faces'] == []
    assert result['face']['error'] == 'invalid_result_shape'


def test_missing_processor_for_non_face_step_omits_faces_key(monkeypatch, steps_ctx):
    # Non-face steps (ocr/exif/map_detection/thumbnail/ai_vision) resolve via a
    # flat hasData check in storage_utils, not a 'faces' list -- confirms the
    # face-specific shape isn't applied where it isn't needed.
    monkeypatch.delitem(app.IPWORK_STEP_PROCESSORS, 'ocr', raising=False)

    result = app._run_ipwork_steps('lib-A', 'photo.jpg', ['ocr'])

    assert result['ocr'] == {'hasData': False, 'error': 'not_implemented'}
