"""ipworker's face detect + align + embed step processor.

Ports the browser's onnxruntime-web pipeline (YOLOv8n-face detection,
5-point similarity alignment, AdaFace embedding) to Python so ipworker can
run the SAME .onnx weights server-side. Every numeric convention below
(input size, normalization, score/iou thresholds, alignment template,
plausibility bounds) is copied verbatim from the browser implementation so
the two pipelines are as close to equivalent as the landmark-source swap
allows:
  - frontend/src/services/yoloFaceDetectionRuntime.ts (detection)
  - frontend/src/components/PhotoGallery.tsx (crop/align orchestration,
    ARC_FACE_* constants)
  - frontend/src/services/faceAlignment.ts (similarity-transform solve)
  - frontend/src/services/arcFaceEmbeddingRuntime.ts (embedding)

The one deliberate difference is the landmark source: the browser uses
face-api.js's 68-point TFJS model (no ONNX/Python equivalent exists);
ipworker uses MediaPipe FaceMesh instead, reduced to the same 5 anatomical
points (eye centers, nose tip, mouth corners). This is exactly why faces
detected here are tagged with their own alignmentMethod tier
('landmark-5pt-mp'/'landmark-2pt-mp', not '...-mp'-less 'landmark-5pt') --
see PEOPLE_CLUSTER_ALIGNMENT_TIERS in app.py. Whether this tier is close
enough to reuse the browser's clustering thresholds or needs its own is an
explicit, separate, data-driven decision (not yet made) -- see
validate_face_pipeline.py for the cosine-similarity groundwork.

Heavy deps (onnxruntime, opencv, mediapipe) live only in the ipworker
image's requirements -- this module is imported lazily, only from
app.run_ipworker(), never from the plain backend/worker roles.
"""
from __future__ import annotations

import io
import os
import threading
from typing import Dict, List, Optional, Tuple

import numpy as np
from PIL import Image, ImageOps

from image_utils import RAW_EXTENSIONS_CINEMA, RAW_EXTENSIONS_RAWPY, extract_raw_preview_bytes

# cv2/onnxruntime/mediapipe are ipworker-only deps (requirements-ipworker.txt),
# not in backend/requirements.txt -- guarded the same way ipwork_ocr.py guards
# pytesseract, so importing this module never hard-crashes outside the
# ipworker image (it shouldn't be imported there at all, but defense in depth
# costs nothing here).
try:
    import cv2
except Exception:  # pragma: no cover - only absent outside the ipworker image
    cv2 = None

try:
    import onnxruntime as ort
except Exception:  # pragma: no cover - only absent outside the ipworker image
    ort = None

try:
    import mediapipe as mp
    from mediapipe.tasks.python import BaseOptions
    from mediapipe.tasks.python.vision import FaceLandmarker, FaceLandmarkerOptions
except Exception:  # pragma: no cover - only absent outside the ipworker image
    mp = None

# --- Model locations ---------------------------------------------------------
# Bundled into the ipworker image at build time from the SAME files the
# frontend ships (see backend/ipworker.Dockerfile) -- not fetched at runtime,
# so ipworker has no dependency on the frontend container being reachable.
MODELS_DIR = os.getenv('IPWORKER_MODELS_DIR', '/app/models')
YOLO_FACE_MODEL_PATH = os.path.join(MODELS_DIR, 'yolo-face', 'model.onnx')
ADAFACE_MODEL_PATH = os.path.join(MODELS_DIR, 'adaface', 'model.onnx')
# Google's official MediaPipe Face Landmarker task bundle (downloaded at
# ipworker image build time -- see backend/ipworker.Dockerfile -- from
# https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task).
# mediapipe>=1.0 only exposes the newer Tasks API (mp.solutions was removed),
# which -- unlike the old solutions.face_mesh.FaceMesh -- requires this
# explicit model file rather than bundling weights internally.
FACE_LANDMARKER_MODEL_PATH = os.path.join(MODELS_DIR, 'face_landmarker.task')

# --- YOLOv8n-face detection (yoloFaceDetectionRuntime.ts) --------------------
YOLO_INPUT_SIZE = 640
YOLO_SCORE_THRESHOLD = 0.35
YOLO_IOU_THRESHOLD = 0.45
YOLO_MAX_FACES = 32

