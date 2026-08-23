import io
import hashlib
import json
import os
import uuid
import base64
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import BinaryIO, Callable, Dict, List, Optional, Tuple, Union

import numpy as np
from azure.core.exceptions import ResourceNotFoundError, ResourceModifiedError
from azure.storage.blob import ContentSettings as BlobContentSettings
from werkzeug.utils import secure_filename

from image_utils import (
    convert_image_to_jpeg,
    compute_file_hash,
    create_thumbnail_data,
    create_video_thumbnail_data,
    extract_raw_preview_bytes,
    is_video_file,
    RAW_EXTENSIONS_CINEMA,
    RAW_EXTENSIONS_RAWPY,
)
from exif_utils import extract_exif_from_bytes, extract_gps_decimal_from_exif
from search_utils import MAX_TAGS_STORED, build_semantic_layers, build_semantic_text, curate_tag_records, normalize_tags
import maps_utils
import vision_utils

CLIENT_PROCESSING_SCHEMA_VERSION = 2
CLIENT_PROCESSING_MAX_THUMBNAIL_BYTES = 512 * 1024
CLIENT_PROCESSING_MAX_EXIF_KEYS = 200
CLIENT_PROCESSING_MAX_EXIF_VALUE_LENGTH = 512
CLIENT_PROCESSING_MAX_AI_TAGS = 400
CLIENT_PROCESSING_MAX_AI_TEXT_LENGTH = 2048
# Browser-computed CLIP image embedding (Xenova/clip-vit-base-patch32, i.e.
# openai/clip-vit-base-patch32 exported to ONNX). Must match vision_utils'
# server-side text embedding model/checkpoint so image and text vectors are
# comparable by cosine similarity.
PHOTO_EMBEDDING_MODEL_VERSION = 'clip-vit-base-patch32:openai:browser-v1'
PHOTO_EMBEDDING_DIMENSION = 512
SUSPICIOUS_FACE_CONFIDENCE = float(os.getenv('SUSPICIOUS_FACE_CONFIDENCE', '0.60'))
FACE_MIN_STORE_CONFIDENCE = float(os.getenv('FACE_MIN_STORE_CONFIDENCE', '0.24'))
FACE_LOW_CONFIDENCE_REJECT_BELOW = float(os.getenv('FACE_LOW_CONFIDENCE_REJECT_BELOW', '0.32'))
# Face-detection failure stages that mean "a model wasn't ready yet" (cold-cache
# load timeout / model still downloading) rather than a genuine failure. These
# are left retryable ('pending') so the automatic pipeline re-runs them once the
# YOLO/ArcFace models are warm, instead of surfacing a hard 'failed'.
_TRANSIENT_FACE_FAILURE_STAGES = {
    'timeout',
    'model_load_failed',
    'embedding_model_load_failed',
    'face_api_load_failed',
}
FACE_LOW_CONFIDENCE_MAX_AREA_RATIO = float(os.getenv('FACE_LOW_CONFIDENCE_MAX_AREA_RATIO', '0.08'))
FACE_LOW_CONFIDENCE_MAX_SIDE_RATIO = float(os.getenv('FACE_LOW_CONFIDENCE_MAX_SIDE_RATIO', '0.42'))
CLIENT_PROCESSING_ALLOWED_STEPS = {'thumbnail', 'exif', 'ocr', 'ai_vision', 'map_detection', 'face'}
CLIENT_PROCESSING_ALLOWED_STATUSES = {'done', 'skipped', 'failed', 'timeout', 'unsupported'}
CLIENT_PROCESSING_TERMINAL_STATUSES = {'done', 'skipped', 'failed', 'timeout', 'unsupported'}
BROWSER_PROCESSING_STEPS = ('thumbnail', 'exif', 'ocr', 'ai_vision', 'map_detection', 'face')
HEIF_EXTENSIONS = {'heic', 'heif'}
CLIENT_PROCESSING_ALLOWED_REASONS = {
    'done',
    'offline',
    'poor_network',
    'save_data_enabled',
    'unsupported_runtime',
    'model_download_timeout',
    'model_load_failed',
    'model_unavailable',
    'model_budget_exceeded',
    'file_too_large',
    'image_too_large',
    'memory_budget',
    'inference_timeout',
    'finalize_grace_expired',
    'background_throttled',
    'upstream_incomplete',
    'sas_expired_or_upload_retry',
    'user_cancelled',
    'raw_preview_missing',
    'raw_preview_invalid',
    'raw_container_unsupported',
    'raw_exif_only',
    'video_unsupported',
    'unknown_error',
}
CLIENT_PROCESSING_ALLOWED_SOURCE_KINDS = {
    'original',
    'raw_embedded_jpeg',
    'raw_converted_jpeg',
    'backend_converted_jpeg',
    'raw_exif_only',
    'unsupported',
}
CLIENT_PROCESSING_ALLOWED_MODEL_AVAILABILITY = {
    'available',
    'cached',
    'downloaded',
    'unavailable',
    'skipped',
}
CLIENT_PROCESSING_ALLOWED_MODEL_CACHE_STATUS = {
    'hit',
    'miss',
    'downloaded',
    'failed',
}
LOCAL_VISION_FALLBACK_MODEL = 'photostore-local-vision-fallback'
LOCAL_VISION_FALLBACK_TAXONOMY_VERSION = 'photostore-local-vision-v1'
LOCAL_VISION_FALLBACK_RUNTIME = 'browser-worker/local-vision-heuristics'
AI_VISION_RETRYABLE_REASONS = {
    'model_download_timeout',
    'model_load_failed',
    'model_unavailable',
    'model_budget_exceeded',
    'inference_timeout',
    'raw_container_unsupported',
    'raw_preview_invalid',
    'raw_preview_missing',
    'upstream_incomplete',
}

_METADATA_UPDATE_MAX_RETRIES = 5
_METADATA_UPDATE_RETRY_BASE_SECONDS = 0.05
_VECTOR_INDEX_CACHE_LOCK = threading.RLock()
_VECTOR_INDEX_CACHE: Dict[str, Dict[str, object]] = {}
_VECTOR_INDEX_RELEVANT_FIELDS = {
    'address',
    'aiPersonLabel',
    'aiPersonScore',
    'caption',
    'faceCount',
    'faces',
    'locationCity',
    'locationCountry',
    'objects',
    'ocrText',
    'peopleIds',
    'processing_metadata',
    'semanticEmbedding',
    'semanticEmbeddingVersion',
    'semanticLayers',
    'photoEmbedding',
    'photoEmbeddingVersion',
    'semanticText',
    'backgroundTags',
    'subjectTags',
    'tags',
    'tagBuckets',
    'tagMetadata',
    'weakTags',
}

_CTX: Dict[str, object] = {}

# Image name anonymization: caches mapping of library_id + anonymous_id -> original_filename
# and the reverse (original_filename -> anonymous_id) so both the download path
# (blob id -> real name) and the SAS/direct-URL path (real name -> physical blob)
# resolve in O(1) once the library's mappings are warm.
_ANONYMOUS_IMAGE_CACHE_LOCK = threading.RLock()
_ANONYMOUS_IMAGE_CACHE: Dict[str, Dict[str, str]] = {}  # {library_id: {anonymous_id: original_filename}}
_ANONYMOUS_IMAGE_REVERSE_CACHE: Dict[str, Dict[str, str]] = {}  # {library_id: {'kind:original_filename': anonymous_id}}


def _reverse_cache_key(kind: str, original_filename: str) -> str:
    return f'{kind}:{original_filename}'


def _cache_image_name(library_id: str, anonymous_id: str, original_filename: str, kind: str = 'image') -> None:
    """Populate both the forward and reverse in-memory caches for one mapping."""
    if not library_id or not anonymous_id:
        return
    with _ANONYMOUS_IMAGE_CACHE_LOCK:
        _ANONYMOUS_IMAGE_CACHE.setdefault(library_id, {})[anonymous_id] = original_filename
        if original_filename:
            _ANONYMOUS_IMAGE_REVERSE_CACHE.setdefault(library_id, {})[_reverse_cache_key(kind, original_filename)] = anonymous_id


@dataclass
class VectorIndexSnapshot:
    user_id: str
    source_version: str
    embedding_version: str
    updated_at: str
    row_keys: List[str]
    embeddings: np.ndarray


@dataclass
class ImageNameMapping:
    library_id: str
    anonymous_id: str
    original_filename: str
    original_extension: str
    kind: str
    blob_container: str
    created_at: str


def _escape_odata(value: str) -> str:
    return str(value).replace("'", "''")


def configure_storage(
    *,
    metadata_table_client,
    face_table_client=None,
    person_table_client=None,
    blob_service_client=None,
    blob_image_container: Optional[str] = None,
    blob_thumbnail_container: Optional[str] = None,
    blob_cover_container: Optional[str] = None,
    blob_vector_index_container: Optional[str] = None,
    image_names_table_client=None,
    hash_index_table_client=None,
    filename_owners_table_client=None,
    queue_map_on_upload: bool = False,
) -> None:
    _CTX['metadata_table_client'] = metadata_table_client
    _CTX['face_table_client'] = face_table_client
    _CTX['person_table_client'] = person_table_client
    _CTX['blob_service_client'] = blob_service_client
    _CTX['blob_image_container'] = (blob_image_container or '').strip()
    _CTX['blob_thumbnail_container'] = (blob_thumbnail_container or '').strip()
    _CTX['blob_cover_container'] = (blob_cover_container or '').strip()
    _CTX['blob_vector_index_container'] = (blob_vector_index_container or '').strip()
    _CTX['image_names_table_client'] = image_names_table_client
    _CTX['hash_index_table_client'] = hash_index_table_client
    _CTX['filename_owners_table_client'] = filename_owners_table_client
    _CTX['queue_map_on_upload'] = bool(queue_map_on_upload)


def _require_context() -> None:
    required = [
        'metadata_table_client',
        'blob_service_client',
    ]
    missing = [key for key in required if key not in _CTX]
    if missing:
        raise RuntimeError(f'storage_utils is not configured. Missing: {", ".join(missing)}')


def _generate_anonymous_id() -> str:
    return str(uuid.uuid4())


def _store_image_name_mapping(
    library_id: str,
    original_filename: str,
    kind: str,
    blob_container: str,
    anonymous_id: Optional[str] = None,
) -> Optional[str]:
    image_names_table_client = _CTX.get('image_names_table_client')
    if image_names_table_client is None:
        return None

    try:
        # Use the caller-supplied id (the UUID the blob was actually uploaded under
        # at /upload/init) when given, so the mapping matches the real blob name;
        # only mint a fresh id when none was provided.
        anonymous_id = str(anonymous_id or '').strip() or _generate_anonymous_id()
        ext = _filename_extension(original_filename)
        entity = {
            'PartitionKey': library_id,
            'RowKey': anonymous_id,
            'original_filename': original_filename,
            'original_extension': ext,
            'kind': kind,
            'blob_container': blob_container,
            'created_at': datetime.now(timezone.utc).isoformat(),
        }
        image_names_table_client.upsert_entity(entity)
        _cache_image_name(library_id, anonymous_id, original_filename, kind)
        return anonymous_id
    except Exception:
        return None


def _get_anonymous_id_for_filename(
    library_id: str,
    original_filename: str,
    kind: str,
) -> Optional[str]:
    """Look up existing anonymous ID for a file (reverse cache first, then table)."""
    if not library_id or not original_filename:
        return None

    with _ANONYMOUS_IMAGE_CACHE_LOCK:
        reverse = _ANONYMOUS_IMAGE_REVERSE_CACHE.get(library_id)
        if reverse is not None:
            cached = reverse.get(_reverse_cache_key(kind, original_filename))
            if cached:
                return cached

    image_names_table_client = _CTX.get('image_names_table_client')
    if image_names_table_client is None:
        return None

    try:
        query = f"PartitionKey eq '{_escape_odata(library_id)}' and original_filename eq '{_escape_odata(original_filename)}' and kind eq '{_escape_odata(kind)}'"
        results = list(image_names_table_client.query_entities(query))
        if results:
            anonymous_id = str(results[0].get('RowKey') or '').strip()
            if anonymous_id:
                _cache_image_name(library_id, anonymous_id, original_filename, kind)
                return anonymous_id
    except Exception:
        pass

    return None


def _get_original_filename(library_id: str, anonymous_id: str) -> Optional[str]:
    """Look up original filename from anonymous ID, with caching."""
    if not library_id or not anonymous_id:
        return None

    with _ANONYMOUS_IMAGE_CACHE_LOCK:
        if library_id in _ANONYMOUS_IMAGE_CACHE:
            cached = _ANONYMOUS_IMAGE_CACHE[library_id].get(anonymous_id)
            if cached:
                return cached

    image_names_table_client = _CTX.get('image_names_table_client')
    if image_names_table_client is None:
        return None

    try:
        entity = image_names_table_client.get_entity(partition_key=library_id, row_key=anonymous_id)
        if entity:
            original = str(entity.get('original_filename') or '').strip()
            kind = str(entity.get('kind') or 'image').strip() or 'image'
            _cache_image_name(library_id, anonymous_id, original, kind)
            return original if original else None
    except Exception:
        pass

    return None


def resolve_physical_blob_name(library_id: str, original_filename: str, kind: str = 'image') -> str:
    """Resolve the physical blob name for a real filename: the anonymous UUID when
    the file was anonymized, else the original filename (pre-anonymization photos).

    Used by the SAS/direct-URL paths that only hold the original filename (e.g.
    People-page face thumbnails), where the metadata row isn't already in hand.
    Resolution is O(1) once the library's mappings are warm in the reverse cache."""
    if not library_id or not original_filename:
        return original_filename
    anonymous_id = _get_anonymous_id_for_filename(library_id, original_filename, kind)
    return anonymous_id or original_filename


def original_filename_for_anonymous_id(library_id: str, anonymous_id: str) -> Optional[str]:
    """Reverse of resolve_physical_blob_name: whichever real filename currently
    owns this anonymous blob id (stamped into the name-mapping table at
    finalize), or None if nothing currently claims it.

    Used to tell a genuinely orphaned blob reservation apart from a stale
    pre-rename metadata row whose blob was since claimed by a *different*,
    successfully finalized filename (see _resolve_filename_for_upload) --
    deleting the blob by UUID in the latter case would silently break that
    other, live row.
    """
    if not library_id or not anonymous_id:
        return None
    return _get_original_filename(library_id, anonymous_id)


def load_image_names_cache(library_id: str) -> None:
    """Load all image name mappings for a library into memory cache."""
    image_names_table_client = _CTX.get('image_names_table_client')
    if image_names_table_client is None:
        return

    try:
        query = f"PartitionKey eq '{_escape_odata(library_id)}'"
        results = list(image_names_table_client.query_entities(query))
        for entity in results:
            anonymous_id = str(entity.get('RowKey') or '').strip()
            original = str(entity.get('original_filename') or '').strip()
            kind = str(entity.get('kind') or 'image').strip() or 'image'
            if anonymous_id and original:
                _cache_image_name(library_id, anonymous_id, original, kind)
    except Exception:
        pass


def invalidate_image_names_cache(library_id: Optional[str] = None) -> None:
    """Clear image names cache for a library or all libraries."""
    with _ANONYMOUS_IMAGE_CACHE_LOCK:
        if library_id:
            _ANONYMOUS_IMAGE_CACHE.pop(library_id, None)
            _ANONYMOUS_IMAGE_REVERSE_CACHE.pop(library_id, None)
        else:
            _ANONYMOUS_IMAGE_CACHE.clear()
            _ANONYMOUS_IMAGE_REVERSE_CACHE.clear()


def delete_image_name_mapping(library_id: str, anonymous_id: str) -> None:
    """Delete the anonymous-ID mapping row for a deleted image and drop it from
    the cache. Best-effort: a missing row is not an error. Leaving the row behind
    would keep the original filename on record (partly defeating anonymization)
    and accumulate dead rows over time."""
    if not library_id or not anonymous_id:
        return
    image_names_table_client = _CTX.get('image_names_table_client')
    if image_names_table_client is not None:
        try:
            image_names_table_client.delete_entity(partition_key=library_id, row_key=anonymous_id)
        except Exception:
            pass
    with _ANONYMOUS_IMAGE_CACHE_LOCK:
        cached = _ANONYMOUS_IMAGE_CACHE.get(library_id)
        if cached is not None:
            cached.pop(anonymous_id, None)
        reverse = _ANONYMOUS_IMAGE_REVERSE_CACHE.get(library_id)
        if reverse is not None:
            # Drop any reverse entries (across kinds) that point at this id.
            for key in [k for k, v in reverse.items() if v == anonymous_id]:
                reverse.pop(key, None)


