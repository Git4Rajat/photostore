"""Unit tests for ipwork_face.py's pure geometry helpers (similarity-transform
solve, plausibility guard, NMS) -- the parts of the ported face pipeline that
don't need the real onnx/mediapipe models to verify. End-to-end correctness
against the real bundled weights was validated separately (see the
module-level docstring in ipwork_face.py) using a real face photo, not
project data; that validation isn't repeatable here as a fast unit test since
it needs the actual model files and a Python face-recognition stack most
contributors won't have installed (opencv/onnxruntime/mediapipe are
ipworker-only deps, not in backend/requirements.txt).
"""
from __future__ import annotations

import numpy as np
import pytest
import io
from types import SimpleNamespace
from PIL import Image

cv2 = pytest.importorskip('cv2')
pytest.importorskip('onnxruntime')

import ipwork_face as face_mod  # noqa: E402


def test_solve_similarity_transform_recovers_known_scale_rotation_translation():
    # Ground truth: scale 2, rotate 90 degrees, translate by (10, 5).
    a, b = 0.0, 2.0  # scale*cos(90)=0, scale*sin(90)=2
    tx, ty = 10.0, 5.0
    src = np.array([[0.0, 0.0], [1.0, 0.0], [0.0, 1.0], [1.0, 1.0], [2.0, 1.0]])
    dst = np.array([
        [a * x - b * y + tx, b * x + a * y + ty]
        for x, y in src
    ])
    solved = face_mod.solve_similarity_transform(src, dst)
    assert solved is not None
    sa, sb, stx, sty = solved
    assert sa == pytest.approx(a, abs=1e-6)
    assert sb == pytest.approx(b, abs=1e-6)
    assert stx == pytest.approx(tx, abs=1e-6)
    assert sty == pytest.approx(ty, abs=1e-6)


def test_solve_similarity_transform_returns_none_for_insufficient_points():
    assert face_mod.solve_similarity_transform(np.array([[0.0, 0.0]]), np.array([[1.0, 1.0]])) is None


def test_is_plausible_transform_accepts_scale_within_calibrated_range():
    # A real source-image-space-to-112px-template solve typically lands in
    # ARC_FACE_MIN_SCALE..ARC_FACE_MAX_SCALE (0.03-0.6) -- confirmed against a
    # real photo (see module docstring), not just asserted here.
    mid_scale = (face_mod.ARC_FACE_MIN_SCALE + face_mod.ARC_FACE_MAX_SCALE) / 2
    assert face_mod._is_plausible_transform(mid_scale, 0.0) is True


@pytest.mark.parametrize('scale', [0.001, 5.0])
def test_is_plausible_transform_rejects_out_of_range_scale(scale):
    assert face_mod._is_plausible_transform(scale, 0.0) is False


def test_is_plausible_transform_rejects_extreme_rotation():
    # scale=0.2 (in-range), rotation=90 degrees (out of the 60-degree bound).
    assert face_mod._is_plausible_transform(0.0, 0.2) is False


def test_nms_keeps_highest_scoring_non_overlapping_boxes():
    boxes = [
        {'left': 0, 'top': 0, 'width': 100, 'height': 100, 'score': 0.9},
        {'left': 5, 'top': 5, 'width': 100, 'height': 100, 'score': 0.5},  # heavily overlaps box 0
        {'left': 500, 'top': 500, 'width': 100, 'height': 100, 'score': 0.6},  # disjoint
    ]
    kept = face_mod._nms(boxes, iou_threshold=0.45)
    assert len(kept) == 2
    assert {b['score'] for b in kept} == {0.9, 0.6}


def test_compute_padded_crop_bounds_clamps_to_image_size():
    bounds = face_mod._compute_padded_crop_bounds(
        {'left': 0, 'top': 0, 'width': 50, 'height': 50}, 0.25, width=60, height=60,
    )
    assert bounds is not None
    crop_left, crop_top, crop_w, crop_h = bounds
    assert crop_left == 0 and crop_top == 0
    assert crop_left + crop_w <= 60
    assert crop_top + crop_h <= 60