# --- Alignment (PhotoGallery.tsx ARC_FACE_* constants) -----------------------
ARC_FACE_EMBEDDING_CANVAS_SIZE = 112
ARC_FACE_MIN_SCALE = 0.03
ARC_FACE_MAX_SCALE = 0.6
ARC_FACE_MAX_ROTATION_RADIANS = (60 * np.pi) / 180
ARC_FACE_TARGET_EYE_X_RATIO = 0.50
ARC_FACE_TARGET_EYE_Y_RATIO = 0.38
ARC_FACE_TARGET_EYE_DISTANCE_RATIO = 0.36
FIVE_POINT_LANDMARK_PADDING_RATIO = 0.25

# Standard 112x112 ArcFace/InsightFace 5-point reference template
# (faceAlignment.ts ARC_FACE_5POINT_TEMPLATE). Order: rightEye, leftEye,
# nose, leftMouth, rightMouth (subject-anatomical left/right).
ARC_FACE_5POINT_TEMPLATE = np.array([
    [38.2946, 51.6963],
    [73.5318, 51.5014],
    [56.0252, 71.7366],
    [41.5493, 92.3655],
    [70.7299, 92.2041],
], dtype=np.float64)

FACE_EMBEDDING_DIMENSIONS = 512
# Distinct from the browser's 'adaface-ir101-webface4m-512d-v1+face-landmark-68-v1':
# same embedding weights, different (MediaPipe, not face-api) landmark source.
FACE_EMBEDDING_MODEL_TAXONOMY_VERSION = 'adaface-ir101-webface4m-512d-v1+mediapipe-landmark-478'
FACE_EMBEDDING_RUNTIME = 'onnxruntime+mediapipe'

_yolo_session: Optional[ort.InferenceSession] = None
_adaface_session: Optional[ort.InferenceSession] = None
_face_mesh = None
# MediaPipe's Tasks API is not documented thread-safe for concurrent
# .detect() calls on one shared FaceLandmarker instance -- unlike ONNX
# Runtime's Run(), this is a real correctness risk (crossed-up/corrupted
# landmark results across threads, not just a crash), so serialize just
# the detect() call below.
_FACE_LANDMARKER_LOCK = threading.Lock()


def _get_yolo_session() -> ort.InferenceSession:
    global _yolo_session
    if ort is None or cv2 is None:
        raise RuntimeError('onnxruntime_or_opencv_unavailable')
    if _yolo_session is None:
        _yolo_session = ort.InferenceSession(YOLO_FACE_MODEL_PATH, providers=['CPUExecutionProvider'])
    return _yolo_session


def _get_adaface_session() -> ort.InferenceSession:
    global _adaface_session
    if ort is None or cv2 is None:
        raise RuntimeError('onnxruntime_or_opencv_unavailable')
    if _adaface_session is None:
        _adaface_session = ort.InferenceSession(ADAFACE_MODEL_PATH, providers=['CPUExecutionProvider'])
    return _adaface_session


def _get_face_landmarker() -> 'FaceLandmarker':
    global _face_mesh
    if _face_mesh is None:
        if mp is None:
            raise RuntimeError('mediapipe_unavailable')
        options = FaceLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=FACE_LANDMARKER_MODEL_PATH),
            num_faces=1,
            min_face_detection_confidence=0.5,
        )
        _face_mesh = FaceLandmarker.create_from_options(options)
    return _face_mesh


