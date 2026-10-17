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

import io

import pytest
from PIL import Image

import app


def _tiny_jpeg() -> bytes:
    buffer = io.BytesIO()
    Image.new('RGB', (8, 8), 'white').save(buffer, 'JPEG')
    return buffer.getvalue()


@pytest.fixture
def steps_ctx(monkeypatch):
    monkeypatch.setattr(app, '_get_metadata_entity', lambda user_id, filename: {})
    # A genuinely decodable source so the shared canonical-decode step passes and
    # a visual step's registered processor is actually reached (the undecodable
    # path is covered separately below).
    monkeypatch.setattr(app, 'download_media_bytes', lambda kind, name: _tiny_jpeg())
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
    # The raw exception text is logged server-side (worker_logger.exception)
    # but not handed back in the shape -- it can carry internal paths/details
    # that shouldn't reach the client, even the account's own owner.
    assert result['face']['error'] == 'processing_failed'


def test_non_dict_result_produces_diagnosable_face_shape(monkeypatch, steps_ctx):
    monkeypatch.setitem(app.IPWORK_STEP_PROCESSORS, 'face', lambda user_id, filename, image_bytes: None)

    result = app._run_ipwork_steps('lib-A', 'photo.jpg', ['face'])

    assert result['face']['hasData'] is False
    assert result['face']['faces'] == []
    assert result['face']['error'] == 'invalid_result_shape'


def test_undecodable_source_fails_all_visual_steps_uniformly(monkeypatch, steps_ctx):
    # #1: when the one shared visual source can't be decoded by the most-capable
    # path (convert_image_to_jpeg yields no usable JPEG -- the CRW/exotic-RAW
    # case), every visual step reports the SAME 'source_undecodable' reason and
    # no per-step processor runs -- instead of each step re-decoding the raw
    # original and failing with its own unrelated error.
    import image_utils
    monkeypatch.setattr(image_utils, 'convert_image_to_jpeg', lambda data, name='': b'\x00not-a-jpeg')
    ran = []
    monkeypatch.setitem(app.IPWORK_STEP_PROCESSORS, 'thumbnail', lambda *a: ran.append('thumbnail') or {'hasData': True})
    monkeypatch.setitem(app.IPWORK_STEP_PROCESSORS, 'ocr', lambda *a: ran.append('ocr') or {'hasData': True})
    monkeypatch.setitem(app.IPWORK_STEP_PROCESSORS, 'face', lambda *a: ran.append('face') or {'hasData': True})
    monkeypatch.setitem(app.IPWORK_STEP_PROCESSORS, 'ai_vision', lambda *a: ran.append('ai_vision') or {'hasData': True})

    result = app._run_ipwork_steps('lib-A', 'photo.raw', ['thumbnail', 'ocr', 'face', 'ai_vision'])

    assert ran == []  # no visual processor decodes an undecodable source
    for step in ('thumbnail', 'ocr', 'ai_vision'):
        assert result[step] == {'hasData': False, 'error': 'source_undecodable'}
    assert result['face']['hasData'] is False
    assert result['face']['faces'] == []
    assert result['face']['faceFailureStage'] == 'unsupported_runtime'
    assert result['face']['error'] == 'source_undecodable'


def test_decodable_source_still_reaches_each_visual_processor(monkeypatch, steps_ctx):
    # Guard against over-eager short-circuiting: a decodable source must still
    # run every visual processor on the one shared canonical image.
    ran = []
    monkeypatch.setitem(app.IPWORK_STEP_PROCESSORS, 'ocr', lambda *a: ran.append('ocr') or {'hasData': True, 'text': 'hi'})
    monkeypatch.setitem(app.IPWORK_STEP_PROCESSORS, 'face', lambda *a: ran.append('face') or {'hasData': False, 'faces': []})

    result = app._run_ipwork_steps('lib-A', 'photo.jpg', ['ocr', 'face'])

    assert ran == ['ocr', 'face']
    assert result['ocr']['hasData'] is True
    assert 'error' not in result['ocr']


def test_missing_processor_for_non_face_step_omits_faces_key(monkeypatch, steps_ctx):
    # Non-face steps (ocr/exif/map_detection/thumbnail/ai_vision) resolve via a
    # flat hasData check in storage_utils, not a 'faces' list -- confirms the
    # face-specific shape isn't applied where it isn't needed.
    monkeypatch.delitem(app.IPWORK_STEP_PROCESSORS, 'ocr', raising=False)

    result = app._run_ipwork_steps('lib-A', 'photo.jpg', ['ocr'])

    assert result['ocr'] == {'hasData': False, 'error': 'not_implemented'}