# --- process_face's failure-path shape (no real models needed) -------------
#
# storage_utils._apply_client_processing_results' face block only resolves
# face_status to a terminal state when the payload includes 'faces' (even
# empty) -- see _step_locked_done/the isinstance(faces, list) gate. These
# confirm both of process_face's own except blocks produce that shape,
# rather than a bare {'hasData': False, 'error': ...} that would leave
# face_status stuck at 'running' forever.

def test_process_face_returns_diagnosable_shape_on_decode_failure():
    result = face_mod.process_face('lib-A', 'photo.jpg', b'not an image')
    assert result['hasData'] is False
    assert result['faces'] == []
    assert result['rawFaceCount'] == 0
    assert result['faceFailureStage'] == 'unsupported_runtime'
    assert 'error' in result


# --- RAW (CR3/etc.) decode routing ------------------------------------------
#
# PIL has no generic codec for RAW containers -- Image.open() on the raw bytes
# raises 'cannot identify image file'. Confirmed live in production: every
# CR3 upload in a real backlog had ai_vision_status/face_status stuck
# 'failed' with exactly that error, 0/173 non-RAW files affected.
# _decodable_image_bytes must route RAW extensions through
# image_utils.extract_raw_preview_bytes (the same helper ipwork_thumbnail.py
# already uses successfully) before anything touches PIL.

def test_decodable_image_bytes_passes_through_non_raw_unchanged():
    original = b'plain-jpeg-bytes'
    assert face_mod._decodable_image_bytes(original, 'photo.jpg') == original


def test_decodable_image_bytes_extracts_preview_for_raw_extension(monkeypatch):
    calls = []

    def fake_extract(image_bytes, filename):
        calls.append((image_bytes, filename))
        return b'decodable-jpeg-preview'

    monkeypatch.setattr(face_mod, 'extract_raw_preview_bytes', fake_extract)

    result = face_mod._decodable_image_bytes(b'raw-cr3-bytes', 'IMG_0036.cr3')

    assert result == b'decodable-jpeg-preview'
    assert calls == [(b'raw-cr3-bytes', 'IMG_0036.cr3')]


def test_decodable_image_bytes_falls_back_to_original_when_extraction_fails(monkeypatch):
    monkeypatch.setattr(face_mod, 'extract_raw_preview_bytes', lambda image_bytes, filename: None)

    result = face_mod._decodable_image_bytes(b'raw-cr3-bytes', 'IMG_0036.cr3')

    assert result == b'raw-cr3-bytes'


def test_process_face_decodes_raw_extension_via_extracted_preview(monkeypatch):
    # End-to-end through process_face's own Image.open call: raw bytes alone
    # would raise (as in the decode-failure test above), but routing a CR3
    # filename through a stubbed extractor that returns a real JPEG lets
    # decode succeed -- proving process_face actually calls
    # _decodable_image_bytes rather than only ipwork_thumbnail.py having the
    # RAW-aware path.
    from PIL import Image
    import io
    buffer = io.BytesIO()
    Image.new('RGB', (64, 64), color=(10, 20, 30)).save(buffer, format='JPEG')
    real_jpeg = buffer.getvalue()

    monkeypatch.setattr(face_mod, 'extract_raw_preview_bytes', lambda image_bytes, filename: real_jpeg)
    monkeypatch.setattr(face_mod, 'YOLO_FACE_MODEL_PATH', '/nonexistent/missing-model.onnx')
    monkeypatch.setattr(face_mod, '_yolo_session', None)

    result = face_mod.process_face('lib-A', 'IMG_0036.cr3', b'not-a-real-raw-container')

    # Decode succeeded (no longer 'ipworker_decode_failed'); it fails later at
    # detection only because no real ONNX model is available in this test env.
    assert 'detection_failed' in result['error']