def _letterbox(image_bgr: np.ndarray, size: int) -> Tuple[np.ndarray, float, int, int]:
    src_h, src_w = image_bgr.shape[:2]
    scale = min(size / src_w, size / src_h)
    new_w, new_h = round(src_w * scale), round(src_h * scale)
    pad_x = (size - new_w) // 2
    pad_y = (size - new_h) // 2
    canvas = np.full((size, size, 3), 114, dtype=np.uint8)
    resized = cv2.resize(image_bgr, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
    canvas[pad_y:pad_y + new_h, pad_x:pad_x + new_w] = resized
    return canvas, scale, pad_x, pad_y


def _nms(boxes: List[dict], iou_threshold: float) -> List[dict]:
    boxes_sorted = sorted(boxes, key=lambda b: b['score'], reverse=True)
    kept: List[dict] = []

    def iou(a: dict, b: dict) -> float:
        ax2, ay2 = a['left'] + a['width'], a['top'] + a['height']
        bx2, by2 = b['left'] + b['width'], b['top'] + b['height']
        ix1, iy1 = max(a['left'], b['left']), max(a['top'], b['top'])
        ix2, iy2 = min(ax2, bx2), min(ay2, by2)
        iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
        inter = iw * ih
        union = a['width'] * a['height'] + b['width'] * b['height'] - inter
        return inter / union if union > 0 else 0.0

    for box in boxes_sorted:
        if all(iou(k, box) < iou_threshold for k in kept):
            kept.append(box)
            if len(kept) >= YOLO_MAX_FACES:
                break
    return kept


def detect_faces(image_bgr: np.ndarray) -> List[dict]:
    """Detect faces in a BGR image (opencv convention). Returns boxes in
    image pixel coordinates: [{left, top, width, height, score}, ...]."""
    session = _get_yolo_session()
    src_h, src_w = image_bgr.shape[:2]
    letterboxed, scale, pad_x, pad_y = _letterbox(image_bgr, YOLO_INPUT_SIZE)
    rgb = cv2.cvtColor(letterboxed, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    tensor = np.transpose(rgb, (2, 0, 1))[np.newaxis, ...]  # 1x3xHxW

    input_name = session.get_inputs()[0].name
    output_name = session.get_outputs()[0].name
    raw = session.run([output_name], {input_name: tensor})[0]
    if raw.ndim != 3 or raw.shape[1] < 5:
        return []
    raw = raw[0]  # 5xN
    scores = raw[4]
    candidates: List[dict] = []
    for a in np.where(scores >= YOLO_SCORE_THRESHOLD)[0]:
        # Cast out of numpy.float32 immediately -- confirmed via a real
        # end-to-end run (Azurite + real queue + real table write) that
        # letting these flow downstream as numpy scalars reaches
        # storage_utils.py's json.dumps(bbox) and throws "Object of type
        # float32 is not JSON serializable". None of the earlier isolated
        # function-call smoke tests caught this because they only ever
        # printed results through round(float(...)), never actually
        # serialized the raw returned dict.
        cx, cy, w, h = (float(raw[0, a]), float(raw[1, a]), float(raw[2, a]), float(raw[3, a]))
        left = (cx - w / 2 - pad_x) / scale
        top = (cy - h / 2 - pad_y) / scale
        width = w / scale
        height = h / scale
        clamped_left = max(0.0, min(left, src_w))
        clamped_top = max(0.0, min(top, src_h))
        clamped_right = max(0.0, min(left + width, src_w))
        clamped_bottom = max(0.0, min(top + height, src_h))
        cw, ch = clamped_right - clamped_left, clamped_bottom - clamped_top
        if cw <= 0 or ch <= 0:
            continue
        candidates.append({
            'left': clamped_left, 'top': clamped_top,
            'width': cw, 'height': ch, 'score': float(scores[a]),
        })
    return _nms(candidates, YOLO_IOU_THRESHOLD)


# --- Landmarks (MediaPipe stand-in for face-api's 68-point net) -------------

# MediaPipe FaceMesh 468-point indices. Eye "center" is approximated the same
# way face-api's meanLandmarkPoint averages its own 6-point eye contour (here:
# outer/inner corner + upper/lower mid, per eye) -- not pixel-identical to
# face-api's specific 6 points, but the same "average a small eye-contour
# subset" strategy, which is what the empirical validation step needs to
# check is close enough.
_RIGHT_EYE_INDICES = (33, 133, 159, 145)
_LEFT_EYE_INDICES = (362, 263, 386, 374)
_NOSE_TIP_INDEX = 1
_MOUTH_LEFT_INDEX = 61
_MOUTH_RIGHT_INDEX = 291


def _compute_padded_crop_bounds(bbox: dict, padding_ratio: float, width: int, height: int) -> Optional[Tuple[int, int, int, int]]:
    left, top = bbox['left'], bbox['top']
    w, h = bbox['width'], bbox['height']
    if w <= 0 or h <= 0:
        return None
    pad_x, pad_y = w * padding_ratio, h * padding_ratio
    crop_left = max(0, int(np.floor(left - pad_x)))
    crop_top = max(0, int(np.floor(top - pad_y)))
    crop_right = min(width, int(np.ceil(left + w + pad_x)))
    crop_bottom = min(height, int(np.ceil(top + h + pad_y)))
    return crop_left, crop_top, max(1, crop_right - crop_left), max(1, crop_bottom - crop_top)


def detect_five_landmarks(image_bgr: np.ndarray, bbox: dict) -> Optional[np.ndarray]:
    """Returns 5x2 array [rightEye, leftEye, nose, leftMouth, rightMouth] in
    SOURCE image pixel coordinates, or None if no face mesh was found."""
    bounds = _compute_padded_crop_bounds(bbox, FIVE_POINT_LANDMARK_PADDING_RATIO, image_bgr.shape[1], image_bgr.shape[0])
    if bounds is None:
        return None
    crop_left, crop_top, crop_w, crop_h = bounds
    crop = image_bgr[crop_top:crop_top + crop_h, crop_left:crop_left + crop_w]
    if crop.size == 0:
        return None
    landmarker = _get_face_landmarker()
    rgb_crop = np.ascontiguousarray(cv2.cvtColor(crop, cv2.COLOR_BGR2RGB))
    mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb_crop)
    with _FACE_LANDMARKER_LOCK:
        result = landmarker.detect(mp_image)
    if not result.face_landmarks:
        return None
    landmarks = result.face_landmarks[0]

    def point(indices) -> Tuple[float, float]:
        xs = [landmarks[i].x * crop_w for i in indices]
        ys = [landmarks[i].y * crop_h for i in indices]
        return sum(xs) / len(xs), sum(ys) / len(ys)

    right_eye = point(_RIGHT_EYE_INDICES)
    left_eye = point(_LEFT_EYE_INDICES)
    nose = point((_NOSE_TIP_INDEX,))
    mouth_left = point((_MOUTH_LEFT_INDEX,))
    mouth_right = point((_MOUTH_RIGHT_INDEX,))
    local = np.array([right_eye, left_eye, nose, mouth_left, mouth_right], dtype=np.float64)
    local[:, 0] += crop_left
    local[:, 1] += crop_top
    return local


# --- Similarity transform (faceAlignment.ts solveSimilarityTransform) -------

def solve_similarity_transform(src: np.ndarray, dst: np.ndarray) -> Optional[Tuple[float, float, float, float]]:
    """Closed-form least-squares similarity transform (uniform scale +
    rotation + translation) mapping src[i] -> dst[i]. Direct port of the same
    normal-equations derivation faceAlignment.ts uses, not cv2's RANSAC-based
    estimateAffinePartial2D, so the two pipelines solve the same math."""
    n = min(len(src), len(dst))
    if n < 2:
        return None
    x, y = src[:n, 0], src[:n, 1]
    u, v = dst[:n, 0], dst[:n, 1]
    sxx, syy = float(np.sum(x * x)), float(np.sum(y * y))
    sx, sy = float(np.sum(x)), float(np.sum(y))
    sxu, syv = float(np.sum(x * u)), float(np.sum(y * v))
    syu, sxv = float(np.sum(y * u)), float(np.sum(x * v))
    su, sv = float(np.sum(u)), float(np.sum(v))
    m = np.array([
        [sxx + syy, 0, sx, sy],
        [0, sxx + syy, -sy, sx],
        [sx, -sy, n, 0],
        [sy, sx, 0, n],
    ], dtype=np.float64)
    rhs = np.array([sxu + syv, sxv - syu, su, sv], dtype=np.float64)
    try:
        solution = np.linalg.solve(m, rhs)
    except np.linalg.LinAlgError:
        return None
    if not np.all(np.isfinite(solution)):
        return None
    a, b, tx, ty = solution
    return float(a), float(b), float(tx), float(ty)


def _is_plausible_transform(a: float, b: float) -> bool:
    scale = float(np.hypot(a, b))
    if not np.isfinite(scale) or scale < ARC_FACE_MIN_SCALE or scale > ARC_FACE_MAX_SCALE:
        return False
    rotation = float(np.arctan2(b, a))
    return np.isfinite(rotation) and abs(rotation) <= ARC_FACE_MAX_ROTATION_RADIANS


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(value, hi))