def get_anonymous_blob_name(
    library_id: str,
    original_filename: str,
    kind: str = 'image',
    blob_container: str = '',
) -> Optional[str]:
    """
    Get or create an anonymous ID for a file. Returns just the ID (no extension).
    For new uploads: creates a new mapping.
    For re-processing: looks up existing ID and reuses it.
    """
    if not library_id or not original_filename:
        return None

    # For re-processing (e.g., thumbnail regeneration), check if we already have an ID
    existing_id = _get_anonymous_id_for_filename(library_id, original_filename, kind)
    if existing_id:
        return existing_id

    # New upload: create mapping
    return _store_image_name_mapping(library_id, original_filename, kind, blob_container)


# The anonymous blob name the browser uploads to is reserved at /upload/init and
# read back at /finalize. It MUST be durable and stable for the whole init ->
# upload -> finalize lifecycle, because:
#   * finalize can land on a different backend replica than init (HTTP ingress
#     round-robins across up to 300 replicas, and the app scales to zero), so an
#     in-process map would miss and finalize would 404 "blob not found"; and
#   * a resumed/retried upload re-calls /upload/init, and Azure block-blob blocks
#     are staged against a specific blob name — a fresh UUID per init would orphan
#     the already-staged blocks and make commitBlockList fail (InvalidBlockList).
# Persisting it on the upload-tracking metadata row (keyed by filename) gives both
# durability across replicas and a stable name to reuse on resume.
PENDING_ANONYMOUS_BLOB_FIELD = 'pendingAnonymousBlob'


def reserve_pending_anonymous_blob(user_id: str, original_filename: str) -> Optional[str]:
    """Return the anonymous blob name for this upload, reserving one on the
    metadata row if none exists yet. Reused verbatim on a resumed /upload/init so
    the browser keeps staging blocks to the SAME blob."""
    _require_context()
    metadata_table_client = _CTX['metadata_table_client']
    if not user_id or not original_filename:
        return None
    try:
        entity = metadata_table_client.get_entity(partition_key=user_id, row_key=original_filename)
    except Exception:
        entity = {'PartitionKey': user_id, 'RowKey': original_filename}
    existing = str(entity.get(PENDING_ANONYMOUS_BLOB_FIELD) or entity.get('anonymousImageId') or '').strip()
    if existing:
        return existing
    reserved = _generate_anonymous_id()
    entity[PENDING_ANONYMOUS_BLOB_FIELD] = reserved
    try:
        metadata_table_client.upsert_entity(entity)
    except Exception:
        return reserved
    return reserved


def read_pending_anonymous_blob(user_id: str, original_filename: str) -> Optional[str]:
    """Read the anonymous blob name reserved at /upload/init (durable, replica-safe)."""
    _require_context()
    metadata_table_client = _CTX['metadata_table_client']
    if not user_id or not original_filename:
        return None
    try:
        entity = metadata_table_client.get_entity(partition_key=user_id, row_key=original_filename)
    except Exception:
        return None
    value = str(entity.get(PENDING_ANONYMOUS_BLOB_FIELD) or entity.get('anonymousImageId') or '').strip()
    return value or None


def restore_original_filename(
    library_id: str,
    anonymous_id: str,
) -> Optional[str]:
    """Look up the original filename for an anonymous ID."""
    return _get_original_filename(library_id, anonymous_id)


def _metadata_updates_affect_vector_index(updates: Dict) -> bool:
    if not isinstance(updates, dict) or not updates:
        return False
    return bool(_VECTOR_INDEX_RELEVANT_FIELDS.intersection(updates.keys()))


def _update_metadata_fields(user_id: str, filename: str, updates: Dict) -> Dict:
    _require_context()
    metadata_table_client = _CTX['metadata_table_client']
    last_exc = None
    for attempt in range(_METADATA_UPDATE_MAX_RETRIES):
        try:
            entity = metadata_table_client.get_entity(partition_key=user_id, row_key=filename)
        except Exception as exc:
            raise RuntimeError(f'Metadata entity not found for {user_id}/{filename}') from exc
        if str(entity.get('processing_state') or '').strip().lower() == 'deleted':
            raise RuntimeError('Photo has been deleted.')
        entity.update(updates or {})
        entity['last_processing_update'] = datetime.now(timezone.utc).isoformat()
        try:
            metadata_table_client.upsert_entity(entity)
            if _metadata_updates_affect_vector_index(updates):
                touch_user_vector_index_state(user_id)
            return dict(entity)
        except ResourceModifiedError as exc:
            last_exc = exc
            import time
            time.sleep(_METADATA_UPDATE_RETRY_BASE_SECONDS * (2 ** attempt))
            continue
    raise RuntimeError(f'Concurrent modification on {user_id}/{filename}') from last_exc


def _normalize_face_bbox(face: Dict) -> Dict[str, int]:
    bbox = face.get('bbox', {}) if isinstance(face, dict) else {}
    if isinstance(bbox, str):
        try:
            bbox = json.loads(bbox or '{}')
        except Exception:
            bbox = {}
    def px(key: str) -> int:
        try:
            return int(round(float(bbox.get(key, 0) or 0)))
        except Exception:
            return 0
    return {
        'left': max(0, px('left')),
        'top': max(0, px('top')),
        'width': max(0, px('width')),
        'height': max(0, px('height')),
    }


def _face_identity_key(user_id: str, filename: str, face: Dict) -> str:
    normalized = _normalize_face_bbox(face)
    return json.dumps({
        'v': 1,
        'userId': user_id,
        'filename': filename,
        **normalized,
    }, sort_keys=True, separators=(',', ':'))


def _deterministic_face_id(user_id: str, filename: str, face: Dict) -> str:
    digest = hashlib.sha256(_face_identity_key(user_id, filename, face).encode('utf-8')).hexdigest()
    return f'face-v1-{digest[:40]}'


_FACE_REDETECT_IOU_THRESHOLD = 0.35


def _bbox_iou(a: Dict, b: Dict) -> float:
    ax1, ay1 = a['left'], a['top']
    ax2, ay2 = a['left'] + a['width'], a['top'] + a['height']
    bx1, by1 = b['left'], b['top']
    bx2, by2 = b['left'] + b['width'], b['top'] + b['height']
    iw = max(0, min(ax2, bx2) - max(ax1, bx1))
    ih = max(0, min(ay2, by2) - max(ay1, by1))
    intersection = iw * ih
    if intersection <= 0:
        return 0.0
    union = (a['width'] * a['height']) + (b['width'] * b['height']) - intersection
    return intersection / union if union > 0 else 0.0


def _match_faces_by_iou(new_bboxes: List[Dict], existing_bboxes: List[Dict]) -> Dict[int, int]:
    """Greedily match new-detection indices to existing-row indices by bbox
    overlap so a re-run under a different detector/model still recognizes the
    same physical face even though its bbox shifted. Returns
    ``{new_index: existing_index}`` for matched pairs only (best IoU first,
    each index used at most once)."""
    candidates = []
    for new_idx, new_bbox in enumerate(new_bboxes):
        for existing_idx, existing_bbox in enumerate(existing_bboxes):
            iou = _bbox_iou(new_bbox, existing_bbox)
            if iou >= _FACE_REDETECT_IOU_THRESHOLD:
                candidates.append((iou, new_idx, existing_idx))
    candidates.sort(key=lambda item: item[0], reverse=True)
    matched_new: set = set()
    matched_existing: set = set()
    result: Dict[int, int] = {}
    for _iou, new_idx, existing_idx in candidates:
        if new_idx in matched_new or existing_idx in matched_existing:
            continue
        matched_new.add(new_idx)
        matched_existing.add(existing_idx)
        result[new_idx] = existing_idx
    return result


def _client_face_passes_quality_gate(face: Dict) -> bool:
    try:
        confidence = float(face.get('confidence', 0.0) or 0.0)
    except Exception:
        confidence = 0.0
    if confidence < FACE_MIN_STORE_CONFIDENCE:
        return False
    bbox = _normalize_face_bbox(face)
    if bbox['width'] <= 0 or bbox['height'] <= 0:
        return False
    try:
        image_width = max(0, int(face.get('imageWidth', 0) or 0))
    except Exception:
        image_width = 0
    try:
        image_height = max(0, int(face.get('imageHeight', 0) or 0))
    except Exception:
        image_height = 0
    if image_width <= 0 or image_height <= 0 or confidence >= FACE_LOW_CONFIDENCE_REJECT_BELOW:
        return True
    image_area = max(1, image_width * image_height)
    area_ratio = (bbox['width'] * bbox['height']) / image_area
    side_ratio = max(bbox['width'] / max(1, image_width), bbox['height'] / max(1, image_height))
    return area_ratio <= FACE_LOW_CONFIDENCE_MAX_AREA_RATIO and side_ratio <= FACE_LOW_CONFIDENCE_MAX_SIDE_RATIO


def _store_client_face_entities(user_id: str, filename: str, faces, *, force_reconcile: bool = False) -> List[str]:
    face_table_client = _CTX.get('face_table_client')
    if face_table_client is None or not isinstance(faces, list):
        return []

    candidate_faces: List[Dict] = []
    candidate_bboxes: List[Dict] = []
    for face in faces:
        if not isinstance(face, dict):
            continue
        if not _client_face_passes_quality_gate(face):
            continue
        bbox = _normalize_face_bbox(face)
        if bbox['width'] <= 0 or bbox['height'] <= 0:
            continue
        candidate_faces.append(face)
        candidate_bboxes.append(bbox)

    try:
        existing_rows = list(face_table_client.query_entities(
            f"PartitionKey eq '{_escape_odata(user_id)}' and filename eq '{_escape_odata(filename)}'"
        ))
    except Exception:
        existing_rows = []
    existing_bboxes = [_normalize_face_bbox(row) for row in existing_rows]

    # Match by bbox overlap rather than exact pixels, so re-processing under a
    # different detector/model (the whole point of a forced re-run) still
    # recognizes an already-stored face instead of treating every detection as
    # brand new. An exact-hash id only matches when the SAME detector
    # reproduces a bit-identical bbox, which a detector swap never does.
    matches = _match_faces_by_iou(candidate_bboxes, existing_bboxes)

    stored_ids: List[str] = []
    for new_idx, face in enumerate(candidate_faces):
        bbox = candidate_bboxes[new_idx]
        existing_idx = matches.get(new_idx)
        existing = existing_rows[existing_idx] if existing_idx is not None else None
        # Never resurrect a face the user rejected, whether or not the new
        # detection lands at the exact same pixel as the rejected one.
        if existing is not None:
            existing_rejected = bool(existing.get('rejected', False)) or str(existing.get('reviewStatus') or '').lower() == 'rejected'
            if existing_rejected:
                continue
        face_id = str(existing.get('RowKey')) if existing is not None else _deterministic_face_id(user_id, filename, face)
        embedding = face.get('embedding', [])
        if not isinstance(embedding, list):
            embedding = []
        embedding_values = [float(v) for v in embedding if isinstance(v, (int, float))]
        embedding_version = _sanitize_client_text(
            face.get('embeddingVersion') or face.get('modelTaxonomyVersion') or '',
            100,
        )
        entity = {
            'PartitionKey': user_id,
            'RowKey': face_id,
            'filename': filename,
            'bbox': json.dumps(bbox),
            'imageWidth': int(face.get('imageWidth', 0) or 0),
            'imageHeight': int(face.get('imageHeight', 0) or 0),
            'confidence': float(face.get('confidence', 0.0) or 0.0),
            'embedding': json.dumps(embedding_values),
            'identityKey': _face_identity_key(user_id, filename, face),
            'identityVersion': 'face-v1',
            'embeddingVersion': embedding_version,
            'modelTaxonomyVersion': embedding_version,
            'modelVersion': _sanitize_client_text(face.get('modelVersion') or '', 100),
            'model': _sanitize_client_text(face.get('model') or '', 100),
            'runtime': _sanitize_client_text(face.get('runtime') or '', 100),
            'detector': _sanitize_client_text(face.get('detector') or '', 50),
            'alignmentMethod': _sanitize_client_text(face.get('alignmentMethod') or '', 20),
            'alignmentFailureReason': _sanitize_client_text(face.get('alignmentFailureReason') or '', 200),
            'createdAt': face.get('createdAt') or None,
            'rejected': bool(face.get('rejected', False)),
        }
        if embedding_values and entity['confidence'] < SUSPICIOUS_FACE_CONFIDENCE:
            entity['reviewStatus'] = 'suspicious'
            entity['suspiciousReason'] = 'low_confidence'
        # Explicit client-provided values take precedence over the fresh defaults.
        if face.get('personId'):
            entity['personId'] = face.get('personId')
        if face.get('reviewStatus'):
            entity['reviewStatus'] = face.get('reviewStatus')
        if face.get('suspiciousReason'):
            entity['suspiciousReason'] = face.get('suspiciousReason')
        # Preserve the user's (and identity-propagation's) curation when this is
        # a re-detection of an already-stored face. Without this, a re-process
        # would overwrite the row and silently drop personId / confirmedByUser /
        # reviewStatus='confirmed', detaching merged clusters and reverting the
        # names the user set on the very next recluster. The new detection's
        # embedding and model versions are still stored (the point of a re-run);
        # only the human/curation signals are carried forward.
        if existing is not None:
            existing_person_id = str(existing.get('personId') or '')
            existing_confirmed = (
                bool(existing.get('confirmedByUser', False))
                or str(existing.get('reviewStatus') or '').lower() == 'confirmed'
            )
            existing_propagated = bool(existing.get('assignedByPropagation', False))
            if existing_person_id and not entity.get('personId'):
                entity['personId'] = existing_person_id
            if existing_confirmed:
                entity['confirmedByUser'] = True
                entity['reviewStatus'] = 'confirmed'
                entity.pop('suspiciousReason', None)
            if existing_propagated:
                entity['assignedByPropagation'] = True
            if existing_confirmed or existing_propagated:
                try:
                    entity['confidence'] = max(
                        float(entity.get('confidence', 0.0) or 0.0),
                        float(existing.get('confidence', 0.0) or 0.0),
                    )
                except Exception:
                    pass
        try:
            face_table_client.upsert_entity(entity)
            stored_ids.append(face_id)
        except Exception:
            continue

    # A forced full re-run (Tools > Backfill all photos) is the browser's
    # authoritative statement of every face in this photo right now. Any
    # existing row that matched nothing this pass -- not just shifted bbox,
    # since IoU matching above already absorbs that -- genuinely has no
    # corresponding detection any more. Clean those up (and the person they
    # belonged to) so a backfill actually replaces stale clusters instead of
    # leaving them behind alongside the fresh ones. Rejected rows are explicit
    # user curation, not stale data, so they're left untouched either way.
    if force_reconcile and existing_rows:
        matched_existing_indices = set(matches.values())
        person_table_client = _CTX.get('person_table_client')
        for existing_idx, row in enumerate(existing_rows):
            if existing_idx in matched_existing_indices:
                continue
            if bool(row.get('rejected', False)) or str(row.get('reviewStatus') or '').lower() == 'rejected':
                continue
            face_id = str(row.get('RowKey') or '')
            if not face_id:
                continue
            try:
                face_table_client.delete_entity(partition_key=user_id, row_key=face_id)
            except Exception:
                continue
            person_id = str(row.get('personId') or '')
            if not person_id or person_table_client is None:
                continue
            try:
                person = person_table_client.get_entity(partition_key=user_id, row_key=person_id)
                person_face_ids = json.loads(person.get('faceIds', '[]') or '[]')
            except Exception:
                continue
            next_face_ids = [fid for fid in person_face_ids if str(fid) != face_id]
            if next_face_ids == person_face_ids:
                continue
            try:
                if next_face_ids:
                    person['faceIds'] = json.dumps(next_face_ids)
                    person_table_client.upsert_entity(person)
                else:
                    person_table_client.delete_entity(partition_key=user_id, row_key=person_id)
            except Exception:
                pass

    return stored_ids


def _get_blob_client(container_name: str, filename: str):
    blob_service_client = _CTX.get('blob_service_client')
    if not blob_service_client or not container_name:
        return None
    if not hasattr(blob_service_client, 'get_blob_client'):
        return None
    try:
        return blob_service_client.get_blob_client(container=container_name, blob=filename)
    except Exception:
        return None


def upload_file_to_blob(container_name: str, filename: str, content: Union[bytes, BinaryIO], content_type: str) -> None:
    blob_client = _get_blob_client(container_name, filename)
    if blob_client is None:
        return
    blob_client.upload_blob(content, overwrite=True, content_settings=BlobContentSettings(content_type=content_type))


def download_file_from_blob(container_name: str, filename: str) -> bytes:
    blob_client = _get_blob_client(container_name, filename)
    if blob_client is None:
        raise ResourceNotFoundError('Blob storage is not configured')
    return blob_client.download_blob().readall()


def get_blob_properties(container_name: str, filename: str):
    blob_client = _get_blob_client(container_name, filename)
    if blob_client is None:
        raise ResourceNotFoundError('Blob storage is not configured')
    return blob_client.get_blob_properties()