# --- top-level model provenance on success/no-detections -------------------
#
# storage_utils._client_model_provenance only reads model/modelVersion/
# modelTaxonomyVersion/runtime from the TOP LEVEL of a step's result, not
# from inside individual face entries -- that's what gets copied into
# processing_metadata's client_face.modelTaxonomyVersion summary, which
# _browser_processing_face_version_stale (app.py) reads to decide whether a
# photo needs re-embedding. Before this fix, process_face's return value only
# carried modelTaxonomyVersion per-face, so client_face.modelTaxonomyVersion
# was always left blank for ipworker-processed photos -- permanently "stale"
# even immediately after a successful re-embed, causing ipworker to re-detect
# the same photos on every sweep cycle forever.

def test_process_face_no_detections_still_reports_top_level_model_version(monkeypatch):
    monkeypatch.setattr(face_mod, 'detect_faces', lambda image_bgr: [])

    from PIL import Image
    import io
    buffer = io.BytesIO()
    Image.new('RGB', (64, 64), color=(10, 20, 30)).save(buffer, format='JPEG')

    result = face_mod.process_face('lib-A', 'photo.jpg', buffer.getvalue())

    assert result['hasData'] is False
    assert result['faces'] == []
    assert result['modelTaxonomyVersion'] == face_mod.FACE_EMBEDDING_MODEL_TAXONOMY_VERSION
    assert result['runtime'] == face_mod.FACE_EMBEDDING_RUNTIME


def test_process_face_returns_diagnosable_shape_on_detection_failure(tmp_path, monkeypatch):
    # A real decodable image, but no ONNX model file exists at the configured
    # path in this test environment -- _get_yolo_session's InferenceSession
    # construction fails, exercising the second except block specifically.
    monkeypatch.setattr(face_mod, 'YOLO_FACE_MODEL_PATH', str(tmp_path / 'missing-model.onnx'))
    monkeypatch.setattr(face_mod, '_yolo_session', None)

    from PIL import Image
    import io
    buffer = io.BytesIO()
    Image.new('RGB', (64, 64), color=(10, 20, 30)).save(buffer, format='JPEG')

    result = face_mod.process_face('lib-A', 'photo.jpg', buffer.getvalue())
    assert result['hasData'] is False
    assert result['faces'] == []
    assert result['rawFaceCount'] == 0
    assert result['faceFailureStage'] == 'unsupported_runtime'
    assert 'detection_failed' in result['error']


@pytest.fixture
def image_bytes():
    buffer = io.BytesIO()
    Image.new('RGB', (64, 64), color=(10, 20, 30)).save(buffer, format='JPEG')
    return buffer.getvalue()


@pytest.fixture
def candidate_pipeline(monkeypatch):
    box = {'left': 5., 'top': 5., 'width': 30., 'height': 30., 'score': 0.9}
    monkeypatch.setattr(face_mod, 'detect_faces', lambda image: [dict(box)])
    monkeypatch.setattr(face_mod, 'detect_five_landmarks', lambda *args: np.ones((5, 2)))
    monkeypatch.setattr(face_mod, 'crop_and_align_face', lambda *args: (np.zeros((112, 112, 3), dtype=np.uint8), 'landmark-5pt-mp'))
    monkeypatch.setattr(face_mod, 'compute_face_embedding', lambda *args: np.ones(512, dtype=np.float32) / np.sqrt(512))
    return box


def test_genuine_zero_has_ready_detector_and_no_failure(monkeypatch, image_bytes):
    monkeypatch.setattr(face_mod, 'detect_faces', lambda image: [])
    result = face_mod.process_face('lib-A', 'photo.jpg', image_bytes)
    assert result['rawFaceCount'] == result['filteredFaceCount'] == 0
    assert result['faceModelReady'] is True
    assert result['noDetectionReason'] == 'below_score_threshold'
    assert 'faceFailureStage' not in result
    assert result['faceDiagnostics']['reasonCounts'] == {}
    assert result['faceDiagnostics']['imageWidth'] == 64