def crop_and_align_face(image_bgr: np.ndarray, bbox: dict, landmarks: Optional[np.ndarray]) -> Optional[Tuple[np.ndarray, str]]:
    """Mirrors cropFaceCanvas (PhotoGallery.tsx): 5-point similarity alignment
    when plausible, else a 2-point eyes-only fallback, else None (caller
    should skip storing an embedding for this face, matching the browser's
    'none' tier, which the backend already excludes from clustering)."""
    size = ARC_FACE_EMBEDDING_CANVAS_SIZE

    if landmarks is not None and len(landmarks) >= 5:
        transform = solve_similarity_transform(landmarks[:5], ARC_FACE_5POINT_TEMPLATE)
        if transform is not None:
            a, b, tx, ty = transform
            if np.isfinite(tx) and np.isfinite(ty) and _is_plausible_transform(a, b):
                # Canvas2D's setTransform(a,b,-b,a,tx,ty) + drawImage(source,0,0)
                # places source pixel (x,y) at output (a*x-b*y+tx, b*x+a*y+ty) --
                # exactly the forward mapping cv2.warpAffine's M expects.
                m = np.array([[a, -b, tx], [b, a, ty]], dtype=np.float64)
                aligned = cv2.warpAffine(
                    image_bgr, m, (size, size),
                    flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0),
                )
                return aligned, 'landmark-5pt-mp'

    if landmarks is not None and len(landmarks) >= 2:
        right_eye, left_eye = landmarks[0], landmarks[1]
        eye_dx, eye_dy = left_eye[0] - right_eye[0], left_eye[1] - right_eye[1]
        eye_distance = float(np.hypot(eye_dx, eye_dy))
        if np.isfinite(eye_distance) and eye_distance > 1:
            angle = float(np.arctan2(eye_dy, eye_dx))
            target_eye_x = size * ARC_FACE_TARGET_EYE_X_RATIO
            target_eye_y = size * ARC_FACE_TARGET_EYE_Y_RATIO
            target_eye_distance = size * ARC_FACE_TARGET_EYE_DISTANCE_RATIO
            scale = _clamp(target_eye_distance / eye_distance, ARC_FACE_MIN_SCALE, 3.5)
            cos_a, sin_a = np.cos(-angle), np.sin(-angle)
            # translate(-mid) -> rotate(-angle) -> scale -> translate(target), composed
            # into one affine matrix, matching the four chained canvas calls exactly.
            mid_x, mid_y = (right_eye[0] + left_eye[0]) / 2, (right_eye[1] + left_eye[1]) / 2
            a = scale * cos_a
            b = scale * sin_a
            tx = target_eye_x - (a * mid_x - b * mid_y)
            ty = target_eye_y - (b * mid_x + a * mid_y)
            m = np.array([[a, -b, tx], [b, a, ty]], dtype=np.float64)
            aligned = cv2.warpAffine(
                image_bgr, m, (size, size),
                flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0),
            )
            return aligned, 'landmark-2pt-mp'

    return None