def download_media_bytes(kind: str, filename: str) -> bytes:
    if kind == 'thumbnail':
        container_name = _CTX.get('blob_thumbnail_container')
    elif kind == 'cover':
        container_name = _CTX.get('blob_cover_container') or _CTX.get('blob_thumbnail_container')
    else:
        container_name = _CTX.get('blob_image_container')
    if not container_name:
        raise ResourceNotFoundError(f'{kind} storage is not configured')
    return download_file_from_blob(str(container_name), filename)


def get_media_properties(kind: str, filename: str):
    if kind == 'thumbnail':
        container_name = _CTX.get('blob_thumbnail_container')
    elif kind == 'cover':
        container_name = _CTX.get('blob_cover_container') or _CTX.get('blob_thumbnail_container')
    else:
        container_name = _CTX.get('blob_image_container')
    if not container_name:
        raise ResourceNotFoundError(f'{kind} storage is not configured')
    props = get_blob_properties(str(container_name), filename)
    content_type = getattr(getattr(props, 'content_settings', None), 'content_type', None) or 'image/jpeg'
    return {'content_type': content_type, 'size': getattr(props, 'size', None), 'last_modified': getattr(props, 'last_modified', None)}


def upload_media_file(kind: str, filename: str, content: Union[bytes, BinaryIO], content_type: str) -> None:
    if kind == 'thumbnail':
        container_name = _CTX.get('blob_thumbnail_container')
    elif kind == 'cover':
        container_name = _CTX.get('blob_cover_container') or _CTX.get('blob_thumbnail_container')
    else:
        container_name = _CTX.get('blob_image_container')
    if not container_name:
        raise RuntimeError(f'{kind} storage is not configured')
    upload_file_to_blob(str(container_name), filename, content, content_type)


def _vector_index_container_name() -> str:
    return str(
        _CTX.get('blob_vector_index_container')
        or os.getenv('BLOB_VECTOR_INDEX_CONTAINER', 'vector-index')
    ).strip()


def _vector_index_blob_key(user_id: str) -> str:
    return hashlib.sha256(str(user_id or '').encode('utf-8')).hexdigest()


def _vector_index_npz_blob_name(user_id: str) -> str:
    return f'{_vector_index_blob_key(user_id)}.npz'


def _vector_index_manifest_blob_name(user_id: str) -> str:
    return f'{_vector_index_blob_key(user_id)}.json'


def _vector_index_blob_client(blob_name: str):
    container_name = _vector_index_container_name()
    return _get_blob_client(container_name, blob_name) if container_name else None


def invalidate_user_vector_index_cache(user_id: str) -> None:
    key = str(user_id or '').strip()
    if not key:
        return
    with _VECTOR_INDEX_CACHE_LOCK:
        _VECTOR_INDEX_CACHE.pop(key, None)


def delete_user_vector_index_data(user_id: str) -> None:
    """Delete a library's cached vector-index blobs (npz + manifest) and drop
    it from the in-memory cache. Best-effort: a missing blob is not an error."""
    key = str(user_id or '').strip()
    if not key:
        return
    for blob_name in (_vector_index_npz_blob_name(key), _vector_index_manifest_blob_name(key)):
        blob_client = _vector_index_blob_client(blob_name)
        if blob_client is None:
            continue
        try:
            blob_client.delete_blob()
        except Exception:
            pass
    invalidate_user_vector_index_cache(key)


def touch_user_vector_index_state(user_id: str, *, embedding_version: Optional[str] = None) -> str:
    key = str(user_id or '').strip()
    if not key:
        return ''
    source_version = datetime.now(timezone.utc).isoformat()
    manifest = {
        'userId': key,
        'sourceVersion': source_version,
        'embeddingVersion': str(embedding_version or vision_utils.get_text_embedding_version()),
        'dirty': True,
        'updatedAt': source_version,
    }
    blob_client = _vector_index_blob_client(_vector_index_manifest_blob_name(key))
    if blob_client is not None:
        try:
            blob_client.upload_blob(
                json.dumps(manifest, ensure_ascii=False, separators=(',', ':')).encode('utf-8'),
                overwrite=True,
                content_settings=BlobContentSettings(content_type='application/json'),
            )
        except Exception:
            pass
    invalidate_user_vector_index_cache(key)
    return source_version


def _load_vector_index_manifest(user_id: str) -> Dict[str, str]:
    blob_client = _vector_index_blob_client(_vector_index_manifest_blob_name(user_id))
    if blob_client is None:
        return {}
    try:
        payload = blob_client.download_blob().readall()
    except Exception:
        return {}
    try:
        parsed = json.loads(payload.decode('utf-8'))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _load_vector_index_npz(user_id: str) -> Optional[VectorIndexSnapshot]:
    blob_client = _vector_index_blob_client(_vector_index_npz_blob_name(user_id))
    if blob_client is None:
        return None
    try:
        payload = blob_client.download_blob().readall()
    except Exception:
        return None
    try:
        with np.load(io.BytesIO(payload), allow_pickle=False) as data:
            embeddings = np.asarray(data['embeddings'], dtype=np.float32)
            row_keys_raw = data['row_keys']
            source_version = str(data['source_version'].item() if np.asarray(data['source_version']).shape == () else data['source_version'][0])
            embedding_version = str(data['embedding_version'].item() if np.asarray(data['embedding_version']).shape == () else data['embedding_version'][0])
            updated_at = str(data['updated_at'].item() if np.asarray(data['updated_at']).shape == () else data['updated_at'][0])
            row_keys_json = str(row_keys_raw.item() if np.asarray(row_keys_raw).shape == () else row_keys_raw[0])
            row_keys = json.loads(row_keys_json) if row_keys_json else []
            if not isinstance(row_keys, list):
                row_keys = []
            row_keys = [str(item) for item in row_keys if str(item).strip()]
            if embeddings.ndim != 2 or len(row_keys) != int(embeddings.shape[0]):
                return None
            return VectorIndexSnapshot(
                user_id=str(user_id),
                source_version=source_version,
                embedding_version=embedding_version,
                updated_at=updated_at,
                row_keys=row_keys,
                embeddings=embeddings,
            )
    except Exception:
        return None


def _serialize_vector_index(snapshot: VectorIndexSnapshot) -> bytes:
    buffer = io.BytesIO()
    np.savez_compressed(
        buffer,
        embeddings=np.asarray(snapshot.embeddings, dtype=np.float32),
        row_keys=np.asarray([json.dumps(snapshot.row_keys, ensure_ascii=False, separators=(',', ':'))]),
        source_version=np.asarray([snapshot.source_version]),
        embedding_version=np.asarray([snapshot.embedding_version]),
        updated_at=np.asarray([snapshot.updated_at]),
    )
    return buffer.getvalue()


def _build_user_vector_index_snapshot(user_id: str, source_version: str) -> Optional[VectorIndexSnapshot]:
    metadata_table_client = _CTX.get('metadata_table_client')
    if metadata_table_client is None:
        return None
    try:
        rows = list(metadata_table_client.query_entities(f"PartitionKey eq '{_escape_odata(user_id)}'"))
    except Exception:
        rows = []

    embedding_version = vision_utils.get_text_embedding_version()
    # Photo embeddings only share a vector space with query text embeddings when the
    # server is actually running CLIP (not the hashing fallback, which uses a
    # different dimension/space entirely).
    photo_embeddings_compatible = vision_utils.get_text_embedding_dimension() == PHOTO_EMBEDDING_DIMENSION
    row_keys: List[str] = []
    vectors: List[np.ndarray] = []
    for row in rows:
        filename = str(row.get('RowKey') or '').strip()
        if not filename:
            continue
        # Prefer the browser-computed CLIP image embedding (real visual signal) over
        # a text embedding of the tag list, so search isn't purely a function of
        # (possibly wrong) tags. Falls back to the tag-text embedding for photos
        # that haven't been reprocessed with the image-embedding pipeline yet.
        embedding: List[float] = []
        if photo_embeddings_compatible and str(row.get('photoEmbeddingVersion') or '').strip() == PHOTO_EMBEDDING_MODEL_VERSION:
            try:
                candidate = json.loads(row.get('photoEmbedding', '[]') or '[]')
            except Exception:
                candidate = []
            if isinstance(candidate, list) and len(candidate) == PHOTO_EMBEDDING_DIMENSION:
                embedding = [float(v) for v in candidate if isinstance(v, (int, float))]
        if not embedding:
            embedding = vision_utils.encode_text_embedding(
                build_semantic_text(filename, row),
            )
        if not embedding:
            continue
        vector = np.asarray(embedding, dtype=np.float32)
        if vector.ndim != 1 or vector.size == 0:
            continue
        norm = float(np.linalg.norm(vector))
        if norm <= 0:
            continue
        row_keys.append(filename)
        vectors.append((vector / norm).astype(np.float32, copy=False))

    if not row_keys:
        return VectorIndexSnapshot(
            user_id=str(user_id),
            source_version=source_version,
            embedding_version=embedding_version,
            updated_at=source_version,
            row_keys=[],
            embeddings=np.zeros((0, 0), dtype=np.float32),
        )

    embeddings = np.vstack(vectors).astype(np.float32, copy=False)
    return VectorIndexSnapshot(
        user_id=str(user_id),
        source_version=source_version,
        embedding_version=embedding_version,
        updated_at=source_version,
        row_keys=row_keys,
        embeddings=embeddings,
    )


def refresh_user_vector_index(user_id: str, *, source_version: Optional[str] = None) -> Optional[VectorIndexSnapshot]:
    key = str(user_id or '').strip()
    if not key:
        return None
    source_version = str(source_version or datetime.now(timezone.utc).isoformat())
    snapshot = _build_user_vector_index_snapshot(key, source_version)
    if snapshot is None:
        return None
    container_name = _vector_index_container_name()
    if container_name:
        blob_client = _get_blob_client(container_name, _vector_index_npz_blob_name(key))
        if blob_client is not None:
            try:
                blob_client.upload_blob(
                    _serialize_vector_index(snapshot),
                    overwrite=True,
                    content_settings=BlobContentSettings(content_type='application/octet-stream'),
                )
            except Exception:
                pass
        manifest = {
            'userId': key,
            'sourceVersion': snapshot.source_version,
            'embeddingVersion': snapshot.embedding_version,
            'rowCount': len(snapshot.row_keys),
            'dirty': False,
            'updatedAt': snapshot.updated_at,
        }
        manifest_client = _get_blob_client(container_name, _vector_index_manifest_blob_name(key))
        if manifest_client is not None:
            try:
                manifest_client.upload_blob(
                    json.dumps(manifest, ensure_ascii=False, separators=(',', ':')).encode('utf-8'),
                    overwrite=True,
                    content_settings=BlobContentSettings(content_type='application/json'),
                )
            except Exception:
                pass
    with _VECTOR_INDEX_CACHE_LOCK:
        _VECTOR_INDEX_CACHE[key] = {
            'source_version': snapshot.source_version,
            'embedding_version': snapshot.embedding_version,
            'updated_at': snapshot.updated_at,
            'row_keys': snapshot.row_keys,
            'embeddings': snapshot.embeddings,
        }
    return snapshot


def get_user_vector_index(user_id: str, *, allow_refresh: bool = True) -> Optional[Dict[str, object]]:
    key = str(user_id or '').strip()
    if not key:
        return None

    manifest = _load_vector_index_manifest(key)
    manifest_source_version = str(manifest.get('sourceVersion') or '').strip()
    manifest_dirty = bool(manifest.get('dirty'))
    current_embedding_version = vision_utils.get_text_embedding_version()

    with _VECTOR_INDEX_CACHE_LOCK:
        cached = _VECTOR_INDEX_CACHE.get(key)
        if cached:
            if (
                cached.get('source_version') == manifest_source_version
                and cached.get('embedding_version') == current_embedding_version
                and not manifest_dirty
            ):
                return cached

    snapshot = _load_vector_index_npz(key)
    if snapshot and snapshot.source_version == manifest_source_version and snapshot.embedding_version == current_embedding_version and not manifest_dirty:
        data = {
            'source_version': snapshot.source_version,
            'embedding_version': snapshot.embedding_version,
            'updated_at': snapshot.updated_at,
            'row_keys': snapshot.row_keys,
            'embeddings': snapshot.embeddings,
        }
        with _VECTOR_INDEX_CACHE_LOCK:
            _VECTOR_INDEX_CACHE[key] = data
        return data

    if not allow_refresh:
        return None

    source_version = manifest_source_version or datetime.now(timezone.utc).isoformat()
    refreshed = refresh_user_vector_index(key, source_version=source_version)
    if refreshed is None:
        return None
    return {
        'source_version': refreshed.source_version,
        'embedding_version': refreshed.embedding_version,
        'updated_at': refreshed.updated_at,
        'row_keys': refreshed.row_keys,
        'embeddings': refreshed.embeddings,
    }


def _vector_index_container_client():
    blob_service_client = _CTX.get('blob_service_client')
    container_name = _vector_index_container_name()
    if not blob_service_client or not container_name or not hasattr(blob_service_client, 'get_container_client'):
        return None
    try:
        return blob_service_client.get_container_client(container_name)
    except Exception:
        return None


def list_vector_index_users_from_storage(*, limit: int = 200) -> List[str]:
    limit = max(0, int(limit))
    if limit <= 0:
        return []
    container_client = _vector_index_container_client()
    if container_client is None:
        return []
    users: List[str] = []
    seen = set()
    try:
        blobs = container_client.list_blobs()
    except Exception:
        return []
    for blob in blobs:
        name = str(getattr(blob, 'name', '') or '')
        if not name.endswith('.json'):
            continue
        blob_client = _vector_index_blob_client(name)
        if blob_client is None:
            continue
        try:
            payload = blob_client.download_blob().readall()
            parsed = json.loads(payload.decode('utf-8'))
        except Exception:
            continue
        user_id = str(parsed.get('userId') or '').strip() if isinstance(parsed, dict) else ''
        if not user_id or user_id in seen:
            continue
        users.append(user_id)
        seen.add(user_id)
        if len(users) >= limit:
            break
    return users


def prime_available_vector_indexes(*, max_users: int = 200) -> Dict[str, int]:
    loaded = 0
    skipped = 0
    failed = 0
    for user_id in list_vector_index_users_from_storage(limit=max_users):
        try:
            index = get_user_vector_index(user_id, allow_refresh=False)
            if index:
                loaded += 1
            else:
                skipped += 1
        except Exception:
            failed += 1
    return {'loaded': loaded, 'skipped': skipped, 'failed': failed}


def vector_search_candidates(user_id: str, query_embedding: List[float], top_k: int = 200, *, allow_refresh: bool = True) -> List[Tuple[str, float]]:
    if not query_embedding or top_k <= 0:
        return []
    index = get_user_vector_index(user_id, allow_refresh=allow_refresh)
    if not index:
        return []
    embeddings = index.get('embeddings')
    row_keys = index.get('row_keys')
    if not isinstance(embeddings, np.ndarray) or not isinstance(row_keys, list):
        return []
    if embeddings.ndim != 2 or embeddings.shape[0] == 0 or embeddings.shape[1] == 0:
        return []

    query = np.asarray([float(value) for value in query_embedding if isinstance(value, (int, float))], dtype=np.float32)
    if query.ndim != 1 or query.size == 0 or query.size != embeddings.shape[1]:
        return []
    query_norm = float(np.linalg.norm(query))
    if query_norm <= 0:
        return []
    query = query / query_norm

    scores = embeddings @ query
    if scores.size == 0:
        return []
    candidate_count = min(int(top_k), int(scores.size))
    if candidate_count <= 0:
        return []
    if candidate_count == int(scores.size):
        ranked_indices = np.arange(scores.size)
    else:
        ranked_indices = np.argpartition(scores, scores.size - candidate_count)[-candidate_count:]
    ranked_indices = ranked_indices[np.argsort(scores[ranked_indices])[::-1]]
    results: List[Tuple[str, float]] = []
    for idx in ranked_indices:
        row_key = row_keys[int(idx)] if int(idx) < len(row_keys) else ''
        if not row_key:
            continue
        results.append((row_key, float(scores[int(idx)])))
    return results


def prime_user_vector_index(user_id: str) -> None:
    try:
        get_user_vector_index(user_id)
    except Exception:
        pass