def test_success_reports_bounded_aggregate_outcomes(candidate_pipeline, image_bytes):
    result = face_mod.process_face('lib-A', 'photo.jpg', image_bytes)
    assert result['hasData'] is True
    assert len(result['faces'][0]['embedding']) == 512
    assert 'faceFailureStage' not in result
    diagnostics = result['faceDiagnostics']
    assert diagnostics['embeddedCount'] == diagnostics['alignedCount'] == diagnostics['landmarkCount'] == 1
    assert diagnostics['failureCount'] == diagnostics['qualityRejectedCount'] == 0
    assert set(diagnostics['stageTimingsMs']) == {'decode', 'detect', 'landmarkWait', 'landmark', 'align', 'embed', 'total'}
    assert all(np.isfinite(value) and value >= 0 for value in diagnostics['stageTimingsMs'].values())
    assert face_mod._FACE_DIAGNOSTICS.get() is None


def test_all_alignment_rejected_is_quality_failure_not_zero(monkeypatch, candidate_pipeline, image_bytes):
    monkeypatch.setattr(face_mod, 'crop_and_align_face', lambda *args: None)
    monkeypatch.setattr(face_mod, 'compute_face_embedding', lambda *args: pytest.fail('must not embed an unaligned box'))
    result = face_mod.process_face('lib-A', 'photo.jpg', image_bytes)
    assert result['faces'] == []
    assert result['rawFaceCount'] == result['filteredFaceCount'] == 1
    assert result['faceFailureStage'] == 'quality_filter_rejected'
    assert result['faceDiagnostics']['qualityRejectedCount'] == 1
    assert result['faceDiagnostics']['failureCount'] == 0
    assert result['faceDiagnostics']['reasonCounts'] == {'alignment_transform_rejected': 1}


@pytest.mark.parametrize('stage,mode,reason', [
    ('landmark', 'none', 'landmark_detection_failed'),
    ('landmark', 'exception', 'landmark_exception'),
    ('landmark', 'nonfinite', 'landmark_invalid'),
    ('align', 'exception', 'alignment_exception'),
    ('embed', 'none', 'embedding_computation_failed'),
    ('embed', 'exception', 'embedding_exception'),
    ('embed', 'nonfinite', 'embedding_computation_failed'),
])
def test_postprocessing_failure_is_explicit(monkeypatch, candidate_pipeline, image_bytes, stage, mode, reason):
    def fail(*args):
        if mode == 'exception':
            raise RuntimeError('model failure')
        if mode == 'nonfinite':
            return np.full((5, 2) if stage == 'landmark' else (512,), np.nan)
        return None
    name = {'landmark': 'detect_five_landmarks', 'align': 'crop_and_align_face', 'embed': 'compute_face_embedding'}[stage]
    monkeypatch.setattr(face_mod, name, fail)
    result = face_mod.process_face('lib-A', 'photo.jpg', image_bytes)
    assert result['faces'] == []
    assert result['faceFailureStage'] == 'postprocessing_failed'
    assert result['rawFaceCount'] == result['candidateFaceCount'] == result['filteredFaceCount'] == 1
    assert result['faceDiagnostics']['reasonCounts'] == {reason: 1}
    assert result['faceDiagnostics']['failureCount'] == 1
    assert result['faceDiagnostics']['qualityRejectedCount'] == 0


@pytest.mark.parametrize('failed_stage', ['landmark', 'embed'])
def test_partial_failure_retains_valid_embedding_and_failure_marker(monkeypatch, candidate_pipeline, image_bytes, failed_stage):
    monkeypatch.setattr(face_mod, 'detect_faces', lambda image: [dict(candidate_pipeline), dict(candidate_pipeline, left=40.)])
    calls = []
    def partial(*args):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError('one candidate failed')
        return np.ones((5, 2)) if failed_stage == 'landmark' else np.ones(512)
    monkeypatch.setattr(face_mod, 'detect_five_landmarks' if failed_stage == 'landmark' else 'compute_face_embedding', partial)
    result = face_mod.process_face('lib-A', 'photo.jpg', image_bytes)
    assert len(result['faces']) == 1
    assert result['hasData'] is True
    assert result['faceFailureStage'] == 'postprocessing_failed'
    assert result['rawFaceCount'] == 2
    assert result['filteredFaceCount'] == result['faceDiagnostics']['failureCount'] == 1