# --- AdaFace embedding (arcFaceEmbeddingRuntime.ts computeArcFaceEmbedding) -

def compute_face_embedding(aligned_bgr: np.ndarray) -> Optional[np.ndarray]:
    session = _get_adaface_session()
    rgb = cv2.cvtColor(aligned_bgr, cv2.COLOR_BGR2RGB).astype(np.float32)
    # The AdaFace ONNX graph bakes in its own (x-127.5)/128 normalization as
    # its first ops -- feed it RAW [0,255] pixels, same as the browser.
    tensor = np.transpose(rgb, (2, 0, 1))[np.newaxis, ...]
    input_name = session.get_inputs()[0].name
    output_name = session.get_outputs()[0].name
    raw = session.run([output_name], {input_name: tensor})[0]
    values = raw.reshape(-1)
    if values.shape[0] != FACE_EMBEDDING_DIMENSIONS or not np.all(np.isfinite(values)):
        return None
    norm = float(np.linalg.norm(values))
    if not np.isfinite(norm) or norm <= 0:
        return None
    normalized = values / norm
    if not np.all(np.isfinite(normalized)):
        return None
    return normalized.astype(np.float32)


# --- Top-level ipworker step processor --------------------------------------

def _decodable_image_bytes(image_bytes: bytes, filename: str) -> bytes:
    """RAW formats (e.g. CR3) have no generic PIL codec -- Image.open() on the
    raw bytes raises 'cannot identify image file'. Extract the same embedded/
    rawpy preview ipwork_thumbnail.py already uses so RAW photos decode here
    too, instead of every RAW upload silently failing face + ai_vision."""
    ext = filename.rsplit('.', 1)[-1].lower() if filename and '.' in filename else ''
    if ext in RAW_EXTENSIONS_RAWPY or ext in RAW_EXTENSIONS_CINEMA:
        preview = extract_raw_preview_bytes(image_bytes, filename)
        if preview:
            return preview
    return image_bytes