def get_or_create_metadata(user_id: str, filename: str) -> Dict:
    _require_context()
    metadata_table_client = _CTX['metadata_table_client']
    try:
        return metadata_table_client.get_entity(partition_key=user_id, row_key=filename)
    except Exception:
        return {
            'PartitionKey': user_id,
            'RowKey': filename,
            'uploadDate': datetime.now(timezone.utc).isoformat(),
            'rating': 0,
            'likes': 0,
            'likedBy': json.dumps([]),
            'tags': json.dumps([]),
            'objects': json.dumps([]),
            'subjectTags': json.dumps([]),
            'backgroundTags': json.dumps([]),
            'weakTags': json.dumps([]),
            'tagBuckets': json.dumps({}),
            'tagMetadata': json.dumps([]),
            'ocrText': '',
            'caption': '',
            'latitude': '',
            'longitude': '',
            'address': '',
            'locationCity': '',
            'locationCountry': '',
            'fileHash': '',
            'perceptualHash': '',
            'faces': json.dumps([]),
            'faceCount': 0,
            'peopleIds': json.dumps([]),
            'semanticText': '',
            'semanticEmbedding': json.dumps([]),
            'semanticEmbeddingVersion': '',
            'photoEmbedding': json.dumps([]),
            'photoEmbeddingVersion': '',
        }


def apply_upload_hash_result(metadata: Dict, file_hash: str) -> None:
    expected_hash = str(metadata.get('upload_sha256_expected') or '')
    if not expected_hash:
        return

    metadata['upload_sha256_actual'] = file_hash
    metadata['upload_sha256_match'] = expected_hash == file_hash
    if expected_hash != file_hash:
        metadata['corrupted'] = True
        metadata['verification_error'] = 'upload_sha256_mismatch'
        metadata['corrupted_at'] = datetime.now(timezone.utc).isoformat()
        return

    if metadata.get('verification_error') == 'upload_sha256_mismatch':
        metadata.pop('verification_error', None)
    metadata['corrupted'] = False
    metadata.pop('corrupted_at', None)


def hamming_distance(hash1: str, hash2: str) -> int:
    if len(hash1) != len(hash2):
        return max(len(hash1), len(hash2))
    return sum(c1 != c2 for c1, c2 in zip(hash1, hash2))


def detect_duplicates(user_id: str, file_hash: str, perceptual_hash: Optional[str] = None) -> List[Dict]:
    """Detect duplicates. Exact-hash check is an O(1) point lookup against the
    hash index (see _store_hash_index) rather than a scan of the user's whole
    partition -- that scan cost grew with library size, so early uploads in a
    batch were fast and later ones in the same batch got progressively slower
    as the partition filled up. If perceptual_hash is None, only the exact
    check runs (fast path)."""
    _require_context()
    metadata_table_client = _CTX['metadata_table_client']
    hash_index_table_client = _CTX.get('hash_index_table_client')

    duplicates = []
    try:
        matched_filename = None
        if hash_index_table_client is not None:
            try:
                index_row = hash_index_table_client.get_entity(partition_key=user_id, row_key=file_hash)
                candidate = str(index_row.get('filename') or '')
            except Exception:
                candidate = ''
            if candidate:
                # Self-heal: confirm the photo the index points at still exists
                # before reporting a duplicate, so a stale index row (left behind
                # by a delete path that hasn't caught up) can never cause a real
                # upload to be wrongly skipped as "already have it".
                try:
                    metadata_table_client.get_entity(partition_key=user_id, row_key=candidate)
                    matched_filename = candidate
                except Exception:
                    try:
                        hash_index_table_client.delete_entity(partition_key=user_id, row_key=file_hash)
                    except Exception:
                        pass
        else:
            # No hash index configured (e.g. older deploy) -- fall back to the
            # original partition scan so dedup still works, just not O(1).
            query = f"PartitionKey eq '{_escape_odata(user_id)}' and fileHash eq '{_escape_odata(file_hash)}'"
            exact_matches = list(metadata_table_client.query_entities(query))
            if exact_matches:
                matched_filename = str(exact_matches[0]['RowKey'])

        if matched_filename:
            duplicates.append({
                'filename': matched_filename,
                'type': 'exact',
                'hash': file_hash,
            })

        # Only perform perceptual hash matching if explicitly provided
        if perceptual_hash:
            query = f"PartitionKey eq '{user_id}'"
            all_photos = list(metadata_table_client.query_entities(query))
            for photo in all_photos:
                other_phash = photo.get('perceptualHash', '')
                if other_phash and other_phash != perceptual_hash:
                    dist = hamming_distance(perceptual_hash, other_phash)
                    if dist <= 5:
                        duplicates.append({
                            'filename': photo['RowKey'],
                            'type': 'perceptual',
                            'similarity': 100 - (dist * 5),
                        })
    except Exception:
        pass

    return duplicates


def list_known_file_hashes(user_id: str) -> Dict[str, str]:
    """Return {fileHash: filename} for every indexed photo in this library.

    Meant to be fetched ONCE per upload batch by the frontend (not per file --
    that would just reintroduce the scan-per-upload problem this index was
    built to fix) so the browser can skip re-uploading a file it can already
    tell is a duplicate, before spending any transfer bandwidth on it. The
    backend's own point-lookup in detect_duplicates() stays authoritative at
    finalize time regardless -- this is a bandwidth-saving prefetch, not a
    replacement for server-side validation (two tabs/devices could otherwise
    race past a client-side-only check).
    """
    _require_context()
    hash_index_table_client = _CTX.get('hash_index_table_client')
    if hash_index_table_client is None:
        return {}
    try:
        rows = hash_index_table_client.query_entities(f"PartitionKey eq '{_escape_odata(user_id)}'")
        return {str(row.get('RowKey') or ''): str(row.get('filename') or '') for row in rows if row.get('RowKey')}
    except Exception:
        return {}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sanitize_client_exif(exif_value) -> Dict[str, str]:
    if not isinstance(exif_value, dict):
        return {}
    cleaned: Dict[str, str] = {}
    for key, value in list(exif_value.items())[:CLIENT_PROCESSING_MAX_EXIF_KEYS]:
        safe_key = str(key).strip()[:128]
        if not safe_key:
            continue
        if isinstance(value, (dict, list, tuple)):
            safe_value = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
        else:
            safe_value = str(value)
        cleaned[safe_key] = safe_value[:CLIENT_PROCESSING_MAX_EXIF_VALUE_LENGTH]
    return cleaned


def _valid_decimal(value, minimum: float, maximum: float) -> str:
    try:
        parsed = float(value)
    except Exception:
        return ''
    if parsed < minimum or parsed > maximum:
        return ''
    return str(round(parsed, 7))


def _reverse_geocode_fallback(latitude: str, longitude: str) -> Dict[str, str]:
    try:
        return maps_utils.reverse_geocode(latitude, longitude) or {}
    except Exception:
        return {}


def _normalize_client_report(client_asset_id: str, report_value) -> List[Dict]:
    if not isinstance(report_value, list):
        return []
    normalized = []
    for item in report_value[:20]:
        if not isinstance(item, dict):
            continue
        step = str(item.get('step') or '').strip()
        status = str(item.get('status') or '').strip()
        reason = str(item.get('reason') or 'unknown_error').strip()
        if step not in CLIENT_PROCESSING_ALLOWED_STEPS or status not in CLIENT_PROCESSING_ALLOWED_STATUSES:
            continue
        if reason not in CLIENT_PROCESSING_ALLOWED_REASONS:
            reason = 'unknown_error'
        try:
            duration_ms = max(0, min(int(item.get('durationMs') or 0), 24 * 60 * 60 * 1000))
        except Exception:
            duration_ms = 0
        normalized_item = {
            'clientAssetId': str(item.get('clientAssetId') or client_asset_id or '')[:128],
            'step': step,
            'status': status,
            'reason': reason,
            'durationMs': duration_ms,
            'model': str(item.get('model') or '')[:100],
            'modelVersion': str(item.get('modelVersion') or '')[:100],
            'modelTaxonomyVersion': str(item.get('modelTaxonomyVersion') or '')[:100],
            'runtime': str(item.get('runtime') or '')[:100],
        }
        detail = str(item.get('detail') or '').strip()
        if detail:
            normalized_item['detail'] = detail[:500]
        model_availability = str(item.get('modelAvailability') or '').strip()
        if model_availability in CLIENT_PROCESSING_ALLOWED_MODEL_AVAILABILITY:
            normalized_item['modelAvailability'] = model_availability
        model_cache_status = str(item.get('modelCacheStatus') or '').strip()
        if model_cache_status in CLIENT_PROCESSING_ALLOWED_MODEL_CACHE_STATUS:
            normalized_item['modelCacheStatus'] = model_cache_status
        model_manifest_version = str(item.get('modelManifestVersion') or '').strip()
        if model_manifest_version:
            normalized_item['modelManifestVersion'] = model_manifest_version[:100]
        try:
            model_acquisition_ms = max(0, min(int(item.get('modelAcquisitionMs') or 0), 24 * 60 * 60 * 1000))
        except Exception:
            model_acquisition_ms = 0
        if model_acquisition_ms > 0:
            normalized_item['modelAcquisitionMs'] = model_acquisition_ms
        source_kind = str(item.get('sourceKind') or '').strip()
        if source_kind in CLIENT_PROCESSING_ALLOWED_SOURCE_KINDS:
            normalized_item['sourceKind'] = source_kind
        source_format = str(item.get('sourceFormat') or '').strip().lower()
        if source_format:
            normalized_item['sourceFormat'] = source_format[:40]
        raw_parser_version = str(item.get('rawParserVersion') or '').strip()
        if raw_parser_version:
            normalized_item['rawParserVersion'] = raw_parser_version[:100]
        for field in ('previewWidth', 'previewHeight', 'originalBytes', 'sourceBytes'):
            try:
                value = int(item.get(field) or 0)
            except Exception:
                value = 0
            if value > 0:
                normalized_item[field] = min(value, 10 * 1024 * 1024 * 1024 if field.endswith('Bytes') else 100000)
        normalized.append(normalized_item)
    return normalized


def _status_from_client_report(item: Dict) -> Optional[str]:
    step = str(item.get('step') or '').strip()
    status = str(item.get('status') or '').strip()
    reason = str(item.get('reason') or '').strip()
    if status == 'done':
        return 'done'
    if status in {'failed', 'timeout'}:
        if step == 'ai_vision' and reason in AI_VISION_RETRYABLE_REASONS:
            return 'pending'
        return 'failed'
    if status == 'unsupported':
        return 'unsupported'
    if status == 'skipped':
        if step == 'face' and reason == 'background_throttled':
            return 'pending'
        if step == 'ai_vision' and reason in AI_VISION_RETRYABLE_REASONS:
            return 'pending'
        if reason in {'upstream_incomplete', 'raw_container_unsupported', 'raw_preview_missing', 'raw_exif_only'}:
            return 'no_data'
        return 'skipped'
    return None


def _apply_client_report_statuses(metadata: Dict, report: List[Dict]) -> None:
    reported_steps = set()
    for item in report:
        step = str(item.get('step') or '').strip()
        status = _status_from_client_report(item)
        if step in CLIENT_PROCESSING_ALLOWED_STEPS and status:
            reported_steps.add(step)
            field = f'{step}_status'
            current = str(metadata.get(field) or '').strip().lower()
            if current not in {'done', 'no_data'}:
                # Guard: never overwrite a thumbnail that was previously generated
                # successfully (recorded in processing_metadata.client_thumbnail)
                # with a transient failure or unsupported report. This prevents a
                # backfill run — which resets thumbnail_status from 'done' to
                # 'running' via claim_processing_lease — from permanently marking a
                # working thumbnail as failed when the browser hits memory pressure
                # or a canvas API hiccup during the heavier reprocessing pipeline.
                if step == 'thumbnail' and status in {'failed', 'unsupported', 'timeout'}:
                    try:
                        prior_meta = json.loads(metadata.get('processing_metadata') or '{}')
                    except Exception:
                        prior_meta = {}
                    if isinstance(prior_meta, dict) and isinstance(prior_meta.get('client_thumbnail'), dict):
                        continue
                metadata[field] = status
    if reported_steps:
        for step in BROWSER_PROCESSING_STEPS:
            field = f'{step}_status'
            if step not in reported_steps and str(metadata.get(field) or '').strip().lower() == 'running':
                metadata[field] = 'no_data'


def _client_source_provenance(value) -> Dict:
    if not isinstance(value, dict):
        return {}
    provenance = {}
    source_kind = str(value.get('sourceKind') or '').strip()
    if source_kind in CLIENT_PROCESSING_ALLOWED_SOURCE_KINDS:
        provenance['sourceKind'] = source_kind
    source_format = str(value.get('sourceFormat') or '').strip().lower()
    if source_format:
        provenance['sourceFormat'] = source_format[:40]
    raw_parser_version = str(value.get('rawParserVersion') or '').strip()
    if raw_parser_version:
        provenance['rawParserVersion'] = raw_parser_version[:100]
    for field in ('previewWidth', 'previewHeight', 'originalBytes', 'sourceBytes'):
        try:
            parsed = int(value.get(field) or 0)
        except Exception:
            parsed = 0
        if parsed > 0:
            provenance[field] = min(parsed, 10 * 1024 * 1024 * 1024 if field.endswith('Bytes') else 100000)
    return provenance


def _thumbnail_matches_source(source_bytes: bytes, thumbnail_bytes: bytes, source_kind: str) -> bool:
    if not source_bytes or not thumbnail_bytes:
        return False
    expected_source = source_bytes
    if str(source_kind or '').strip() == 'raw_embedded_jpeg':
        preview = extract_raw_preview_bytes(source_bytes)
        if not preview:
            return False
        expected_source = preview
    try:
        expected_thumbnail = compute_file_hash(create_thumbnail_data(expected_source))
        actual_thumbnail = compute_file_hash(thumbnail_bytes)
    except Exception:
        return False
    return expected_thumbnail == actual_thumbnail


def _filename_extension(filename: str) -> str:
    return filename.rsplit('.', 1)[-1].lower() if filename and '.' in filename else ''


def _looks_like_jpeg_bytes(data: bytes) -> bool:
    return bool(data) and data.startswith(b'\xff\xd8')


def _create_server_thumbnail_for_upload(image_bytes: bytes, filename: str) -> Optional[bytes]:
    if is_video_file(filename):
        return create_video_thumbnail_data(image_bytes, filename)
    ext = _filename_extension(filename)
    if ext in HEIF_EXTENSIONS:
        preview_bytes = convert_image_to_jpeg(image_bytes, filename)
        if not _looks_like_jpeg_bytes(preview_bytes):
            return None
        return create_thumbnail_data(preview_bytes)
    if ext in RAW_EXTENSIONS_RAWPY or ext in RAW_EXTENSIONS_CINEMA:
        # RAW thumbnails are normally produced in-browser, but the browser cannot
        # decode every RAW (e.g. DNG that stores its raw data as a lossless JPEG),
        # so generate one server-side too. extract_raw_preview_bytes tries the
        # embedded JPEG, then rawpy/exiftool, yielding a decodable preview.
        preview_bytes = extract_raw_preview_bytes(image_bytes, filename)
        if not preview_bytes or not _looks_like_jpeg_bytes(preview_bytes):
            return None
        return create_thumbnail_data(preview_bytes)
    # Ordinary photos land here -- used when the reactive fallback below runs
    # for a format that normally never needs the backend (the browser thumbnail
    # failed for some other reason, e.g. a canvas API hiccup under memory
    # pressure). create_thumbnail_data handles arbitrary PIL-openable bytes.
    return create_thumbnail_data(image_bytes)


def _parse_json_list(value) -> List[str]:
    try:
        parsed = json.loads(value or '[]') if isinstance(value, str) else value
    except Exception:
        parsed = []
    if not isinstance(parsed, list):
        return []
    return [str(item).strip() for item in parsed if str(item).strip()]


def _normalize_tag_list(values, limit: int = CLIENT_PROCESSING_MAX_AI_TAGS) -> List[str]:
    if not isinstance(values, list):
        return []
    normalized: List[str] = []
    seen = set()
    for value in values:
        tag = str(value or '').strip().lower()
        tag = ''.join(ch for ch in tag if ch.isalnum() or ch in (' ', '-', '_')).strip()
        if not tag or len(tag) > 80 or tag in seen:
            continue
        seen.add(tag)
        normalized.append(tag)
        if len(normalized) >= limit:
            break
    return normalized


def _sanitize_client_text(value, limit: int = CLIENT_PROCESSING_MAX_AI_TEXT_LENGTH) -> str:
    text = str(value or '').replace('\x00', ' ').strip()
    return text[:limit]


def _sanitize_client_predictions(value) -> List[Dict]:
    if not isinstance(value, list):
        return []
    predictions = []
    for item in value[:400]:
        if not isinstance(item, dict):
            continue
        label = _normalize_tag_list([item.get('label')], limit=1)
        if not label:
            continue
        try:
            score = float(item.get('score') or 0)
        except Exception:
            score = 0
        predictions.append({'label': label[0], 'score': round(max(0, min(score, 1)), 4)})
    return predictions


def _sanitize_client_image_embedding(value) -> List[float]:
    if not isinstance(value, list) or len(value) != PHOTO_EMBEDDING_DIMENSION:
        return []
    try:
        vector = [float(item) for item in value]
    except Exception:
        return []
    if not all(np.isfinite(v) for v in vector):
        return []
    norm = float(np.linalg.norm(np.asarray(vector, dtype=np.float32)))
    if norm <= 0:
        return []
    return [v / norm for v in vector]