def _mock_yolo(monkeypatch, output):
    session = SimpleNamespace(
        get_inputs=lambda: [SimpleNamespace(name='images')],
        get_outputs=lambda: [SimpleNamespace(name='output0')],
        run=lambda *args: [output],
    )
    monkeypatch.setattr(face_mod, '_get_yolo_session', lambda: session)


@pytest.mark.parametrize('output', [
    np.zeros((5, 3), dtype=np.float32),
    np.zeros((2, 5, 3), dtype=np.float32),
    np.zeros((1, 6, 3), dtype=np.float32),
    np.zeros((1, 3, 5), dtype=np.float32),
    np.zeros((1, 5, 0), dtype=np.float32),
    np.full((1, 5, 3), np.nan, dtype=np.float32),
    np.full((1, 5, 3), np.inf, dtype=np.float32),
])
def test_invalid_yolo_is_explicit_failure_not_zero(monkeypatch, image_bytes, output):
    _mock_yolo(monkeypatch, output)
    with pytest.raises(ValueError, match='yolo_output_'):
        face_mod.detect_faces(np.zeros((64, 64, 3), dtype=np.uint8))
    result = face_mod.process_face('lib-A', 'photo.jpg', image_bytes)
    assert result['faces'] == []
    assert result['faceFailureStage'] == 'detection_failed'
    assert 'noDetectionReason' not in result
    assert result['faceDiagnostics']['detectorOutputShape'] == list(output.shape)


def test_valid_channel_major_yolo_contract_and_diagnostics(monkeypatch, image_bytes):
    output = np.array([[[320, 320, 100], [320, 320, 100], [200, 200, 20], [200, 200, 20], [.9, .8, .1]]], dtype=np.float32)
    _mock_yolo(monkeypatch, output)
    monkeypatch.setattr(face_mod, 'detect_five_landmarks', lambda *args: None)
    result = face_mod.process_face('lib-A', 'photo.jpg', image_bytes)
    diagnostics = result['faceDiagnostics']
    assert diagnostics['detectorOutputShape'] == [1, 5, 3]
    assert diagnostics['anchorCount'] == 3
    assert diagnostics['scorePassedCount'] == diagnostics['validBoxCount'] == 2
    assert diagnostics['nmsKeptCount'] == diagnostics['nmsRemovedCount'] == 1
    assert result['rawFaceCount'] == 1
    assert result['faceFailureStage'] == 'postprocessing_failed'


def test_score_passed_but_invalid_boxes_is_not_genuine_zero(monkeypatch, image_bytes):
    output = np.array([[[320], [320], [0], [0], [.9]]], dtype=np.float32)
    _mock_yolo(monkeypatch, output)
    result = face_mod.process_face('lib-A', 'photo.jpg', image_bytes)
    assert result['rawFaceCount'] == result['filteredFaceCount'] == 1
    assert result['faceFailureStage'] == 'quality_filter_rejected'
    assert result['faceDiagnostics']['invalidBoxCount'] == 1
    assert result['faceDiagnostics']['failureCount'] == 0


def test_mixed_invalid_box_and_valid_embedding_still_incomplete(monkeypatch, image_bytes):
    # Run the real decoder with one accepted box and one score-qualified box
    # lost to geometry filtering; the surviving embedding is not a full set.
    output = np.array([[[320, 100], [320, 100], [200, 0], [200, 0], [.9, .8]]], dtype=np.float32)
    _mock_yolo(monkeypatch, output)
    monkeypatch.setattr(face_mod, 'detect_five_landmarks', lambda *args: np.ones((5, 2)))
    monkeypatch.setattr(face_mod, 'crop_and_align_face', lambda *args: (np.zeros((112, 112, 3), dtype=np.uint8), 'landmark-5pt-mp'))
    monkeypatch.setattr(face_mod, 'compute_face_embedding', lambda *args: np.ones(512))
    result = face_mod.process_face('lib-A', 'photo.jpg', image_bytes)
    assert len(result['faces']) == 1
    assert result['rawFaceCount'] == 2
    assert result['filteredFaceCount'] == 1
    assert result['faceFailureStage'] == 'quality_filter_rejected'
    assert result['faceDiagnostics']['reasonCounts'] == {'invalid_detection_boxes': 1}