def process_face(user_id: str, filename: str, image_bytes: bytes) -> Optional[Dict]:
    try:
        with Image.open(io.BytesIO(_decodable_image_bytes(image_bytes, filename))) as pil_image:
            # Most phone photos are stored with an EXIF orientation tag rather
            # than physically rotated pixels. Confirmed against a real sideways
            # photo (orientation=6) during validation: without this, detection
            # still "succeeds" (onnx models are somewhat rotation-tolerant) but
            # returns a bbox in the wrong coordinate frame entirely -- a
            # completely different box, not just a rotated one -- silently
            # corrupting the alignment crop fed to AdaFace. The browser's own
            # canvas-based pipeline never hits this because canvas drawImage
            # already auto-orients.
            pil_image = ImageOps.exif_transpose(pil_image)
            rgb_image = pil_image.convert('RGB')
            image_width, image_height = rgb_image.size
            image_bgr = cv2.cvtColor(np.array(rgb_image), cv2.COLOR_RGB2BGR)
    except Exception as exc:
        # 'faces' must be present (even empty) and faceFailureStage must be a
        # non-transient value -- storage_utils._apply_client_processing_results'
        # face block only resolves face_status to a terminal state when
        # isinstance(faces, list) is true; omitting it here left face_status
        # stuck at 'running' forever instead of surfacing as a retryable
        # 'failed' on the Tools page.
        return {
            'hasData': False,
            'faces': [],
            'rawFaceCount': 0,
            'error': str(exc),
            'faceFailureStage': 'unsupported_runtime',
            'faceFailureDetail': f'ipworker_decode_failed: {exc}',
        }

    try:
        detections = detect_faces(image_bgr)
    except Exception as exc:
        return {
            'hasData': False,
            'faces': [],
            'rawFaceCount': 0,
            'error': f'detection_failed: {exc}',
            'faceFailureStage': 'unsupported_runtime',
            'faceFailureDetail': f'ipworker_detection_failed: {exc}',
        }

    faces: List[Dict] = []
    for detection in detections:
        bbox = {k: detection[k] for k in ('left', 'top', 'width', 'height')}
        try:
            landmarks = detect_five_landmarks(image_bgr, bbox)
        except Exception:
            landmarks = None
        crop_result = crop_and_align_face(image_bgr, bbox, landmarks)
        if crop_result is None:
            continue
        aligned, alignment_method = crop_result
        embedding = compute_face_embedding(aligned)
        if embedding is None:
            continue
        faces.append({
            'bbox': bbox,
            'confidence': detection['score'],
            'imageWidth': image_width,
            'imageHeight': image_height,
            'embedding': embedding.tolist(),
            'alignmentMethod': alignment_method,
            'model': 'AdaFace IR-101 (mediapipe-aligned)',
            'modelVersion': 'adaface-ir101-webface4m-512d-v1',
            'modelTaxonomyVersion': FACE_EMBEDDING_MODEL_TAXONOMY_VERSION,
            'runtime': FACE_EMBEDDING_RUNTIME,
            'modelAvailability': 'available',
        })

    # Top-level model/version fields (not just per-face) are required so
    # storage_utils._client_model_provenance can stamp processing_metadata's
    # client_face.modelTaxonomyVersion summary -- that field, not the per-face
    # rows, is what _browser_processing_face_version_stale reads. Without
    # this, ipworker-processed photos never clear the stale-version flag and
    # get re-detected on every sweep cycle forever, even after succeeding.
    model_fields = {
        'model': 'AdaFace IR-101 (mediapipe-aligned)',
        'modelVersion': 'adaface-ir101-webface4m-512d-v1',
        'modelTaxonomyVersion': FACE_EMBEDDING_MODEL_TAXONOMY_VERSION,
        'runtime': FACE_EMBEDDING_RUNTIME,
        'modelAvailability': 'available',
    }
    if not faces:
        return {'hasData': False, 'faces': [], 'rawFaceCount': len(detections), **model_fields}
    return {'hasData': True, 'faces': faces, 'rawFaceCount': len(detections), **model_fields}