def _json_compact(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def _tag_provenance(source: str, model_provenance: Dict, source_provenance: Dict, client_asset_id: str) -> Dict:
    provenance = {
        'source': source,
        'clientAssetId': client_asset_id,
        **(source_provenance or {}),
    }
    for key in ('model', 'modelVersion', 'modelTaxonomyVersion', 'modelManifestVersion', 'runtime'):
        if model_provenance.get(key):
            provenance[key] = model_provenance[key]
    return provenance


def _build_curated_ai_tag_payload(
    existing_tags: List[str],
    ai_tags: List[str],
    ai_objects: List[str],
    predictions: List[Dict],
    person_label: List[str],
    person_score: float,
    ai_source_provenance: Dict,
    ai_model_provenance: Dict,
    client_asset_id: str,
) -> Dict[str, object]:
    # Real per-label confidence from the classifier, keyed by its raw label, so
    # ai_tag/ai_object records reflect the model's actual score instead of a flat
    # per-source default (which previously let low-confidence guesses through at
    # full weight and made the AI_TAG_MIN_CONFIDENCE/GENERIC_TAG_MIN_CONFIDENCE
    # thresholds in curate_tag_records ineffective for these sources).
    score_by_label: Dict[str, float] = {}
    for item in predictions:
        label = str(item.get('label') or '').strip()
        if label and label not in score_by_label:
            try:
                score_by_label[label] = float(item.get('score'))
            except Exception:
                pass

    records: List[Dict] = []
    for tag in normalize_tags(existing_tags):
        records.append({
            'label': tag,
            'source': 'stored',
            'confidence': 0.95,
            'provenance': {'source': 'existing_metadata'},
        })
    for tag in ai_tags:
        records.append({
            'label': tag,
            'source': 'ai_tag',
            'confidence': score_by_label.get(tag),
            'provenance': _tag_provenance('browser_ai_tag', ai_model_provenance, ai_source_provenance, client_asset_id),
        })
    for tag in ai_objects:
        records.append({
            'label': tag,
            'source': 'ai_object',
            'confidence': score_by_label.get(tag),
            'provenance': _tag_provenance('browser_ai_object', ai_model_provenance, ai_source_provenance, client_asset_id),
        })
    for item in predictions:
        records.append({
            'label': item.get('label'),
            'source': 'ai_prediction',
            'confidence': item.get('score'),
            'provenance': _tag_provenance('browser_ai_prediction', ai_model_provenance, ai_source_provenance, client_asset_id),
        })
    for tag in person_label:
        records.append({
            'label': tag,
            'source': 'ai_person',
            'confidence': person_score,
            'provenance': _tag_provenance('browser_ai_person', ai_model_provenance, ai_source_provenance, client_asset_id),
        })
    return curate_tag_records(records)


def _apply_exif_metadata(metadata: Dict, exif_data: Dict[str, str]) -> Tuple[str, str]:
    lat, lon = extract_gps_decimal_from_exif(exif_data)
    if lat and lon:
        exif_data['GPS.LatitudeDecimal'] = lat
        exif_data['GPS.LongitudeDecimal'] = lon
        metadata['latitude'] = lat
        metadata['longitude'] = lon
    metadata['exifData'] = json.dumps(exif_data, ensure_ascii=False, separators=(',', ':'))
    metadata['exifCount'] = len(exif_data)
    return lat, lon


def _extract_server_exif_for_processing(image_bytes: bytes, filename: str) -> Dict[str, str]:
    try:
        return extract_exif_from_bytes(image_bytes, filename)
    except Exception:
        return {}


def _client_model_provenance(value) -> Dict:
    if not isinstance(value, dict):
        return {}
    provenance = {}
    model_availability = str(value.get('modelAvailability') or '').strip()
    if model_availability in CLIENT_PROCESSING_ALLOWED_MODEL_AVAILABILITY:
        provenance['modelAvailability'] = model_availability
    model_cache_status = str(value.get('modelCacheStatus') or '').strip()
    if model_cache_status in CLIENT_PROCESSING_ALLOWED_MODEL_CACHE_STATUS:
        provenance['modelCacheStatus'] = model_cache_status
    for source, target in (
        ('model', 'model'),
        ('modelVersion', 'modelVersion'),
        ('modelTaxonomyVersion', 'modelTaxonomyVersion'),
        ('modelManifestVersion', 'modelManifestVersion'),
        ('runtime', 'runtime'),
    ):
        cleaned = str(value.get(source) or '').strip()
        if cleaned:
            provenance[target] = cleaned[:100]
    try:
        acquisition_ms = int(value.get('modelAcquisitionMs') or 0)
    except Exception:
        acquisition_ms = 0
    if acquisition_ms > 0:
        provenance['modelAcquisitionMs'] = max(0, min(acquisition_ms, 24 * 60 * 60 * 1000))
    return provenance


def _is_local_vision_fallback_provenance(value: Dict) -> bool:
    if not isinstance(value, dict):
        return False
    model = str(value.get('model') or '').strip()
    taxonomy = str(value.get('modelTaxonomyVersion') or '').strip()
    runtime = str(value.get('runtime') or '').strip()
    return (
        model == LOCAL_VISION_FALLBACK_MODEL
        or taxonomy == LOCAL_VISION_FALLBACK_TAXONOMY_VERSION
        or runtime == LOCAL_VISION_FALLBACK_RUNTIME
    )


def _remove_tags_from_json_list(raw_value, tags_to_remove: List[str]) -> str:
    remove_set = set(_normalize_tag_list(tags_to_remove, limit=CLIENT_PROCESSING_MAX_AI_TAGS))
    existing = _normalize_tag_list(_parse_json_list(raw_value), limit=CLIENT_PROCESSING_MAX_AI_TAGS)
    if not remove_set:
        return json.dumps(existing, ensure_ascii=False, separators=(',', ':'))
    return json.dumps([tag for tag in existing if tag not in remove_set], ensure_ascii=False, separators=(',', ':'))


def _clear_previous_local_vision_fallback_metadata(metadata: Dict) -> None:
    try:
        processing = json.loads(metadata.get('processing_metadata') or '{}')
    except Exception:
        processing = {}
    previous_ai = processing.get('client_ai_vision') if isinstance(processing, dict) else None
    if not isinstance(previous_ai, dict) or not _is_local_vision_fallback_provenance(previous_ai):
        return

    fallback_tags = _normalize_tag_list(previous_ai.get('tags'), limit=CLIENT_PROCESSING_MAX_AI_TAGS)
    fallback_objects = _normalize_tag_list(previous_ai.get('objects') or fallback_tags, limit=CLIENT_PROCESSING_MAX_AI_TAGS)
    if fallback_tags:
        metadata['tags'] = _remove_tags_from_json_list(metadata.get('tags', '[]'), fallback_tags)
    if fallback_objects:
        metadata['objects'] = _remove_tags_from_json_list(metadata.get('objects', '[]'), fallback_objects)

    caption = str(metadata.get('caption') or '').strip()
    caption_lower = caption.lower()
    if caption and (
        caption_lower.startswith('photo with ')
        or caption_lower.startswith('photo containing ')
    ):
        metadata['caption'] = ''


def _client_report_has_retryable_ai_vision_failure(report: List[Dict]) -> bool:
    for item in report:
        if str(item.get('step') or '').strip() != 'ai_vision':
            continue
        status = str(item.get('status') or '').strip().lower()
        reason = str(item.get('reason') or '').strip().lower()
        if status in {'failed', 'skipped', 'timeout'} and reason in AI_VISION_RETRYABLE_REASONS:
            return True
    return False


def _client_report_needs_server_exif_fallback(report: List[Dict]) -> bool:
    for item in report:
        if str(item.get('step') or '').strip() != 'exif':
            continue
        status = str(item.get('status') or '').strip().lower()
        if status in {'unsupported', 'failed', 'timeout', 'skipped'}:
            return True
    return False


def _client_report_needs_server_thumbnail_fallback(report: List[Dict]) -> bool:
    # Deliberately excludes 'skipped': that status is only ever reported for
    # RAW/video (see rawFallback / createVideoBrowserThumbnail in
    # PhotoGallery.tsx), formats that never attempt a client-side thumbnail at
    # all and instead rely on ipworker's queued 'thumbnail' step (video also
    # still gets an eager one at finalize time -- see finalize_uploaded_file).
    # Re-triggering here would just redo work ipworker already has queued.
    for item in report:
        if str(item.get('step') or '').strip() != 'thumbnail':
            continue
        status = str(item.get('status') or '').strip().lower()
        if status in {'unsupported', 'failed', 'timeout'}:
            return True
    return False


def _build_client_semantic_text(filename: str, metadata: Dict) -> str:
    return build_semantic_text(filename, metadata)


def _refresh_semantic_fields(filename: str, metadata: Dict) -> None:
    semantic_text = _build_client_semantic_text(filename, metadata)
    metadata['semanticText'] = semantic_text
    metadata['semanticLayers'] = _json_compact(build_semantic_layers(filename, metadata))
    metadata['semanticEmbedding'] = json.dumps(
        vision_utils.encode_text_embedding(semantic_text),
        ensure_ascii=False,
        separators=(',', ':'),
    )
    metadata['semanticEmbeddingVersion'] = vision_utils.get_text_embedding_version()


def _merge_processing_metadata(entity: Dict, key: str, value) -> None:
    try:
        processing = json.loads(entity.get('processing_metadata') or '{}')
    except Exception:
        processing = {}
    processing[key] = value
    entity['processing_metadata'] = json.dumps(processing, ensure_ascii=False, separators=(',', ':'))


def _apply_server_exif_fallback(
    user_id: str,
    filename: str,
    metadata: Dict,
    image_bytes: bytes,
    *,
    fallback_for: str,
) -> Dict[str, object]:
    status_updates: Dict[str, object] = {}
    server_exif = _extract_server_exif_for_processing(image_bytes, filename)
    if server_exif:
        lat, lon = _apply_exif_metadata(metadata, server_exif)
        _merge_processing_metadata(metadata, 'server_exif', {
            'source': 'server',
            'acceptedAt': _utc_now(),
            'fallbackFor': fallback_for,
            'hasGps': bool(lat and lon),
        })
        status_updates['exif_status'] = 'done'
        if lat and lon and not _CTX.get('queue_map_on_upload'):
            mark_step_done(user_id, filename, 'map_detection', result={
                'source': 'server',
                'latitude': lat,
                'longitude': lon,
            })
            status_updates['map_detection_status'] = 'done'
        elif not lat or not lon:
            mark_step_no_data(user_id, filename, 'map_detection')
            status_updates['map_detection_status'] = 'no_data'
    else:
        metadata['exifData'] = json.dumps({}, ensure_ascii=False, separators=(',', ':'))
        metadata['exifCount'] = 0
        # Found via a real end-to-end test (ipworker + Azurite): a photo with
        # truly no extractable EXIF via any path (client report said no data,
        # and server-side exiftool/PIL re-extraction also found nothing) left
        # exif_status stuck at whatever it was before this call ('running',
        # if a lease claim had just set it) -- this branch never resolved it
        # to a terminal status. Pre-existing gap, not specific to ipworker
        # (the browser's own fallback-trigger path hits the same code).
        status_updates['exif_status'] = 'no_data'
        mark_step_no_data(user_id, filename, 'map_detection')
        status_updates['map_detection_status'] = 'no_data'
    return status_updates


def _apply_server_thumbnail_fallback(
    user_id: str,
    filename: str,
    metadata: Dict,
    image_bytes: bytes,
    *,
    fallback_for: str,
) -> Dict[str, object]:
    status_updates: Dict[str, object] = {}
    try:
        thumbnail_bytes = _create_server_thumbnail_for_upload(image_bytes, filename)
    except Exception:
        thumbnail_bytes = None
    if not thumbnail_bytes:
        return status_updates
    thumbnail_blob_name = str(metadata.get('anonymousImageId') or '').strip() or filename
    upload_media_file('thumbnail', thumbnail_blob_name, thumbnail_bytes, 'image/jpeg')
    mark_step_done(user_id, filename, 'thumbnail', result={
        'source': 'server',
        'reason': fallback_for,
    })
    status_updates['thumbnail_status'] = 'done'
    _merge_processing_metadata(metadata, 'server_thumbnail', {
        'source': 'server',
        'acceptedAt': _utc_now(),
        'fallbackFor': fallback_for,
    })
    return status_updates


def _step_locked_done(metadata: Dict, step: str) -> bool:
    """True if `step` is already 'done' on this photo.

    Guards against two writers racing on the same photo (browser + a
    server-side ipworker both processing the same upload in "both" mode) --
    whichever result lands first wins, and the second is discarded rather than
    clobbering it. Safe against legitimate forced re-runs (Tools > Backfill all
    photos) because those reset `{step}_status` away from 'done' via
    _enqueue_processing_steps *before* the new result is submitted, so a
    forced resubmission never actually observes 'done' here.
    """
    return str(metadata.get(f'{step}_status') or '').strip().lower() == 'done'


def _try_get_image_bytes(get_image_bytes: Callable[[], bytes]) -> Optional[bytes]:
    """Best-effort source-image fetch for the server-side fallback paths below.

    The source blob can genuinely be gone (e.g. a lost upload reservation) --
    in that case every server-side fallback (thumbnail/exif regeneration) is
    equally unable to help, so callers should resolve the step to 'failed'
    instead of calling into a fallback with unusable bytes. Without this, the
    download's ResourceNotFoundError propagated straight out of
    _apply_client_processing_results uncaught, turning "the source is missing"
    into a 500 on the whole /upload/client-processing request instead of a
    normal terminal step failure.
    """
    try:
        return get_image_bytes()
    except Exception:
        return None


def _apply_client_processing_results(
    user_id: str,
    filename: str,
    metadata: Dict,
    get_image_bytes: Callable[[], bytes],
    client_processing: Optional[Dict],
    client_processing_report: Optional[List[Dict]],
    client_asset_id: str,
    thumbnail_already_uploaded: bool = False,
    origin: str = 'browser',
) -> None:
    metadata_table_client = _CTX['metadata_table_client']
    payload = client_processing if isinstance(client_processing, dict) else {}
    report = _normalize_client_report(client_asset_id, client_processing_report)
    status_updates: Dict[str, object] = {}
    # Captured before _apply_client_report_statuses below can overwrite
    # thumbnail_status from this call's report, so the fallback trigger further
    # down reflects whether a thumbnail already existed BEFORE this call, not
    # after -- otherwise a report of this call's own failure would always look
    # like "no existing thumbnail" even when a prior successful one exists.
    had_thumbnail_before = str(metadata.get('thumbnail_status') or '').strip().lower() == 'done'
    face_report_background_throttled = any(
        str(item.get('step') or '').strip() == 'face'
        and str(item.get('reason') or '').strip().lower() == 'background_throttled'
        for item in report
    )
    if report:
        _merge_processing_metadata(metadata, 'clientProcessingReport', {
            'schemaVersion': CLIENT_PROCESSING_SCHEMA_VERSION,
            'clientAssetId': client_asset_id,
            'receivedAt': _utc_now(),
            'items': report,
        })
        _apply_client_report_statuses(metadata, report)
        if _client_report_has_retryable_ai_vision_failure(report):
            try:
                processing = json.loads(metadata.get('processing_metadata') or '{}')
            except Exception:
                processing = {}
            previous_ai = processing.get('client_ai_vision') if isinstance(processing, dict) else None
            if isinstance(previous_ai, dict) and _is_local_vision_fallback_provenance(previous_ai):
                _clear_previous_local_vision_fallback_metadata(metadata)
                status_updates['ai_vision_status'] = 'pending'
    # Anonymized photos keep their thumbnail under the same anonymous blob name as
    # the image; fall back to the original filename for pre-anonymization photos.
    thumbnail_blob_name = str(metadata.get('anonymousImageId') or '').strip() or filename
    thumbnail_payload = payload.get('thumbnail')
    if (
        not thumbnail_already_uploaded
        and thumbnail_payload is None
        and not had_thumbnail_before
        and any(
            str(item.get('step') or '').strip() == 'thumbnail'
            and str(item.get('status') or '').strip().lower() == 'done'
            for item in report
        )
    ):
        # _apply_client_report_statuses above already trusted this report and
        # set metadata['thumbnail_status'] = 'done' -- but a report saying
        # "done" only proves the client-side render succeeded, not that the
        # bytes actually reached blob storage (that's a separate direct-PUT
        # step, tracked by thumbnail_already_uploaded/thumbnail_payload, which
        # are both absent here). A dropped upload after a successful render
        # left exactly this combination and permanently orphaned
        # thumbnail_status at 'done' with no blob behind it (2026-08-13
        # incident). Verify before trusting it; self-heal via the same
        # fallback path used for an explicit failure report if it's missing.
        try:
            get_media_properties('thumbnail', thumbnail_blob_name)
        except Exception:
            source_bytes = _try_get_image_bytes(get_image_bytes)
            if source_bytes is not None:
                status_updates.update(_apply_server_thumbnail_fallback(
                    user_id,
                    filename,
                    metadata,
                    source_bytes,
                    fallback_for='report_claimed_done_but_blob_missing',
                ))
            else:
                status_updates['thumbnail_status'] = 'failed'
    if thumbnail_payload is not None and had_thumbnail_before:
        # Race guard, same as ocr/map_detection/face/ai_vision below: now that
        # ipworker can also generate thumbnails (ipwork_thumbnail.py), 'both'
        # mode can have the browser's kickOffThumbnailForFile and ipworker
        # both racing to write this photo's thumbnail. had_thumbnail_before
        # was captured above, before this call's own report could touch
        # thumbnail_status, so it reflects genuinely prior state -- whichever
        # side lands first wins and this second write is discarded.
        pass
    elif thumbnail_payload is not None:
        thumbnail_provenance = _client_source_provenance(thumbnail_payload)
        thumbnail_data = str(thumbnail_payload.get('data') or '').strip()
        if thumbnail_data and not thumbnail_already_uploaded:
            try:
                thumbnail_bytes = base64.b64decode(thumbnail_data)
                if thumbnail_bytes:
                    upload_media_file('thumbnail', thumbnail_blob_name, thumbnail_bytes, str(thumbnail_payload.get('contentType') or 'image/jpeg'))
                    thumbnail_already_uploaded = True
            except Exception:
                pass
        if thumbnail_already_uploaded:
            mark_step_done(user_id, filename, 'thumbnail', result={
                'source': origin,
                'clientAssetId': client_asset_id,
                **thumbnail_provenance,
            })
            status_updates['thumbnail_status'] = 'done'
            _merge_processing_metadata(metadata, 'client_thumbnail', {
                'source': origin,
                'acceptedAt': _utc_now(),
                'clientAssetId': client_asset_id,
                **({'rotationDegrees': thumbnail_payload.get('rotationDegrees')} if thumbnail_payload.get('rotationDegrees') is not None else {}),
                **thumbnail_provenance,
            })
    if (
        not thumbnail_already_uploaded
        and not had_thumbnail_before
        and (
            _client_report_needs_server_thumbnail_fallback(report)
            # ipworker reports failure inline via clientProcessing.thumbnail's
            # hasData (it never populates the separate clientProcessingReport
            # array _client_report_needs_server_thumbnail_fallback inspects --
            # see _run_ipwork_steps in app.py). Without this, an ipworker
            # thumbnail failure left thumbnail_status stuck at 'running'
            # forever instead of resolving to a terminal state.
            or (isinstance(thumbnail_payload, dict) and thumbnail_payload.get('hasData') is False)
        )
    ):
        source_bytes = _try_get_image_bytes(get_image_bytes)
        if source_bytes is not None:
            status_updates.update(_apply_server_thumbnail_fallback(
                user_id,
                filename,
                metadata,
                source_bytes,
                fallback_for='browser_thumbnail_failed',
            ))
        else:
            status_updates['thumbnail_status'] = 'failed'

    exif_result = payload.get('exif')
    if isinstance(exif_result, dict) and _step_locked_done(metadata, 'exif'):
        # Race guard, same as ocr/map_detection/face/ai_vision/thumbnail: the
        # browser's hand-rolled EXIF parser and the server's exiftool-based one
        # (exif_utils.py) are genuinely different implementations and can
        # disagree at the margins (e.g. one finds GPS, the other doesn't), so
        # 'both' mode needs first-result-wins here too, not just for steps
        # with an obviously model-dependent result.
        pass
    elif isinstance(exif_result, dict):
        exif_provenance = _client_source_provenance(exif_result)
        if exif_result.get('hasData') is False:
            source_bytes = _try_get_image_bytes(get_image_bytes)
            if source_bytes is not None:
                status_updates.update(_apply_server_exif_fallback(
                    user_id,
                    filename,
                    metadata,
                    source_bytes,
                    fallback_for='browser_no_data',
                ))
            else:
                status_updates['exif_status'] = 'failed'
        elif exif_result.get('hasData') is True:
            exif_data = _sanitize_client_exif(exif_result.get('data') or {})
            lat = _valid_decimal(exif_result.get('latitude') or exif_data.get('GPS.LatitudeDecimal'), -90.0, 90.0)
            lon = _valid_decimal(exif_result.get('longitude') or exif_data.get('GPS.LongitudeDecimal'), -180.0, 180.0)
            if lat and lon:
                exif_data['GPS.LatitudeDecimal'] = lat
                exif_data['GPS.LongitudeDecimal'] = lon
                metadata['latitude'] = lat
                metadata['longitude'] = lon
            _apply_exif_metadata(metadata, exif_data)
            _merge_processing_metadata(metadata, 'client_exif', {
                'source': origin,
                'acceptedAt': _utc_now(),
                'hasGps': bool(lat and lon),
                'clientAssetId': client_asset_id,
                **exif_provenance,
            })
            if lat and lon and not _CTX.get('queue_map_on_upload'):
                # Reverse geocode the EXIF GPS coordinates to populate location names
                place = _reverse_geocode_fallback(lat, lon)
                if place:
                    if place.get('address'):
                        metadata['address'] = place['address']
                    metadata['locationCity'] = place.get('city', '')
                    metadata['locationCountry'] = place.get('country', '')
                    metadata['semanticText'] = _build_client_semantic_text(filename, metadata)
                    metadata['semanticLayers'] = _json_compact(build_semantic_layers(filename, metadata))
                mark_step_done(user_id, filename, 'map_detection', result={
                    'source': origin,
                    'latitude': lat,
                    'longitude': lon,
                    'clientAssetId': client_asset_id,
                    **exif_provenance,
                })
                status_updates['map_detection_status'] = 'done'
            elif not lat or not lon:
                mark_step_no_data(user_id, filename, 'map_detection')
                status_updates['map_detection_status'] = 'no_data'
            status_updates['exif_status'] = 'done'
    elif exif_result is not None:
        status_updates['exif_status'] = 'failed'
    elif _client_report_needs_server_exif_fallback(report):
        source_bytes = _try_get_image_bytes(get_image_bytes)
        if source_bytes is not None:
            status_updates.update(_apply_server_exif_fallback(
                user_id,
                filename,
                metadata,
                source_bytes,
                fallback_for='browser_unsupported',
            ))
        else:
            status_updates['exif_status'] = 'failed'

    ocr_result = payload.get('ocr')
    if isinstance(ocr_result, dict) and _step_locked_done(metadata, 'ocr'):
        pass
    elif isinstance(ocr_result, dict):
        ocr_text = _sanitize_client_text(ocr_result.get('text'), 2048)
        if ocr_text:
            metadata['ocrText'] = ocr_text
            metadata['semanticText'] = _build_client_semantic_text(filename, metadata)
            metadata['semanticLayers'] = _json_compact(build_semantic_layers(filename, metadata))
            status_updates['ocr_status'] = 'done'
            _merge_processing_metadata(metadata, 'client_ocr', {
                'source': origin,
                'acceptedAt': _utc_now(),
                'clientAssetId': client_asset_id,
                'textLength': len(ocr_text),
            })
        elif ocr_result.get('hasData') is False:
            metadata['ocr_status'] = 'no_data'
    elif ocr_result is not None:
        metadata['ocr_status'] = 'failed'

    map_result = payload.get('map_detection')
    if isinstance(map_result, dict) and _step_locked_done(metadata, 'map_detection'):
        pass
    elif isinstance(map_result, dict):
        lat = _valid_decimal(map_result.get('latitude'), -90.0, 90.0)
        lon = _valid_decimal(map_result.get('longitude'), -180.0, 180.0)
        if lat and lon:
            metadata['latitude'] = lat
            metadata['longitude'] = lon
            metadata['address'] = _sanitize_client_text(map_result.get('address'), 512)
            metadata['locationCity'] = _sanitize_client_text(map_result.get('city'), 256)
            metadata['locationCountry'] = _sanitize_client_text(map_result.get('country'), 256)
            # The browser's reverse-geocode call is a single best-effort request to a
            # public third-party API (rate-limited, no SLA); when it fails or comes
            # back without a city/country, fall back to the server-side geocoder
            # (which has its own provider config/retries) so location search doesn't
            # silently lose coverage whenever the client-side call has a bad day.
            if not metadata['locationCity'] and not metadata['locationCountry']:
                place = _reverse_geocode_fallback(lat, lon)
                if place:
                    if place.get('address'):
                        metadata['address'] = place['address']
                    metadata['locationCity'] = place.get('city', '')
                    metadata['locationCountry'] = place.get('country', '')
            metadata['semanticText'] = _build_client_semantic_text(filename, metadata)
            metadata['semanticLayers'] = _json_compact(build_semantic_layers(filename, metadata))
            status_updates['map_detection_status'] = 'done'
            _merge_processing_metadata(metadata, 'client_map_detection', {
                'source': origin,
                'acceptedAt': _utc_now(),
                'clientAssetId': client_asset_id,
                **_client_source_provenance(map_result),
            })
        elif map_result.get('hasData') is False:
            metadata['map_detection_status'] = 'no_data'
    elif map_result is not None:
        metadata['map_detection_status'] = 'failed'

    face_result = payload.get('face')
    if isinstance(face_result, dict) and _step_locked_done(metadata, 'face'):
        pass
    elif isinstance(face_result, dict):
        # Set by _enqueue_processing_steps at queue time (force=True) and
        # untouched since -- nothing above this point writes the 'face' key of
        # processing_metadata. Distinguishes an explicit forced re-run (Tools >
        # Backfill all photos) from ordinary/opportunistic re-processing, so
        # only the former is allowed to clean up rows the new pass superseded.
        try:
            prior_processing = json.loads(metadata.get('processing_metadata') or '{}')
        except Exception:
            prior_processing = {}
        prior_face_meta = prior_processing.get('face') if isinstance(prior_processing, dict) else None
        face_was_forced = isinstance(prior_face_meta, dict) and bool(prior_face_meta.get('forced'))
        faces = face_result.get('faces')
        face_count = len(faces) if isinstance(faces, list) else 0
        try:
            reported_raw_face_count = max(0, int(face_result.get('rawFaceCount', face_result.get('detectedFaceCount', face_count)) or 0))
        except Exception:
            reported_raw_face_count = face_count
        face_deferred_reason = str(face_result.get('deferredReason') or '').strip().lower()
        face_failure_stage = str(face_result.get('faceFailureStage') or '').strip().lower()
        face_background_throttled = face_deferred_reason == 'background_throttled' or face_report_background_throttled
        face_model_provenance = _client_model_provenance(face_result)
        faces_with_embeddings = []
        # The frontend can report a face as fully successful (status='done', its
        # own embedding validated) and this backend-side re-validation still drops
        # it to no_data with zero visibility into why (observed 2026-08-01: a
        # batch where every one of the 7 affected photos' client report showed
        # status/reason 'done' with a real confidence/bbox, yet stored_face_ids
        # ended up empty). Capture *why* the first rejection happened so the next
        # occurrence is diagnosable from stored data instead of a guess.
        face_reject_diagnostic = None
        if isinstance(faces, list):
            for face in faces:
                if not isinstance(face, dict):
                    if face_reject_diagnostic is None:
                        face_reject_diagnostic = f'not_a_dict: type={type(face).__name__}'
                    continue
                if not _client_face_passes_quality_gate(face):
                    if face_reject_diagnostic is None:
                        bbox = _normalize_face_bbox(face)
                        try:
                            conf = float(face.get('confidence', 0.0) or 0.0)
                        except Exception:
                            conf = 0.0
                        face_reject_diagnostic = (
                            f'quality_gate_rejected: confidence={conf}_bbox={bbox}_'
                            f'imageWidth={face.get("imageWidth")}_imageHeight={face.get("imageHeight")}'
                        )
                    continue
                embedding = face.get('embedding', [])
                if isinstance(embedding, list) and any(isinstance(v, (int, float)) for v in embedding):
                    face_payload = dict(face)
                    for key in ('model', 'modelVersion', 'modelTaxonomyVersion', 'runtime'):
                        if face_model_provenance.get(key) and not face_payload.get(key):
                            face_payload[key] = face_model_provenance[key]
                    if face_payload.get('modelTaxonomyVersion') and not face_payload.get('embeddingVersion'):
                        face_payload['embeddingVersion'] = face_payload['modelTaxonomyVersion']
                    faces_with_embeddings.append(face_payload)
                elif face_reject_diagnostic is None:
                    is_list = isinstance(embedding, list)
                    face_reject_diagnostic = (
                        f'embedding_invalid: type={type(embedding).__name__}_isList={is_list}_'
                        f'length={len(embedding) if is_list else None}'
                    )
        if isinstance(faces, list):
            # A forced backfill's reconcile step (_store_client_face_entities,
            # force_reconcile=True) deletes any previously-stored face for this
            # photo that this pass didn't re-detect -- correct when detection
            # genuinely ran and found nothing (or found a different set), but
            # not when it didn't really run at all: throttled, deferred for a
            # not-yet-ready model, or a real failure (e.g. an ipworker step
            # crashing -- see ipwork_face.py's/​_run_ipwork_steps' failure
            # shapes). Without this, one bad pass during Tools > Backfill could
            # silently wipe out previously-detected/curated faces for a photo
            # that the detector never actually got to examine this time.
            detection_genuinely_ran = bool(faces_with_embeddings) or not (
                face_background_throttled
                or face_deferred_reason == 'inference_timeout'
                or face_failure_stage in _TRANSIENT_FACE_FAILURE_STAGES
                or face_failure_stage
            )
            stored_face_ids = _store_client_face_entities(
                user_id, filename, faces_with_embeddings,
                force_reconcile=face_was_forced and detection_genuinely_ran,
            )
            if faces_with_embeddings:
                faces_for_metadata = [{k: v for k, v in f.items() if k != 'embedding'} for f in faces_with_embeddings]
                metadata['faces'] = json.dumps(faces_for_metadata, ensure_ascii=False, separators=(',', ':'))
                status_updates['faceCount'] = len(faces_with_embeddings)
                status_updates['face_status'] = 'done'
            elif face_background_throttled:
                status_updates['faceCount'] = 0
                status_updates['face_status'] = 'pending'
            elif face_deferred_reason == 'inference_timeout' or face_failure_stage in _TRANSIENT_FACE_FAILURE_STAGES:
                # The detector/embedder model was not ready in time (cold-cache load
                # timeout, model still downloading) — a transient condition, not a
                # real failure. Leave the step retryable ('pending') so it re-runs
                # once the models are warm, instead of a hard 'failed' that forces
                # the user to manually re-run face detection. Mirrors the
                # background-throttled handling above.
                status_updates['faceCount'] = 0
                status_updates['face_status'] = 'pending'
            elif face_failure_stage:
                status_updates['faceCount'] = 0
                status_updates['face_status'] = 'failed'
            else:
                status_updates['faceCount'] = 0
                status_updates['face_status'] = 'no_data'
            _merge_processing_metadata(metadata, 'client_face', {
                'source': origin,
                'acceptedAt': _utc_now(),
                'clientAssetId': client_asset_id,
                'faceCount': len(faces_with_embeddings),
                'hasData': bool(faces_with_embeddings),
                'embeddingsReady': bool(faces_with_embeddings),
                'faceModelReady': bool(face_result.get('faceModelReady')) or bool(faces) or reported_raw_face_count > 0,
                'embeddingMissing': (bool(faces) or reported_raw_face_count > 0) and not bool(faces_with_embeddings),
                'storedFaceIds': stored_face_ids,
                'rawFaceCount': reported_raw_face_count,
                **({'detectedFaceCount': face_result.get('detectedFaceCount')} if face_result.get('detectedFaceCount') is not None else {}),
                **({'candidateFaceCount': face_result.get('candidateFaceCount')} if face_result.get('candidateFaceCount') is not None else {}),
                **({'filteredFaceCount': face_result.get('filteredFaceCount')} if face_result.get('filteredFaceCount') is not None else {}),
                **({'filteredReason': face_result.get('filteredReason')} if face_result.get('filteredReason') is not None else {}),
                **({'debugStages': face_result.get('debugStages')} if face_result.get('debugStages') is not None else {}),
                **({'deferredReason': 'background_throttled'} if face_background_throttled else ({'deferredReason': face_deferred_reason} if face_deferred_reason else {})),
                **({'faceFailureStage': face_result.get('faceFailureStage')} if face_result.get('faceFailureStage') is not None else {}),
                **({'faceFailureDetail': face_result.get('faceFailureDetail')} if face_result.get('faceFailureDetail') is not None else {}),
                **({'backendRejectDiagnostic': face_reject_diagnostic} if (not faces_with_embeddings and faces and face_reject_diagnostic) else {}),
                **_client_source_provenance(face_result),
                **_client_model_provenance(face_result),
            })
        elif face_result.get('hasData') is False:
            status_updates['faceCount'] = 0
            status_updates['face_status'] = 'pending' if face_background_throttled else 'no_data'
        else:
            status_updates['faceCount'] = 0
            status_updates['face_status'] = 'pending' if face_background_throttled else 'no_data'
    elif face_result is not None:
        status_updates['face_status'] = 'failed'

    if str(status_updates.get('face_status') or '').strip().lower() in {'done', 'no_data', 'failed', 'unsupported'}:
        metadata['processing_lease_owner'] = ''
        metadata['processing_lease'] = ''
        metadata['processing_lease_expires_at'] = ''

    ai_result = payload.get('ai_vision')
    if isinstance(ai_result, dict) and _step_locked_done(metadata, 'ai_vision'):
        pass
    elif isinstance(ai_result, dict):
        ai_source_provenance = _client_source_provenance(ai_result)
        ai_model_provenance = _client_model_provenance(ai_result)
        model_ready = (
            ai_model_provenance.get('modelAvailability') in {'available', 'cached', 'downloaded'}
            and ai_model_provenance.get('modelVersion')
            and ai_model_provenance.get('modelTaxonomyVersion')
            and ai_model_provenance.get('runtime')
        )
        local_vision_fallback = _is_local_vision_fallback_provenance(ai_model_provenance)
        if (ai_result.get('hasData') is False or local_vision_fallback) and model_ready:
            _clear_previous_local_vision_fallback_metadata(metadata)
            mark_step_no_data(user_id, filename, 'ai_vision')
            status_updates['ai_vision_status'] = 'no_data'
            _merge_processing_metadata(metadata, 'client_ai_vision', {
                'source': origin,
                'acceptedAt': _utc_now(),
                'hasData': False,
                'clientAssetId': client_asset_id,
                **({'rejectedReason': 'local_vision_fallback_non_authoritative'} if local_vision_fallback else {}),
                **ai_source_provenance,
                **ai_model_provenance,
            })
        elif ai_result.get('hasData') is True and model_ready:
            tags = _normalize_tag_list(ai_result.get('tags'))
            objects = _normalize_tag_list(ai_result.get('objects') or tags)
            caption = _sanitize_client_text(ai_result.get('caption'), 512)
            ocr_text = _sanitize_client_text(ai_result.get('ocrText'), 2048)
            predictions = _sanitize_client_predictions(ai_result.get('predictions'))
            person_label = _normalize_tag_list([ai_result.get('aiPersonLabel')], limit=1)
            try:
                person_score = float(ai_result.get('aiPersonScore') or 0)
            except Exception:
                person_score = 0
            person_score = round(max(0, min(person_score, 1)), 4)
            curated_tags = _build_curated_ai_tag_payload(
                _parse_json_list(metadata.get('tags', '[]')),
                tags,
                objects,
                predictions,
                person_label,
                person_score,
                ai_source_provenance,
                ai_model_provenance,
                client_asset_id,
            )
            metadata['tags'] = _json_compact(curated_tags.get('tags', []))
            metadata['subjectTags'] = _json_compact(curated_tags.get('subjectTags', []))
            metadata['backgroundTags'] = _json_compact(curated_tags.get('backgroundTags', []))
            metadata['weakTags'] = _json_compact(curated_tags.get('weakTags', []))
            metadata['tagBuckets'] = _json_compact(curated_tags.get('tagBuckets', {}))
            metadata['tagMetadata'] = _json_compact(curated_tags.get('tagMetadata', []))
            bucket_payload = curated_tags.get('tagBuckets') if isinstance(curated_tags.get('tagBuckets'), dict) else {}
            object_bucket_tags = bucket_payload.get('object', [])
            person_bucket_tags = bucket_payload.get('person', [])
            if not isinstance(object_bucket_tags, list):
                object_bucket_tags = []
            if not isinstance(person_bucket_tags, list):
                person_bucket_tags = []
            metadata['objects'] = _json_compact(normalize_tags(object_bucket_tags + person_bucket_tags))
            if caption:
                metadata['caption'] = caption
            if ocr_text:
                metadata['ocrText'] = ocr_text
            if person_label:
                metadata['aiPersonLabel'] = person_label[0]
                metadata['aiPersonScore'] = person_score
                metadata['aiPersonCandidate'] = bool(ai_result.get('aiPersonCandidate')) and person_score >= 0.2
            image_embedding = _sanitize_client_image_embedding(ai_result.get('imageEmbedding'))
            if image_embedding:
                metadata['photoEmbedding'] = _json_compact(image_embedding)
                metadata['photoEmbeddingVersion'] = PHOTO_EMBEDDING_MODEL_VERSION
                metadata['photoEmbeddingDimension'] = len(image_embedding)
            metadata['semanticText'] = _build_client_semantic_text(filename, metadata)
            metadata['semanticLayers'] = _json_compact(build_semantic_layers(filename, metadata))
            mark_step_done(user_id, filename, 'ai_vision', result={
                'source': origin,
                'tags': tags,
                'ocrText': ocr_text,
                'clientAssetId': client_asset_id,
                **ai_source_provenance,
                **ai_model_provenance,
            })
            status_updates['ai_vision_status'] = 'done'
            _merge_processing_metadata(metadata, 'client_ai_vision', {
                'source': origin,
                'acceptedAt': _utc_now(),
                'tags': tags,
                'objects': objects,
                'predictions': predictions,
                'tagQuality': {
                    'storedTagCount': len(curated_tags.get('tags', [])),
                    'subjectTagCount': len(curated_tags.get('subjectTags', [])),
                    'backgroundTagCount': len(curated_tags.get('backgroundTags', [])),
                    'weakTagCount': len(curated_tags.get('weakTags', [])),
                    'maxStoredTags': MAX_TAGS_STORED,
                },
                'hasPersonCandidate': bool(metadata.get('aiPersonCandidate')),
                'clientAssetId': client_asset_id,
                **ai_source_provenance,
                **ai_model_provenance,
            })
        elif ai_result is not None:
            # Was previously a silent no-op on ai_vision_status: neither
            # "hasData is False" nor "hasData is True" was paired with
            # model_ready, so this branch wrote only a diagnostic breadcrumb
            # and left the step stuck at whatever it was before (e.g.
            # 'running' from a lease claim) forever. Genuinely reachable --
            # every one of ipwork_vision.py's own failure paths (CLIP model
            # unavailable, vocabulary missing, embedding failed, an unhandled
            # exception via _run_ipwork_steps) omits modelAvailability/
            # modelVersion/modelTaxonomyVersion/runtime entirely, so
            # model_ready is always False for them, same as a browser
            # payload with genuinely invalid/missing provenance. 'failed'
            # (not 'no_data') so it's retryable and visible on the Tools page
            # instead of looking like a confirmed "nothing to see here".
            _merge_processing_metadata(metadata, 'client_ai_vision_rejected', {
                'source': origin,
                'rejectedAt': _utc_now(),
                'reason': 'invalid_or_missing_model_provenance',
                'clientAssetId': client_asset_id,
                **ai_source_provenance,
                **ai_model_provenance,
            })
            status_updates['ai_vision_status'] = 'failed'

    _refresh_semantic_fields(filename, metadata)

    if status_updates:
        metadata.update(status_updates)
    metadata_table_client.upsert_entity(metadata)
    # A full eager refresh here would rescan and re-embed the user's whole
    # library on every single photo (measured at ~20s for a ~6k-photo
    # library) -- the same per-photo full-library-work bug already fixed
    # once for clustering and once for the people/face scan cache. Just mark
    # the index stale; get_user_vector_index's existing lazy path rebuilds it
    # once, on demand, the next time someone actually searches.
    touch_user_vector_index_state(user_id)


def apply_client_processing_results_for_file(
    user_id: str,
    filename: str,
    *,
    client_processing: Optional[Dict] = None,
    client_processing_report: Optional[List[Dict]] = None,
    client_asset_id: str = '',
    thumbnail_already_uploaded: bool = False,
    origin: str = 'browser',
) -> Dict:
    """Validate and apply late processing results to an existing asset.

    `origin` identifies who computed these results ('browser' or 'ipworker')
    and is stamped into each step's provenance; it does not change which
    fields get written.
    """
    _require_context()
    metadata_table_client = _CTX['metadata_table_client']
    metadata = metadata_table_client.get_entity(partition_key=user_id, row_key=filename)
    if str(metadata.get('processing_state') or '').strip().lower() == 'deleted':
        raise RuntimeError('Photo has been deleted.')
    # Anonymized photos are stored under the anonymous UUID; resolve it from the
    # metadata row we just loaded so the byte read hits the real blob.
    source_blob = str(metadata.get('anonymousImageId') or '').strip() or filename
    # Most upload reports need no server-side fallback at all, so don't pay for
    # a download unless _apply_client_processing_results actually reaches a
    # fallback path (EXIF or thumbnail) that needs the bytes.
    image_bytes_cache: List[bytes] = []

    def get_image_bytes() -> bytes:
        if not image_bytes_cache:
            image_bytes_cache.append(download_media_bytes('image', source_blob))
        return image_bytes_cache[0]

    _apply_client_processing_results(
        user_id,
        filename,
        metadata,
        get_image_bytes,
        client_processing,
        client_processing_report,
        client_asset_id,
        thumbnail_already_uploaded=thumbnail_already_uploaded,
        origin=origin,
    )
    refresh_metadata_entity(user_id, filename, {
        'processing_lease_owner': '',
        'processing_lease': '',
        'processing_lease_expires_at': '',
    })
    return metadata


def finalize_uploaded_file(
    user_id: str,
    filename: str,
    content_type: str,
    *,
    client_processing: Optional[Dict] = None,
    client_processing_report: Optional[List[Dict]] = None,
    client_asset_id: str = '',
    client_sha256: str = '',
    anonymous_blob_name: Optional[str] = None,
) -> Tuple[List[Dict], str]:
    """Finalize upload and initialize durable browser-only processing state.

    The file has already been uploaded straight to blob storage by the browser, so
    this avoids moving the bytes again wherever possible:
      * ordinary photos (including HEIC/HEIF) never touch the backend here —
        dedup/integrity reuse the SHA-256 the browser computed and sent, and no
        re-upload happens (the blob is already in place); if the browser's own
        thumbnail attempt fails, the backend steps in reactively later, once
        the client's processing report actually says so (see
        _client_report_needs_server_thumbnail_fallback), not here;
      * only video/RAW are downloaded eagerly, because they need a server-side
        thumbnail or metadata the browser can't reliably produce on its own;
      * the blob is only (re)written under a new name in the rare event a
        cross-tenant filename clash forces a rename.
      * anonymized images use the anonymous_blob_name instead of filename for storage.
    """
    _require_context()
    metadata_table_client = _CTX['metadata_table_client']

    # Only video still needs eager bytes here (for the metadata extraction
    # below). RAW used to be lumped in with video via _upload_needs_server_bytes
    # -- forcing a blob re-download + synchronous decode inside this HTTP
    # request even when the browser already sent a trustworthy hash -- but
    # RAW's thumbnail/EXIF work is redundant with what ipworker's queued
    # 'thumbnail'/'exif' steps already do for every upload (see
    # _queue_upload_processing below), so there's no reason to also do it here,
    # synchronously, on one of the backend's few gunicorn threads.
    is_video = is_video_file(filename)
    blob_to_read = anonymous_blob_name or filename

    # The browser sends the file's hex SHA-256 (same algorithm and bytes as the
    # server would compute), so we can dedup and stamp fileHash without a download.
    # Fall back to reading the blob when there is no client hash to trust.
    image_bytes: Optional[bytes] = None
    client_hash = str(client_sha256 or '')
    if is_video or not client_hash:
        image_bytes = download_media_bytes('image', blob_to_read)
        file_hash = hashlib.sha256(image_bytes).hexdigest()
    else:
        file_hash = client_hash

    duplicates = detect_duplicates(user_id, file_hash, perceptual_hash=None)
    final_filename = _resolve_filename_for_upload(user_id, filename, file_hash)

    if final_filename != filename and not anonymous_blob_name:
        # Rare: a cross-tenant filename clash forced a unique name, so place the
        # bytes under it. (Non-renamed uploads are already stored in place, so the
        # common path moves no image data at all.)
        #
        # Anonymized uploads skip this entirely: the blob already lives under a
        # globally-unique UUID (anonymous_blob_name), so there's no storage-name
        # collision to resolve — only the metadata RowKey changes, and the
        # name-mapping below records final_filename -> anonymous_blob_name. Moving
        # the bytes to a plaintext `final_filename` would also defeat anonymization.
        if image_bytes is None:
            image_bytes = download_media_bytes('image', filename)
        try:
            upload_media_file('image', final_filename, image_bytes, content_type)
        except Exception:
            pass

    metadata = get_or_create_metadata(user_id, final_filename)
    # The metadata row is usually pre-created at /upload/init (which records
    # upload_started_at but no uploadDate), so get_or_create_metadata returns it
    # without an uploadDate. Persist one here so the gallery has a stable
    # "recently uploaded" sort key instead of falling back to a volatile field.
    metadata.setdefault('uploadDate', datetime.now(timezone.utc).isoformat())
    metadata['fileHash'] = file_hash
    metadata['mimeType'] = content_type
    apply_upload_hash_result(metadata, file_hash)
    metadata.setdefault('peopleIds', json.dumps([]))

    # Record the mapping for an anonymized upload using the SAME id the blob was
    # actually uploaded under at /upload/init (anonymous_blob_name) — not a fresh
    # UUID — so the mapping resolves to the real blob. Stamp anonymousImageId onto
    # the metadata row so every read/serve/delete path can resolve it in one hop.
    if anonymous_blob_name:
        try:
            _store_image_name_mapping(
                user_id,
                final_filename,
                'image',
                _CTX.get('blob_image_container') or 'images',
                anonymous_id=anonymous_blob_name,
            )
        except Exception:
            pass
        metadata['anonymousImageId'] = anonymous_blob_name
        # The reservation has been promoted to the permanent anonymousImageId;
        # drop the transient pending marker so it can't be mistaken for an
        # in-flight upload later.
        metadata.pop(PENDING_ANONYMOUS_BLOB_FIELD, None)

    metadata_table_client.upsert_entity(metadata)
    # Keep the O(1) dedup/collision indexes current for the next upload -- see
    # detect_duplicates() and _resolve_filename_for_upload().
    _store_hash_index(user_id, file_hash, final_filename)
    _store_filename_owner(user_id, final_filename, file_hash)
    file_is_video = is_video_file(final_filename)
    video_status_overrides = {'ocr': 'skipped', 'ai_vision': 'skipped', 'face': 'skipped'}
    try:
        _init_processing_status_for_image(
            user_id,
            final_filename,
            status_overrides=video_status_overrides if file_is_video else {},
        )
    except Exception:
        pass

    # Server-side thumbnail / metadata extraction, eagerly, here -- video only.
    # RAW used to get this treatment too, but it's redundant: ipworker's queued
    # 'thumbnail'/'exif' steps (_queue_upload_processing, below) call the exact
    # same helpers for every uploaded photo, including RAW, so doing it again
    # here just duplicated the work while holding a gunicorn thread for as long
    # as the RAW decode took (measured multi-second to ~60s on real CR3 files).
    # RAW capture dates and thumbnails now show up whenever ipworker gets to
    # them instead of instantly -- an intentional trade (see the "own highway"
    # discussion): correctness-only background work no longer blocks the
    # upload-critical path.
    if file_is_video and image_bytes is not None:
        try:
            thumbnail_bytes = _create_server_thumbnail_for_upload(image_bytes, final_filename)
            if thumbnail_bytes:
                # Use anonymous ID for thumbnail if image was anonymized
                thumbnail_blob_name = metadata.get('anonymousImageId') or final_filename
                upload_media_file('thumbnail', thumbnail_blob_name, thumbnail_bytes, 'image/jpeg')
                mark_step_done(user_id, final_filename, 'thumbnail', result={
                    'source': 'server',
                    'reason': 'video_browser_unsupported',
                })
        except Exception:
            pass
        try:
            _finalize_server_side_exif(
                user_id,
                final_filename,
                image_bytes,
                fallback_for='video_upload',
            )
        except Exception:
            pass
    return duplicates, final_filename


def _finalize_server_side_exif(user_id: str, filename: str, image_bytes: bytes, *, fallback_for: str) -> None:
    """Extract EXIF (exiftool) server-side so capture-date sort/search works
    immediately for formats the browser can't reliably self-report (video,
    RAW), instead of waiting on client-side processing."""
    metadata_table_client = _CTX['metadata_table_client']
    entity = metadata_table_client.get_entity(partition_key=user_id, row_key=filename)
    status_updates = _apply_server_exif_fallback(user_id, filename, entity, image_bytes, fallback_for=fallback_for)
    for field, value in status_updates.items():
        entity[field] = value
    entity['exif_status'] = str(status_updates.get('exif_status') or 'no_data')
    try:
        _refresh_semantic_fields(filename, entity)
    except Exception:
        pass
    entity['last_processing_update'] = datetime.now(timezone.utc).isoformat()
    metadata_table_client.upsert_entity(entity)


PROCESSING_STEPS = (*BROWSER_PROCESSING_STEPS, 'verify')
CLIENT_PROCESSING_LEASE_SECONDS = int(os.getenv('CLIENT_PROCESSING_LEASE_SECONDS', '120'))


def _init_processing_status_for_image(user_id: str, filename: str, status_overrides: Optional[Dict[str, str]] = None) -> None:
    _require_context()
    metadata_table_client = _CTX['metadata_table_client']
    try:
        entity = metadata_table_client.get_entity(partition_key=user_id, row_key=filename)
    except Exception:
        entity = {'PartitionKey': user_id, 'RowKey': filename}

    status_overrides = status_overrides or {}
    for step in PROCESSING_STEPS:
        field = f'{step}_status'
        entity[field] = status_overrides.get(step, 'pending')

    entity['processing_metadata'] = json.dumps({})
    entity['processing_state'] = 'active'
    entity['processing_lease'] = ''
    entity['processing_lease_expires_at'] = ''
    entity['processing_lease_owner'] = ''
    entity['retry_count'] = 0
    entity['last_processing_update'] = datetime.now(timezone.utc).isoformat()
    metadata_table_client.upsert_entity(entity)


def init_processing_status_for_image(user_id: str, filename: str, status_overrides: Optional[Dict[str, str]] = None) -> None:
    _init_processing_status_for_image(user_id, filename, status_overrides=status_overrides)


def update_processing_status(
    user_id: str,
    filename: str,
    step: str,
    status: str,
    *,
    result: Optional[Dict] = None,
    error: Optional[str] = None,
    increment_retry: bool = False,
) -> None:
    _require_context()
    metadata_table_client = _CTX['metadata_table_client']
    try:
        entity = metadata_table_client.get_entity(partition_key=user_id, row_key=filename)
    except Exception:
        return
    if str(entity.get('processing_state') or '').strip().lower() == 'deleted':
        return

    status_field = f'{step}_status'
    entity[status_field] = status

    try:
        processing = json.loads(entity.get('processing_metadata') or '{}')
    except Exception:
        processing = {}

    if result is not None:
        processing[step] = result
    elif status == 'no_data':
        processing[step] = None

    if error:
        entity['last_error'] = str(error)
        if increment_retry:
            entity['retry_count'] = int(entity.get('retry_count', 0) or 0) + 1

    entity['processing_metadata'] = json.dumps(processing, ensure_ascii=False, separators=(',', ':'))
    entity['last_processing_update'] = datetime.now(timezone.utc).isoformat()
    metadata_table_client.upsert_entity(entity)


def _processing_is_expired(entity: Dict) -> bool:
    expires_at = str(entity.get('processing_lease_expires_at') or '').strip()
    if not expires_at:
        return True
    try:
        return datetime.fromisoformat(expires_at.replace('Z', '+00:00')) <= datetime.now(timezone.utc)
    except Exception:
        return True


def claim_processing_lease(
    user_id: str,
    filename: str,
    owner_id: str,
    *,
    lease_seconds: int = CLIENT_PROCESSING_LEASE_SECONDS,
    steps: Optional[List[str]] = None,
    mark_running: bool = True,
) -> Dict:
    _require_context()
    metadata_table_client = _CTX['metadata_table_client']
    try:
        entity = metadata_table_client.get_entity(partition_key=user_id, row_key=filename)
    except Exception:
        raise RuntimeError('Photo not found.')
    if str(entity.get('processing_state') or '').strip().lower() == 'deleted':
        raise RuntimeError('Photo has been deleted.')

    lease_expired = _processing_is_expired(entity)
    current_owner = str(entity.get('processing_lease_owner') or '').strip()
    if current_owner and current_owner != owner_id and not lease_expired:
        raise RuntimeError('Processing lease is already held by another client.')

    if mark_running:
        requested_steps = tuple(step for step in (steps or list(BROWSER_PROCESSING_STEPS)) if step in BROWSER_PROCESSING_STEPS)
        for step in requested_steps:
            field = f'{step}_status'
            current_status = str(entity.get(field) or 'pending').strip().lower()
            if current_status == 'running' and lease_expired:
                current_status = 'pending'
            if current_status not in {'done', 'no_data', 'deleted', 'skipped', 'unsupported'}:
                entity[field] = 'running'

    expires_at = datetime.now(timezone.utc) + timedelta(seconds=max(30, int(lease_seconds or CLIENT_PROCESSING_LEASE_SECONDS)))
    updated = _update_metadata_fields(user_id, filename, {
        'processing_lease_owner': owner_id,
        'processing_lease': owner_id,
        'processing_lease_expires_at': expires_at.isoformat(),
        **({
            f'{step}_status': 'running'
            for step in (steps or list(BROWSER_PROCESSING_STEPS))
            if mark_running and step in BROWSER_PROCESSING_STEPS and str(entity.get(f'{step}_status') or 'pending').strip().lower() not in {'done', 'no_data', 'deleted', 'skipped', 'unsupported'}
        } if mark_running else {}),
    })
    return {
        'filename': filename,
        'ownerId': owner_id,
        'leaseExpiresAt': updated.get('processing_lease_expires_at', ''),
        'statuses': {f'{step}Status': updated.get(f'{step}_status', 'pending') for step in PROCESSING_STEPS},
    }


def heartbeat_processing_lease(user_id: str, filename: str, owner_id: str, *, lease_seconds: int = CLIENT_PROCESSING_LEASE_SECONDS) -> Dict:
    _require_context()
    metadata_table_client = _CTX['metadata_table_client']
    try:
        entity = metadata_table_client.get_entity(partition_key=user_id, row_key=filename)
    except Exception:
        raise RuntimeError('Photo not found.')
    if str(entity.get('processing_state') or '').strip().lower() == 'deleted':
        raise RuntimeError('Photo has been deleted.')
    current_owner = str(entity.get('processing_lease_owner') or entity.get('processing_lease') or '').strip()
    if current_owner and current_owner != owner_id and not _processing_is_expired(entity):
        raise RuntimeError('Processing lease is already held by another client.')
    return claim_processing_lease(user_id, filename, owner_id, lease_seconds=lease_seconds, mark_running=False)


def release_processing_lease(user_id: str, filename: str, owner_id: str) -> None:
    _require_context()
    metadata_table_client = _CTX['metadata_table_client']
    try:
        entity = metadata_table_client.get_entity(partition_key=user_id, row_key=filename)
    except Exception:
        return
    if str(entity.get('processing_state') or '').strip().lower() == 'deleted':
        return
    current_owner = str(entity.get('processing_lease_owner') or entity.get('processing_lease') or '').strip()
    if current_owner and current_owner != owner_id:
        return
    _update_metadata_fields(user_id, filename, {
        'processing_lease_owner': '',
        'processing_lease': '',
        'processing_lease_expires_at': '',
    })


def refresh_metadata_entity(user_id: str, filename: str, updates: Dict) -> None:
    if not updates:
        return
    _update_metadata_fields(user_id, filename, updates)


def mark_step_no_data(user_id: str, filename: str, step: str) -> None:
    update_processing_status(user_id, filename, step, 'no_data')


def mark_step_done(user_id: str, filename: str, step: str, result: Optional[Dict] = None) -> None:
    update_processing_status(user_id, filename, step, 'done', result=result)


def _query_filename_owners(filename: str) -> List[Dict[str, str]]:
    """Return owner rows for a filename as [{'owner': library_id, 'fileHash': ...}].

    Uses the filename-owners index (PartitionKey=filename) when configured --
    a query scoped to just this one filename. Falls back to a full metadata-
    table scan (RowKey eq filename, across every library) when the index isn't
    wired up yet, e.g. immediately after a deploy before the backfill runs.
    """
    filename_owners_table_client = _CTX.get('filename_owners_table_client')
    if filename_owners_table_client is not None:
        query = f"PartitionKey eq '{_escape_odata(filename)}'"
        rows = list(filename_owners_table_client.query_entities(query))
        return [{'owner': str(r.get('RowKey') or ''), 'fileHash': str(r.get('fileHash') or '')} for r in rows]

    metadata_table_client = _CTX['metadata_table_client']
    entities = list(metadata_table_client.query_entities(f"RowKey eq '{_escape_odata(filename)}'"))
    return [{'owner': str(e.get('PartitionKey') or ''), 'fileHash': str(e.get('fileHash') or '')} for e in entities]


def _resolve_filename_for_upload(user_id: str, filename: str, file_hash: str) -> str:
    """Return a safe filename to use for storing this upload.

    If another library already owns this exact filename with different content
    (a different fileHash), generate a unique candidate to avoid a cross-tenant
    blob-name collision -- blob storage names are not partitioned per library,
    so two libraries independently uploading "IMG_1234.jpg" would otherwise
    silently overwrite each other's bytes.
    """
    _require_context()

    safe_name = secure_filename(filename)
    if not safe_name or safe_name != filename:
        return filename

    try:
        for row in _query_filename_owners(filename):
            owner = row['owner']
            existing_hash = row['fileHash']
            if owner and owner != user_id and existing_hash and existing_hash != file_hash:
                # Conflict: pick a unique filename not already owned by anyone.
                name, ext = os.path.splitext(filename)
                for _ in range(5):
                    candidate = f"{name}-{uuid.uuid4().hex[:8]}{ext}"
                    if not _query_filename_owners(candidate):
                        return candidate
                return f"{name}-{uuid.uuid4().hex[:8]}{ext}"
    except Exception:
        # On error, fall back to original filename to avoid blocking uploads.
        return filename

    return filename


def _store_hash_index(user_id: str, file_hash: str, filename: str) -> None:
    """Record filename <- fileHash so a later detect_duplicates() call is an
    O(1) point lookup instead of a partition scan. Best-effort: dedup still
    works (just slower, via the fallback scan) if this write fails."""
    hash_index_table_client = _CTX.get('hash_index_table_client')
    if hash_index_table_client is None or not file_hash or not filename:
        return
    try:
        hash_index_table_client.upsert_entity({
            'PartitionKey': user_id,
            'RowKey': file_hash,
            'filename': filename,
        })
    except Exception:
        pass


def delete_hash_index_entry(user_id: str, file_hash: str) -> None:
    """Drop the dedup-index row for a deleted photo so its hash doesn't keep
    matching new uploads (detect_duplicates self-heals a stale row too, but
    proactive cleanup keeps the index from accreting dead rows)."""
    hash_index_table_client = _CTX.get('hash_index_table_client')
    if hash_index_table_client is None or not file_hash:
        return
    try:
        hash_index_table_client.delete_entity(partition_key=user_id, row_key=file_hash)
    except Exception:
        pass


def _store_filename_owner(user_id: str, filename: str, file_hash: str) -> None:
    """Record filename -> (owner library, fileHash) so a later
    _resolve_filename_for_upload() collision check is scoped to just this one
    filename instead of scanning the entire metadata table."""
    filename_owners_table_client = _CTX.get('filename_owners_table_client')
    if filename_owners_table_client is None or not filename:
        return
    try:
        filename_owners_table_client.upsert_entity({
            'PartitionKey': filename,
            'RowKey': user_id,
            'fileHash': file_hash,
        })
    except Exception:
        pass


def delete_filename_owner_entry(user_id: str, filename: str) -> None:
    """Drop this library's filename-ownership row for a deleted photo."""
    filename_owners_table_client = _CTX.get('filename_owners_table_client')
    if filename_owners_table_client is None or not filename:
        return
    try:
        filename_owners_table_client.delete_entity(partition_key=filename, row_key=user_id)
    except Exception:
        pass


def reset_received_ranges(user_id: str, filename: str, total_size: int, expected_hash: Optional[str] = None) -> None:
    _require_context()
    metadata_table_client = _CTX['metadata_table_client']
    try:
        entity = metadata_table_client.get_entity(partition_key=user_id, row_key=filename)
    except Exception:
        entity = {'PartitionKey': user_id, 'RowKey': filename}
    entity['received_ranges'] = json.dumps([])
    entity['upload_total_size'] = total_size
    entity['upload_started_at'] = datetime.now(timezone.utc).isoformat()
    if expected_hash:
        entity['upload_sha256_expected'] = expected_hash
    metadata_table_client.upsert_entity(entity)


def reset_upload_tracking_and_reserve_blob(
    user_id: str,
    filename: str,
    total_size: int,
    expected_hash: Optional[str] = None,
) -> Optional[str]:
    """Fresh-upload version of /upload/init's per-file bookkeeping: does the
    combined work of reset_received_ranges() + reserve_pending_anonymous_blob()
    against a single fetch of the metadata row instead of each function
    re-reading (and re-writing) it separately. Those two still exist standalone
    for their own tests/callers; this is just the fast path for the common
    case (a brand-new upload, not a resume) where both need to run back to
    back on the same row anyway.
    """
    _require_context()
    metadata_table_client = _CTX['metadata_table_client']
    if not user_id or not filename:
        return None
    try:
        entity = metadata_table_client.get_entity(partition_key=user_id, row_key=filename)
    except Exception:
        entity = {'PartitionKey': user_id, 'RowKey': filename}

    entity['received_ranges'] = json.dumps([])
    entity['upload_total_size'] = total_size
    entity['upload_started_at'] = datetime.now(timezone.utc).isoformat()
    if expected_hash:
        entity['upload_sha256_expected'] = expected_hash

    existing = str(entity.get(PENDING_ANONYMOUS_BLOB_FIELD) or entity.get('anonymousImageId') or '').strip()
    anonymous_blob_name = existing or _generate_anonymous_id()
    entity[PENDING_ANONYMOUS_BLOB_FIELD] = anonymous_blob_name

    try:
        metadata_table_client.upsert_entity(entity)
    except Exception:
        return anonymous_blob_name
    return anonymous_blob_name


def reset_upload_tracking_and_reserve_blobs_batch(
    user_id: str,
    files: List[Dict[str, object]],
) -> Dict[str, str]:
    """Batched version of reset_upload_tracking_and_reserve_blob for
    /upload/init-batch: one query to fetch every existing row for this chunk
    of filenames, one transactional batch write to commit all of them --
    instead of a read + write PER FILE. Meant to be called once per ~10-15
    file chunk, not once per file.

    `files` is a list of {'filename', 'total_size', 'expected_hash', 'is_fresh'}
    dicts (all for the same user_id/partition, which submit_transaction
    requires). Returns {filename: anonymous_blob_name}.
    """
    _require_context()
    metadata_table_client = _CTX['metadata_table_client']
    if not user_id or not files:
        return {}

    filenames = [str(f['filename']) for f in files]
    existing_by_name: Dict[str, Dict] = {}
    or_clause = ' or '.join(f"RowKey eq '{_escape_odata(name)}'" for name in filenames)
    try:
        rows = metadata_table_client.query_entities(
            f"PartitionKey eq '{_escape_odata(user_id)}' and ({or_clause})"
        )
        for row in rows:
            existing_by_name[str(row.get('RowKey') or '')] = dict(row)
    except Exception:
        existing_by_name = {}

    anonymous_blob_names: Dict[str, str] = {}
    operations = []
    for f in files:
        filename = str(f['filename'])
        entity = existing_by_name.get(filename) or {'PartitionKey': user_id, 'RowKey': filename}
        if f.get('is_fresh'):
            entity['received_ranges'] = json.dumps([])
            entity['upload_total_size'] = f['total_size']
            entity['upload_started_at'] = datetime.now(timezone.utc).isoformat()
            if f.get('expected_hash'):
                entity['upload_sha256_expected'] = f['expected_hash']

        existing_blob = str(entity.get(PENDING_ANONYMOUS_BLOB_FIELD) or entity.get('anonymousImageId') or '').strip()
        anonymous_blob_name = existing_blob or _generate_anonymous_id()
        entity[PENDING_ANONYMOUS_BLOB_FIELD] = anonymous_blob_name
        anonymous_blob_names[filename] = anonymous_blob_name
        operations.append(('upsert', entity))

    try:
        metadata_table_client.submit_transaction(operations)
    except Exception:
        # A transaction is all-or-nothing -- if the batch itself failed (rare:
        # a real Azure error, not an app bug), fall back to committing each
        # entity individually so this chunk's files still work, just without
        # the round-trip savings for this one chunk.
        for _, entity in operations:
            try:
                metadata_table_client.upsert_entity(entity)
            except Exception:
                pass
    return anonymous_blob_names


