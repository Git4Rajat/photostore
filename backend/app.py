import base64
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import zipfile
import time
import logging
import threading
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

from azure.core.exceptions import AzureError
from azure.data.tables import TableServiceClient
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobSasPermissions, BlobServiceClient, generate_blob_sas
from azure.storage.queue import QueueServiceClient
from flask import Flask, Response, jsonify, make_response, request
from auth_utils import get_request_user_id as resolve_request_user_id
import password_auth
import email_utils
from image_utils import (
    RAW_EXTENSIONS_CINEMA,
    RAW_EXTENSIONS_RAWPY,
    allowed_file,
    convert_image_to_jpeg,
    create_placeholder_thumbnail,
    is_video_file,
)
import vision_utils
from search_utils import (
    build_expanded_query_text,
    build_semantic_text,
    cosine_similarity,
    lexical_search_score,
    parse_json_list,
    parse_tags,
    parse_search_query,
)
from storage_utils import (
    configure_storage,
    apply_client_processing_results_for_file,
    download_media_bytes,
    finalize_uploaded_file,
    get_media_properties,
    claim_processing_lease,
    heartbeat_processing_lease,
    reset_received_ranges,
    release_processing_lease,
    update_processing_status,
    upload_media_file,
    prime_available_vector_indexes,
    refresh_user_vector_index,
    touch_user_vector_index_state,
    vector_search_candidates,
    LOCAL_VISION_FALLBACK_MODEL,
    LOCAL_VISION_FALLBACK_TAXONOMY_VERSION,
    LOCAL_VISION_FALLBACK_RUNTIME,
    PHOTO_EMBEDDING_MODEL_VERSION,
    PHOTO_EMBEDDING_DIMENSION,
)
from pillow_heif import register_heif_opener
from PIL import Image, ImageOps
from werkzeug.utils import secure_filename
from exif_utils import (
    extract_gps_decimal_from_exif,
    exif_summary,
    parse_exif_data,
)
app = Flask(__name__)
# The app always runs behind the Azure Container Apps ingress (a single trusted
# reverse proxy) in production. Honor its X-Forwarded-* headers so request.is_secure,
# request.host, and the client IP reflect the real external request. In local
# development there is no proxy, so these headers are absent and behavior is unchanged.
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
worker_logger = logging.getLogger(__name__)
placeholder_bytes = create_placeholder_thumbnail()

# Face embeddings are unit-normalized before clustering. Browser-generated
# embeddings are the clustering source of truth now, so version gating must stay
# strict and avoid comparing mixed embedding sources.


def _resolve_people_cluster_preset() -> str:
    preset = os.getenv('PEOPLE_CLUSTER_PRESET', 'strictest').strip().lower()
    return preset if preset in {'strictest', 'strict', 'balanced', 'loose'} else 'strictest'


def _resolve_people_cluster_config() -> Dict[str, object]:
    presets = {
        'strictest': {
            # Prefer false negatives over false merges by a wide margin.
            'eps': 0.03,
            'absolute_max_pair_distance': 0.025,
            'match_threshold': 0.99,
            'match_margin': 0.20,
            'assign_threshold': 0.99,
            'assign_margin': 0.20,
        },
        'strict': {
            # Favor false negatives over false merges.
            'eps': 0.10,
            'absolute_max_pair_distance': 0.08,
            'match_threshold': 0.95,
            'match_margin': 0.12,
            'assign_threshold': 0.97,
            'assign_margin': 0.14,
        },
        'balanced': {
            'eps': 0.12,
            'absolute_max_pair_distance': 0.10,
            'match_threshold': 0.90,
            'match_margin': 0.08,
            'assign_threshold': 0.94,
            'assign_margin': 0.12,
        },
        'loose': {
            'eps': 0.14,
            'absolute_max_pair_distance': 0.12,
            'match_threshold': 0.88,
            'match_margin': 0.06,
            'assign_threshold': 0.92,
            'assign_margin': 0.10,
        },
    }
    preset = _resolve_people_cluster_preset()
    defaults = presets[preset]
    strictest = presets['strictest']

    def _resolve_float(name: str, default: float) -> float:
        raw = os.getenv(name, '').strip()
        if not raw:
            return float(default)
        try:
            return float(raw)
        except Exception:
            return float(default)

    eps = _resolve_float('PEOPLE_CLUSTER_EPS', defaults['eps'])
    absolute_max_pair_distance = _resolve_float(
        'PEOPLE_CLUSTER_ABSOLUTE_MAX_PAIR_DISTANCE',
        defaults['absolute_max_pair_distance'],
    )
    match_threshold = _resolve_float('PEOPLE_MATCH_THRESHOLD', defaults['match_threshold'])
    match_margin = _resolve_float('PEOPLE_MATCH_MARGIN', defaults['match_margin'])
    assign_threshold = _resolve_float('PEOPLE_CLUSTER_ASSIGN_THRESHOLD', defaults['assign_threshold'])
    assign_margin = _resolve_float('PEOPLE_CLUSTER_ASSIGN_MARGIN', defaults['assign_margin'])

    if preset == 'strictest':
        eps = min(eps, strictest['eps'])
        absolute_max_pair_distance = min(absolute_max_pair_distance, strictest['absolute_max_pair_distance'])
        match_threshold = max(match_threshold, strictest['match_threshold'])
        match_margin = max(match_margin, strictest['match_margin'])
        assign_threshold = max(assign_threshold, strictest['assign_threshold'])
        assign_margin = max(assign_margin, strictest['assign_margin'])

    return {
        'preset': preset,
        'eps': eps,
        'absolute_max_pair_distance': absolute_max_pair_distance,
        'match_threshold': match_threshold,
        'match_margin': match_margin,
        'assign_threshold': assign_threshold,
        'assign_margin': assign_margin,
    }


def _resolve_people_cluster_job_params(eps=None, min_samples=2) -> Tuple[float, int]:
    try:
        requested_eps = PEOPLE_CLUSTER_EPS if eps is None else float(eps)
    except Exception:
        requested_eps = PEOPLE_CLUSTER_EPS
    effective_eps = min(float(requested_eps), float(PEOPLE_CLUSTER_EPS))
    try:
        requested_min_samples = int(min_samples)
    except Exception:
        requested_min_samples = 2
    effective_min_samples = max(2, requested_min_samples)
    return effective_eps, effective_min_samples


_PEOPLE_CLUSTER_CONFIG = _resolve_people_cluster_config()
PEOPLE_CLUSTER_PRESET = str(_PEOPLE_CLUSTER_CONFIG['preset'])
# Keep the default strictest so similar-looking but different people stay
# separate unless an environment override explicitly tightens clustering even
# further.
PEOPLE_CLUSTER_EPS = float(_PEOPLE_CLUSTER_CONFIG['eps'])
# Extra guardrail: do not keep members in the same cluster if they are farther
# apart than this absolute cosine-distance ceiling.
PEOPLE_CLUSTER_ABSOLUTE_MAX_PAIR_DISTANCE = float(_PEOPLE_CLUSTER_CONFIG['absolute_max_pair_distance'])
PEOPLE_CLUSTER_MAX_PAIR_DISTANCE = float(os.getenv('PEOPLE_CLUSTER_MAX_PAIR_DISTANCE', str(PEOPLE_CLUSTER_ABSOLUTE_MAX_PAIR_DISTANCE)))

register_heif_opener()

MAX_UPLOAD_FILE_BYTES = int(os.getenv('MAX_UPLOAD_FILE_BYTES', str(5 * 1024 * 1024 * 1024)))
DIRECT_UPLOAD_SAS_MINUTES = int(os.getenv('DIRECT_UPLOAD_SAS_MINUTES', '360'))
UPLOAD_TMP_DIR = os.getenv('UPLOAD_TMP_DIR', '/tmp/photostore-uploads')

STORAGE_ACCOUNT_NAME = os.getenv('STORAGE_ACCOUNT_NAME') or os.getenv('AZURE_STORAGE_ACCOUNT_NAME')
STORAGE_CONNECTION_STRING = os.getenv('AZURE_STORAGE_CONNECTION_STRING') or os.getenv('AzureWebJobsStorage')
IMAGE_CONTAINER = os.getenv('IMAGE_CONTAINER', 'images')
THUMBNAIL_CONTAINER = os.getenv('THUMBNAIL_CONTAINER', 'thumbnails')
METADATA_TABLE = os.getenv('METADATA_TABLE', 'photometadata')
ALBUMS_TABLE = os.getenv('ALBUMS_TABLE', 'photoalbums')
PEOPLE_TABLE = os.getenv('PEOPLE_TABLE', 'photopeople')
FACE_TABLE = os.getenv('FACE_TABLE', 'photofaces')
MERGE_TABLE = os.getenv('MERGE_TABLE', 'personmerges')
ALLOWED_ORIGINS = os.getenv('ALLOWED_ORIGINS', 'http://localhost:3000,http://localhost:5173')
SPA_BASE_URL = os.getenv('SPA_BASE_URL', '').strip()
AZURE_AD_TENANT_ID = os.getenv('AZURE_AD_TENANT_ID', '').strip()
AZURE_AD_CLIENT_ID = os.getenv('AZURE_AD_CLIENT_ID', '').strip()
AZURE_AD_API_AUDIENCE = os.getenv('AZURE_AD_API_AUDIENCE', '').strip()
AUTH_REQUIRED = os.getenv('AUTH_REQUIRED', 'false').lower() in ('1', 'true', 'yes')
# Auth mode: 'password' (single-owner email + password, the simple self-host default)
# or 'entra' (Microsoft Entra SSO, for advanced/enterprise deployments).
AUTH_MODE = os.getenv('AUTH_MODE', 'password').strip().lower()
# Single-owner password-mode configuration.
OWNER_EMAIL = os.getenv('OWNER_EMAIL', '').strip()
OWNER_PASSWORD = os.getenv('OWNER_PASSWORD', '')
CONFIG_TABLE = os.getenv('CONFIG_TABLE', 'photostoreconfig')
# Secret used to sign stateless session tokens. Falls back to a per-process random
# value so the app still runs, but sessions then invalidate on restart / across
# replicas — set it explicitly (a Container App secret) in production.
SESSION_SECRET = os.getenv('SESSION_SECRET', '') or secrets.token_hex(32)
SESSION_TTL_SECONDS = int(os.getenv('SESSION_TTL_SECONDS', str(30 * 24 * 3600)))
# Base URL of the web app, used to build password-reset links in emails.
PUBLIC_APP_BASE_URL = os.getenv('PUBLIC_APP_BASE_URL', '').strip() or SPA_BASE_URL
# When false (the default), the unauthenticated `X-User-ID` header is never trusted as
# an identity. It may only be used as a local development convenience by explicitly
# opting in AND leaving auth un-enforced. Any enforced deployment ignores it entirely.
TRUST_USER_HEADER = os.getenv('TRUST_USER_HEADER', 'false').lower() in ('1', 'true', 'yes')
# Admin-only operations (user invite/revoke, index rebuilds) require the caller's role.
# Optionally seed a comma-separated allow-list of admin identifiers/emails for bootstrap.
ADMIN_USER_IDS = {
    value.strip().lower()
    for value in os.getenv('ADMIN_USER_IDS', '').split(',')
    if value.strip()
}

BLOB_CONNECTION_STRING = os.getenv('BLOB_CONNECTION_STRING', '').strip()
BLOB_IMAGE_CONTAINER = os.getenv('BLOB_IMAGE_CONTAINER', IMAGE_CONTAINER).strip()
BLOB_THUMBNAIL_CONTAINER = os.getenv('BLOB_THUMBNAIL_CONTAINER', THUMBNAIL_CONTAINER).strip()
BLOB_COVER_CONTAINER = os.getenv('BLOB_COVER_CONTAINER', 'covers').strip()
BLOB_VECTOR_INDEX_CONTAINER = os.getenv('BLOB_VECTOR_INDEX_CONTAINER', 'vector-index').strip()
VECTOR_INDEX_PRIME_ON_STARTUP = os.getenv('VECTOR_INDEX_PRIME_ON_STARTUP', 'true').lower() in ('1', 'true', 'yes')
VECTOR_INDEX_PRIME_MAX_USERS = max(0, int(os.getenv('VECTOR_INDEX_PRIME_MAX_USERS', '200')))
SEMANTIC_SEARCH_ALLOW_QUERYTIME_ROW_EMBEDDINGS = os.getenv(
    'SEMANTIC_SEARCH_ALLOW_QUERYTIME_ROW_EMBEDDINGS',
    'false',
).lower() in ('1', 'true', 'yes')

# Feature toggles
MAPS_ENABLED = os.getenv('MAPS_ENABLED', 'true').lower() in ('1', 'true', 'yes')
MAPS_ON_UPLOAD = os.getenv('MAPS_ON_UPLOAD', 'false').lower() in ('1', 'true', 'yes')
MAPS_QUEUE_ON_UPLOAD = os.getenv('MAPS_QUEUE_ON_UPLOAD', 'true').lower() in ('1', 'true', 'yes')
BROWSER_ONLY_PROCESSING = os.getenv('BROWSER_ONLY_PROCESSING', 'false').lower() in ('1', 'true', 'yes')
CLUSTERING_QUEUE_NAME = os.getenv('CLUSTERING_QUEUE_NAME', 'photostore-clustering')
CLIENT_PROCESSING_LATE_RESULT_WAIT_SECONDS = max(0, int(os.getenv('CLIENT_PROCESSING_LATE_RESULT_WAIT_SECONDS', '750')))
CLIENT_PROCESSING_DEFAULT_LEASE_SECONDS = max(30, int(os.getenv('CLIENT_PROCESSING_DEFAULT_LEASE_SECONDS', '120')))
FACE_REQUIRE_AI_PERSON_TAG = os.getenv('FACE_REQUIRE_AI_PERSON_TAG', 'true').lower() in ('1', 'true', 'yes')
DEFAULT_FACE_PERSON_TAGS = (
    'person,people,portrait,human,face,selfie,man,woman,boy,girl,child,baby,'
    'toddler,adult,group,family,crowd'
)
FACE_PERSON_TAGS = {
    tag.strip().lower()
    for tag in os.getenv('FACE_PERSON_TAGS', DEFAULT_FACE_PERSON_TAGS).split(',')
    if tag.strip()
}
FACE_PERSON_SCORE_THRESHOLD = float(os.getenv('FACE_PERSON_SCORE_THRESHOLD', '0.20'))

# Keep person matching conservative so clustering does not collapse distinct faces into one cluster.
PEOPLE_MATCH_THRESHOLD = float(_PEOPLE_CLUSTER_CONFIG['match_threshold'])
PEOPLE_MATCH_MARGIN = float(_PEOPLE_CLUSTER_CONFIG['match_margin'])
PEOPLE_CLUSTER_ASSIGN_THRESHOLD = float(_PEOPLE_CLUSTER_CONFIG['assign_threshold'])
PEOPLE_CLUSTER_ASSIGN_MARGIN = float(_PEOPLE_CLUSTER_CONFIG['assign_margin'])
PEOPLE_SUGGEST_THRESHOLD = float(os.getenv('PEOPLE_SUGGEST_THRESHOLD', '0.78'))
PEOPLE_SUGGEST_LIMIT = int(os.getenv('PEOPLE_SUGGEST_LIMIT', '20'))
PEOPLE_SUGGEST_PER_PERSON = int(os.getenv('PEOPLE_SUGGEST_PER_PERSON', '2'))
SUSPICIOUS_FACE_CONFIDENCE = float(os.getenv('SUSPICIOUS_FACE_CONFIDENCE', '0.60'))
FACE_MIN_STORE_CONFIDENCE = float(os.getenv('FACE_MIN_STORE_CONFIDENCE', '0.24'))
FACE_LOW_CONFIDENCE_REJECT_BELOW = float(os.getenv('FACE_LOW_CONFIDENCE_REJECT_BELOW', '0.32'))
FACE_LOW_CONFIDENCE_MAX_AREA_RATIO = float(os.getenv('FACE_LOW_CONFIDENCE_MAX_AREA_RATIO', '0.08'))
FACE_LOW_CONFIDENCE_MAX_SIDE_RATIO = float(os.getenv('FACE_LOW_CONFIDENCE_MAX_SIDE_RATIO', '0.42'))
FACE_CLUSTER_EMBEDDING_VERSION = (
    os.getenv('FACE_CLUSTER_EMBEDDING_VERSION')
    or 'browser-hybrid-arcface-faceapi-v1'
).strip()
FACE_CLUSTER_EMBEDDING_DIMENSIONS = int(os.getenv('FACE_CLUSTER_EMBEDDING_DIMENSIONS', '640'))
FACE_CLUSTER_LEGACY_EMBEDDING_DIMENSIONS = 512
FACE_CLUSTER_LEGACY_ARCFACE_VERSION = 'browser-arcface-onnx-model-zoo-v1'
FACE_CLUSTER_LEGACY_FACE_VERSION = 'browser-face-embedding-v1'


def _face_embedding_allowed_versions() -> set:
    return {
        version
        for version in {
            FACE_CLUSTER_EMBEDDING_VERSION,
            FACE_CLUSTER_LEGACY_ARCFACE_VERSION,
            FACE_CLUSTER_LEGACY_FACE_VERSION,
        }
        if version
    }
PHOTO_TABLE_SCAN_PAGE_SIZE = int(os.getenv('PHOTO_TABLE_SCAN_PAGE_SIZE', '1000'))
PHOTO_TABLE_SCAN_MAX_ROWS = int(os.getenv('PHOTO_TABLE_SCAN_MAX_ROWS', '250000'))

# Module-level storage/credential defaults (set during startup if available)
account_name = None
credential = None
metadata_table_client = None
blob_service_client = None
albums_table_client = None
face_table_client = None
person_table_client = None
merge_table_client = None
config_table_client = None
clustering_queue_client = None
queue_service_client = None


def _prime_vector_indexes_on_startup() -> None:
    if not VECTOR_INDEX_PRIME_ON_STARTUP or VECTOR_INDEX_PRIME_MAX_USERS <= 0:
        return

    def _worker() -> None:
        try:
            result = prime_available_vector_indexes(max_users=VECTOR_INDEX_PRIME_MAX_USERS)
            app.logger.info('Vector index startup prime completed: %s', result)
        except Exception as exc:
            app.logger.warning('Vector index startup prime skipped: %s', exc)

    thread = threading.Thread(target=_worker, name='vector-index-prime', daemon=True)
    thread.start()


def _init_storage_clients():
    global account_name, credential
    global metadata_table_client
    global blob_service_client, albums_table_client, face_table_client, person_table_client, merge_table_client
    global config_table_client
    global clustering_queue_client, queue_service_client

    account_name = STORAGE_ACCOUNT_NAME or os.getenv('AZURE_STORAGE_ACCOUNT_NAME')

    # Prefer local/Azurite connection string when provided.
    if STORAGE_CONNECTION_STRING:
        tbl_svc = TableServiceClient.from_connection_string(STORAGE_CONNECTION_STRING)
        metadata_table_client_local = tbl_svc.get_table_client(METADATA_TABLE)
        albums_table_client_local = tbl_svc.get_table_client(ALBUMS_TABLE)
        face_table_client_local = tbl_svc.get_table_client(FACE_TABLE)
        person_table_client_local = tbl_svc.get_table_client(PEOPLE_TABLE)
        merge_table_client_local = tbl_svc.get_table_client(MERGE_TABLE)
        config_table_client_local = tbl_svc.get_table_client(CONFIG_TABLE)

        if BLOB_CONNECTION_STRING:
            blob_service_client_local = BlobServiceClient.from_connection_string(BLOB_CONNECTION_STRING)
        else:
            blob_service_client_local = BlobServiceClient.from_connection_string(STORAGE_CONNECTION_STRING)
        queue_service_client_local = QueueServiceClient.from_connection_string(STORAGE_CONNECTION_STRING)
        clustering_queue_client_local = queue_service_client_local.get_queue_client(CLUSTERING_QUEUE_NAME)
    else:
        # Managed identity mode (Azure)
        credential = DefaultAzureCredential()
        if not account_name:
            raise RuntimeError('STORAGE_ACCOUNT_NAME must be set for managed identity authentication.')

        if BLOB_CONNECTION_STRING:
            blob_service_client_local = BlobServiceClient.from_connection_string(BLOB_CONNECTION_STRING)
        else:
            blob_service_client_local = BlobServiceClient(
                account_url=f'https://{account_name}.blob.core.windows.net',
                credential=credential,
            )
        queue_service_client_local = QueueServiceClient(
            account_url=f'https://{account_name}.queue.core.windows.net',
            credential=credential,
        )
        clustering_queue_client_local = queue_service_client_local.get_queue_client(CLUSTERING_QUEUE_NAME)

        # Table clients
        tbl_svc = TableServiceClient(endpoint=f'https://{account_name}.table.core.windows.net', credential=credential)
        metadata_table_client_local = tbl_svc.get_table_client(METADATA_TABLE)
        albums_table_client_local = tbl_svc.get_table_client(ALBUMS_TABLE)
        face_table_client_local = tbl_svc.get_table_client(FACE_TABLE)
        person_table_client_local = tbl_svc.get_table_client(PEOPLE_TABLE)
        merge_table_client_local = tbl_svc.get_table_client(MERGE_TABLE)
        config_table_client_local = tbl_svc.get_table_client(CONFIG_TABLE)

    # assign to globals
    metadata_table_client = metadata_table_client_local
    config_table_client = config_table_client_local
    blob_service_client = blob_service_client_local
    albums_table_client = albums_table_client_local
    face_table_client = face_table_client_local
    person_table_client = person_table_client_local
    merge_table_client = merge_table_client_local
    clustering_queue_client = clustering_queue_client_local
    queue_service_client = queue_service_client_local

    try:
        clustering_queue_client.create_queue()
    except Exception as exc:
        app.logger.debug('Queue ensure skipped for %s: %s', CLUSTERING_QUEUE_NAME, exc)

    # Password-mode: ensure the config table exists and seed the initial owner
    # credential from OWNER_EMAIL/OWNER_PASSWORD on first boot (no-op afterwards).
    if AUTH_MODE == 'password':
        try:
            config_table_client.create_table()
        except Exception as exc:
            app.logger.debug('Config table ensure skipped for %s: %s', CONFIG_TABLE, exc)
        try:
            if password_auth.seed_owner_if_missing(config_table_client, OWNER_EMAIL, OWNER_PASSWORD):
                app.logger.info('Seeded initial owner credential for %s', OWNER_EMAIL or '(no email)')
        except Exception as exc:
            app.logger.warning('Owner credential seeding failed: %s', exc)

    # Configure storage_utils (do not pass account keys or SAS keys)
    configure_storage(
        metadata_table_client=metadata_table_client,
        face_table_client=face_table_client,
        blob_service_client=blob_service_client,
        blob_image_container=BLOB_IMAGE_CONTAINER,
        blob_thumbnail_container=BLOB_THUMBNAIL_CONTAINER,
        blob_cover_container=BLOB_COVER_CONTAINER,
        blob_vector_index_container=BLOB_VECTOR_INDEX_CONTAINER,
        queue_map_on_upload=(MAPS_QUEUE_ON_UPLOAD and not MAPS_ON_UPLOAD),
    )
    _prime_vector_indexes_on_startup()


# Run initialization at import time (best-effort)
try:
    _init_storage_clients()
except Exception as exc:
    app.logger.error('Storage init failed: %s', exc)


def _ensure_table_service_client():
    if STORAGE_CONNECTION_STRING:
        return TableServiceClient.from_connection_string(STORAGE_CONNECTION_STRING)

    # Enforce managed identity only for table access
    if not STORAGE_ACCOUNT_NAME:
        raise RuntimeError('STORAGE_ACCOUNT_NAME is required to initialize TableServiceClient with managed identity.')
    credential = DefaultAzureCredential()
    table_endpoint = f'https://{STORAGE_ACCOUNT_NAME}.table.core.windows.net'
    return TableServiceClient(endpoint=table_endpoint, credential=credential)


def create_metadata_table() -> None:
    try:
        svc = _ensure_table_service_client()
        svc.create_table_if_not_exists(table_name=METADATA_TABLE)
    except AzureError:
        pass


def create_albums_table() -> None:
    try:
        svc = _ensure_table_service_client()
        svc.create_table_if_not_exists(table_name=ALBUMS_TABLE)
    except AzureError:
        pass


def create_face_table() -> None:
    try:
        svc = _ensure_table_service_client()
        svc.create_table_if_not_exists(table_name=FACE_TABLE)
    except AzureError:
        pass


def create_person_table() -> None:
    try:
        svc = _ensure_table_service_client()
        svc.create_table_if_not_exists(table_name=PEOPLE_TABLE)
    except AzureError:
        pass


def create_merge_table() -> None:
    try:
        svc = _ensure_table_service_client()
        svc.create_table_if_not_exists(table_name=MERGE_TABLE)
    except AzureError:
        pass


def create_blob_containers() -> None:
    if blob_service_client is None:
        return
    for container_name in (BLOB_IMAGE_CONTAINER, BLOB_THUMBNAIL_CONTAINER, BLOB_VECTOR_INDEX_CONTAINER):
        if not container_name:
            continue
        try:
            blob_service_client.create_container(container_name)
        except AzureError:
            pass


# Use the implementations from the utility modules (`image_utils`, `storage_utils`).
# The local copies were removed to avoid shadowing the imported helpers.

def parse_allowed_origins(origins_value: str) -> List[str]:
    if not origins_value:
        return []
    origins = []
    for origin in origins_value.split(','):
        cleaned = origin.strip().rstrip('/')
        if not cleaned or cleaned == '*':
            continue
        origins.append(cleaned)
    return origins


DEFAULT_ALLOWED_ORIGINS = set(parse_allowed_origins(ALLOWED_ORIGINS))
# Localhost dev origins are only allowed when auth is not enforced (i.e. local development),
# or when explicitly opted in. An enforced production deployment does not reflect them.
_ALLOW_LOCALHOST_ORIGINS = (
    os.getenv('ALLOW_LOCALHOST_ORIGINS', '').lower() in ('1', 'true', 'yes')
    or not AUTH_REQUIRED
)
if _ALLOW_LOCALHOST_ORIGINS:
    DEFAULT_ALLOWED_ORIGINS.update({
        'http://localhost:3000',
        'http://127.0.0.1:3000',
        'http://localhost:5173',
        'http://127.0.0.1:5173',
        'http://localhost:3001',
        'http://127.0.0.1:3001'
    })
if SPA_BASE_URL:
    DEFAULT_ALLOWED_ORIGINS.add(SPA_BASE_URL.rstrip('/'))


def _origin_is_allowed(origin: str) -> bool:
    origin = (origin or '').strip().rstrip('/')
    if not origin:
        return False
    if origin in DEFAULT_ALLOWED_ORIGINS:
        return True

    parsed = urlparse(origin)
    origin_host = (parsed.hostname or '').lower()
    request_host = (request.headers.get('X-Forwarded-Host') or request.host or '').split(',')[0].strip().split(':')[0].lower()
    if parsed.scheme not in {'http', 'https'} or not origin_host or not request_host:
        return False
    if not origin_host.endswith('.azurecontainerapps.io') or not request_host.endswith('.azurecontainerapps.io'):
        return False

    origin_parts = origin_host.split('.')
    request_parts = request_host.split('.')
    if len(origin_parts) < 5 or len(request_parts) < 5:
        return False
    return (
        origin_parts[0].startswith('photostore-frontend-')
        and request_parts[0].startswith('photostore-backend-')
        and origin_parts[1:] == request_parts[1:]
    )


def _escape_odata(value: str) -> str:
    return str(value).replace("'", "''")


def _coerce_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in ('1', 'true', 'yes')


def _parse_iso_date(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return None


def _parse_capture_date(exif_data: Dict[str, str]) -> Optional[datetime]:
    if not exif_data:
        return None
    raw = (
        exif_data.get('DateTimeOriginal')
        or exif_data.get('DateTime')
        or exif_data.get('CreateDate')
        or exif_data.get('MediaCreateDate')
        or exif_data.get('TrackCreateDate')
        or ''
    )
    if not raw:
        return None
    # exiftool video dates may carry a timezone offset (e.g. 2024:01:02 10:00:00+05:30).
    for fmt in ('%Y:%m:%d %H:%M:%S', '%Y:%m:%d %H:%M:%S%z'):
        try:
            parsed = datetime.strptime(raw[:26], fmt)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except Exception:
            continue
    return None


def _parse_capture_filter(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.strptime(value, '%Y-%m-%d')
        return parsed.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _metadata_upload_date(metadata: Dict) -> datetime:
    parsed = _parse_iso_date(str(metadata.get('uploadDate') or metadata.get('last_processing_update') or ''))
    return parsed or datetime.min.replace(tzinfo=timezone.utc)


def _metadata_capture_date(metadata: Dict) -> datetime:
    captured = _parse_capture_date(parse_exif_data(metadata.get('exifData', '{}')))
    if captured:
        return captured  # already UTC-aware from _parse_capture_date
    return _metadata_upload_date(metadata)


def _capture_in_range(metadata: Dict, capture_start: Optional[datetime], capture_end: Optional[datetime]) -> bool:
    if not capture_start and not capture_end:
        return True
    exif_data = parse_exif_data(metadata.get('exifData', '{}'))
    captured = _parse_capture_date(exif_data)
    if not captured:
        return False
    if capture_start and captured.date() < capture_start.date():
        return False
    if capture_end and captured.date() > capture_end.date():
        return False
    return True


def _get_spa_base_url() -> str:
    if SPA_BASE_URL:
        return SPA_BASE_URL.rstrip('/')
    origin = (request.headers.get('Origin') or '').strip()
    if origin:
        return origin.rstrip('/')
    return request.host_url.rstrip('/')


def _album_is_expired(entity: Dict) -> bool:
    expires_at = entity.get('publicExpiresAt') or ''
    expires_dt = _parse_iso_date(str(expires_at))
    if not expires_dt:
        return False
    return datetime.now(timezone.utc) > expires_dt


# Secret used to sign short-lived access grants for code-protected public albums so that
# the media routes (loaded as <img src>, which cannot carry the access code) can verify
# the visitor already cleared the code check. Falls back to a per-process random secret,
# which simply means outstanding grants are invalidated on restart.
_ALBUM_GRANT_SECRET = (
    os.getenv('ALBUM_GRANT_SECRET', '').strip()
    or secrets.token_hex(32)
)
_ALBUM_GRANT_COOKIE_PREFIX = 'album_grant_'


def _album_access_code(entity: Dict) -> str:
    return str(entity.get('accessCode') or '').strip()


def _album_grant_cookie_name(token: str) -> str:
    digest = hashlib.sha256(str(token).encode('utf-8')).hexdigest()[:16]
    return f'{_ALBUM_GRANT_COOKIE_PREFIX}{digest}'


def _sign_album_grant(token: str, access_code: str) -> str:
    message = f'{token}:{access_code}'.encode('utf-8')
    return hmac.new(_ALBUM_GRANT_SECRET.encode('utf-8'), message, hashlib.sha256).hexdigest()


def _album_grant_valid(entity: Dict, token: str) -> bool:
    """True when the album is unprotected, or the request carries a valid signed grant."""
    access_code = _album_access_code(entity)
    if not access_code:
        return True
    provided = str(request.cookies.get(_album_grant_cookie_name(token), '') or '')
    if not provided:
        return False
    return hmac.compare_digest(provided, _sign_album_grant(token, access_code))


def _album_entity_to_payload(entity: Dict) -> Dict:
    filenames = []
    try:
        filenames = json.loads(entity.get('filenames', '[]') or '[]')
    except Exception:
        filenames = []
    is_public = _coerce_bool(entity.get('isPublic', False))
    token = entity.get('publicToken') or ''
    has_access_code = bool(str(entity.get('accessCode', '')).strip())
    is_expired = _album_is_expired(entity)
    public_url = ''
    if is_public and token and not is_expired:
        public_url = f"{_get_spa_base_url()}/public/album/{token}"
    return {
        'id': entity.get('RowKey'),
        'name': entity.get('name', ''),
        'photoCount': len(filenames),
        'filenames': filenames,
        'isPublic': is_public and not is_expired,
        'publicUrl': public_url,
        'publicExpiresAt': entity.get('publicExpiresAt') or '',
        'hasAccessCode': has_access_code,
        'isExpired': is_expired,
    }


def _location_from_metadata(metadata: Dict, exif_data: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    lat = str(metadata.get('latitude', '') or '')
    lon = str(metadata.get('longitude', '') or '')
    if (not lat or not lon) and exif_data:
        exif_lat, exif_lon = extract_gps_decimal_from_exif(exif_data)
        lat = lat or exif_lat
        lon = lon or exif_lon

    return {
        'latitude': lat,
        'longitude': lon,
        'address': metadata.get('address', ''),
        'city': metadata.get('locationCity', ''),
        'country': metadata.get('locationCountry', ''),
    }


def _resolution_from_exif(exif_data: Dict[str, str]) -> Dict[str, int]:
    def _to_int(value) -> int:
        try:
            return max(0, int(float(value)))
        except Exception:
            return 0

    width = (
        _to_int(exif_data.get('ExifImageWidth'))
        or _to_int(exif_data.get('PixelXDimension'))
        or _to_int(exif_data.get('ImageWidth'))
    )
    height = (
        _to_int(exif_data.get('ExifImageHeight'))
        or _to_int(exif_data.get('PixelYDimension'))
        or _to_int(exif_data.get('ImageLength'))
    )
    return {'width': width, 'height': height}


def _normalize_rotation(value) -> int:
    try:
        rotation = int(value or 0)
    except Exception:
        rotation = 0
    return rotation % 360


def _thumbnail_url_from_metadata(metadata: Dict, filename: str) -> str:
    """Return a thumbnail proxy URL when a real thumbnail or backend preview can be served."""
    if (
        str((metadata or {}).get('thumbnail_status') or '').strip().lower() != 'done'
        and not _filename_requires_backend_preview(filename)
    ):
        return ''
    return make_proxy_url(filename, 'thumbnail')


def _private_photo_media_urls(filename: str) -> Dict[str, str]:
    return {
        'url': make_proxy_url(filename, 'image'),
        'thumbnailUrl': make_proxy_url(filename, 'thumbnail'),
    }


def _build_photo_summary(user_id: str, filename: str, metadata: Dict, include_props: bool = True) -> Dict:
    last_modified = None
    size = 0
    if include_props:
        try:
            props = get_media_properties('image', filename)
            size = props.get('size') or 0
            last_modified = props.get('last_modified')
        except Exception:
            last_modified = None
            size = 0

    exif_data = parse_exif_data(metadata.get('exifData', '{}'))
    summary = exif_summary(exif_data) if exif_data else {}
    liked_by = json.loads(metadata.get('likedBy', '[]') or '[]')
    try:
        processing_metadata = json.loads(metadata.get('processing_metadata') or '{}')
    except Exception:
        processing_metadata = {}
    client_face = processing_metadata.get('client_face') if isinstance(processing_metadata, dict) else {}
    face_source = ''
    if isinstance(client_face, dict):
        face_source = str(client_face.get('detectionSource') or client_face.get('source') or '').strip()
    client_thumbnail = processing_metadata.get('client_thumbnail') if isinstance(processing_metadata, dict) else {}
    thumbnail_rotation = 0
    if isinstance(client_thumbnail, dict):
        thumbnail_rotation = _normalize_rotation(client_thumbnail.get('rotationDegrees', 0))

    media_urls = _private_photo_media_urls(filename)
    return {
        'filename': filename,
        'url': media_urls['url'],
        'thumbnailUrl': _thumbnail_url_from_metadata(metadata, filename),
        'size': size,
        'lastModified': last_modified.isoformat() if last_modified else None,
        'rating': metadata.get('rating', 0),
        'likes': metadata.get('likes', 0),
        'liked': user_id in liked_by,
        'tags': json.loads(metadata.get('tags', '[]') or '[]'),
        'rotation': _normalize_rotation(metadata.get('rotation', 0)),
        'thumbnailRotation': thumbnail_rotation,
        'location': _location_from_metadata(metadata, exif_data),
        'hasExif': bool(metadata.get('exifCount', 0)),
        'exifSummary': summary,
        'resolution': _resolution_from_exif(exif_data),
        'faceCount': metadata.get('faceCount', 0),
        'processing': {
            'thumbnail': metadata.get('thumbnail_status'),
            'exif': metadata.get('exif_status'),
            'ocr': metadata.get('ocr_status'),
            'face': metadata.get('face_status'),
            'faceSource': face_source or None,
            'aiVision': metadata.get('ai_vision_status'),
            'mapDetection': metadata.get('map_detection_status'),
        },
    }


def _require_user_id(require_auth: bool = False):
    # Password mode: resolve the single owner identity from a signed session token.
    if AUTH_MODE == 'password':
        must_auth = AUTH_REQUIRED or require_auth
        user_id, error = password_auth.resolve_password_user_id(request.headers, SESSION_SECRET)
        if user_id:
            return user_id, None
        if not must_auth:
            # Auth not enforced (e.g. local dev): fall back to the owner identity.
            return password_auth.OWNER_USER_ID, None
        return None, (jsonify({'error': error or 'Authentication required.'}), 401)

    try:
        user_id = resolve_request_user_id(
            request.headers,
            AUTH_REQUIRED or require_auth,
            AZURE_AD_TENANT_ID,
            AZURE_AD_CLIENT_ID,
            AZURE_AD_API_AUDIENCE,
            trust_user_header=TRUST_USER_HEADER and not AUTH_REQUIRED,
        )
        return user_id, None
    except Exception as exc:
        return None, (jsonify({'error': str(exc)}), 401)


def _resolve_user_role(user_id: str) -> str:
    """Role lookup for the authenticated identity (admin allow-list only)."""
    if user_id and str(user_id).strip().lower() in ADMIN_USER_IDS:
        return 'admin'
    return ''


def _require_admin(require_auth: bool = True):
    """Return (user_id, None) for admins, or (None, error_response) otherwise."""
    user_id, error = _require_user_id(require_auth=require_auth)
    if error:
        return None, error
    if _resolve_user_role(user_id) != 'admin':
        return None, (jsonify({'error': 'Administrator privileges are required.'}), 403)
    return user_id, None


def _get_metadata_entity(user_id: str, filename: str) -> Optional[Dict]:
    try:
        return metadata_table_client.get_entity(partition_key=user_id, row_key=filename)
    except Exception:
        return None


def _get_throughput_metrics(window_minutes: int = 60) -> Dict[str, Dict[str, float]]:
    result = {
        'uploads': {'count': 0, 'bytes': 0},
        'processed': {'count': 0, 'bytes': 0},
    }
    if metadata_table_client is None:
        return result
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
    try:
        rows = metadata_table_client.query_entities("PartitionKey eq 'performance'")
    except Exception:
        return result
    for row in rows:
        try:
            occurred_at = datetime.fromisoformat(str(row.get('occurredAt') or '').replace('Z', '+00:00'))
        except Exception:
            continue
        if occurred_at < cutoff:
            continue
        metric_type = str(row.get('metricType') or '').lower()
        if metric_type not in result:
            continue
        result[metric_type]['count'] += 1
        result[metric_type]['bytes'] += int(row.get('byteCount') or 0)
    for key in result:
        bytes_per_second = result[key]['bytes'] / max(window_minutes * 60, 1)
        result[key]['bytesPerSecond'] = round(bytes_per_second, 2)
        result[key]['mbPerSecond'] = round(bytes_per_second / (1024 * 1024), 2)
    return result


def _normalize_search_phrase(value: str) -> str:
    folded = unicodedata.normalize('NFKD', str(value)).encode('ascii', 'ignore').decode('ascii')
    return re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9]+', ' ', folded.lower())).strip()


def _parse_embedding(value) -> List[float]:
    if isinstance(value, list):
        return [float(item) for item in value if isinstance(item, (int, float))]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [float(item) for item in parsed if isinstance(item, (int, float))]
        except Exception:
            pass
    return []


def _semantic_embedding_for_row(
    filename: str,
    metadata: Dict,
    current_version: str,
    *,
    allow_compute: bool = True,
) -> Tuple[List[float], str]:
    semantic_text = str(metadata.get('semanticText') or '').strip()
    if not semantic_text:
        semantic_text = build_semantic_text(filename, metadata)
    # A real image embedding (from the browser's CLIP encoder) is a much stronger
    # semantic signal than an embedding of the tag list, and doesn't inherit tag
    # mistakes. Use it whenever it shares the active embedding's vector space.
    if (
        vision_utils.get_text_embedding_dimension() == PHOTO_EMBEDDING_DIMENSION
        and str(metadata.get('photoEmbeddingVersion') or '').strip() == PHOTO_EMBEDDING_MODEL_VERSION
    ):
        photo_embedding = _parse_embedding(metadata.get('photoEmbedding', '[]'))
        if len(photo_embedding) == PHOTO_EMBEDDING_DIMENSION:
            return photo_embedding, semantic_text
    stored_version = str(metadata.get('semanticEmbeddingVersion') or '').strip()
    stored_embedding = _parse_embedding(metadata.get('semanticEmbedding', '[]'))
    if stored_embedding and stored_version == current_version:
        return stored_embedding, semantic_text
    if not allow_compute:
        return [], semantic_text
    return vision_utils.encode_text_embedding(semantic_text), semantic_text


def _load_people_name_index(user_id: str) -> Tuple[Dict[str, str], Dict[str, List[str]]]:
    pid_to_name: Dict[str, str] = {}
    name_to_ids: Dict[str, List[str]] = {}
    if person_table_client is None:
        return pid_to_name, name_to_ids
    try:
        rows = list(person_table_client.query_entities(f"PartitionKey eq '{_escape_odata(user_id)}'"))
    except Exception:
        rows = []
    for row in rows:
        person_id = str(row.get('RowKey') or '')
        name = str(row.get('name') or '').strip()
        if not person_id or not name or _is_unnamed_name(name):
            continue
        pid_to_name[person_id] = name
        normalized_name = _normalize_search_phrase(name)
        if normalized_name:
            name_to_ids.setdefault(normalized_name, []).append(person_id)
        first_name = _normalize_search_phrase(name.split()[0])
        if first_name and first_name != normalized_name:
            name_to_ids.setdefault(first_name, []).append(person_id)
    return pid_to_name, name_to_ids


def _metadata_with_people_names(metadata: Dict, pid_to_name: Dict[str, str]) -> Dict:
    row = dict(metadata)
    try:
        people_ids = json.loads(row.get('peopleIds', '[]') or '[]')
    except Exception:
        people_ids = []
    people_names = [pid_to_name.get(str(pid), '') for pid in people_ids]
    row['peopleNames'] = json.dumps([name for name in people_names if name])
    return row


def _matched_query_people_groups(query_text: str, name_to_ids: Dict[str, List[str]]) -> List[List[str]]:
    # One group of person_ids per distinct name matched in the query (a name can
    # map to more than one person_id when duplicate/unmerged clusters share a
    # display name). Kept as separate groups -- not flattened into one list --
    # so a multi-person query ("alice and bob") can require a photo to satisfy
    # EVERY named person (at least one id from each group), instead of ANY
    # queried person, which is what a single flat list would collapse to.
    query_norm = _normalize_search_phrase(query_text)
    groups = []
    for name, person_ids in name_to_ids.items():
        if name and re.search(rf'(^| ){re.escape(name)}( |$)', query_norm):
            groups.append(list(dict.fromkeys(person_ids)))
    return groups


def _known_location_terms(rows: List[Dict]) -> List[str]:
    terms = []
    for row in rows:
        for field in ('locationCity', 'locationCountry', 'address'):
            term = _normalize_search_phrase(str(row.get(field) or ''))
            for part in term.split(' '):
                if len(part) >= 3 and part not in terms:
                    terms.append(part)
            if term and term not in terms:
                terms.append(term)
    return sorted(terms, key=len, reverse=True)


def _matched_query_locations(query_text: str, rows: List[Dict]) -> List[str]:
    query_norm = _normalize_search_phrase(query_text)
    return [term for term in _known_location_terms(rows) if re.search(rf'(^| ){re.escape(term)}( |$)', query_norm)]


def _metadata_matches_locations(metadata: Dict, location_terms: List[str]) -> bool:
    if not location_terms:
        return True
    location_text = _normalize_search_phrase(' '.join([
        str(metadata.get('address', '')),
        str(metadata.get('locationCity', '')),
        str(metadata.get('locationCountry', '')),
    ]))
    return any(term in location_text for term in location_terms)


PROCESSING_STUCK_SECONDS = int(os.getenv('PROCESSING_STUCK_SECONDS', '900'))


def _running_processing_started_at(entity: Dict, step: str) -> Optional[datetime]:
    try:
        processing = json.loads(entity.get('processing_metadata') or '{}')
    except Exception:
        processing = {}
    step_meta = processing.get(step) or {}
    if isinstance(step_meta, dict):
        started_at = _parse_iso_date(str(step_meta.get('startedAt') or ''))
        if started_at is not None:
            return started_at
    return _parse_iso_date(str(entity.get('last_processing_update') or ''))


def _is_stale_running_processing(entity: Dict, step: str) -> bool:
    started_at = _running_processing_started_at(entity, step)
    if started_at is None:
        return False
    return (datetime.now(timezone.utc) - started_at).total_seconds() >= PROCESSING_STUCK_SECONDS


def _job_row_key(job_id: str) -> str:
    return secure_filename(job_id) or str(uuid.uuid4())


def _upsert_job_status(job_id: str, user_id: str, job_type: str, status: str, **fields) -> None:
    if metadata_table_client is None:
        return
    entity = {
        'PartitionKey': 'jobs',
        'RowKey': _job_row_key(job_id),
        'jobId': job_id,
        'userId': user_id,
        'jobType': job_type,
        'status': status,
        'updatedAt': datetime.now(timezone.utc).isoformat(),
    }
    for key, value in fields.items():
        if value is not None:
            entity[key] = json.dumps(value, separators=(',', ':')) if isinstance(value, (dict, list)) else value
    try:
        metadata_table_client.upsert_entity(entity)
    except Exception:
        pass


def _update_metadata_entity_fields(user_id: str, filename: str, updates: Dict) -> Optional[Dict]:
    if metadata_table_client is None:
        return None
    last_exc = None
    for attempt in range(5):
        try:
            entity = metadata_table_client.get_entity(partition_key=user_id, row_key=filename)
        except Exception:
            return None
        if str(entity.get('processing_state') or '').strip().lower() == 'deleted':
            return None
        entity.update(updates or {})
        entity['last_processing_update'] = datetime.now(timezone.utc).isoformat()
        try:
            metadata_table_client.upsert_entity(entity)
            if any(key in {
                'tags', 'objects', 'caption', 'ocrText', 'address', 'locationCity', 'locationCountry',
                'semanticText', 'semanticEmbedding', 'semanticEmbeddingVersion', 'faceCount', 'faces',
                'peopleIds', 'aiPersonLabel', 'aiPersonScore', 'subjectTags', 'backgroundTags',
                'weakTags', 'tagBuckets', 'tagMetadata', 'semanticLayers',
                'photoEmbedding', 'photoEmbeddingVersion',
            } for key in (updates or {}).keys()):
                touch_user_vector_index_state(user_id)
            return entity
        except Exception as exc:
            last_exc = exc
            time.sleep(0.05 * (2 ** attempt))
    if last_exc:
        app.logger.warning('Failed to update metadata entity %s/%s: %s', user_id, filename, last_exc)
    return None


def _clustering_job_types() -> set:
    return {'people_recluster', 'people_cluster'}


def _has_active_clustering_job(user_id: str) -> Optional[str]:
    if metadata_table_client is None:
        return None
    try:
        rows = list(metadata_table_client.query_entities("PartitionKey eq 'jobs'"))
    except Exception:
        return None
    for row in rows:
        if str(row.get('userId') or '') != user_id:
            continue
        if str(row.get('jobType') or '') not in _clustering_job_types():
            continue
        if str(row.get('status') or '').lower() not in {'queued', 'running'}:
            continue
        return str(row.get('jobId') or '')
    return None


def _enqueue_clustering_job(
    user_id: str,
    *,
    force: bool = False,
    job_type: str = 'people_recluster',
    allow_reassign_confirmed: bool = False,
    payload: Optional[Dict] = None,
) -> Dict[str, str]:
    if not force:
        existing_job_id = _has_active_clustering_job(user_id)
        if existing_job_id:
            return {'status': 'already_queued', 'jobId': existing_job_id}
    job_id = f"cluster:{user_id}:{uuid.uuid4().hex}"
    if clustering_queue_client is None:
        app.logger.warning('Clustering queue client is unavailable; job %s was not enqueued', job_id)
        return {'status': 'unavailable', 'jobId': job_id}
    message = {
        'jobId': job_id,
        'correlationId': job_id,
        'user_id': user_id,
        'type': job_type,
        'force': bool(force),
        'allowReassignConfirmed': bool(allow_reassign_confirmed),
    }
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key not in message and value is not None:
                message[key] = value
    try:
        clustering_queue_client.send_message(json.dumps(message, separators=(',', ':')))
    except Exception:
        app.logger.exception('Failed to enqueue clustering job %s', job_id)
        return {'status': 'failed', 'jobId': job_id}
    _upsert_job_status(job_id, user_id, 'clustering', 'queued', payload=payload or {})
    return {'status': 'queued', 'jobId': job_id}


def _clustering_queue_response(queue_result: Dict[str, str], **extra) -> Dict:
    response = {
        'success': queue_result.get('status') == 'queued',
        'queued': queue_result.get('status') == 'queued',
        'jobId': queue_result.get('jobId'),
        'status': queue_result.get('status'),
    }
    response.update({key: value for key, value in extra.items() if value is not None})
    return response


def _enqueue_processing_steps(
    user_id: str,
    filename: str,
    steps: List[str],
    *,
    force: bool = False,
    visibility_timeout: int = 0,
) -> Dict[str, Dict[str, str]]:
    results: Dict[str, Dict[str, str]] = {}
    entity = _get_metadata_entity(user_id, filename)
    if entity is None:
        for step in steps:
            results[step] = {'status': 'error', 'reason': 'not found'}
        return results

    visibility_timeout = max(0, min(int(visibility_timeout or 0), 7 * 24 * 60 * 60))
    for step in steps:
        status_field = f'{step}_status'
        current = str(entity.get(status_field) or '').lower()
        if current == 'done' and not force:
            results[step] = {'status': 'skipped', 'reason': f'already_{current}'}
            continue
        if current == 'running' and not force and not _is_stale_running_processing(entity, step):
            results[step] = {'status': 'skipped', 'reason': 'already_running'}
            continue
        update_processing_status(
            user_id,
            filename,
            step,
            'queued',
            result={'forced': True} if force else ({'delaySeconds': visibility_timeout, 'reason': 'client_late_result_wait'} if visibility_timeout > 0 else None),
        )
        results[step] = {
            'status': 'queued',
            'reason': 'browser_only_processing' if BROWSER_ONLY_PROCESSING else (
                'client_late_result_wait' if visibility_timeout > 0 else ('force_queued' if force else 'queued')
            ),
        }
    return results


def _count_processing_statuses(user_id: str, steps: List[str]) -> Dict[str, Dict[str, int]]:
    counts = {step: {'queued': 0, 'pending': 0, 'running': 0, 'failed': 0, 'no_data': 0} for step in steps}
    if metadata_table_client is None:
        return counts
    try:
        max_rows = int(os.getenv('PROCESSING_STATUS_MAX_ROWS', '1000'))
        fields = [f'{step}_status' for step in steps]
        rows_iter = metadata_table_client.query_entities(
            f"PartitionKey eq '{_escape_odata(user_id)}'",
            select=fields,
        )
    except Exception:
        return counts
    try:
        for idx, row in enumerate(rows_iter):
            if idx >= max_rows:
                break
            for step in steps:
                field = f'{step}_status'
                status = str(row.get(field) or '').lower()
                if status in counts[step]:
                    counts[step][status] += 1
    except Exception:
        return counts
    return counts


def _query_metadata_rows_for_user(user_id: str, select: Optional[List[str]] = None, purpose: str = 'metadata') -> List[Dict]:
    if metadata_table_client is None:
        raise RuntimeError('Metadata table is not configured.')

    query = f"PartitionKey eq '{_escape_odata(user_id)}'"
    kwargs = {}
    if select:
        kwargs['select'] = select
    if PHOTO_TABLE_SCAN_PAGE_SIZE > 0:
        kwargs['results_per_page'] = PHOTO_TABLE_SCAN_PAGE_SIZE

    started = time.monotonic()
    try:
        try:
            rows_iter = metadata_table_client.query_entities(query, **kwargs)
        except TypeError:
            kwargs.pop('results_per_page', None)
            try:
                rows_iter = metadata_table_client.query_entities(query, **kwargs)
            except TypeError:
                rows_iter = metadata_table_client.query_entities(query)

        rows: List[Dict] = []
        if hasattr(rows_iter, 'by_page'):
            for page in rows_iter.by_page():
                for row in page:
                    rows.append(dict(row))
                    if len(rows) > PHOTO_TABLE_SCAN_MAX_ROWS:
                        raise RuntimeError(f'Metadata scan exceeded {PHOTO_TABLE_SCAN_MAX_ROWS} rows.')
        else:
            for row in rows_iter:
                rows.append(dict(row))
                if len(rows) > PHOTO_TABLE_SCAN_MAX_ROWS:
                    raise RuntimeError(f'Metadata scan exceeded {PHOTO_TABLE_SCAN_MAX_ROWS} rows.')
        app.logger.info(
            'Metadata scan completed purpose=%s user=%s rows=%s elapsed_ms=%s',
            purpose,
            user_id,
            len(rows),
            round((time.monotonic() - started) * 1000),
        )
        return rows
    except Exception:
        app.logger.exception('Metadata scan failed purpose=%s user=%s', purpose, user_id)
        raise


def _is_filename_shared(filename: str, user_id: str) -> bool:
    """Returns True if the filename exists in any user's metadata except user_id."""
    if metadata_table_client is None or not filename:
        return False
    try:
        safe = _escape_odata(filename)
        rows = list(metadata_table_client.query_entities(f"RowKey eq '{safe}'"))
        for row in rows:
            if row.get('PartitionKey') != user_id:
                return True
    except Exception:
        return False
    return False


def _normalize_face_bbox(face_or_row: Dict) -> Dict[str, int]:
    bbox = face_or_row.get('bbox', {}) if isinstance(face_or_row, dict) else {}
    if isinstance(bbox, str):
        try:
            bbox = json.loads(bbox or '{}')
        except Exception:
            bbox = {}
    try:
        image_width = max(0, int(face_or_row.get('imageWidth', 0) or 0))
    except Exception:
        image_width = 0
    try:
        image_height = max(0, int(face_or_row.get('imageHeight', 0) or 0))
    except Exception:
        image_height = 0

    def px(key: str) -> int:
        try:
            return int(round(float(bbox.get(key, 0) or 0)))
        except Exception:
            return 0

    left = max(0, px('left'))
    top = max(0, px('top'))
    width = max(0, px('width'))
    height = max(0, px('height'))
    if image_width > 0:
        left = min(left, image_width)
        width = min(width, max(0, image_width - left))
    if image_height > 0:
        top = min(top, image_height)
        height = min(height, max(0, image_height - top))
    return {
        'left': left,
        'top': top,
        'width': width,
        'height': height,
        'imageWidth': image_width,
        'imageHeight': image_height,
    }


def _face_identity_key(user_id: str, filename: str, face_or_row: Dict) -> str:
    normalized = _normalize_face_bbox(face_or_row)
    return json.dumps({
        'v': 1,
        'userId': user_id,
        'filename': filename,
        **normalized,
    }, sort_keys=True, separators=(',', ':'))


def _deterministic_face_id(user_id: str, filename: str, face_or_row: Dict) -> str:
    digest = hashlib.sha256(_face_identity_key(user_id, filename, face_or_row).encode('utf-8')).hexdigest()
    return f'face-v1-{digest[:40]}'


def _face_is_rejected(face: Dict) -> bool:
    return _coerce_bool(face.get('rejected', False)) or str(face.get('reviewStatus') or '').lower() == 'rejected'


def _face_is_confirmed(face: Dict) -> bool:
    return _coerce_bool(face.get('confirmedByUser', False)) or str(face.get('reviewStatus') or '').lower() == 'confirmed'


def _face_is_suspicious(face: Dict) -> bool:
    if _face_is_confirmed(face) or _face_is_rejected(face):
        return False
    if str(face.get('reviewStatus') or '').lower() == 'suspicious':
        return True
    try:
        return float(face.get('confidence', 0.0) or 0.0) < SUSPICIOUS_FACE_CONFIDENCE
    except Exception:
        return True


def _face_is_clusterable(face: Dict) -> bool:
    if _face_is_rejected(face):
        return False
    if _face_is_confirmed(face):
        return True
    if str(face.get('reviewStatus') or '').lower() == 'suspicious':
        return False
    confidence = face.get('confidence')
    if confidence is None or str(confidence).strip() == '':
        return True
    try:
        return float(confidence) >= SUSPICIOUS_FACE_CONFIDENCE
    except Exception:
        return False


def _face_passes_auto_store_quality(face: Dict, confidence: Optional[float] = None, normalized: Optional[Dict] = None) -> bool:
    try:
        confidence_value = float(confidence if confidence is not None else (face.get('confidence', 0.0) or 0.0))
    except Exception:
        confidence_value = 0.0
    if confidence_value < FACE_MIN_STORE_CONFIDENCE:
        return False
    bbox = normalized or _normalize_face_bbox(face)
    if bbox.get('width', 0) <= 0 or bbox.get('height', 0) <= 0:
        return False
    image_width = max(0, int(bbox.get('imageWidth', 0) or face.get('imageWidth', 0) or 0))
    image_height = max(0, int(bbox.get('imageHeight', 0) or face.get('imageHeight', 0) or 0))
    if image_width <= 0 or image_height <= 0 or confidence_value >= FACE_LOW_CONFIDENCE_REJECT_BELOW:
        return True
    image_area = max(1, image_width * image_height)
    area_ratio = (bbox.get('width', 0) * bbox.get('height', 0)) / image_area
    side_ratio = max(
        bbox.get('width', 0) / max(1, image_width),
        bbox.get('height', 0) / max(1, image_height),
    )
    return area_ratio <= FACE_LOW_CONFIDENCE_MAX_AREA_RATIO and side_ratio <= FACE_LOW_CONFIDENCE_MAX_SIDE_RATIO


def _face_payload_for_metadata(face_id: str, face: Dict) -> Dict:
    bbox = face.get('bbox', {})
    if isinstance(bbox, str):
        try:
            bbox = json.loads(bbox or '{}')
        except Exception:
            bbox = {}
    payload = {
        'faceId': face_id,
        'bbox': bbox,
        'imageWidth': int(face.get('imageWidth', 0) or 0),
        'imageHeight': int(face.get('imageHeight', 0) or 0),
        'confidence': float(face.get('confidence', 0.0) or 0.0),
    }
    if face.get('personId'):
        payload['personId'] = face.get('personId')
    if face.get('reviewStatus'):
        payload['reviewStatus'] = face.get('reviewStatus')
    if face.get('suspiciousReason'):
        payload['suspiciousReason'] = face.get('suspiciousReason')
    for key in ('qualityScore', 'detector', 'model', 'modelVersion', 'embeddingVersion', 'runtime'):
        if face.get(key) is not None:
            payload[key] = face.get(key)
    if _face_is_rejected(face):
        payload['rejected'] = True
    return payload


def _create_person_entity(
    user_id: str,
    face_ids: List[str],
    rep_embedding: List[float],
    *,
    person_id: Optional[str] = None,
    name: str = '',
) -> str:
    if person_table_client is None:
        return ''
    person_id = person_id or str(uuid.uuid4())
    entity = {
        'PartitionKey': user_id,
        'RowKey': person_id,
        'name': name or '',
        'faceIds': json.dumps(face_ids),
        'repEmbedding': json.dumps(rep_embedding),
        'createdAt': None,
    }
    try:
        person_table_client.upsert_entity(entity)
    except Exception:
        pass
    return person_id


def _face_embedding_from_entity(face: Dict) -> List[float]:
    try:
        emb = json.loads(face.get('embedding', '[]') or '[]')
        return emb if isinstance(emb, list) else []
    except Exception:
        return []


def _face_embedding_version(face: Dict) -> str:
    return str(
        face.get('embeddingVersion')
        or face.get('modelTaxonomyVersion')
        or ''
    ).strip()


def _face_embedding_allowed_for_clustering(face: Dict) -> bool:
    versions = _face_embedding_allowed_versions()
    if not versions:
        return True
    return _face_embedding_version(face) in versions


def _compute_rep_embedding(face_entities: List[Dict], np) -> List[float]:
    if not face_entities:
        return []

    embeddings = []
    weights = []
    expected_dim = 0
    for face in face_entities:
        if _face_is_rejected(face):
            continue
        if not _face_embedding_allowed_for_clustering(face):
            continue
        emb = _face_embedding_from_entity(face)
        if not emb:
            continue
        try:
            confidence = float(face.get('confidence', 0.5) or 0.5)
        except Exception:
            confidence = 0.5
        if _coerce_bool(face.get('confirmedByUser', False)):
            confidence = max(confidence, 1.0)
        elif _face_is_suspicious(face):
            confidence = min(confidence, 0.35)
        embeddings.append(emb)
        weights.append(max(0.05, confidence))

    if not embeddings:
        return []

    expected_dim = max(len(emb) for emb in embeddings)
    X = np.vstack([
        np.asarray(_align_embedding_dimension(emb, expected_dim), dtype=_embedding_precision_dtype(np))
        for emb in embeddings
    ])
    w = np.asarray(weights, dtype=_embedding_precision_dtype(np))
    mean = np.average(X, axis=0, weights=w)
    mean = mean / (np.linalg.norm(mean) + 1e-12)

    Xn = X / (np.linalg.norm(X, axis=1, keepdims=True) + 1e-12)
    similarities = Xn @ mean
    combined_weights = w * np.clip(similarities, 0.1, 1.0)
    refined = np.average(X, axis=0, weights=combined_weights)
    refined = refined / (np.linalg.norm(refined) + 1e-12)
    return refined.tolist()


def _normalized_embedding(vec: List[float], np):
    if not vec:
        return None
    arr = np.asarray(vec, dtype=_embedding_precision_dtype(np))
    norm = np.linalg.norm(arr) + 1e-12
    return arr / norm


def _normalized_embedding_for_entry(entry: Dict, np):
    cached = entry.get('_normalized_rep_embedding')
    if cached is not None:
        return cached
    normalized = _normalized_embedding(entry.get('repEmbedding') or [], np)
    entry['_normalized_rep_embedding'] = normalized
    return normalized


def _align_embedding_dimension(vec: List[float], target_dim: int) -> List[float]:
    if not vec:
        return []
    try:
        target = max(1, int(target_dim))
    except Exception:
        target = len(vec)
    if len(vec) >= target:
        return [float(item) for item in vec[:target]]
    return [float(item) for item in vec] + [0.0] * (target - len(vec))


def _shared_embedding_views(vec_a: List[float], vec_b: List[float]) -> Tuple[List[float], List[float]]:
    if not vec_a or not vec_b:
        return [], []
    if len(vec_a) == len(vec_b):
        return vec_a, vec_b
    shared_dim = min(len(vec_a), len(vec_b))
    if shared_dim <= 0:
        return [], []
    return vec_a[:shared_dim], vec_b[:shared_dim]


def _embeddings_are_comparable(vec_a: List[float], vec_b: List[float]) -> bool:
    return bool(vec_a and vec_b)


def _supported_person_match_score_from_normalized(
    rep_norm,
    person_entry: Dict,
    np,
    *,
    allow_confirmed_bonus: bool = True,
) -> Optional[float]:
    existing = _normalized_embedding_for_entry(person_entry, np)
    if rep_norm is None or existing is None:
        return None
    rep_view, existing_view = _shared_embedding_views(list(rep_norm), list(existing))
    rep_norm_view = _normalized_embedding(rep_view, np)
    existing_view_norm = _normalized_embedding(existing_view, np)
    if rep_norm_view is None or existing_view_norm is None:
        return None
    score = float(np.dot(rep_norm_view, existing_view_norm))
    if allow_confirmed_bonus:
        confirmed_count = int(person_entry.get('confirmedFaceCount') or 0)
        if confirmed_count > 0:
            score = min(score + min(0.05 * confirmed_count, 0.10), 1.0)
    return score


def _embedding_similarity(vec_a: List[float], vec_b: List[float], np) -> Optional[float]:
    if not _embeddings_are_comparable(vec_a, vec_b):
        return None
    vec_a, vec_b = _shared_embedding_views(vec_a, vec_b)
    a = _normalized_embedding(vec_a, np)
    b = _normalized_embedding(vec_b, np)
    if a is None or b is None:
        return None
    return float(np.dot(a, b))


def _embedding_similarity_between_normalized(vec_a_norm, vec_b_norm, np) -> Optional[float]:
    if vec_a_norm is None or vec_b_norm is None:
        return None
    vec_a, vec_b = _shared_embedding_views(list(vec_a_norm), list(vec_b_norm))
    if not vec_a or not vec_b:
        return None
    return float(np.dot(
        np.asarray(vec_a, dtype=_embedding_precision_dtype(np)),
        np.asarray(vec_b, dtype=_embedding_precision_dtype(np)),
    ))


def _embedding_precision_dtype(np):
    return getattr(np, 'float64', getattr(np, 'float32', float))


def _split_cluster_by_max_pair_distance(indices: List[int], dist_matrix, max_distance: float) -> List[List[int]]:
    if len(indices) <= 1:
        return [list(indices)]

    threshold = max(0.0, float(max_distance))
    remaining = list(indices)
    split_clusters: List[List[int]] = []

    while remaining:
        seed = min(
            remaining,
            key=lambda idx: (
                sum(float(dist_matrix[idx, other]) for other in remaining if other != idx),
                idx,
            ),
        )
        cluster = [seed]
        remaining.remove(seed)

        while remaining:
            candidates = []
            for idx in remaining:
                candidate_cluster = [*cluster, idx]
                max_pair_distance = max(
                    float(dist_matrix[left, right])
                    for pos, left in enumerate(candidate_cluster)
                    for right in candidate_cluster[pos + 1:]
                )
                if max_pair_distance <= threshold:
                    distances_to_cluster = [float(dist_matrix[idx, member]) for member in cluster]
                    candidates.append((max_pair_distance, sum(distances_to_cluster), idx))
            if not candidates:
                break
            _, _, next_idx = min(candidates)
            cluster.append(next_idx)
            remaining.remove(next_idx)

        split_clusters.append(sorted(cluster))

    return split_clusters


def _refine_clusters_by_max_pair_distance(
    clusters: Dict[int, List[int]],
    dist_matrix,
    max_distance: float,
) -> Dict[int, List[int]]:
    refined: Dict[int, List[int]] = {}
    next_label = 0
    for indices in clusters.values():
        for split_indices in _split_cluster_by_max_pair_distance(indices, dist_matrix, max_distance):
            refined[next_label] = split_indices
            next_label += 1
    return refined


def _face_is_owned_by_person(face: Optional[Dict], person_id: str) -> bool:
    if not face or not person_id:
        return False
    return str(face.get('personId') or '') == str(person_id)


def _update_person_rep_embedding(user_id: str, person_id: str) -> None:
    if face_table_client is None or person_table_client is None:
        return
    try:
        person = person_table_client.get_entity(partition_key=user_id, row_key=person_id)
        face_ids = json.loads(person.get('faceIds', '[]') or '[]')
    except Exception:
        return

    face_entities = []
    for face_id in face_ids:
        try:
            face = face_table_client.get_entity(partition_key=user_id, row_key=face_id)
            if _face_is_owned_by_person(face, person_id):
                face_entities.append(face)
        except Exception:
            continue

    try:
        import numpy as np
        rep = _compute_rep_embedding(face_entities, np)
    except Exception:
        rep = []
    _update_person_entity(user_id, person_id, {'repEmbedding': json.dumps(rep)})


def _confirmed_face_count(user_id: str, face_ids: List[str], person_id: str = '') -> int:
    if face_table_client is None:
        return 0
    count = 0
    for face_id in face_ids:
        try:
            face = face_table_client.get_entity(partition_key=user_id, row_key=face_id)
            if person_id and not _face_is_owned_by_person(face, person_id):
                continue
            if _face_is_rejected(face):
                continue
            if _coerce_bool(face.get('confirmedByUser', False)):
                count += 1
        except Exception:
            continue
    return count


def _load_people_embedding_index(user_id: str) -> List[Dict]:
    if person_table_client is None:
        return []
    try:
        rows = list(person_table_client.query_entities(f"PartitionKey eq '{_escape_odata(user_id)}'"))
    except Exception:
        return []

    index = []
    for row in rows:
        try:
            face_ids = json.loads(row.get('faceIds', '[]') or '[]')
        except Exception:
            face_ids = []
        person_id = str(row.get('RowKey') or '')
        active_face_ids = _active_face_ids_for_person(user_id, person_id, face_ids)
        if not active_face_ids:
            continue
        try:
            rep = json.loads(row.get('repEmbedding', '[]') or '[]')
        except Exception:
            rep = []
        if not rep:
            continue
        index.append({
            'personId': person_id,
            'name': row.get('name', ''),
            'faceIds': active_face_ids,
            'repEmbedding': rep,
            'confirmedFaceCount': _confirmed_face_count(user_id, active_face_ids, person_id),
        })
    return index


def _next_unnamed_person_name(user_id: str) -> str:
    if person_table_client is None:
        return 'Unnamed 1'
    try:
        rows = list(person_table_client.query_entities(f"PartitionKey eq '{_escape_odata(user_id)}'"))
    except Exception:
        return 'Unnamed 1'
    max_suffix = 0
    for row in rows:
        candidate = str(row.get('name') or '').strip()
        match = re.match(r'^unnamed\s*(\d+)$', candidate, re.IGNORECASE)
        if not match:
            continue
        try:
            value = int(match.group(1))
        except ValueError:
            continue
        if value > max_suffix:
            max_suffix = value
    return f'Unnamed {max_suffix + 1}'


def _make_unnamed_person_name_allocator(user_id: str):
    next_suffix = 0
    try:
        if person_table_client is not None:
            rows = list(person_table_client.query_entities(f"PartitionKey eq '{_escape_odata(user_id)}'"))
        else:
            rows = []
    except Exception:
        rows = []
    for row in rows:
        candidate = str(row.get('name') or '').strip()
        match = re.match(r'^unnamed\s*(\d+)$', candidate, re.IGNORECASE)
        if not match:
            continue
        try:
            next_suffix = max(next_suffix, int(match.group(1)))
        except ValueError:
            continue

    def _next_name() -> str:
        nonlocal next_suffix
        next_suffix += 1
        return f'Unnamed {next_suffix}'

    return _next_name


def _is_unnamed_name(name: str) -> bool:
    return bool(re.match(r'^unnamed\s*\d*$', (name or '').strip(), re.IGNORECASE))


def _update_person_entity(user_id: str, person_id: str, updates: Dict) -> bool:
    if person_table_client is None:
        return False
    try:
        entity = person_table_client.get_entity(partition_key=user_id, row_key=person_id)
    except Exception:
        return False
    entity.update(updates)
    try:
        person_table_client.upsert_entity(entity)
        return True
    except Exception:
        return False


def _load_searchable_person_name_index(user_id: str) -> Dict[str, str]:
    if person_table_client is None:
        return {}
    try:
        rows = list(person_table_client.query_entities(f"PartitionKey eq '{_escape_odata(user_id)}'"))
    except Exception:
        rows = []
    index: Dict[str, str] = {}
    for row in rows:
        person_id = str(row.get('RowKey') or '').strip()
        name = str(row.get('name') or '').strip()
        if person_id and name and not _is_unnamed_name(name):
            index[person_id] = name
    return index


def _filename_from_face(user_id: str, face_id: str) -> str:
    if face_table_client is None or not face_id:
        return ''
    try:
        face = face_table_client.get_entity(partition_key=user_id, row_key=face_id)
        return str(face.get('filename') or '')
    except Exception:
        return ''


def _filenames_for_face_ids(user_id: str, face_ids: List[str]) -> List[str]:
    filenames = []
    seen = set()
    for face_id in face_ids:
        filename = _filename_from_face(user_id, str(face_id))
        if filename and filename not in seen:
            filenames.append(filename)
            seen.add(filename)
    return filenames


def _remove_face_from_person(user_id: str, person_id: str, face_id: str) -> None:
    if person_table_client is None:
        return
    try:
        person = person_table_client.get_entity(partition_key=user_id, row_key=person_id)
    except Exception:
        return
    try:
        face_ids = json.loads(person.get('faceIds', '[]'))
    except Exception:
        face_ids = []
    if face_id not in face_ids:
        return
    face_ids = [fid for fid in face_ids if fid != face_id]
    if not face_ids:
        try:
            person_table_client.delete_entity(partition_key=user_id, row_key=person_id)
        except Exception:
            pass
        return
    person['faceIds'] = json.dumps(face_ids)
    try:
        person_table_client.upsert_entity(person)
        _update_person_rep_embedding(user_id, person_id)
    except Exception:
        pass


def _remove_face_from_other_people(user_id: str, face_id: str, keep_person_id: str) -> Dict:
    if person_table_client is None or not face_id:
        return {'removed': 0, 'deletedPeople': 0, 'touchedPeople': []}
    try:
        rows = list(person_table_client.query_entities(f"PartitionKey eq '{_escape_odata(user_id)}'"))
    except Exception:
        rows = []

    removed = 0
    deleted_people = 0
    touched_people = []
    for person in rows:
        person_id = str(person.get('RowKey') or '')
        if not person_id or person_id == keep_person_id:
            continue
        try:
            face_ids = json.loads(person.get('faceIds', '[]') or '[]')
        except Exception:
            face_ids = []
        if face_id not in face_ids:
            continue
        next_face_ids = [fid for fid in face_ids if fid != face_id]
        removed += len(face_ids) - len(next_face_ids)
        touched_people.append(person_id)
        try:
            if next_face_ids:
                person['faceIds'] = json.dumps(next_face_ids)
                person_table_client.upsert_entity(person)
                _update_person_rep_embedding(user_id, person_id)
            else:
                person_table_client.delete_entity(partition_key=user_id, row_key=person_id)
                deleted_people += 1
        except Exception:
            pass
    return {'removed': removed, 'deletedPeople': deleted_people, 'touchedPeople': touched_people}


def _add_face_to_person(user_id: str, person_id: str, face_id: str) -> None:
    if person_table_client is None or not person_id or not face_id:
        return
    if face_table_client is not None:
        try:
            face = face_table_client.get_entity(partition_key=user_id, row_key=face_id)
            if _face_is_rejected(face) or (_face_is_suspicious(face) and not _face_is_confirmed(face)):
                return
        except Exception:
            pass
    _remove_face_from_other_people(user_id, face_id, person_id)
    try:
        person = person_table_client.get_entity(partition_key=user_id, row_key=person_id)
    except Exception:
        return
    try:
        face_ids = json.loads(person.get('faceIds', '[]') or '[]')
    except Exception:
        face_ids = []
    next_face_ids = _dedupe_face_ids_preserving_order([*face_ids, face_id])
    if next_face_ids == face_ids:
        return
    person['faceIds'] = json.dumps(next_face_ids)
    try:
        person_table_client.upsert_entity(person)
        _update_person_rep_embedding(user_id, person_id)
    except Exception:
        pass


def _remove_faces_for_filename(user_id: str, filename: str) -> None:
    if face_table_client is None:
        return
    try:
        query = f"PartitionKey eq '{_escape_odata(user_id)}' and filename eq '{_escape_odata(filename)}'"
        rows = list(face_table_client.query_entities(query))
    except Exception:
        rows = []
    removed_face_ids = []
    for row in rows:
        face_id = row.get('RowKey')
        person_id = row.get('personId')
        if face_id:
            removed_face_ids.append(str(face_id))
        try:
            face_table_client.delete_entity(partition_key=user_id, row_key=face_id)
        except Exception:
            pass
        if person_id and face_id:
            _remove_face_from_person(user_id, person_id, face_id)
    if removed_face_ids and person_table_client is not None:
        try:
            people = list(person_table_client.query_entities(f"PartitionKey eq '{_escape_odata(user_id)}'"))
        except Exception:
            people = []
        removed_face_ids_set = set(removed_face_ids)
        for person in people:
            person_id = str(person.get('RowKey') or '')
            if not person_id:
                continue
            try:
                face_ids = json.loads(person.get('faceIds', '[]') or '[]')
            except Exception:
                face_ids = []
            next_face_ids = [face_id for face_id in face_ids if str(face_id) not in removed_face_ids_set]
            if next_face_ids == face_ids:
                continue
            try:
                if next_face_ids:
                    person['faceIds'] = json.dumps(next_face_ids)
                    person_table_client.upsert_entity(person)
                    _update_person_rep_embedding(user_id, person_id)
                else:
                    person_table_client.delete_entity(partition_key=user_id, row_key=person_id)
            except Exception:
                pass
    _rebuild_metadata_faces_for_filename(user_id, filename)


def _remove_job_rows_for_filename(user_id: str, filename: str) -> int:
    if metadata_table_client is None or not filename:
        return 0
    try:
        rows = list(metadata_table_client.query_entities("PartitionKey eq 'jobs'"))
    except Exception:
        rows = []

    removed = 0
    job_prefixes = (
        f'processing:{user_id}:{filename}:',
        f'processing:{user_id}:{filename}',
        f'{user_id}:{filename}:',
        f'{user_id}:{filename}',
        filename,
    )
    for row in rows:
        row_key = str(row.get('RowKey') or '')
        job_id = str(row.get('jobId') or '')
        row_filename = str(row.get('filename') or '')
        row_user_id = str(row.get('userId') or '')
        correlation_id = str(row.get('correlationId') or '')
        if not (
            (row_filename == filename and (not row_user_id or row_user_id == user_id))
            or (filename == correlation_id and (not row_user_id or row_user_id == user_id))
            or any(token and (job_id.startswith(token) or row_key.startswith(token) or correlation_id.startswith(token)) for token in job_prefixes)
        ):
            continue
        try:
            metadata_table_client.delete_entity(partition_key='jobs', row_key=row_key)
            removed += 1
        except Exception:
            pass
    return removed


def _dedupe_face_ids_preserving_order(face_ids: List[str]) -> List[str]:
    return list(dict.fromkeys([str(face_id) for face_id in face_ids if face_id]))


def _remove_filename_from_albums(user_id: str, filename: str) -> None:
    if albums_table_client is None:
        return
    try:
        rows = list(albums_table_client.query_entities(f"PartitionKey eq '{_escape_odata(user_id)}'"))
    except Exception:
        rows = []
    for row in rows:
        try:
            filenames = json.loads(row.get('filenames', '[]') or '[]')
        except Exception:
            filenames = []
        updated = [item for item in filenames if item != filename]
        if updated == filenames:
            continue
        row['filenames'] = json.dumps(updated)
        try:
            albums_table_client.upsert_entity(row)
        except Exception:
            pass


def _prepare_existing_people_match(existing_people: Optional[List[Dict]], np=None) -> Dict[str, object]:
    if not existing_people:
        return {'face_to_person': {}, 'embedding_index': []}

    face_to_person: Dict[str, Dict[str, str]] = {}
    embedding_index = []
    for person in existing_people:
        person_id = str(person.get('personId') or '')
        if not person_id:
            continue
        name = str(person.get('name') or '')
        face_ids = person.get('faceIds') or []
        for face_id in face_ids:
            if face_id:
                face_to_person[str(face_id)] = {'personId': person_id, 'name': name}
        rep = person.get('repEmbedding') or []
        confirmed_count = int(person.get('confirmedFaceCount') or 0)
        entry = {
            'personId': person_id,
            'name': name,
            'repEmbedding': rep,
            'confirmedFaceCount': confirmed_count,
        }
        if np is not None:
            entry['_normalized_rep_embedding'] = _normalized_embedding(rep, np)
        embedding_index.append(entry)

    return {'face_to_person': face_to_person, 'embedding_index': embedding_index}


def _active_face_ids_for_person(user_id: str, person_id: str, face_ids: List[str]) -> List[str]:
    if face_table_client is None or not user_id or not person_id:
        return []
    active_face_ids = []
    for face_id in face_ids:
        try:
            face = face_table_client.get_entity(partition_key=user_id, row_key=str(face_id))
        except Exception:
            continue
        if _face_is_owned_by_person(face, person_id) and not _face_is_rejected(face):
            active_face_ids.append(str(face_id))
    return active_face_ids


def _match_existing_person(
    cluster_face_ids: List[str],
    rep_embedding: List[float],
    match_index: Dict[str, object],
    np,
    *,
    threshold: float = PEOPLE_MATCH_THRESHOLD,
    margin: float = PEOPLE_MATCH_MARGIN,
    rep_norm=None,
) -> Tuple[Optional[str], str]:
    face_to_person = match_index.get('face_to_person', {})
    embedding_index = match_index.get('embedding_index', [])

    overlap_counts: Dict[str, int] = {}
    for face_id in cluster_face_ids:
        match = face_to_person.get(str(face_id))
        if not match:
            continue
        person_id = match.get('personId')
        if person_id:
            overlap_counts[person_id] = overlap_counts.get(person_id, 0) + 1

    if overlap_counts:
        ranked_candidates = sorted(overlap_counts.items(), key=lambda kv: kv[1], reverse=True)
        if rep_embedding:
            if rep_norm is None:
                rep_norm = _normalized_embedding(rep_embedding, np)
            for person_id, _count in ranked_candidates:
                for entry in embedding_index:
                    if entry.get('personId') != person_id:
                        continue
                    score = _embedding_similarity_between_normalized(rep_norm, _normalized_embedding_for_entry(entry, np), np)
                    if score is not None and score >= PEOPLE_MATCH_THRESHOLD:
                        return person_id, str(entry.get('name') or '')
                    break
            # No overlap candidate passed the similarity check; fall through to the
            # normal embedding matching path instead of forcing a stale merge.
        else:
            return None, ''

    if not rep_embedding or not embedding_index:
        return None, ''

    if rep_norm is None:
        rep_norm = _normalized_embedding(rep_embedding, np)
    best_score = None
    second_best_score = None
    best_person = None
    for entry in embedding_index:
        score = _supported_person_match_score_from_normalized(rep_norm, entry, np, allow_confirmed_bonus=False)
        if score is None:
            continue
        if best_score is None or score > best_score:
            second_best_score = best_score
            best_score = score
            best_person = entry
        elif second_best_score is None or score > second_best_score:
            second_best_score = score

    if (
        best_person
        and best_score is not None
        and best_score >= threshold
        and (second_best_score is None or (best_score - second_best_score) >= margin)
    ):
        return str(best_person.get('personId') or ''), str(best_person.get('name') or '')

    return None, ''


def _assign_faces_to_people_incrementally(user_id: str, filename: str, face_ids: List[str]) -> Dict[str, str]:
    if not face_ids or face_table_client is None or person_table_client is None:
        return {}
    try:
        import numpy as np
    except Exception:
        return {}

    session_embedding_index = [dict(entry) for entry in _load_people_embedding_index(user_id)]
    assignments: Dict[str, str] = {}
    people_to_refresh = set()
    next_unnamed_person_name = _make_unnamed_person_name_allocator(user_id)

    for face_id in face_ids:
        try:
            face_ent = face_table_client.get_entity(partition_key=user_id, row_key=face_id)
        except Exception:
            continue
        if not _face_is_clusterable(face_ent):
            continue
        if not _face_embedding_allowed_for_clustering(face_ent):
            continue
        emb = _face_embedding_from_entity(face_ent)
        if not emb:
            continue
        face_norm = _normalized_embedding(emb, np)

        best_score = 0.0
        second_best_score = 0.0
        best_person = None

        for entry in session_embedding_index:
            existing = entry.get('repEmbedding') or []
            if not _embeddings_are_comparable(emb, existing):
                continue
            score = _embedding_similarity_between_normalized(face_norm, _normalized_embedding_for_entry(entry, np), np)
            if score is None:
                continue
            if score > best_score:
                second_best_score = best_score
                best_score = score
                best_person = entry
            elif score > second_best_score:
                second_best_score = score

        person_id = ''
        if (
            best_person
            and best_score >= PEOPLE_CLUSTER_ASSIGN_THRESHOLD
            and (best_score - second_best_score) >= PEOPLE_CLUSTER_ASSIGN_MARGIN
        ):
            person_id = str(best_person.get('personId') or '')
            _add_face_to_person(user_id, person_id, face_id)
        else:
            name = next_unnamed_person_name()
            person_id = _create_person_entity(user_id, [face_id], emb, name=name)
            session_embedding_index.append({
                'personId': person_id,
                'name': name,
                'faceIds': [face_id],
                'repEmbedding': emb,
                '_normalized_rep_embedding': face_norm,
                'confirmedFaceCount': 0,
            })

        if not person_id:
            continue
        face_ent['personId'] = person_id
        try:
            face_table_client.upsert_entity(face_ent)
            people_to_refresh.add(person_id)
        except Exception:
            pass
        assignments[face_id] = person_id

    for person_id in people_to_refresh:
        _update_person_rep_embedding(user_id, person_id)

    if assignments:
        _rebuild_metadata_faces_for_filename(user_id, filename)
    return assignments


def cluster_user_faces(
    user_id: str,
    eps: Optional[float] = None,
    min_samples: int = 2,
    *,
    preserve_people: Optional[List[Dict]] = None,
) -> Dict:
    if face_table_client is None or person_table_client is None:
        return {'created': [], 'clusters': {}}
    try:
        rows = list(face_table_client.query_entities(f"PartitionKey eq '{_escape_odata(user_id)}'"))
    except Exception:
        return {'created': [], 'clusters': {}}

    embeddings = []
    face_ids = []
    filenames = []
    face_id_to_entity = {}  # Cache face data to avoid N+1 queries
    for row in rows:
        try:
            if not _face_is_clusterable(row):
                continue
            if not _face_embedding_allowed_for_clustering(row):
                continue
            emb = _face_embedding_from_entity(row)
            if not emb:
                continue
            embeddings.append(emb)
            face_id = row['RowKey']
            face_ids.append(face_id)
            filenames.append(row.get('filename'))
            face_id_to_entity[face_id] = row  # Store for later use
        except Exception:
            continue

    if not embeddings:
        return {'created': [], 'clusters': {}}

    try:
        import numpy as np
        from sklearn.cluster import DBSCAN
    except Exception:
        return {'created': [], 'clusters': {}}

    effective_eps, effective_min_samples = _resolve_people_cluster_job_params(eps, min_samples)
    target_embedding_dim = max(len(emb) for emb in embeddings)
    X = np.asarray([
        _align_embedding_dimension(emb, target_embedding_dim)
        for emb in embeddings
    ], dtype=_embedding_precision_dtype(np))
    norms = np.linalg.norm(X, axis=1, keepdims=True) + 1e-12
    Xn = X / norms
    dist_matrix = np.clip(1.0 - (Xn @ Xn.T), 0.0, 2.0)
    clustering = DBSCAN(eps=effective_eps, min_samples=effective_min_samples, metric='precomputed').fit(dist_matrix)
    labels = clustering.labels_

    clusters: Dict[int, List[int]] = {}
    next_noise_label = int(np.max(labels)) + 1
    for idx, label in enumerate(labels):
        if label == -1:
            clusters[next_noise_label] = [idx]
            next_noise_label += 1
        else:
            clusters.setdefault(int(label), []).append(idx)
    clusters = _refine_clusters_by_max_pair_distance(
        clusters,
        dist_matrix,
        min(
            effective_eps,
            PEOPLE_CLUSTER_MAX_PAIR_DISTANCE,
            PEOPLE_CLUSTER_ABSOLUTE_MAX_PAIR_DISTANCE,
        ),
    )

    match_index = _prepare_existing_people_match(preserve_people, np)
    preserved_face_ids_by_person: Dict[str, List[str]] = {}
    for person in preserve_people or []:
        person_id = str(person.get('personId') or '')
        if person_id:
            preserved_face_ids_by_person[person_id] = _dedupe_face_ids_preserving_order(person.get('faceIds') or [])
    created = []
    created_by_person_id: Dict[str, Dict[str, object]] = {}
    faces_to_update = []  # Batch updates instead of one-by-one
    metadata_updates: Dict[str, set] = {}  # filename -> person ids
    
    for label, indices in clusters.items():
        cluster_face_ids = [face_ids[i] for i in indices]
        cluster_faces = []
        
        # Use cached face data instead of calling get_entity() again (eliminates N+1 queries)
        for i in indices:
            face_id = face_ids[i]
            if face_id in face_id_to_entity:
                cluster_faces.append(face_id_to_entity[face_id])
        
        if cluster_faces:
            rep = _compute_rep_embedding(cluster_faces, np)
        else:
            cluster_embs = X[indices]
            rep = np.mean(cluster_embs, axis=0).tolist()
        rep_norm = _normalized_embedding(rep, np)
        matched_id, matched_name = _match_existing_person(
            cluster_face_ids,
            rep,
            match_index,
            np,
            threshold=PEOPLE_MATCH_THRESHOLD,
            margin=PEOPLE_MATCH_MARGIN,
            rep_norm=rep_norm,
        )
        if not matched_name:
            matched_name = _next_unnamed_person_name(user_id)

        person_id = matched_id or str(uuid.uuid4())
        existing_created = created_by_person_id.get(person_id)
        if existing_created:
            existing_face_ids = existing_created['faceIds']
            existing_rep = existing_created.get('repEmbedding') or []
            split_from_existing = False
            cross_score = _embedding_similarity(existing_rep, rep, np)
            if cross_score is not None and cross_score < PEOPLE_MATCH_THRESHOLD:
                person_id = str(uuid.uuid4())
                matched_id = None
                matched_name = _next_unnamed_person_name(user_id)
                existing_created = None
                existing_face_ids = []
                split_from_existing = True
            combined_face_ids = list(dict.fromkeys([*existing_face_ids, *cluster_face_ids]))
            combined_faces = [
                face_id_to_entity[face_id]
                for face_id in combined_face_ids
                if face_id in face_id_to_entity
            ]
            combined_rep = _compute_rep_embedding(combined_faces, np) if combined_faces else rep
            _create_person_entity(
                user_id,
                combined_face_ids,
                combined_rep,
                person_id=person_id,
                name=str((existing_created or {}).get('name') or matched_name),
            )
            if existing_created is not None:
                existing_created['faceIds'] = combined_face_ids
                existing_created['repEmbedding'] = combined_rep
            elif split_from_existing:
                created_entry = {
                    'personId': person_id,
                    'faceIds': combined_face_ids,
                    'name': matched_name,
                    'repEmbedding': combined_rep,
                }
                created.append(created_entry)
                created_by_person_id[person_id] = created_entry
        else:
            existing_face_ids = preserved_face_ids_by_person.get(person_id, []) if matched_id else []
            combined_face_ids = list(dict.fromkeys([*existing_face_ids, *cluster_face_ids]))
            combined_faces = [
                face_id_to_entity[face_id]
                for face_id in combined_face_ids
                if face_id in face_id_to_entity
            ]
            combined_rep = _compute_rep_embedding(combined_faces, np) if combined_faces else rep
            person_id = _create_person_entity(
                user_id,
                combined_face_ids,
                combined_rep,
                person_id=person_id,
                name=matched_name,
            )
            created_entry = {
                'personId': person_id,
                'faceIds': combined_face_ids,
                'name': matched_name,
                'repEmbedding': combined_rep,
            }
            created.append(created_entry)
            created_by_person_id[person_id] = created_entry

        # Queue face updates for batch operation
        for i in indices:
            face_id = face_ids[i]
            if face_id in face_id_to_entity:
                face_ent = face_id_to_entity[face_id]
                face_ent['personId'] = person_id
                faces_to_update.append(face_ent)

            # Queue metadata updates
            if filenames[i]:
                filename = filenames[i]
                if filename not in metadata_updates:
                    metadata_updates[filename] = set()
                metadata_updates[filename].add(person_id)

    # Batch update faces (more efficient than one-by-one)
    for face_ent in faces_to_update:
        try:
            face_table_client.upsert_entity(face_ent)
        except Exception:
            pass

    candidate_face_ids = set(face_ids)
    assigned_face_ids = {str(face_ent.get('RowKey') or '') for face_ent in faces_to_update if face_ent.get('RowKey')}
    if assigned_face_ids != candidate_face_ids:
        return {
            'error': 'invalid clustering result: incomplete face assignment',
            'candidateFaces': len(face_ids),
            'assignedFaces': len(assigned_face_ids),
            'missingFaceIds': sorted(candidate_face_ids - assigned_face_ids)[:50],
            'unexpectedFaceIds': sorted(assigned_face_ids - candidate_face_ids)[:50],
        }

    # Batch update metadata
    if metadata_updates:
        try:
            query = f"PartitionKey eq '{_escape_odata(user_id)}'"
            metadata_rows = list(metadata_table_client.query_entities(query))
            for metadata in metadata_rows:
                if metadata.get('RowKey') in metadata_updates:
                    people_ids = parse_json_list(metadata.get('peopleIds', '[]'))
                    for person_id in metadata_updates[metadata.get('RowKey')]:
                        if person_id not in people_ids:
                            people_ids.append(person_id)
                    metadata['peopleIds'] = json.dumps(people_ids)
                    try:
                        metadata_table_client.upsert_entity(metadata)
                    except Exception:
                        pass
        except Exception:
            pass

    return {'created': created, 'clusters': {str(k): [face_ids[i] for i in v] for k, v in clusters.items()}}


def _assign_unclustered_faces(user_id: str) -> Dict:
    if not _people_features_available():
        return {'error': 'People features not configured'}
    if clustering_queue_client is None:
        try:
            rows = list(face_table_client.query_entities(f"PartitionKey eq '{_escape_odata(user_id)}'"))
        except Exception:
            rows = []
        candidates_by_filename: Dict[str, List[str]] = {}
        for row in rows:
            face_id = str(row.get('RowKey') or '')
            filename = str(row.get('filename') or '')
            if not face_id or not filename:
                continue
            if row.get('personId'):
                continue
            if not _face_is_clusterable(row):
                continue
            if not _face_embedding_allowed_for_clustering(row):
                continue
            if not _face_embedding_from_entity(row):
                continue
            candidates_by_filename.setdefault(filename, []).append(face_id)

        try:
            people_before = len(list(person_table_client.query_entities(f"PartitionKey eq '{_escape_odata(user_id)}'")))
        except Exception:
            people_before = 0

        assignments: Dict[str, str] = {}
        for filename, face_ids in candidates_by_filename.items():
            assignments.update(_assign_faces_to_people_incrementally(user_id, filename, face_ids))

        try:
            people_after = len(list(person_table_client.query_entities(f"PartitionKey eq '{_escape_odata(user_id)}'")))
        except Exception:
            people_after = people_before

        return {
            'success': True,
            'queued': False,
            'candidateFaces': sum(len(face_ids) for face_ids in candidates_by_filename.values()),
            'assignedFaces': len(assignments),
            'createdPeople': max(0, people_after - people_before),
        }
    queued = _enqueue_clustering_job(
        user_id,
        force=True,
        job_type='people_recluster',
        allow_reassign_confirmed=False,
    )
    return {
        'success': queued.get('status') == 'queued',
        'queued': queued.get('status') == 'queued',
        'jobId': queued.get('jobId'),
        'status': queued.get('status'),
    }


def _serialize_table_row(row: Dict) -> Dict:
    return dict(row or {})


def _create_people_repair_snapshot(
    user_id: str,
    *,
    snapshot_prefix: str = 'recluster-snapshot',
    kind: str = 'recluster_snapshot',
) -> str:
    if merge_table_client is None:
        return ''
    snapshot_id = f"{snapshot_prefix}-{uuid.uuid4().hex}"
    try:
        people_rows = [_serialize_table_row(row) for row in person_table_client.query_entities(f"PartitionKey eq '{_escape_odata(user_id)}'")]
    except Exception:
        people_rows = []
    try:
        face_rows = [_serialize_table_row(row) for row in face_table_client.query_entities(f"PartitionKey eq '{_escape_odata(user_id)}'")]
    except Exception:
        face_rows = []
    try:
        metadata_rows = [
            {
                'PartitionKey': row.get('PartitionKey'),
                'RowKey': row.get('RowKey'),
                'peopleIds': row.get('peopleIds', '[]'),
            }
            for row in metadata_table_client.query_entities(f"PartitionKey eq '{_escape_odata(user_id)}'")
        ]
    except Exception:
        metadata_rows = []

    payload = json.dumps({
        'people': people_rows,
        'faces': face_rows,
        'metadata': metadata_rows,
    }, separators=(',', ':'))
    chunk_size = 24000
    chunks = [payload[i:i + chunk_size] for i in range(0, len(payload), chunk_size)] or ['']
    created_at = datetime.now(timezone.utc).isoformat()
    manifest = {
        'PartitionKey': user_id,
        'RowKey': snapshot_id,
        'kind': kind,
        'chunkCount': len(chunks),
        'createdAt': created_at,
    }
    merge_table_client.upsert_entity(manifest)
    for index, chunk in enumerate(chunks):
        merge_table_client.upsert_entity({
            'PartitionKey': user_id,
            'RowKey': f'{snapshot_id}:chunk:{index}',
            'kind': f'{kind}_chunk',
            'snapshotId': snapshot_id,
            'chunkIndex': index,
            'payload': chunk,
            'createdAt': created_at,
        })
    return snapshot_id


def _load_people_repair_snapshot(user_id: str, snapshot_id: str) -> Optional[Dict]:
    if merge_table_client is None or not snapshot_id:
        return None
    try:
        manifest = merge_table_client.get_entity(partition_key=user_id, row_key=snapshot_id)
    except Exception:
        return None
    if manifest.get('kind') != 'recluster_snapshot':
        return None
    try:
        chunk_count = int(manifest.get('chunkCount') or 0)
    except Exception:
        chunk_count = 0
    parts = []
    for index in range(chunk_count):
        try:
            chunk = merge_table_client.get_entity(partition_key=user_id, row_key=f'{snapshot_id}:chunk:{index}')
            parts.append(str(chunk.get('payload') or ''))
        except Exception:
            return None
    try:
        return json.loads(''.join(parts))
    except Exception:
        return None


def _restore_people_repair_snapshot(user_id: str, snapshot_id: str) -> Dict:
    payload = _load_people_repair_snapshot(user_id, snapshot_id)
    if payload is None:
        return {'success': False, 'error': 'snapshot not found'}

    try:
        for row in person_table_client.query_entities(f"PartitionKey eq '{_escape_odata(user_id)}'"):
            person_table_client.delete_entity(partition_key=user_id, row_key=row.get('RowKey'))
    except Exception:
        pass

    restored_people = 0
    for row in payload.get('people') or []:
        if row.get('PartitionKey') == user_id and row.get('RowKey'):
            try:
                person_table_client.upsert_entity(row)
                restored_people += 1
            except Exception:
                pass

    snapshot_faces = {
        str(row.get('RowKey')): row
        for row in (payload.get('faces') or [])
        if row.get('PartitionKey') == user_id and row.get('RowKey')
    }
    restored_faces = 0
    try:
        current_faces = list(face_table_client.query_entities(f"PartitionKey eq '{_escape_odata(user_id)}'"))
    except Exception:
        current_faces = []
    seen_face_ids = set()
    for face in current_faces:
        face_id = str(face.get('RowKey') or '')
        if face_id:
            seen_face_ids.add(face_id)
        snapshot_face = snapshot_faces.get(face_id)
        if snapshot_face:
            for key in ('personId', 'confirmedByUser', 'confidence'):
                if key in snapshot_face:
                    face[key] = snapshot_face[key]
                else:
                    face.pop(key, None)
            restored_faces += 1
        else:
            face.pop('personId', None)
            face.pop('confirmedByUser', None)
        try:
            face_table_client.upsert_entity(face)
        except Exception:
            pass
    for face_id, snapshot_face in snapshot_faces.items():
        if face_id in seen_face_ids:
            continue
        try:
            face_table_client.upsert_entity(snapshot_face)
            restored_faces += 1
        except Exception:
            pass

    metadata_people = {
        str(row.get('RowKey')): row.get('peopleIds', '[]')
        for row in (payload.get('metadata') or [])
        if row.get('PartitionKey') == user_id and row.get('RowKey')
    }
    restored_metadata = 0
    try:
        current_metadata = list(metadata_table_client.query_entities(f"PartitionKey eq '{_escape_odata(user_id)}'"))
    except Exception:
        current_metadata = []
    for metadata in current_metadata:
        row_key = str(metadata.get('RowKey') or '')
        metadata['peopleIds'] = metadata_people.get(row_key, json.dumps([]))
        try:
            metadata_table_client.upsert_entity(metadata)
            restored_metadata += 1
        except Exception:
            pass

    return {
        'success': True,
        'snapshotId': snapshot_id,
        'restoredPeople': restored_people,
        'restoredFaces': restored_faces,
        'restoredMetadata': restored_metadata,
    }


def _build_people_recluster_plan(user_id: str, *, allow_reassign_confirmed: bool = False) -> Dict:
    if face_table_client is None or person_table_client is None:
        return {'created': [], 'assignments': {}, 'people': {}}
    try:
        import numpy as np
        from sklearn.cluster import DBSCAN
    except Exception as exc:
        return {'error': f'clustering unavailable: {exc}'}

    try:
        rows = list(face_table_client.query_entities(f"PartitionKey eq '{_escape_odata(user_id)}'"))
    except Exception:
        rows = []
    try:
        existing_rows = list(person_table_client.query_entities(f"PartitionKey eq '{_escape_odata(user_id)}'"))
    except Exception:
        existing_rows = []

    existing_people = []
    existing_face_ids_by_person: Dict[str, List[str]] = {}
    for row in existing_rows:
        try:
            face_ids = json.loads(row.get('faceIds', '[]') or '[]')
        except Exception:
            face_ids = []
        person_id = str(row.get('RowKey') or '')
        active_face_ids = _active_face_ids_for_person(user_id, person_id, face_ids)
        if not active_face_ids:
            continue
        try:
            rep_embedding = json.loads(row.get('repEmbedding', '[]') or '[]')
        except Exception:
            rep_embedding = []
        existing_face_ids_by_person[person_id] = list(active_face_ids)
        existing_people.append({
            'personId': person_id,
            'name': row.get('name', ''),
            'faceIds': active_face_ids,
            'repEmbedding': rep_embedding,
            'confirmedFaceCount': _confirmed_face_count(user_id, active_face_ids, person_id),
        })

    embeddings = []
    face_ids = []
    face_entities: Dict[str, Dict] = {}
    skipped_confirmed = 0
    expected_embedding_dim = 0
    for row in rows:
        face_id = str(row.get('RowKey') or '')
        emb = _face_embedding_from_entity(row)
        if not face_id or not emb:
            continue
        if not _face_is_clusterable(row):
            continue
        if not _face_embedding_allowed_for_clustering(row):
            continue
        if expected_embedding_dim == 0:
            expected_embedding_dim = len(emb)
        elif len(emb) != expected_embedding_dim:
            continue
        if row.get('personId') and _coerce_bool(row.get('confirmedByUser', False)) and not allow_reassign_confirmed:
            skipped_confirmed += 1
            continue
        embeddings.append(emb)
        face_ids.append(face_id)
        face_entities[face_id] = row

    if not embeddings:
        return {
            'created': [],
            'assignments': {},
            'people': {},
            'candidateFaces': 0,
            'skippedConfirmedFaces': skipped_confirmed,
        }

    X = np.asarray(embeddings, dtype=_embedding_precision_dtype(np))
    Xn = X / (np.linalg.norm(X, axis=1, keepdims=True) + 1e-12)
    dist_matrix = np.clip(1.0 - (Xn @ Xn.T), 0.0, 2.0)
    labels = DBSCAN(eps=PEOPLE_CLUSTER_EPS, min_samples=2, metric='precomputed').fit(dist_matrix).labels_

    clusters: Dict[int, List[int]] = {}
    next_noise_label = int(np.max(labels)) + 1
    for idx, label in enumerate(labels):
        if label == -1:
            clusters[next_noise_label] = [idx]
            next_noise_label += 1
        else:
            clusters.setdefault(int(label), []).append(idx)
    clusters = _refine_clusters_by_max_pair_distance(
        clusters,
        dist_matrix,
        min(
            PEOPLE_CLUSTER_EPS,
            PEOPLE_CLUSTER_MAX_PAIR_DISTANCE,
            PEOPLE_CLUSTER_ABSOLUTE_MAX_PAIR_DISTANCE,
        ),
    )

    match_index = _prepare_existing_people_match(existing_people, np)
    planned_people: Dict[str, Dict] = {}
    assignments: Dict[str, str] = {}
    created = []
    next_unnamed_person_name = _make_unnamed_person_name_allocator(user_id)
    used_existing_person_ids: set = set()
    for _, indices in clusters.items():
        cluster_face_ids = [face_ids[i] for i in indices]
        cluster_faces = [face_entities[fid] for fid in cluster_face_ids if fid in face_entities]
        rep = _compute_rep_embedding(cluster_faces, np) if cluster_faces else np.mean(X[indices], axis=0).tolist()
        rep_norm = _normalized_embedding(rep, np)
        matched_id, matched_name = _match_existing_person(
            cluster_face_ids,
            rep,
            match_index,
            np,
            threshold=PEOPLE_MATCH_THRESHOLD,
            margin=PEOPLE_MATCH_MARGIN,
            rep_norm=rep_norm,
        )
        if matched_id:
            cluster_face_id_set = set(cluster_face_ids)
            existing_face_id_set = set(existing_face_ids_by_person.get(matched_id, []))
            if matched_id in used_existing_person_ids and not (cluster_face_id_set & existing_face_id_set):
                matched_id = None
                matched_name = ''
        if matched_id:
            used_existing_person_ids.add(matched_id)
        person_id = matched_id or str(uuid.uuid4())
        if not matched_name:
            matched_name = next_unnamed_person_name()

        existing_face_ids = planned_people.get(person_id, {}).get('faceIds') or existing_face_ids_by_person.get(person_id, [])
        existing_rep = planned_people.get(person_id, {}).get('repEmbedding')
        if existing_face_ids and existing_rep:
            cross_score = _embedding_similarity_between_normalized(_normalized_embedding(existing_rep, np), rep_norm, np)
            if cross_score is not None and cross_score < PEOPLE_MATCH_THRESHOLD:
                person_id = str(uuid.uuid4())
                matched_id = None
                matched_name = next_unnamed_person_name()
                existing_face_ids = []
        combined_face_ids = list(dict.fromkeys([*existing_face_ids, *cluster_face_ids]))
        combined_faces = [face_entities[fid] for fid in combined_face_ids if fid in face_entities]
        combined_rep = _compute_rep_embedding(combined_faces, np) if combined_faces else rep
        planned_people[person_id] = {
            'personId': person_id,
            'name': matched_name,
            'faceIds': combined_face_ids,
            'repEmbedding': combined_rep,
        }
        if not matched_id:
            created.append({'personId': person_id, 'faceIds': cluster_face_ids, 'name': matched_name})
        for face_id in cluster_face_ids:
            assignments[face_id] = person_id

    candidate_face_ids = set(face_ids)
    assigned_face_ids = set(assignments.keys())
    if assigned_face_ids != candidate_face_ids:
        return {
            'error': 'invalid plan: incomplete face assignment',
            'candidateFaces': len(face_ids),
            'assignedFaces': len(assignments),
            'missingFaceIds': sorted(candidate_face_ids - assigned_face_ids)[:50],
            'unexpectedFaceIds': sorted(assigned_face_ids - candidate_face_ids)[:50],
        }
    return {
        'created': created,
        'assignments': assignments,
        'people': planned_people,
        'candidateFaces': len(face_ids),
        'skippedConfirmedFaces': skipped_confirmed,
    }


def _apply_people_recluster_plan(user_id: str, plan: Dict) -> Dict:
    assignments = plan.get('assignments') or {}
    people = plan.get('people') or {}
    if not isinstance(assignments, dict) or not isinstance(people, dict):
        return {'processed': 0, 'failed': 1}

    processed = 0
    failed = 0
    touched_people = set()
    affected_files = set()
    for person_id, person_plan in people.items():
        face_ids = list(dict.fromkeys(person_plan.get('faceIds') or []))
        rep_embedding = person_plan.get('repEmbedding') or []
        name = str(person_plan.get('name') or '')
        try:
            existing = person_table_client.get_entity(partition_key=user_id, row_key=person_id)
            if not name:
                name = str(existing.get('name') or '')
            existing.update({
                'name': name,
                'faceIds': json.dumps(face_ids),
                'repEmbedding': json.dumps(rep_embedding),
            })
            person_table_client.upsert_entity(existing)
        except Exception:
            _create_person_entity(user_id, face_ids, rep_embedding, person_id=person_id, name=name)
        touched_people.add(person_id)

    for face_id, person_id in assignments.items():
        try:
            face_ent = face_table_client.get_entity(partition_key=user_id, row_key=face_id)
            if not _face_is_clusterable(face_ent):
                continue
            old_person_id = str(face_ent.get('personId') or '')
            filename = face_ent.get('filename')
            if old_person_id and old_person_id != person_id:
                _remove_face_from_person(user_id, old_person_id, face_id)
                if filename:
                    affected_files.add(filename)
            _remove_face_from_other_people(user_id, face_id, person_id)
            face_ent['personId'] = person_id
            face_table_client.upsert_entity(face_ent)
            if filename:
                affected_files.add(filename)
            touched_people.add(person_id)
            processed += 1
        except Exception:
            failed += 1

    try:
        current_people = list(person_table_client.query_entities(f"PartitionKey eq '{_escape_odata(user_id)}'"))
    except Exception:
        current_people = []
    planned_person_ids = set(people.keys())
    for person in current_people:
        person_id = str(person.get('RowKey') or '')
        if not person_id or person_id in planned_person_ids:
            continue
        try:
            face_ids = json.loads(person.get('faceIds', '[]') or '[]')
        except Exception:
            face_ids = []
        active_face_ids = []
        for face_id in face_ids:
            try:
                face_ent = face_table_client.get_entity(partition_key=user_id, row_key=str(face_id))
                if _face_is_owned_by_person(face_ent, person_id) and not _face_is_rejected(face_ent):
                    active_face_ids.append(str(face_id))
            except Exception:
                continue
        if active_face_ids:
            continue
        try:
            person_table_client.delete_entity(partition_key=user_id, row_key=person_id)
        except Exception:
            continue

    for person_id in touched_people:
        _update_person_rep_embedding(user_id, person_id)
    rebuild = _rebuild_metadata_faces_for_filenames(user_id, affected_files)
    return {'processed': processed, 'failed': failed, 'rebuiltMetadataFiles': rebuild.get('updatedFiles', 0)}


def _face_duplicate_group_key(user_id: str, row: Dict) -> str:
    filename = str(row.get('filename') or '').strip()
    return _face_identity_key(user_id, filename, row)


def _choose_canonical_face_row(rows: List[Dict]) -> Dict:
    def score(row: Dict) -> Tuple[int, int, float, int]:
        deterministic = 1 if str(row.get('RowKey') or '').startswith('face-v1-') else 0
        confirmed = 1 if _coerce_bool(row.get('confirmedByUser', False)) else 0
        assigned = 1 if row.get('personId') else 0
        try:
            confidence = float(row.get('confidence', 0.0) or 0.0)
        except Exception:
            confidence = 0.0
        return (confirmed, assigned, confidence, deterministic)

    return sorted(rows, key=score, reverse=True)[0]


def _rebuild_metadata_faces_for_filename(
    user_id: str,
    filename: str,
    *,
    searchable_person_index: Optional[Dict[str, str]] = None,
    dry_run: bool = False,
) -> Dict:
    if metadata_table_client is None or face_table_client is None:
        return {'updated': False, 'missingMetadata': True}
    try:
        metadata = metadata_table_client.get_entity(partition_key=user_id, row_key=filename)
    except Exception:
        return {'updated': False, 'missingMetadata': True}
    try:
        rows = list(face_table_client.query_entities(
            f"PartitionKey eq '{_escape_odata(user_id)}' and filename eq '{_escape_odata(filename)}'"
        ))
    except Exception:
        rows = []
    if searchable_person_index is None:
        searchable_person_index = _load_searchable_person_name_index(user_id)
    rows = sorted([row for row in rows if not _face_is_rejected(row)], key=lambda row: str(row.get('RowKey') or ''))
    faces_payload = [_face_payload_for_metadata(str(row.get('RowKey') or ''), row) for row in rows if row.get('RowKey')]
    people_ids = []
    for row in rows:
        person_id = str(row.get('personId') or '').strip()
        if person_id and person_id in searchable_person_index and person_id not in people_ids:
            people_ids.append(person_id)
    try:
        before_people_ids = [str(pid) for pid in json.loads(metadata.get('peopleIds', '[]') or '[]')]
    except Exception:
        before_people_ids = []
    try:
        before_faces = json.loads(metadata.get('faces', '[]') or '[]')
    except Exception:
        before_faces = []
    before_face_count = int(metadata.get('faceCount', 0) or 0)
    after_people_json = json.dumps(people_ids)
    changed = (
        json.dumps(before_faces, sort_keys=True, separators=(',', ':')) != json.dumps(faces_payload, sort_keys=True, separators=(',', ':'))
        or before_face_count != len(faces_payload)
        or before_people_ids != people_ids
    )
    result = {
        'updated': bool(changed and not dry_run),
        'changed': changed,
        'missingMetadata': False,
        'filename': filename,
        'faceCountBefore': before_face_count,
        'faceCountAfter': len(faces_payload),
        'peopleIdsBefore': before_people_ids,
        'peopleIdsAfter': people_ids,
        'peopleIdsAdded': len([pid for pid in people_ids if pid not in before_people_ids]),
        'peopleIdsRemoved': len([pid for pid in before_people_ids if pid not in people_ids]),
        'stalePeopleIdsRemoved': len([pid for pid in before_people_ids if pid not in people_ids]),
    }
    if dry_run:
        return result
    try:
        _update_metadata_entity_fields(user_id, filename, {
            'faces': json.dumps(faces_payload),
            'faceCount': len(faces_payload),
            'peopleIds': after_people_json,
        })
    except Exception:
        pass
    return result


def _rebuild_metadata_faces_for_filenames(
    user_id: str,
    filenames,
    *,
    searchable_person_index: Optional[Dict[str, str]] = None,
    dry_run: bool = False,
) -> Dict:
    if searchable_person_index is None:
        searchable_person_index = _load_searchable_person_name_index(user_id)
    unique_filenames = []
    seen = set()
    for filename in filenames or []:
        value = str(filename or '').strip()
        if value and value not in seen:
            unique_filenames.append(value)
            seen.add(value)
    results = [
        _rebuild_metadata_faces_for_filename(
            user_id,
            filename,
            searchable_person_index=searchable_person_index,
            dry_run=dry_run,
        )
        for filename in unique_filenames
    ]
    return {
        'affectedFiles': len(unique_filenames),
        'updatedFiles': sum(1 for result in results if result.get('updated')),
        'changedFiles': sum(1 for result in results if result.get('changed')),
        'missingMetadataFiles': sum(1 for result in results if result.get('missingMetadata')),
        'peopleIdsAdded': sum(int(result.get('peopleIdsAdded') or 0) for result in results),
        'peopleIdsRemoved': sum(int(result.get('peopleIdsRemoved') or 0) for result in results),
        'stalePeopleIdsRemoved': sum(int(result.get('stalePeopleIdsRemoved') or 0) for result in results),
        'files': results[:100],
    }


def _dedupe_duplicate_faces(user_id: str, *, dry_run: bool = True) -> Dict:
    if face_table_client is None or person_table_client is None:
        return {'success': False, 'error': 'People features not configured'}
    try:
        rows = list(face_table_client.query_entities(f"PartitionKey eq '{_escape_odata(user_id)}'"))
    except Exception:
        rows = []

    groups: Dict[str, List[Dict]] = {}
    for row in rows:
        filename = str(row.get('filename') or '').strip()
        if not filename:
            continue
        groups.setdefault(_face_duplicate_group_key(user_id, row), []).append(row)
    duplicate_groups = [group for group in groups.values() if len(group) > 1]

    impact_groups = []
    face_id_to_canonical: Dict[str, Tuple[str, str]] = {}
    affected_files = set()
    affected_people = set()
    duplicate_faces_to_delete = 0

    for group in duplicate_groups:
        canonical = _choose_canonical_face_row(group)
        filename = str(canonical.get('filename') or '').strip()
        canonical_id = _deterministic_face_id(user_id, filename, canonical)
        canonical_person_id = str(canonical.get('personId') or '').strip()
        for row in group:
            row_person_id = str(row.get('personId') or '').strip()
            if row_person_id:
                affected_people.add(row_person_id)
        if canonical_person_id:
            affected_people.add(canonical_person_id)
        if filename:
            affected_files.add(filename)
        ids = [str(row.get('RowKey') or '') for row in group if row.get('RowKey')]
        for face_id in ids:
            face_id_to_canonical[face_id] = (canonical_id, canonical_person_id)
            if face_id != canonical_id:
                duplicate_faces_to_delete += 1
        impact_groups.append({
            'filename': filename,
            'canonicalFaceId': canonical_id,
            'canonicalPersonId': canonical_person_id,
            'faceIds': ids,
            'deleteCount': len([face_id for face_id in ids if face_id != canonical_id]),
            'bbox': _normalize_face_bbox(canonical),
        })

    result = {
        'success': True,
        'dryRun': dry_run,
        'duplicateGroups': len(duplicate_groups),
        'duplicateFacesToDelete': duplicate_faces_to_delete,
        'affectedFiles': len(affected_files),
        'affectedPeople': len(affected_people),
        'groups': impact_groups[:100],
    }
    if dry_run or not duplicate_groups:
        return result

    snapshot_id = _create_people_repair_snapshot(
        user_id,
        snapshot_prefix='face-dedupe-snapshot',
        kind='face_dedupe_snapshot',
    )

    canonical_entities: Dict[str, Dict] = {}
    for group in duplicate_groups:
        canonical = _choose_canonical_face_row(group)
        filename = str(canonical.get('filename') or '').strip()
        canonical_id = _deterministic_face_id(user_id, filename, canonical)
        normalized = _normalize_face_bbox(canonical)
        max_confidence = 0.0
        confirmed = False
        canonical_person_id = str(canonical.get('personId') or '').strip()
        for row in group:
            confirmed = confirmed or _coerce_bool(row.get('confirmedByUser', False))
            try:
                max_confidence = max(max_confidence, float(row.get('confidence', 0.0) or 0.0))
            except Exception:
                pass
        entity = dict(canonical)
        entity.update({
            'PartitionKey': user_id,
            'RowKey': canonical_id,
            'filename': filename,
            'bbox': json.dumps({
                'left': normalized['left'],
                'top': normalized['top'],
                'width': normalized['width'],
                'height': normalized['height'],
            }),
            'imageWidth': normalized['imageWidth'],
            'imageHeight': normalized['imageHeight'],
            'confidence': max_confidence,
            'identityKey': _face_identity_key(user_id, filename, canonical),
            'identityVersion': 'face-v1',
        })
        if canonical_person_id:
            entity['personId'] = canonical_person_id
        else:
            entity.pop('personId', None)
        if confirmed:
            entity['confirmedByUser'] = True
        else:
            entity.pop('confirmedByUser', None)
        canonical_entities[canonical_id] = entity

    for entity in canonical_entities.values():
        try:
            face_table_client.upsert_entity(entity)
        except Exception:
            pass

    try:
        people_rows = list(person_table_client.query_entities(f"PartitionKey eq '{_escape_odata(user_id)}'"))
    except Exception:
        people_rows = []
    updated_people = 0
    for person in people_rows:
        person_id = str(person.get('RowKey') or '')
        try:
            face_ids = json.loads(person.get('faceIds', '[]') or '[]')
        except Exception:
            face_ids = []
        next_face_ids = []
        changed = False
        for face_id in face_ids:
            face_id = str(face_id)
            canonical_info = face_id_to_canonical.get(face_id)
            if not canonical_info:
                next_face_ids.append(face_id)
                continue
            canonical_id, canonical_person_id = canonical_info
            changed = True
            if canonical_person_id and person_id == canonical_person_id:
                next_face_ids.append(canonical_id)
        next_face_ids = _dedupe_face_ids_preserving_order(next_face_ids)
        if changed or next_face_ids != face_ids:
            person['faceIds'] = json.dumps(next_face_ids)
            try:
                person_table_client.upsert_entity(person)
                updated_people += 1
            except Exception:
                pass
            affected_people.add(person_id)

    deleted_faces = 0
    for group in duplicate_groups:
        canonical = _choose_canonical_face_row(group)
        filename = str(canonical.get('filename') or '').strip()
        canonical_id = _deterministic_face_id(user_id, filename, canonical)
        for row in group:
            face_id = str(row.get('RowKey') or '')
            if not face_id or face_id == canonical_id:
                continue
            try:
                face_table_client.delete_entity(partition_key=user_id, row_key=face_id)
                deleted_faces += 1
            except Exception:
                pass

    rebuild = _rebuild_metadata_faces_for_filenames(user_id, affected_files)
    for person_id in affected_people:
        _update_person_rep_embedding(user_id, person_id)

    result.update({
        'snapshotId': snapshot_id,
        'deletedFaces': deleted_faces,
        'updatedPeople': updated_people,
        'rebuiltMetadataFiles': rebuild.get('updatedFiles', 0),
    })
    return result


def _suppress_suspicious_faces(user_id: str, *, dry_run: bool = True) -> Dict:
    if face_table_client is None or person_table_client is None:
        return {'success': False, 'error': 'People features not configured'}
    try:
        rows = list(face_table_client.query_entities(f"PartitionKey eq '{_escape_odata(user_id)}'"))
    except Exception:
        rows = []

    candidates = []
    affected_files = set()
    affected_people = set()
    singleton_clusters_to_delete = set()
    for row in rows:
        if _face_is_rejected(row) or _face_is_confirmed(row):
            continue
        try:
            confidence = float(row.get('confidence', 0.0) or 0.0)
        except Exception:
            confidence = 0.0
        if confidence >= SUSPICIOUS_FACE_CONFIDENCE:
            continue
        face_id = str(row.get('RowKey') or '')
        filename = str(row.get('filename') or '')
        person_id = str(row.get('personId') or '')
        delete_singleton = False
        if person_id:
            try:
                person = person_table_client.get_entity(partition_key=user_id, row_key=person_id)
                face_ids = json.loads(person.get('faceIds', '[]') or '[]')
                has_confirmed = _confirmed_face_count(user_id, face_ids, person_id) > 0
                delete_singleton = len(face_ids) == 1 and face_ids[0] == face_id and _is_unnamed_name(str(person.get('name') or '')) and not has_confirmed
            except Exception:
                delete_singleton = False
        if filename:
            affected_files.add(filename)
        if person_id:
            affected_people.add(person_id)
        if delete_singleton and person_id:
            singleton_clusters_to_delete.add(person_id)
        normalized = _normalize_face_bbox(row)
        reject_as_false_face = not _face_passes_auto_store_quality(row, confidence, normalized)
        candidates.append({
            'faceId': face_id,
            'filename': filename,
            'personId': person_id,
            'confidence': confidence,
            'deleteSingletonCluster': delete_singleton,
            'rejectAsFalseFace': reject_as_false_face,
        })

    false_positive_candidates = [item for item in candidates if item.get('rejectAsFalseFace')]
    result = {
        'success': True,
        'dryRun': dry_run,
        'threshold': SUSPICIOUS_FACE_CONFIDENCE,
        'autoRejectThreshold': FACE_MIN_STORE_CONFIDENCE,
        'candidateFaces': len(candidates),
        'falsePositiveCandidates': len(false_positive_candidates),
        'affectedFiles': len(affected_files),
        'affectedPeople': len(affected_people),
        'singletonClustersToDelete': len(singleton_clusters_to_delete),
        'faces': candidates[:100],
    }
    if dry_run or not candidates:
        return result

    snapshot_id = _create_people_repair_snapshot(
        user_id,
        snapshot_prefix='suspicious-face-snapshot',
        kind='suspicious_face_snapshot',
    )

    marked = 0
    unassigned = 0
    rejected_false_faces = 0
    deleted_people = 0
    for item in candidates:
        face_id = item['faceId']
        person_id = item.get('personId') or ''
        try:
            face = face_table_client.get_entity(partition_key=user_id, row_key=face_id)
        except Exception:
            continue
        reject_as_false_face = bool(item.get('rejectAsFalseFace'))
        if reject_as_false_face:
            face['reviewStatus'] = 'rejected'
            face['rejected'] = True
            face['rejectedReason'] = 'low_confidence_false_positive'
            face['rejectedAt'] = datetime.now(timezone.utc).isoformat()
            face.pop('suspiciousReason', None)
            face.pop('confirmedByUser', None)
            rejected_false_faces += 1
        else:
            face['reviewStatus'] = 'suspicious'
            face['suspiciousReason'] = 'low_confidence'
            face['rejected'] = False
        face.pop('personId', None)
        if person_id:
            try:
                person = person_table_client.get_entity(partition_key=user_id, row_key=person_id)
                face_ids = json.loads(person.get('faceIds', '[]') or '[]')
                next_face_ids = [fid for fid in face_ids if fid != face_id]
                if item.get('deleteSingletonCluster'):
                    person_table_client.delete_entity(partition_key=user_id, row_key=person_id)
                    deleted_people += 1
                    unassigned += 1
                elif reject_as_false_face and len(next_face_ids) != len(face_ids):
                    person['faceIds'] = json.dumps(next_face_ids)
                    person_table_client.upsert_entity(person)
                    unassigned += 1
            except Exception:
                pass
        face_table_client.upsert_entity(face)
        marked += 1

    rebuild = _rebuild_metadata_faces_for_filenames(user_id, affected_files)
    for person_id in affected_people:
        if person_id not in singleton_clusters_to_delete:
            _update_person_rep_embedding(user_id, person_id)

    result.update({
        'snapshotId': snapshot_id,
        'markedSuspicious': marked,
        'unassignedFaces': unassigned,
        'rejectedFalseFaces': rejected_false_faces,
        'deletedPeople': deleted_people,
        'rebuiltMetadataFiles': rebuild.get('updatedFiles', 0),
    })
    return result


def _rebuild_photo_people_index(user_id: str, *, dry_run: bool = True) -> Dict:
    if metadata_table_client is None or face_table_client is None or person_table_client is None:
        return {'success': False, 'error': 'People features not configured'}
    try:
        rows = list(face_table_client.query_entities(f"PartitionKey eq '{_escape_odata(user_id)}'"))
    except Exception:
        rows = []

    filenames = []
    seen = set()
    scanned_faces = 0
    skipped_rejected = 0
    for row in rows:
        if _face_is_rejected(row):
            skipped_rejected += 1
            continue
        scanned_faces += 1
        filename = str(row.get('filename') or '').strip()
        if filename and filename not in seen:
            filenames.append(filename)
            seen.add(filename)

    rebuild = _rebuild_metadata_faces_for_filenames(
        user_id,
        filenames,
        searchable_person_index=_load_searchable_person_name_index(user_id),
        dry_run=dry_run,
    )
    return {
        'success': True,
        'dryRun': dry_run,
        'scannedFaces': scanned_faces,
        'skippedRejectedFaces': skipped_rejected,
        'affectedFiles': rebuild.get('affectedFiles', 0),
        'changedFiles': rebuild.get('changedFiles', 0),
        'updatedFiles': rebuild.get('updatedFiles', 0),
        'missingMetadataFiles': rebuild.get('missingMetadataFiles', 0),
        'peopleIdsAdded': rebuild.get('peopleIdsAdded', 0),
        'peopleIdsRemoved': rebuild.get('peopleIdsRemoved', 0),
        'stalePeopleIdsRemoved': rebuild.get('stalePeopleIdsRemoved', 0),
        'files': rebuild.get('files', []),
    }


def _repair_face_memberships(user_id: str, *, dry_run: bool = True) -> Dict:
    if face_table_client is None or person_table_client is None:
        return {'success': False, 'error': 'People features not configured'}
    try:
        people_rows = list(person_table_client.query_entities(f"PartitionKey eq '{_escape_odata(user_id)}'"))
    except Exception:
        people_rows = []
    try:
        face_rows = list(face_table_client.query_entities(f"PartitionKey eq '{_escape_odata(user_id)}'"))
    except Exception:
        face_rows = []

    people_by_id = {str(row.get('RowKey') or ''): dict(row) for row in people_rows if row.get('RowKey')}
    faces_by_id = {str(row.get('RowKey') or ''): dict(row) for row in face_rows if row.get('RowKey')}
    planned_face_ids: Dict[str, List[str]] = {}
    changed_people = set()
    deleted_people = set()
    affected_files = set()
    removed_missing_faces = 0
    removed_rejected_faces = 0
    removed_stale_references = 0
    removed_duplicate_references = 0
    added_missing_owner_references = 0
    orphaned_face_owners_cleared = 0

    for person_id, person in people_by_id.items():
        try:
            face_ids = json.loads(person.get('faceIds', '[]') or '[]')
        except Exception:
            face_ids = []
        next_face_ids = []
        seen = set()
        for raw_face_id in face_ids:
            face_id = str(raw_face_id or '')
            if not face_id:
                continue
            if face_id in seen:
                removed_duplicate_references += 1
                changed_people.add(person_id)
                continue
            seen.add(face_id)
            face = faces_by_id.get(face_id)
            if not face:
                removed_missing_faces += 1
                changed_people.add(person_id)
                continue
            filename = str(face.get('filename') or '')
            if filename:
                affected_files.add(filename)
            if _face_is_rejected(face):
                removed_rejected_faces += 1
                changed_people.add(person_id)
                continue
            if not _face_is_owned_by_person(face, person_id):
                removed_stale_references += 1
                changed_people.add(person_id)
                continue
            next_face_ids.append(face_id)
        planned_face_ids[person_id] = next_face_ids

    faces_to_clear_owner = []
    for face_id, face in faces_by_id.items():
        if _face_is_rejected(face):
            continue
        owner_id = str(face.get('personId') or '')
        if not owner_id:
            continue
        filename = str(face.get('filename') or '')
        if filename:
            affected_files.add(filename)
        if owner_id not in people_by_id:
            faces_to_clear_owner.append(face_id)
            orphaned_face_owners_cleared += 1
            continue
        owner_face_ids = planned_face_ids.setdefault(owner_id, [])
        if face_id not in owner_face_ids:
            owner_face_ids.append(face_id)
            changed_people.add(owner_id)
            added_missing_owner_references += 1

    for person_id, face_ids in planned_face_ids.items():
        if not face_ids and person_id in people_by_id:
            deleted_people.add(person_id)

    result = {
        'success': True,
        'dryRun': dry_run,
        'scannedPeople': len(people_rows),
        'scannedFaces': len(face_rows),
        'changedPeople': len(changed_people),
        'deletedEmptyPeople': len(deleted_people),
        'removedStaleReferences': removed_stale_references,
        'removedMissingFaces': removed_missing_faces,
        'removedRejectedFaces': removed_rejected_faces,
        'removedDuplicateReferences': removed_duplicate_references,
        'addedMissingOwnerReferences': added_missing_owner_references,
        'orphanedFaceOwnersCleared': orphaned_face_owners_cleared,
        'affectedFiles': len(affected_files),
    }
    has_changes = any([
        changed_people,
        deleted_people,
        faces_to_clear_owner,
        removed_stale_references,
        removed_missing_faces,
        removed_rejected_faces,
        removed_duplicate_references,
        added_missing_owner_references,
    ])
    if dry_run or not has_changes:
        return result

    snapshot_id = _create_people_repair_snapshot(
        user_id,
        snapshot_prefix='face-membership-snapshot',
        kind='face_membership_snapshot',
    )

    updated_people = 0
    for person_id, face_ids in planned_face_ids.items():
        if person_id not in people_by_id:
            continue
        try:
            if not face_ids:
                person_table_client.delete_entity(partition_key=user_id, row_key=person_id)
                updated_people += 1
                continue
            person = people_by_id[person_id]
            person['faceIds'] = json.dumps(_dedupe_face_ids_preserving_order(face_ids))
            person_table_client.upsert_entity(person)
            _update_person_rep_embedding(user_id, person_id)
            updated_people += 1
        except Exception:
            pass

    cleared_owners = 0
    for face_id in faces_to_clear_owner:
        try:
            face = faces_by_id[face_id]
            face.pop('personId', None)
            face.pop('confirmedByUser', None)
            face_table_client.upsert_entity(face)
            cleared_owners += 1
        except Exception:
            pass

    rebuild = _rebuild_metadata_faces_for_filenames(user_id, affected_files)
    result.update({
        'snapshotId': snapshot_id,
        'updatedPeople': updated_people,
        'clearedOrphanedFaceOwners': cleared_owners,
        'rebuiltMetadataFiles': rebuild.get('updatedFiles', 0),
    })
    return result


def _cleanup_stale_people_state(user_id: str) -> Dict:
    """Remove stale person rows and orphaned face memberships after clustering work."""
    return _repair_face_memberships(user_id, dry_run=False)


def _people_features_available() -> bool:
    return face_table_client is not None and person_table_client is not None and merge_table_client is not None


def _pick_merge_target(candidate_a: Dict, candidate_b: Dict) -> Dict:
    name_a = str(candidate_a.get('name') or '').strip()
    name_b = str(candidate_b.get('name') or '').strip()
    if bool(name_a) != bool(name_b):
        return candidate_a if name_a else candidate_b
    count_a = int(candidate_a.get('faceCount') or 0)
    count_b = int(candidate_b.get('faceCount') or 0)
    if count_a != count_b:
        return candidate_a if count_a > count_b else candidate_b
    return candidate_a if str(candidate_a.get('personId')) <= str(candidate_b.get('personId')) else candidate_b


FACE_SUMMARY_COLUMNS = [
    'RowKey',
    'filename',
    'bbox',
    'imageWidth',
    'imageHeight',
    'confidence',
    'reviewStatus',
    'suspiciousReason',
    'personId',
    'rejected',
    'confirmedByUser',
]


def _load_user_face_summary_by_id(user_id: str) -> Dict[str, Dict]:
    if face_table_client is None:
        return {}
    query = f"PartitionKey eq '{_escape_odata(user_id)}'"
    try:
        rows = list(face_table_client.query_entities(query, select=FACE_SUMMARY_COLUMNS))
    except TypeError:
        try:
            rows = list(face_table_client.query_entities(query))
        except Exception:
            return {}
    except Exception:
        return {}
    return {str(row.get('RowKey') or ''): row for row in rows if row.get('RowKey')}


def _face_summary_for_person_list(face_id: str, face: Dict) -> Dict:
    bbox_value = face.get('bbox', {})
    if isinstance(bbox_value, str):
        try:
            bbox_value = json.loads(bbox_value or '{}')
        except Exception:
            bbox_value = {}
    if not isinstance(bbox_value, dict):
        bbox_value = {}
    return {
        'faceId': face_id,
        'filename': face.get('filename'),
        'bbox': bbox_value,
        'imageWidth': int(face.get('imageWidth', 0) or 0),
        'imageHeight': int(face.get('imageHeight', 0) or 0),
        'confidence': float(face.get('confidence', 0.0) or 0.0),
        'reviewStatus': face.get('reviewStatus') or '',
        'suspiciousReason': face.get('suspiciousReason') or '',
    }


def _face_preview_priority(face: Dict) -> Tuple[int, float, int]:
    try:
        confidence = float(face.get('confidence', 0.0) or 0.0)
    except Exception:
        confidence = 0.0
    confirmed = 1 if _coerce_bool(face.get('confirmedByUser', False)) or str(face.get('reviewStatus') or '').lower() == 'confirmed' else 0
    rejected = 1 if _face_is_rejected(face) else 0
    return (confirmed, confidence, -rejected)


def _compute_people_suggestions(
    user_id: str,
    *,
    threshold: float = PEOPLE_SUGGEST_THRESHOLD,
    limit: int = PEOPLE_SUGGEST_LIMIT,
    per_person: int = PEOPLE_SUGGEST_PER_PERSON,
) -> List[Dict]:
    if person_table_client is None:
        return []
    try:
        rows = list(person_table_client.query_entities(f"PartitionKey eq '{_escape_odata(user_id)}'"))
    except Exception:
        return []

    face_by_id = _load_user_face_summary_by_id(user_id)
    people = []
    for row in rows:
        try:
            rep = json.loads(row.get('repEmbedding', '[]') or '[]')
        except Exception:
            rep = []
        if not rep:
            continue
        try:
            face_ids = json.loads(row.get('faceIds', '[]') or '[]')
        except Exception:
            face_ids = []
        active_face_ids = []
        for face_id in face_ids:
            try:
                face = face_by_id.get(str(face_id))
                if face is None and face_table_client is not None:
                    face = face_table_client.get_entity(partition_key=user_id, row_key=face_id)
                if (
                    face
                    and _face_is_owned_by_person(face, str(row.get('RowKey') or ''))
                    and not _face_is_rejected(face)
                ):
                    active_face_ids.append(face_id)
            except Exception:
                continue
        if not active_face_ids:
            continue
        people.append({
            'personId': row.get('RowKey'),
            'name': row.get('name', ''),
            'faceCount': len(active_face_ids),
            'repEmbedding': rep,
        })

    if len(people) < 2:
        return []

    try:
        import numpy as np
    except Exception:
        return []

    X = np.asarray([p['repEmbedding'] for p in people], dtype=_embedding_precision_dtype(np))
    if X.ndim != 2 or X.shape[0] < 2:
        return []
    norms = np.linalg.norm(X, axis=1, keepdims=True) + 1e-12
    Xn = X / norms
    sim = Xn @ Xn.T
    np.fill_diagonal(sim, -1.0)

    suggestions = []
    used_pairs = set()
    per_counts = {p['personId']: 0 for p in people}

    for i, person in enumerate(people):
        if per_counts.get(person['personId'], 0) >= per_person:
            continue
        ranked = np.argsort(-sim[i])
        for j in ranked:
            score = float(sim[i, j])
            if score < threshold:
                break
            other = people[int(j)]
            pair_key = "::".join(sorted([str(person['personId']), str(other['personId'])]))
            if pair_key in used_pairs:
                continue
            target = _pick_merge_target(person, other)
            source = other if target is person else person
            if per_counts.get(source['personId'], 0) >= per_person:
                continue
            used_pairs.add(pair_key)
            per_counts[source['personId']] = per_counts.get(source['personId'], 0) + 1
            per_counts[target['personId']] = per_counts.get(target['personId'], 0) + 1
            suggestions.append({
                'sourcePersonId': source.get('personId'),
                'sourceName': source.get('name', ''),
                'sourceFaceCount': source.get('faceCount', 0),
                'targetPersonId': target.get('personId'),
                'targetName': target.get('name', ''),
                'targetFaceCount': target.get('faceCount', 0),
                'similarity': score,
            })
            if len(suggestions) >= limit:
                break
        if len(suggestions) >= limit:
            break

    suggestions.sort(key=lambda s: s.get('similarity', 0.0), reverse=True)
    return suggestions


def _albums_feature_available() -> bool:
    return albums_table_client is not None and person_table_client is not None


def _albums_table_available() -> bool:
    return albums_table_client is not None


def _load_album_entity(user_id: str, album_id: str) -> Optional[Dict]:
    if albums_table_client is None:
        return None
    try:
        return albums_table_client.get_entity(partition_key=user_id, row_key=album_id)
    except Exception:
        return None


def _album_filenames(entity: Dict) -> List[str]:
    try:
        return json.loads(entity.get('filenames', '[]') or '[]')
    except Exception:
        return []


def _save_album_entity(entity: Dict) -> None:
    if albums_table_client is None:
        return
    albums_table_client.upsert_entity(entity)


SMART_ALBUM_RULES = {
    'location': 'location',
    'by_location': 'location',
    'recent-upload': 'recent-upload',
    'recent_upload': 'recent-upload',
    'upload': 'recent-upload',
    'person': 'person',
    'by_person': 'person',
    'event-window': 'event-window',
    'event_time_window': 'event-window',
    'event': 'event-window',
    'time': 'event-window',
    'tag-object': 'tag-object',
    'tag_or_object': 'tag-object',
    'tag': 'tag-object',
    'object': 'tag-object',
}


def _smart_album_title(value: str) -> str:
    cleaned = re.sub(r'\s+', ' ', str(value or '').replace('_', ' ')).strip()
    return cleaned.title() if cleaned.islower() else cleaned


def _smart_album_group_push(groups: Dict[str, Dict], key: str, name: str, filename: str, date_value: datetime) -> None:
    if not key or not filename:
        return
    group = groups.setdefault(key, {
        'name': name,
        'filenames': [],
        'latest': datetime.min.replace(tzinfo=timezone.utc),
    })
    if filename not in group['filenames']:
        group['filenames'].append(filename)
    if date_value > group['latest']:
        group['latest'] = date_value


def _smart_album_person_names(user_id: str) -> Dict[str, str]:
    if person_table_client is None:
        return {}
    try:
        rows = list(person_table_client.query_entities(f"PartitionKey eq '{_escape_odata(user_id)}'"))
    except Exception:
        rows = []
    names = {}
    for row in rows:
        person_id = str(row.get('RowKey') or '').strip()
        if not person_id:
            continue
        name = str(row.get('name') or '').strip()
        names[person_id] = name or f'Person {person_id[:8]}'
    return names


def _smart_album_candidates(user_id: str, rule: str, metadata_rows: List[Dict]) -> List[Dict]:
    groups: Dict[str, Dict] = {}
    person_names = _smart_album_person_names(user_id) if rule == 'person' else {}

    for row in metadata_rows:
        filename = row.get('RowKey')
        if not filename:
            continue
        upload_dt = _metadata_upload_date(row)
        capture_dt = _metadata_capture_date(row)

        if rule == 'location':
            city = str(row.get('locationCity') or '').strip()
            country = str(row.get('locationCountry') or '').strip()
            address = str(row.get('address') or '').strip()
            latitude = str(row.get('latitude') or '').strip()
            longitude = str(row.get('longitude') or '').strip()
            label = ', '.join(part for part in (city, country) if part) or address
            if not label and latitude and longitude:
                label = f'{latitude[:8]}, {longitude[:8]}'
            key = _normalize_search_phrase(label)
            if key:
                _smart_album_group_push(groups, f'location:{key}', f'Location: {_smart_album_title(label)}', filename, capture_dt)
        elif rule == 'recent-upload':
            if upload_dt == datetime.min.replace(tzinfo=timezone.utc):
                continue
            label = upload_dt.strftime('%b %-d, %Y') if os.name != 'nt' else upload_dt.strftime('%b %#d, %Y')
            key = upload_dt.strftime('%Y-%m-%d')
            _smart_album_group_push(groups, f'upload:{key}', f'Uploaded: {label}', filename, upload_dt)
        elif rule == 'person':
            try:
                people_ids = json.loads(row.get('peopleIds', '[]') or '[]')
            except Exception:
                people_ids = []
            for person_id in dict.fromkeys(str(pid).strip() for pid in people_ids if str(pid).strip()):
                label = person_names.get(person_id) or f'Person {person_id[:8]}'
                _smart_album_group_push(groups, f'person:{person_id}', f'Person: {_smart_album_title(label)}', filename, capture_dt)
        elif rule == 'event-window':
            if capture_dt == datetime.min.replace(tzinfo=timezone.utc):
                continue
            label = capture_dt.strftime('%b %-d, %Y') if os.name != 'nt' else capture_dt.strftime('%b %#d, %Y')
            key = capture_dt.strftime('%Y-%m-%d')
            _smart_album_group_push(groups, f'event:{key}', f'Event: {label}', filename, capture_dt)
        elif rule == 'tag-object':
            terms = parse_tags(row.get('tags', '[]')) + parse_json_list(row.get('objects', '[]'))
            for term in dict.fromkeys(terms):
                key = _normalize_search_phrase(term)
                if key:
                    _smart_album_group_push(groups, f'term:{key}', f'Tag/Object: {_smart_album_title(term)}', filename, capture_dt)

    candidates = list(groups.values())
    if rule in {'recent-upload', 'event-window'}:
        candidates.sort(key=lambda item: (item['latest'], len(item['filenames']), item['name']), reverse=True)
    else:
        candidates.sort(key=lambda item: (len(item['filenames']), item['latest'], item['name']), reverse=True)
    return candidates


def _find_public_album_by_token(token: str) -> Optional[Dict]:
    if not albums_table_client or not token:
        return None
    safe = _escape_odata(token)
    try:
        rows = list(albums_table_client.query_entities(f"publicToken eq '{safe}'"))
    except Exception:
        rows = []
    if not rows:
        return None
    return rows[0]


def _public_photo_urls(token: str, filename: str) -> Dict[str, str]:
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    preview_required = ext in RAW_EXTENSIONS_RAWPY or ext in RAW_EXTENSIONS_CINEMA or ext in {'heic', 'heif'}
    preview_url = f'/public/photos/{token}/preview/{filename}' if preview_required else ''
    try:
        image_url, _ = _create_scoped_blob_url(BLOB_IMAGE_CONTAINER, filename, minutes=10)
    except Exception:
        image_url = f'/public/photos/{token}/image/{filename}'
    try:
        thumbnail_url, _ = _create_scoped_blob_url(BLOB_THUMBNAIL_CONTAINER, filename, minutes=10)
    except Exception:
        thumbnail_url = f'/public/photos/{token}/thumbnail/{filename}'
    return {
        'url': image_url,
        'thumbnailUrl': thumbnail_url,
        'previewUrl': preview_url,
    }


def _load_photos_for_filenames(user_id: str, filenames: List[str]) -> List[Dict]:
    photos = []
    for name in filenames:
        metadata = _get_metadata_entity(user_id, name)
        if metadata is None:
            continue
        photos.append(_build_photo_summary(user_id, name, metadata, include_props=False))
    return photos


@app.after_request
def add_cors_headers(response):
    origin = request.headers.get('Origin')
    if origin:
        origin = origin.rstrip('/')
        if _origin_is_allowed(origin):
            response.headers['Access-Control-Allow-Origin'] = origin
            response.headers['Vary'] = 'Origin'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, X-Upload-Id, X-Filename, Content-Range'
    _apply_security_headers(response)
    return response


def _apply_security_headers(response):
    """Baseline hardening headers applied to every response."""
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'DENY')
    response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    response.headers.setdefault('Cross-Origin-Resource-Policy', 'same-site')
    # Only advertise HSTS over genuinely secure (HTTPS) requests so local http
    # development is unaffected.
    if request.is_secure:
        response.headers.setdefault(
            'Strict-Transport-Security', 'max-age=31536000; includeSubDomains'
        )
    return response


@app.before_request
def handle_preflight():
    # Ensure CORS preflight requests get a successful response before route handling.
    if request.method == 'OPTIONS':
        origin = request.headers.get('Origin')
        resp = Response('', status=204)
        if origin:
            origin = origin.rstrip('/')
            if _origin_is_allowed(origin):
                resp.headers['Access-Control-Allow-Origin'] = origin
                resp.headers['Vary'] = 'Origin'
        resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
        resp.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, X-Upload-Id, X-Filename, Content-Range'
        return resp


@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({
        'status': 'healthy',
        'service': 'photo-store-api',
        'storage_account': account_name,
        'uses_managed_identity': credential is not None,
    })


# ---------------------------------------------------------------------------
# Single-owner password authentication endpoints (AUTH_MODE=password).
# ---------------------------------------------------------------------------
def _password_mode_guard():
    if AUTH_MODE != 'password':
        return jsonify({'error': 'Password authentication is not enabled on this deployment.'}), 400
    return None


@app.route('/auth/config', methods=['GET'])
@app.route('/api/auth/config', methods=['GET'])
def auth_config():
    """Public: what the sign-in UI needs to render (no secrets)."""
    return jsonify({
        'authMode': AUTH_MODE,
        'authRequired': AUTH_REQUIRED,
        'passwordResetAvailable': AUTH_MODE == 'password' and email_utils.is_configured(),
    })


@app.route('/auth/login', methods=['POST'])
@app.route('/api/auth/login', methods=['POST'])
def auth_login():
    guard = _password_mode_guard()
    if guard:
        return guard
    data = request.get_json(silent=True) or {}
    password = str(data.get('password', '') or '')
    if not password:
        return jsonify({'error': 'Password is required.'}), 400
    if not password_auth.verify_owner_password(config_table_client, password):
        return jsonify({'error': 'Incorrect password.'}), 401
    email = password_auth.owner_email(config_table_client)
    token = password_auth.issue_session_token(SESSION_SECRET, email, SESSION_TTL_SECONDS)
    return jsonify({'token': token, 'email': email, 'expiresIn': SESSION_TTL_SECONDS})


@app.route('/auth/change-password', methods=['POST'])
@app.route('/api/auth/change-password', methods=['POST'])
def auth_change_password():
    guard = _password_mode_guard()
    if guard:
        return guard
    _, error = _require_user_id(require_auth=True)
    if error:
        return error
    data = request.get_json(silent=True) or {}
    current = str(data.get('currentPassword', '') or '')
    new_password = str(data.get('newPassword', '') or '')
    if len(new_password) < 8:
        return jsonify({'error': 'New password must be at least 8 characters.'}), 400
    if not password_auth.verify_owner_password(config_table_client, current):
        return jsonify({'error': 'Current password is incorrect.'}), 401
    password_auth.set_owner_password(config_table_client, new_password)
    return jsonify({'status': 'ok'})


@app.route('/auth/forgot', methods=['POST'])
@app.route('/api/auth/forgot', methods=['POST'])
def auth_forgot():
    guard = _password_mode_guard()
    if guard:
        return guard
    # Always return success to avoid revealing whether email is configured or
    # which address is on file (this is a single-owner app, but keep the habit).
    generic = jsonify({'status': 'ok'})
    if not email_utils.is_configured():
        return generic
    email = password_auth.owner_email(config_table_client)
    if not email:
        return generic
    try:
        raw_token = password_auth.create_reset_token(config_table_client, ttl_seconds=3600)
        base = (PUBLIC_APP_BASE_URL or '').rstrip('/')
        reset_url = f'{base}/reset-password?token={raw_token}'
        email_utils.send_password_reset_email(email, reset_url)
    except Exception as exc:
        app.logger.warning('Password reset email failed: %s', exc)
    return generic


@app.route('/auth/reset', methods=['POST'])
@app.route('/api/auth/reset', methods=['POST'])
def auth_reset():
    guard = _password_mode_guard()
    if guard:
        return guard
    data = request.get_json(silent=True) or {}
    token = str(data.get('token', '') or '')
    new_password = str(data.get('newPassword', '') or '')
    if len(new_password) < 8:
        return jsonify({'error': 'New password must be at least 8 characters.'}), 400
    if not password_auth.reset_password_with_token(config_table_client, token, new_password):
        return jsonify({'error': 'This reset link is invalid or has expired. Please request a new one.'}), 400
    return jsonify({'status': 'ok'})


@app.route('/api/photos/thumbnail/<path:filename>', methods=['GET'])
def proxy_thumbnail(filename: str):
    """Serve a thumbnail blob or a placeholder when the blob is missing."""
    user_id, error = _require_user_id()
    if error:
        return error
    safe_name = secure_filename(filename)
    if not safe_name or not allowed_file(safe_name) or safe_name != filename:
        return jsonify({'error': 'Invalid filename'}), 400

    if not _get_metadata_entity(user_id, safe_name):
        return jsonify({'error': 'Not found'}), 404

    try:
        props = get_media_properties('thumbnail', safe_name)
        content_type = props.get('content_type') or 'image/jpeg'
        data = download_media_bytes('thumbnail', safe_name)
        resp = Response(data, mimetype=content_type)
        resp.headers['Cache-Control'] = 'private, max-age=3600'
        return resp
    except Exception as e:
        if '404' in str(e) or 'ResourceNotFound' in str(e) or 'does not exist' in str(e).lower():
            if _filename_requires_backend_preview(safe_name):
                return proxy_preview(safe_name)
            resp = Response(placeholder_bytes, mimetype='image/jpeg')
            resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
            return resp
        print(f"Unexpected error serving thumbnail for {safe_name}: {str(e)}", flush=True)
        return jsonify({'error': 'Failed to access thumbnail'}), 503


def _preview_proxy_url(filename: str) -> str:
    return f'/api/photos/preview/{filename}'


def _filename_requires_backend_preview(filename: str) -> bool:
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    return ext in RAW_EXTENSIONS_RAWPY or ext in RAW_EXTENSIONS_CINEMA or ext in {'heic', 'heif'}


def _is_missing_media_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return '404' in text or 'resourcenotfound' in text or 'does not exist' in text or 'not found' in text


def _looks_like_jpeg(data: bytes) -> bool:
    return bool(data) and data.startswith(b'\xff\xd8')


PHOTO_ACCESS_KINDS = {'thumbnail', 'image', 'preview'}


def _is_supported_photo_access_kind(kind: str) -> bool:
    return kind in PHOTO_ACCESS_KINDS


def _photo_access_container(kind: str) -> Optional[str]:
    if kind == 'image':
        return BLOB_IMAGE_CONTAINER
    if kind == 'thumbnail':
        return BLOB_THUMBNAIL_CONTAINER
    return None


def _thumbnail_access_fallback(safe_name: str, *, batch: bool = False) -> Optional[Dict]:
    try:
        get_media_properties('thumbnail', safe_name)
        return None
    except Exception as exc:
        if _filename_requires_backend_preview(safe_name) and _is_missing_media_error(exc):
            return {
                'url': _preview_proxy_url(safe_name),
                'expiresAt': '',
                'filename': safe_name,
                'kind': 'preview',
            }
        if batch:
            app.logger.warning('Thumbnail access batch inspection failed for %s; falling back to backend proxy: %s', safe_name, exc)
        else:
            app.logger.warning('Thumbnail access inspection failed for %s; falling back to backend proxy: %s', safe_name, exc)
        return {
            'url': make_proxy_url(safe_name, 'thumbnail'),
            'expiresAt': '',
            'filename': safe_name,
            'kind': 'thumbnail',
        }


def _thumbnail_access_response(safe_name: str, *, batch: bool = False) -> Optional[Dict]:
    fallback = _thumbnail_access_fallback(safe_name, batch=batch)
    if fallback is not None:
        return fallback
    return None


def _access_url_response(url: str, expires_at: str, filename: str, kind: str) -> Dict:
    return {
        'url': url,
        'expiresAt': expires_at,
        'filename': filename,
        'kind': kind,
    }


@app.route('/api/photos/access/<kind>/<path:filename>', methods=['GET'])
def photo_access_url(kind: str, filename: str):
    user_id, error = _require_user_id()
    if error:
        return error
    safe_name = secure_filename(filename)
    if not safe_name or not allowed_file(safe_name) or safe_name != filename:
        return jsonify({'error': 'Invalid filename'}), 400
    if not _get_metadata_entity(user_id, safe_name):
        return jsonify({'error': 'Not found'}), 404
    if not _is_supported_photo_access_kind(kind):
        return jsonify({'error': 'Invalid media kind'}), 400
    if not blob_service_client or not account_name:
        return jsonify({'error': 'Media access is not configured'}), 503
    if kind == 'preview':
        return jsonify(_access_url_response(_preview_proxy_url(safe_name), '', safe_name, kind))
    if kind == 'thumbnail':
        fallback = _thumbnail_access_response(safe_name)
        if fallback is not None:
            return jsonify(fallback)
    container = _photo_access_container(kind)
    if container is None:
        return jsonify({'error': 'Invalid media kind'}), 400
    try:
        url, expires_at = _create_scoped_blob_url(container, safe_name)
        return jsonify(_access_url_response(url, expires_at, safe_name, kind))
    except Exception as exc:
        app.logger.exception('Failed to mint %s access URL for %s', kind, safe_name)
        return jsonify({'error': f'Failed to create {kind} access URL', 'detail': str(exc)}), 503


@app.route('/api/photos/access-batch', methods=['POST'])
def photo_access_url_batch():
    user_id, error = _require_user_id()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    kind = str(data.get('kind') or 'thumbnail').strip().lower()
    filenames = data.get('filenames') or []
    if not _is_supported_photo_access_kind(kind):
        return jsonify({'error': 'Invalid media kind'}), 400
    if not isinstance(filenames, list) or not filenames:
        return jsonify({'error': 'filenames must be a non-empty list'}), 400
    if len(filenames) > 2000:
        return jsonify({'error': 'Too many filenames'}), 400
    if not blob_service_client or not account_name:
        return jsonify({'error': 'Media access is not configured'}), 503

    urls: Dict[str, str] = {}
    expires_at = ''
    for raw_name in filenames:
        safe_name = secure_filename(str(raw_name or ''))
        if not safe_name or not allowed_file(safe_name):
            continue
        if not _get_metadata_entity(user_id, safe_name):
            continue
        if kind == 'preview':
            urls[safe_name] = _preview_proxy_url(safe_name)
            continue
        container = _photo_access_container(kind)
        if container is None:
            continue
        if kind == 'thumbnail':
            fallback = _thumbnail_access_response(safe_name, batch=True)
            if fallback is not None:
                urls[safe_name] = fallback['url']
                continue
        try:
            url, expires_at = _create_scoped_blob_url(container, safe_name, minutes=15)
            urls[safe_name] = url
        except Exception:
            continue

    return jsonify({
        'kind': kind,
        'expiresAt': expires_at,
        'urls': urls,
    })
 
# Helper to return backend proxy URLs instead of SAS URLs when using managed identity
def make_proxy_url(filename: str, kind: str = 'thumbnail') -> str:
    """Return a backend proxy URL instead of a SAS URL."""
    return f'/api/photos/{kind}/{filename}'


@app.route('/api/photos/preview/<path:filename>', methods=['GET'])
def proxy_preview(filename: str):
    """Serve a browser-displayable preview for files that cannot be shown directly."""
    user_id, error = _require_user_id()
    if error:
        return error
    safe_name = secure_filename(filename)
    if not safe_name or not allowed_file(safe_name) or safe_name != filename:
        return jsonify({'error': 'Invalid filename'}), 400

    if not _get_metadata_entity(user_id, safe_name):
        return jsonify({'error': 'Not found'}), 404

    try:
        image_bytes = download_media_bytes('image', safe_name)
        preview_bytes = convert_image_to_jpeg(image_bytes, safe_name)
        if not preview_bytes:
            return jsonify({'error': 'Preview not available'}), 404
        if _filename_requires_backend_preview(safe_name) and not _looks_like_jpeg(preview_bytes):
            return jsonify({'error': 'Preview not available'}), 404
        resp = Response(preview_bytes, mimetype='image/jpeg')
        resp.headers['Cache-Control'] = 'private, max-age=3600'
        return resp
    except Exception as exc:
        if _is_missing_media_error(exc):
            return jsonify({'error': 'File not found in storage'}), 404
        app.logger.exception('Failed to create preview for %s', safe_name)
        return jsonify({'error': 'Failed to create preview', 'detail': str(exc)}), 503


@app.route('/api/photos/image/<path:filename>', methods=['GET'])
def proxy_image(filename: str):
    """Serve full image bytes from storage via backend proxy."""
    user_id, error = _require_user_id()
    if error:
        return error
    safe_name = secure_filename(filename)
    if not safe_name or not allowed_file(safe_name) or safe_name != filename:
        return jsonify({'error': 'Invalid filename'}), 400

    if not _get_metadata_entity(user_id, safe_name):
        return jsonify({'error': 'Not found'}), 404

    if not blob_service_client:
        return jsonify({'error': 'Image service not configured'}), 503

    try:
        try:
            props = get_media_properties('image', safe_name)
            content_type = props.get('content_type') or 'image/jpeg'
        except Exception as e:
            # File doesn't exist or can't be accessed
            if '404' in str(e) or 'ResourceNotFound' in str(e) or 'does not exist' in str(e).lower():
                return jsonify({'error': 'File not found in storage'}), 404
            return jsonify({'error': 'Failed to access image metadata'}), 503

        data = download_media_bytes('image', safe_name)
        resp = Response(data, mimetype=content_type)
        resp.headers['Cache-Control'] = 'private, max-age=3600'
        return resp
    except Exception as e:
        # Check if it's a file not found error
        if '404' in str(e) or 'ResourceNotFound' in str(e) or 'does not exist' in str(e).lower():
            return jsonify({'error': 'File not found in storage'}), 404
        # Other errors
        print(f"Unexpected error serving image for {safe_name}: {str(e)}", flush=True)
        return jsonify({'error': 'Failed to retrieve image'}), 503


@app.route('/api/photos/cover/<path:filename>', methods=['GET'])
def proxy_cover(filename: str):
    """Serve a face cover crop from the 'cover' container.

    Cover blobs are named '<sha256(user_id)[:16]>/<face_id>.jpg' (see face_crop),
    so the filename here is a two-segment blob path, not a photo filename. We
    validate the user-hash prefix against the caller so covers can't be read
    across accounts, then stream the bytes.
    """
    user_id, error = _require_user_id()
    if error:
        return error

    parts = filename.split('/')
    if len(parts) != 2:
        return jsonify({'error': 'Invalid cover path'}), 400
    user_hash, leaf = parts
    expected_hash = hashlib.sha256(user_id.encode('utf-8')).hexdigest()[:16]
    if user_hash != expected_hash:
        return jsonify({'error': 'Not found'}), 404
    safe_leaf = secure_filename(leaf)
    if not safe_leaf or safe_leaf != leaf:
        return jsonify({'error': 'Invalid cover path'}), 400

    cover_blob = f'{user_hash}/{safe_leaf}'
    try:
        props = get_media_properties('cover', cover_blob)
        content_type = props.get('content_type') or 'image/jpeg'
        data = download_media_bytes('cover', cover_blob)
        resp = Response(data, mimetype=content_type)
        resp.headers['Cache-Control'] = 'private, max-age=3600'
        return resp
    except Exception as e:
        if '404' in str(e) or 'ResourceNotFound' in str(e) or 'does not exist' in str(e).lower():
            return jsonify({'error': 'File not found in storage'}), 404
        print(f"Unexpected error serving cover for {cover_blob}: {str(e)}", flush=True)
        return jsonify({'error': 'Failed to retrieve cover'}), 503


@app.route('/api/persons/cluster', methods=['POST'])
def trigger_clustering():
    try:
        user_id, error = _require_user_id()
        if error:
            return error
        if not _people_features_available():
            return jsonify({'error': 'People features not configured'}), 503
        data = request.get_json(silent=True) or {}
        eps, min_samples = _resolve_people_cluster_job_params(data.get('eps', PEOPLE_CLUSTER_EPS), data.get('minSamples', 2))
        queued = _enqueue_clustering_job(
            user_id,
            job_type='people_cluster',
            payload={'eps': eps, 'minSamples': min_samples},
        )
        response = _clustering_queue_response(queued, eps=eps, minSamples=min_samples)
        if queued.get('status') == 'unavailable':
            return jsonify(response), 503
        if queued.get('status') == 'failed':
            return jsonify(response), 500
        return jsonify(response)
    except Exception as exc:
        app.logger.exception('People clustering endpoint failed')
        return jsonify({'error': 'People clustering failed', 'detail': str(exc)}), 500


@app.route('/api/persons', methods=['GET'])
def list_persons():
    try:
        user_id, error = _require_user_id()
        if error:
            return error
        if not _people_features_available():
            return jsonify({'error': 'People features not configured'}), 503
        q = (request.args.get('q') or '').strip().lower()
        try:
            rows = list(person_table_client.query_entities(f"PartitionKey eq '{_escape_odata(user_id)}'"))
        except Exception:
            rows = []

        face_by_id = _load_user_face_summary_by_id(user_id)
        persons = []
        unnamed_counter = 1
        rows = sorted(rows, key=lambda r: str(r.get('RowKey', '')))
        for row in rows:
            try:
                name = str(row.get('name', '') or '').strip()
                if not name:
                    name = f'Unnamed {unnamed_counter}'
                    unnamed_counter += 1
                if q and q not in str(name).lower():
                    continue
                face_ids = json.loads(row.get('faceIds', '[]'))
                active_face_ids = []
                rep_face = None
                rep_face_score = None
                for rep_face_id in face_ids:
                    try:
                        face = face_by_id.get(str(rep_face_id))
                        if face is None:
                            face = face_table_client.get_entity(partition_key=user_id, row_key=rep_face_id)
                        if _face_is_rejected(face) or not _face_is_owned_by_person(face, row['RowKey']):
                            continue
                        active_face_ids.append(rep_face_id)
                        score = _face_preview_priority(face)
                        if rep_face is None or rep_face_score is None or score > rep_face_score:
                            rep_face = _face_summary_for_person_list(rep_face_id, face)
                            rep_face_score = score
                    except Exception:
                        continue
                persons.append({
                    'personId': row['RowKey'],
                    'name': name,
                    'faceIds': active_face_ids,
                    'faceCount': len(active_face_ids),
                    'representativeFace': rep_face,
                })
            except Exception:
                continue
        return jsonify({'persons': persons})
    except Exception as exc:
        app.logger.exception('List persons endpoint failed')
        return jsonify({'error': 'List persons failed', 'detail': str(exc)}), 500


@app.route('/api/persons/<person_id>', methods=['GET'])
def get_person(person_id: str):
    user_id, error = _require_user_id()
    if error:
        return error
    if not _people_features_available():
        return jsonify({'error': 'People features not configured'}), 503
    try:
        person = person_table_client.get_entity(partition_key=user_id, row_key=person_id)
    except Exception:
        return jsonify({'error': 'Not found'}), 404

    name = str(person.get('name', '') or '').strip()
    if not name:
        name = _next_unnamed_person_name(user_id)
        person['name'] = name
        try:
            person_table_client.upsert_entity(person)
        except Exception:
            pass

    try:
        face_ids = json.loads(person.get('faceIds', '[]'))
    except Exception:
        face_ids = []

    faces = []
    for fid in face_ids:
        try:
            face = face_table_client.get_entity(partition_key=user_id, row_key=fid)
            if _face_is_rejected(face) or not _face_is_owned_by_person(face, person_id):
                continue
            faces.append({
                'faceId': fid,
                'filename': face.get('filename'),
                'bbox': json.loads(face.get('bbox', '{}')),
                'imageWidth': int(face.get('imageWidth', 0) or 0),
                'imageHeight': int(face.get('imageHeight', 0) or 0),
                'confidence': float(face.get('confidence', 0.0) or 0.0),
                'reviewStatus': face.get('reviewStatus') or '',
                'suspiciousReason': face.get('suspiciousReason') or '',
            })
        except Exception:
            continue
    faces.sort(key=lambda face: _face_preview_priority(face), reverse=True)

    return jsonify({
        'personId': person_id,
        'name': name,
        'faces': faces,
    })


@app.route('/api/persons/suggestions', methods=['GET'])
def list_person_suggestions():
    try:
        user_id, error = _require_user_id()
        if error:
            return error
        if not _people_features_available():
            return jsonify({'error': 'People features not configured'}), 503
        try:
            threshold = float(request.args.get('threshold', PEOPLE_SUGGEST_THRESHOLD))
        except ValueError:
            threshold = PEOPLE_SUGGEST_THRESHOLD
        try:
            limit = int(request.args.get('limit', PEOPLE_SUGGEST_LIMIT))
        except ValueError:
            limit = PEOPLE_SUGGEST_LIMIT
        try:
            per_person = int(request.args.get('perPerson', PEOPLE_SUGGEST_PER_PERSON))
        except ValueError:
            per_person = PEOPLE_SUGGEST_PER_PERSON

        suggestions = _compute_people_suggestions(
            user_id,
            threshold=threshold,
            limit=limit,
            per_person=per_person,
        )
        return jsonify({'suggestions': suggestions})
    except Exception as exc:
        app.logger.exception('List person suggestions endpoint failed')
        return jsonify({'error': 'List person suggestions failed', 'detail': str(exc)}), 500


@app.route('/api/persons/<person_id>/label', methods=['POST'])
def label_person(person_id: str):
    user_id, error = _require_user_id()
    if error:
        return error
    if not _people_features_available():
        return jsonify({'error': 'People features not configured'}), 503
    data = request.get_json(silent=True) or {}
    name = data.get('name', '')
    if not isinstance(name, str):
        return jsonify({'error': 'Invalid name'}), 400
    ok = _update_person_entity(user_id, person_id, {'name': name})
    if not ok:
        return jsonify({'error': 'Not found'}), 404
    try:
        person = person_table_client.get_entity(partition_key=user_id, row_key=person_id)
        face_ids = json.loads(person.get('faceIds', '[]') or '[]')
    except Exception:
        face_ids = []
    affected_files = set()
    for face_id in face_ids:
        try:
            face = face_table_client.get_entity(partition_key=user_id, row_key=face_id)
            filename = str(face.get('filename') or '')
            if filename:
                affected_files.add(filename)
            face['confirmedByUser'] = True
            face['reviewStatus'] = 'confirmed'
            face['rejected'] = False
            face.pop('suspiciousReason', None)
            face.pop('rejectedReason', None)
            face.pop('rejectedAt', None)
            face['confidence'] = max(float(face.get('confidence', 0.0) or 0.0), 1.0)
            face_table_client.upsert_entity(face)
        except Exception:
            continue
    _update_person_rep_embedding(user_id, person_id)
    _rebuild_metadata_faces_for_filenames(user_id, affected_files)
    return jsonify({'success': True, 'personId': person_id, 'name': name})


@app.route('/api/faces/crop/<face_id>', methods=['GET'])
def face_crop(face_id: str):
    """Return a cached cover crop generated from the original image when possible."""
    user_id, error = _require_user_id()
    if error:
        return error
    if face_table_client is None:
        return jsonify({'error': 'Face data not available'}), 503
    try:
        entity = face_table_client.get_entity(partition_key=user_id, row_key=face_id)
    except Exception:
        return jsonify({'error': 'Not found'}), 404

    filename = entity.get('filename', '')
    bbox_raw = entity.get('bbox', '{}')
    img_w = int(entity.get('imageWidth', 0) or 0)
    img_h = int(entity.get('imageHeight', 0) or 0)

    if not filename or img_w <= 0 or img_h <= 0:
        return jsonify({'error': 'Incomplete face data'}), 422

    try:
        bbox = json.loads(bbox_raw) if isinstance(bbox_raw, str) else bbox_raw
    except Exception:
        return jsonify({'error': 'Invalid face bbox'}), 422

    x = int(bbox.get('left', bbox.get('x', 0)) or 0)
    y = int(bbox.get('top', bbox.get('y', 0)) or 0)
    w = int(bbox.get('width', 0))
    h = int(bbox.get('height', 0))
    if w <= 0 or h <= 0:
        return jsonify({'error': 'Invalid bbox dimensions'}), 422

    cover_blob = f"{hashlib.sha256(user_id.encode('utf-8')).hexdigest()[:16]}/{secure_filename(face_id)}.jpg"
    try:
        props = get_media_properties('cover', cover_blob)
        if props:
            return jsonify({'url': make_proxy_url(cover_blob, 'cover')})
    except Exception:
        pass

    try:
        image_bytes = download_media_bytes('image', filename)
    except Exception:
        try:
            thumb_bytes = download_media_bytes('thumbnail', filename)
            with Image.open(io.BytesIO(thumb_bytes)) as img:
                tw, th = img.size
                sx = tw / img_w
                sy = th / img_h
                pad = max(1, int(min(w, h) * 0.15))
                left = max(0, int(x * sx) - pad)
                top = max(0, int(y * sy) - pad)
                right = min(tw, int((x + w) * sx) + pad)
                bottom = min(th, int((y + h) * sy) + pad)
                cropped = img.crop((left, top, right, bottom))
                buf = io.BytesIO()
                cropped.convert('RGB').save(buf, format='JPEG', quality=85)
                buf.seek(0)
                data_url = 'data:image/jpeg;base64,' + base64.b64encode(buf.read()).decode('ascii')
            return jsonify({'url': data_url})
        except Exception:
            return jsonify({'error': 'Image not available'}), 404

    with Image.open(io.BytesIO(image_bytes)) as img:
        img = ImageOps.exif_transpose(img)
        try:
            metadata = _get_metadata_entity(user_id, filename) or {}
            rotation = _normalize_rotation(metadata.get('rotation', 0))
        except Exception:
            rotation = 0
        if rotation:
            img = img.rotate(-rotation, expand=True)
        tw, th = img.size
        sx = tw / img_w
        sy = th / img_h
        pad = max(1, int(min(w, h) * 0.35))
        left = max(0, int(x * sx) - pad)
        top = max(0, int(y * sy) - pad)
        right = min(tw, int((x + w) * sx) + pad)
        bottom = min(th, int((y + h) * sy) + pad)
        cropped = img.crop((left, top, right, bottom))
        cropped.thumbnail((512, 512), Image.Resampling.LANCZOS if hasattr(Image, 'Resampling') else Image.LANCZOS)
        buf = io.BytesIO()
        cropped.convert('RGB').save(buf, format='JPEG', quality=88, optimize=True)
        buf.seek(0)
        cover_bytes = buf.read()
        try:
            upload_media_file('cover', cover_blob, cover_bytes, 'image/jpeg')
            return jsonify({'url': make_proxy_url(cover_blob, 'cover')})
        except Exception:
            data_url = 'data:image/jpeg;base64,' + base64.b64encode(cover_bytes).decode('ascii')

    return jsonify({'url': data_url})


@app.route('/api/persons/<person_id>/confirm-face', methods=['POST'])
def confirm_face(person_id: str):
    user_id, error = _require_user_id()
    if error:
        return error
    if not _people_features_available():
        return jsonify({'error': 'People features not configured'}), 503
    data = request.get_json(silent=True) or {}
    face_id = data.get('faceId')
    if not face_id:
        return jsonify({'error': 'faceId required'}), 400

    try:
        person_table_client.get_entity(partition_key=user_id, row_key=person_id)
    except Exception:
        return jsonify({'error': 'person not found'}), 404
    try:
        face = face_table_client.get_entity(partition_key=user_id, row_key=face_id)
    except Exception:
        return jsonify({'error': 'face not found'}), 404

    old_person_id = face.get('personId')
    if old_person_id and old_person_id != person_id:
        _remove_face_from_person(user_id, str(old_person_id), face_id)
    _remove_face_from_other_people(user_id, face_id, person_id)
    _add_face_to_person(user_id, person_id, face_id)
    face['personId'] = person_id
    face['confirmedByUser'] = True
    face['reviewStatus'] = 'confirmed'
    face['rejected'] = False
    face.pop('suspiciousReason', None)
    face.pop('rejectedReason', None)
    face.pop('rejectedAt', None)
    face['confidence'] = max(float(face.get('confidence', 0.0) or 0.0), 1.0)
    face_table_client.upsert_entity(face)
    filename = face.get('filename')
    if filename:
        _rebuild_metadata_faces_for_filename(user_id, filename)
    _update_person_rep_embedding(user_id, person_id)
    return jsonify({'success': True, 'personId': person_id, 'faceId': face_id})


def _delete_person_cluster(user_id: str, person_id: str, *, rebuild_metadata: bool = True) -> Dict:
    if person_table_client is None:
        return {'deleted': False, 'facesUpdated': 0, 'filenames': []}
    try:
        person = person_table_client.get_entity(partition_key=user_id, row_key=person_id)
    except Exception:
        return {'deleted': False, 'facesUpdated': 0, 'filenames': []}
    try:
        face_ids = json.loads(person.get('faceIds', '[]') or '[]')
    except Exception:
        face_ids = []

    filenames = set()
    faces_updated = 0
    for face_id in face_ids:
        try:
            face = face_table_client.get_entity(partition_key=user_id, row_key=face_id)
            filename = face.get('filename')
            if filename:
                filenames.add(filename)
            if face.get('personId') == person_id:
                face.pop('personId', None)
            face.pop('confirmedByUser', None)
            face_table_client.upsert_entity(face)
            faces_updated += 1
        except Exception:
            continue
    try:
        person_table_client.delete_entity(partition_key=user_id, row_key=person_id)
    except Exception:
        pass
    if rebuild_metadata:
        _rebuild_metadata_faces_for_filenames(user_id, filenames)
    return {'deleted': True, 'facesUpdated': faces_updated, 'filenames': sorted(filenames)}


@app.route('/api/persons/<person_id>/delete', methods=['POST', 'DELETE'])
def delete_person_cluster(person_id: str):
    user_id, error = _require_user_id()
    if error:
        return error
    if not _people_features_available():
        return jsonify({'error': 'People features not configured'}), 503
    result = _delete_person_cluster(user_id, person_id)
    if not result.get('deleted'):
        return jsonify({'error': 'person not found'}), 404
    return jsonify({'success': True, 'personId': person_id, **result})


@app.route('/api/persons/delete', methods=['POST'])
def delete_person_clusters():
    user_id, error = _require_user_id()
    if error:
        return error
    if not _people_features_available():
        return jsonify({'error': 'People features not configured'}), 503
    data = request.get_json(silent=True) or {}
    person_ids = data.get('personIds', [])
    if not isinstance(person_ids, list):
        return jsonify({'error': 'personIds must be a list'}), 400

    deleted_person_ids = []
    errors = []
    affected_filenames = set()
    faces_updated = 0
    for raw_person_id in person_ids:
        person_id_value = str(raw_person_id or '').strip()
        if not person_id_value:
            continue
        result = _delete_person_cluster(user_id, person_id_value, rebuild_metadata=False)
        if result.get('deleted'):
            deleted_person_ids.append(person_id_value)
            faces_updated += int(result.get('facesUpdated') or 0)
            affected_filenames.update(result.get('filenames') or [])
        else:
            errors.append({'personId': person_id_value, 'error': 'person not found'})

    metadata_rebuild = _rebuild_metadata_faces_for_filenames(user_id, affected_filenames)
    return jsonify({
        'success': len(errors) == 0,
        'deletedPersonIds': deleted_person_ids,
        'errors': errors,
        'facesUpdated': faces_updated,
        'metadataRebuild': metadata_rebuild,
    })


@app.route('/api/persons/<person_id>/merge', methods=['POST'])
def merge_persons(person_id: str):
    user_id, error = _require_user_id()
    if error:
        return error
    if not _people_features_available():
        return jsonify({'error': 'People features not configured'}), 503
    data = request.get_json(silent=True) or {}
    merge_ids = data.get('mergeIds', [])
    if not isinstance(merge_ids, list):
        return jsonify({'error': 'mergeIds must be a list'}), 400

    try:
        base = person_table_client.get_entity(partition_key=user_id, row_key=person_id)
    except Exception:
        return jsonify({'error': 'base person not found'}), 404

    base_snapshot = dict(base)
    merged_snapshots = []
    face_map = {}
    try:
        base_face_ids = set(json.loads(base.get('faceIds', '[]')))
    except Exception:
        base_face_ids = set()

    for mid in merge_ids:
        try:
            merged = person_table_client.get_entity(partition_key=user_id, row_key=mid)
            merged_snapshots.append(dict(merged))
            try:
                merged_face_ids = json.loads(merged.get('faceIds', '[]'))
            except Exception:
                merged_face_ids = []
            for fid in merged_face_ids:
                try:
                    face_ent = face_table_client.get_entity(partition_key=user_id, row_key=fid)
                    if _face_is_rejected(face_ent):
                        continue
                    current_owner = str(face_ent.get('personId') or '')
                    face_map[fid] = mid
                    base_face_ids.add(fid)
                    if current_owner and current_owner != person_id:
                        _remove_face_from_person(user_id, current_owner, fid)
                    face_ent['personId'] = person_id
                    face_ent['confirmedByUser'] = True
                    face_ent['reviewStatus'] = 'confirmed'
                    face_ent['rejected'] = False
                    face_ent.pop('suspiciousReason', None)
                    face_ent.pop('rejectedReason', None)
                    face_ent.pop('rejectedAt', None)
                    face_ent['confidence'] = max(float(face_ent.get('confidence', 0.0) or 0.0), 1.0)
                    face_table_client.upsert_entity(face_ent)
                except Exception:
                    pass
            try:
                person_table_client.delete_entity(partition_key=user_id, row_key=mid)
            except Exception:
                pass
        except Exception:
            continue

    base_name = str(base.get('name') or '').strip()
    if _is_unnamed_name(base_name):
        best_name = ''
        best_count = -1
        for merged in merged_snapshots:
            merged_name = str(merged.get('name') or '').strip()
            if not merged_name or _is_unnamed_name(merged_name):
                continue
            try:
                merged_faces = json.loads(merged.get('faceIds', '[]'))
            except Exception:
                merged_faces = []
            merged_count = len(merged_faces)
            if merged_count > best_count:
                best_count = merged_count
                best_name = merged_name
        if best_name:
            _update_person_entity(user_id, person_id, {'name': best_name})

    _update_person_entity(user_id, person_id, {
        'faceIds': json.dumps(list(base_face_ids)),
    })
    _update_person_rep_embedding(user_id, person_id)
    _rebuild_metadata_faces_for_filenames(user_id, _filenames_for_face_ids(user_id, list(base_face_ids)))

    merge_id = str(uuid.uuid4())
    payload = {
        'base': base_snapshot,
        'merged': merged_snapshots,
        'faceMap': face_map,
    }
    target_name = base_snapshot.get('name') if isinstance(base_snapshot, dict) else None
    merged_names = []
    for snap in merged_snapshots:
        if isinstance(snap, dict) and snap.get('name'):
            merged_names.append(snap['name'])
    merge_entry = {
        'PartitionKey': user_id,
        'RowKey': merge_id,
        'targetPersonId': person_id,
        'mergedIds': json.dumps(merge_ids),
        'targetName': target_name or '',
        'mergedNames': json.dumps(merged_names),
        'payload': json.dumps(payload),
        'createdAt': None,
    }
    try:
        merge_table_client.upsert_entity(merge_entry)
    except Exception:
        pass

    return jsonify({'success': True, 'personId': person_id, 'mergeId': merge_id})


@app.route('/api/persons/merge/<merge_id>/undo', methods=['POST'])
def undo_merge(merge_id: str):
    user_id, error = _require_user_id()
    if error:
        return error
    if not _people_features_available():
        return jsonify({'error': 'People features not configured'}), 503
    try:
        merge_entry = merge_table_client.get_entity(partition_key=user_id, row_key=merge_id)
    except Exception:
        return jsonify({'error': 'merge not found'}), 404

    try:
        payload = json.loads(merge_entry.get('payload', '{}'))
    except Exception:
        return jsonify({'error': 'invalid merge payload'}), 500

    base = payload.get('base') or {}
    merged = payload.get('merged') or []
    face_map = payload.get('faceMap') or {}
    affected_face_ids = set(str(fid) for fid in face_map.keys())
    try:
        affected_face_ids.update(str(fid) for fid in json.loads(base.get('faceIds', '[]') or '[]'))
    except Exception:
        pass
    for item in merged:
        try:
            affected_face_ids.update(str(fid) for fid in json.loads(item.get('faceIds', '[]') or '[]'))
        except Exception:
            pass

    if base and 'PartitionKey' in base and 'RowKey' in base:
        try:
            person_table_client.upsert_entity(base)
        except Exception:
            pass

    for m in merged:
        if 'PartitionKey' in m and 'RowKey' in m:
            try:
                person_table_client.upsert_entity(m)
            except Exception:
                pass

    for fid, original_pid in face_map.items():
        try:
            face_ent = face_table_client.get_entity(partition_key=user_id, row_key=fid)
            if original_pid:
                face_ent['personId'] = original_pid
            else:
                face_ent.pop('personId', None)
            face_ent.pop('confirmedByUser', None)
            try:
                current_confidence = float(face_ent.get('confidence', 0.0) or 0.0)
            except Exception:
                current_confidence = 0.0
            face_ent['confidence'] = min(current_confidence if current_confidence > 0 else 0.8, 0.95)
            face_table_client.upsert_entity(face_ent)
        except Exception:
            pass

    affected_person_ids = set()
    if base.get('RowKey'):
        affected_person_ids.add(str(base['RowKey']))
    for m in merged:
        if m.get('RowKey'):
            affected_person_ids.add(str(m['RowKey']))
    for person_id in affected_person_ids:
        _update_person_rep_embedding(user_id, person_id)
    _rebuild_metadata_faces_for_filenames(user_id, _filenames_for_face_ids(user_id, list(affected_face_ids)))

    try:
        merge_table_client.delete_entity(partition_key=user_id, row_key=merge_id)
    except Exception:
        pass

    return jsonify({'success': True, 'mergeId': merge_id})


@app.route('/api/persons/merges', methods=['GET'])
def list_merges():
    user_id, error = _require_user_id()
    if error:
        return error
    if not _people_features_available():
        return jsonify({'error': 'People features not configured'}), 503
    try:
        rows = list(merge_table_client.query_entities(f"PartitionKey eq '{_escape_odata(user_id)}'"))
    except Exception:
        return jsonify({'merges': []})

    merges = []
    for row in rows:
        try:
            if str(row.get('kind') or '').startswith(('recluster_snapshot', 'face_dedupe_snapshot', 'suspicious_face_snapshot', 'face_membership_snapshot')):
                continue
            target_name = row.get('targetName')
            merged_names = json.loads(row.get('mergedNames', '[]'))
            if not target_name or not merged_names:
                try:
                    payload = json.loads(row.get('payload', '{}'))
                    base = payload.get('base') or {}
                    merged = payload.get('merged') or []
                    if not target_name:
                        target_name = base.get('name')
                    if not merged_names:
                        merged_names = [m.get('name') for m in merged if isinstance(m, dict) and m.get('name')]
                except Exception:
                    pass
            merges.append({
                'mergeId': row['RowKey'],
                'targetPersonId': row.get('targetPersonId'),
                'mergedIds': json.loads(row.get('mergedIds', '[]')),
                'targetName': target_name,
                'mergedNames': merged_names,
                'createdAt': row.get('createdAt'),
            })
        except Exception:
            continue
    return jsonify({'merges': merges})


def _mark_face_not_a_face(user_id: str, person_id: str, face_id: str) -> Dict:
    try:
        person = person_table_client.get_entity(partition_key=user_id, row_key=person_id)
    except Exception:
        return {'success': False, 'error': 'person not found', 'status': 404}
    try:
        face = face_table_client.get_entity(partition_key=user_id, row_key=face_id)
    except Exception:
        return {'success': False, 'error': 'face not found', 'status': 404}
    if str(face.get('personId') or '') != person_id:
        return {'success': False, 'error': 'face not in person', 'status': 400}

    try:
        face_ids = json.loads(person.get('faceIds', '[]') or '[]')
    except Exception:
        face_ids = []
    if face_id not in face_ids:
        return {'success': False, 'error': 'face not in person', 'status': 400}

    filename = str(face.get('filename') or '')
    next_face_ids = [fid for fid in face_ids if fid != face_id]
    face['reviewStatus'] = 'rejected'
    face['rejected'] = True
    face['rejectedReason'] = 'not_a_face'
    face['rejectedAt'] = datetime.now(timezone.utc).isoformat()
    face.pop('personId', None)
    face.pop('confirmedByUser', None)
    face_table_client.upsert_entity(face)

    person_deleted = False
    if next_face_ids:
        person['faceIds'] = json.dumps(next_face_ids)
        person_table_client.upsert_entity(person)
        _update_person_rep_embedding(user_id, person_id)
    else:
        person_table_client.delete_entity(partition_key=user_id, row_key=person_id)
        person_deleted = True

    if filename:
        _rebuild_metadata_faces_for_filename(user_id, filename)

    return {
        'success': True,
        'personId': person_id,
        'faceId': face_id,
        'filename': filename,
        'personDeleted': person_deleted,
    }


def _split_face_into_new_person(user_id: str, person_id: str, face_id: str) -> Dict:
    if person_table_client is None or face_table_client is None:
        return {'success': False, 'error': 'People features not configured', 'status': 503}
    try:
        person = person_table_client.get_entity(partition_key=user_id, row_key=person_id)
    except Exception:
        return {'success': False, 'error': 'person not found', 'status': 404}
    try:
        face = face_table_client.get_entity(partition_key=user_id, row_key=face_id)
    except Exception:
        return {'success': False, 'error': 'face not found', 'status': 404}
    if str(face.get('personId') or '') != person_id:
        return {'success': False, 'error': 'face not in person', 'status': 400}

    try:
        face_ids = json.loads(person.get('faceIds', '[]') or '[]')
    except Exception:
        face_ids = []
    if face_id not in face_ids:
        return {'success': False, 'error': 'face not in person', 'status': 400}

    # Keep the removed face visible by promoting it into a fresh singleton person.
    allocator = _make_unnamed_person_name_allocator(user_id)
    new_person_name = allocator()
    embedding = _face_embedding_from_entity(face)
    new_person_id = _create_person_entity(user_id, [face_id], embedding, name=new_person_name)
    if not new_person_id:
        return {'success': False, 'error': 'failed to create person', 'status': 500}

    face['personId'] = new_person_id
    face['confirmedByUser'] = True
    face['reviewStatus'] = 'confirmed'
    face['rejected'] = False
    face.pop('suspiciousReason', None)
    face.pop('rejectedReason', None)
    face.pop('rejectedAt', None)
    try:
        confidence = float(face.get('confidence', 0.0) or 0.0)
    except Exception:
        confidence = 0.0
    face['confidence'] = max(confidence, 1.0)
    try:
        face_table_client.upsert_entity(face)
    except Exception as exc:
        try:
            person_table_client.delete_entity(partition_key=user_id, row_key=new_person_id)
        except Exception:
            pass
        return {'success': False, 'error': str(exc), 'status': 500}

    _remove_face_from_other_people(user_id, face_id, new_person_id)
    _update_person_rep_embedding(user_id, new_person_id)

    filename = str(face.get('filename') or '')
    if filename:
        _rebuild_metadata_faces_for_filename(user_id, filename)

    try:
        person_table_client.get_entity(partition_key=user_id, row_key=person_id)
        old_person_deleted = False
    except Exception:
        old_person_deleted = True

    return {
        'success': True,
        'personId': new_person_id,
        'previousPersonId': person_id,
        'faceId': face_id,
        'name': new_person_name,
        'oldPersonDeleted': old_person_deleted,
    }


@app.route('/api/persons/<person_id>/not-face', methods=['POST'])
@app.route('/persons/<person_id>/not-face', methods=['POST'])
def mark_not_face(person_id: str):
    user_id, error = _require_user_id()
    if error:
        return error
    if not _people_features_available():
        return jsonify({'error': 'People features not configured'}), 503
    data = request.get_json(silent=True) or {}
    face_id = str(data.get('faceId') or '').strip()
    if not face_id:
        return jsonify({'error': 'faceId required'}), 400
    result = _mark_face_not_a_face(user_id, person_id, face_id)
    status = int(result.pop('status', 200))
    return jsonify(result), status


@app.route('/api/persons/<person_id>/separate', methods=['POST'])
def separate_face(person_id: str):
    user_id, error = _require_user_id()
    if error:
        return error
    if not _people_features_available():
        return jsonify({'error': 'People features not configured'}), 503
    data = request.get_json(silent=True) or {}
    face_id = data.get('faceId')
    if not face_id:
        return jsonify({'error': 'faceId required'}), 400

    result = _split_face_into_new_person(user_id, person_id, str(face_id))
    status = int(result.pop('status', 200))
    return jsonify(result), status


@app.route('/upload/init', methods=['POST'])
@app.route('/upload/init/', methods=['POST'])
@app.route('/api/upload/init', methods=['POST'])
@app.route('/api/upload/init/', methods=['POST'])
def init_upload():
    user_id, error = _require_user_id()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    filename = secure_filename(data.get('filename', ''))
    total_size = int(data.get('totalSize', 0))
    expected_hash = (data.get('sha256') or '').strip()

    if not filename or not allowed_file(filename):
        return jsonify({'error': 'Invalid filename'}), 400
    if total_size <= 0:
        return jsonify({'error': 'Invalid totalSize'}), 400
    if total_size > MAX_UPLOAD_FILE_BYTES:
        return jsonify({'error': 'File exceeds upload limit'}), 413

    upload_id = secure_filename(str(data.get('uploadId') or '')) or str(uuid.uuid4())
    direct = bool(data.get('directToBlob'))
    if not data.get('uploadId'):
        try:
            _cleanup_failed_upload(user_id, filename)
        except Exception:
            pass
        try:
            reset_received_ranges(user_id, filename, total_size, expected_hash or None)
        except Exception:
            pass
    blob_url = None
    expires_at = None
    if direct:
        try:
            blob_url, expires_at = _create_direct_upload_blob_url(filename)
        except Exception as exc:
            app.logger.exception('Failed to create direct upload SAS for %s', filename)
            return jsonify({'error': 'Direct upload is not configured', 'detail': str(exc)}), 503
    thumbnail_blob_url = None
    thumbnail_sas_expires_at = None
    try:
        thumbnail_blob_url, thumbnail_sas_expires_at = _create_direct_thumbnail_upload_blob_url(filename)
    except Exception:
        pass
    return jsonify({
        'uploadId': upload_id,
        'uploadUrl': f'/upload/{upload_id}?filename={filename}',
        'blobUrl': blob_url,
        'thumbnailBlobUrl': thumbnail_blob_url,
        'blobName': filename,
        'sasExpiresAt': expires_at,
        'thumbnailSasExpiresAt': thumbnail_sas_expires_at,
        'totalSize': total_size,
    })


def _create_blob_sas_url(
    container_name: str,
    filename: str,
    *,
    minutes: int,
    permissions: BlobSasPermissions,
) -> Tuple[str, str]:
    if blob_service_client is None or not container_name:
        raise RuntimeError('Blob storage is not configured')
    if not account_name:
        raise RuntimeError('Storage account name is not configured')
    starts_on = datetime.now(timezone.utc) - timedelta(minutes=5)
    expires_on = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    delegation_key = blob_service_client.get_user_delegation_key(starts_on, expires_on)
    sas = generate_blob_sas(
        account_name=account_name,
        container_name=container_name,
        blob_name=filename,
        user_delegation_key=delegation_key,
        permission=permissions,
        start=starts_on,
        expiry=expires_on,
    )
    blob_client = blob_service_client.get_blob_client(container=container_name, blob=filename)
    return f'{blob_client.url}?{sas}', expires_on.isoformat()


def _create_direct_upload_blob_url(filename: str) -> Tuple[str, str]:
    # Write-only: an upload SAS must not be usable to read back arbitrary blobs.
    return _create_blob_sas_url(
        BLOB_IMAGE_CONTAINER,
        filename,
        minutes=DIRECT_UPLOAD_SAS_MINUTES,
        permissions=BlobSasPermissions(write=True, create=True),
    )


def _create_direct_thumbnail_upload_blob_url(filename: str) -> Tuple[str, str]:
    # Write-only: an upload SAS must not be usable to read back arbitrary blobs.
    return _create_blob_sas_url(
        BLOB_THUMBNAIL_CONTAINER,
        filename,
        minutes=DIRECT_UPLOAD_SAS_MINUTES,
        permissions=BlobSasPermissions(write=True, create=True),
    )


def _create_scoped_blob_url(container_name: str, filename: str, *, minutes: int = 15) -> Tuple[str, str]:
    return _create_blob_sas_url(
        container_name,
        filename,
        minutes=minutes,
        permissions=BlobSasPermissions(read=True),
    )


def _queue_upload_processing(user_id: str, final_name: str) -> None:
    if is_video_file(final_name):
        return
    _enqueue_processing_steps(user_id, final_name, ['face'])


def _queue_people_clustering_after_face_processing(user_id: str, filename: str, metadata: Optional[Dict]) -> Optional[Dict[str, str]]:
    """Queue clustering once browser face results are durable and usable."""
    if not _people_features_available() or not isinstance(metadata, dict):
        return None
    if str(metadata.get('processing_state') or '').strip().lower() == 'deleted':
        return None
    if str(metadata.get('face_status') or '').strip().lower() != 'done':
        return None

    try:
        face_count = int(metadata.get('faceCount') or 0)
    except Exception:
        face_count = 0
    if face_count <= 0:
        faces_value = metadata.get('faces')
        if isinstance(faces_value, str):
            try:
                faces_value = json.loads(faces_value)
            except Exception:
                faces_value = []
        if isinstance(faces_value, list):
            face_count = sum(1 for face in faces_value if isinstance(face, dict))
    if face_count <= 0:
        return None

    return _enqueue_clustering_job(
        user_id,
        job_type='people_cluster',
        payload={
            'trigger': 'upload_face_ready',
            'filename': filename,
            'faceCount': face_count,
        },
    )


@app.route('/upload/finalize', methods=['POST'])
@app.route('/upload/finalize/', methods=['POST'])
@app.route('/api/upload/finalize', methods=['POST'])
@app.route('/api/upload/finalize/', methods=['POST'])
def finalize_direct_upload():
    user_id, error = _require_user_id()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    filename = secure_filename(data.get('filename', ''))
    total_size = int(data.get('totalSize', 0) or 0)
    content_type = str(data.get('contentType') or 'application/octet-stream')
    if not filename or not allowed_file(filename):
        return jsonify({'error': 'Invalid filename'}), 400
    if total_size <= 0 or total_size > MAX_UPLOAD_FILE_BYTES:
        return jsonify({'error': 'Invalid totalSize'}), 400
    try:
        props = blob_service_client.get_blob_client(container=BLOB_IMAGE_CONTAINER, blob=filename).get_blob_properties()
        if int(getattr(props, 'size', 0) or 0) != total_size:
            return jsonify({'error': 'Uploaded blob size mismatch'}), 409
    except Exception as exc:
        return jsonify({'error': 'Uploaded blob not found', 'detail': str(exc)}), 404

    try:
        duplicates, final_name = finalize_uploaded_file(
            user_id,
            filename,
            content_type,
            client_processing=data.get('clientProcessing'),
            client_processing_report=data.get('clientProcessingReport'),
            client_asset_id=str(data.get('clientAssetId') or data.get('uploadId') or ''),
        )
    except Exception as exc:
        app.logger.exception('Direct upload finalization failed for %s', filename)
        return jsonify({'error': 'Upload finalization failed', 'detail': str(exc)}), 500
    metadata = None
    try:
        metadata = metadata_table_client.get_entity(partition_key=user_id, row_key=final_name)
        if metadata.get('upload_sha256_expected') and metadata.get('upload_sha256_match') is False:
            return jsonify({
                'error': 'Upload hash mismatch',
                'filename': final_name,
                'uploadSha256Match': metadata.get('upload_sha256_match'),
            }), 422
    except Exception:
        pass
    if data.get('clientProcessing') or data.get('clientProcessingReport'):
        try:
            metadata = apply_client_processing_results_for_file(
                user_id,
                final_name,
                client_processing=data.get('clientProcessing'),
                client_processing_report=data.get('clientProcessingReport'),
                client_asset_id=str(data.get('clientAssetId') or data.get('uploadId') or ''),
            )
        except Exception:
            app.logger.exception('Inline client processing update failed for %s', final_name)
    try:
        metadata = metadata or metadata_table_client.get_entity(partition_key=user_id, row_key=final_name)
    except Exception:
        pass
    try:
        _queue_upload_processing(user_id, final_name)
    except Exception:
        app.logger.exception('Failed to queue post-finalize processing for %s', final_name)
    try:
        _queue_people_clustering_after_face_processing(user_id, final_name, metadata)
    except Exception:
        app.logger.exception('Failed to auto-queue clustering for %s', final_name)
    return jsonify({
        'uploadId': data.get('uploadId') or '',
        'filename': final_name,
        'bytesReceived': total_size,
        'totalSize': total_size,
        'complete': True,
        'duplicates': duplicates,
        'clientProcessingLateResultWaitSeconds': 0,
    })


@app.route('/upload/client-processing', methods=['POST'])
@app.route('/upload/client-processing/', methods=['POST'])
@app.route('/api/upload/client-processing', methods=['POST'])
@app.route('/api/upload/client-processing/', methods=['POST'])
def upload_client_processing_results():
    user_id, error = _require_user_id()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    filename = secure_filename(data.get('filename', ''))
    if not filename or not allowed_file(filename):
        return jsonify({'error': 'Invalid filename'}), 400
    try:
        metadata = apply_client_processing_results_for_file(
            user_id,
            filename,
            client_processing=data.get('clientProcessing'),
            client_processing_report=data.get('clientProcessingReport'),
            client_asset_id=str(data.get('clientAssetId') or data.get('uploadId') or ''),
            thumbnail_already_uploaded=bool(data.get('thumbnailAlreadyUploaded')),
        )
    except Exception as exc:
        app.logger.exception('Late browser processing update failed for %s', filename)
        message = str(exc)
        if 'deleted' in message.lower():
            return jsonify({'error': 'Photo has been deleted'}), 410
        return jsonify({'error': 'Client processing update failed', 'detail': str(exc)}), 500

    try:
        _queue_people_clustering_after_face_processing(user_id, filename, metadata)
    except Exception:
        app.logger.exception('Failed to auto-queue clustering after browser processing update for %s', filename)

    return jsonify({
        'uploadId': data.get('uploadId') or '',
        'filename': filename,
        'accepted': True,
        'statuses': {
            'thumbnail': metadata.get('thumbnail_status'),
            'face': metadata.get('face_status'),
            'aiVision': metadata.get('ai_vision_status'),
            'mapDetection': metadata.get('map_detection_status'),
            'exif': metadata.get('exif_status'),
            'ocr': metadata.get('ocr_status'),
        },
    })

BROWSER_PROCESSING_STATUS_FIELDS = (
    ('thumbnail_status', 'thumbnail'),
    ('exif_status', 'exif'),
    ('ocr_status', 'ocr'),
    ('ai_vision_status', 'aiVision'),
    ('map_detection_status', 'mapDetection'),
    ('face_status', 'face'),
)
BROWSER_PROCESSING_TERMINAL_STATUSES = {'done', 'no_data', 'deleted', 'skipped', 'unsupported', 'failed', 'timeout'}
BROWSER_PROCESSING_PENDING_SELECT = [
    'RowKey',
    'rotation',
    'processing_state',
    'processing_lease_owner',
    'processing_lease_expires_at',
    'last_processing_update',
    'processing_metadata',
] + [field for field, _key in BROWSER_PROCESSING_STATUS_FIELDS]


def _browser_processing_lease_expired(entity: Dict) -> bool:
    expires_at = str(entity.get('processing_lease_expires_at') or '').strip()
    if not expires_at:
        return True
    try:
        return datetime.fromisoformat(expires_at.replace('Z', '+00:00')) <= datetime.now(timezone.utc)
    except Exception:
        return True


def _browser_processing_face_background_throttled(entity: Dict) -> bool:
    try:
        processing_metadata = json.loads(entity.get('processing_metadata') or '{}')
    except Exception:
        return False
    if not isinstance(processing_metadata, dict):
        return False

    client_face = processing_metadata.get('client_face')
    if isinstance(client_face, dict) and str(client_face.get('deferredReason') or '').strip().lower() == 'background_throttled':
        return True

    client_processing_report = processing_metadata.get('clientProcessingReport')
    report_items = client_processing_report.get('items') if isinstance(client_processing_report, dict) else client_processing_report
    if isinstance(report_items, list):
        for item in report_items:
            if str(item.get('step') or '').strip() == 'face' and str(item.get('reason') or '').strip().lower() == 'background_throttled':
                return True
    return False


RAW_AI_VISION_RETRY_REASONS = {
    'inference_timeout',
    'model_budget_exceeded',
    'model_download_timeout',
    'model_load_failed',
    'model_unavailable',
    'raw_container_unsupported',
    'raw_preview_invalid',
    'raw_preview_missing',
    'upstream_incomplete',
}


def _is_local_vision_fallback_metadata(value: Dict) -> bool:
    if not isinstance(value, dict):
        return False
    return (
        str(value.get('model') or '').strip() == LOCAL_VISION_FALLBACK_MODEL
        or str(value.get('modelTaxonomyVersion') or '').strip() == LOCAL_VISION_FALLBACK_TAXONOMY_VERSION
        or str(value.get('runtime') or '').strip() == LOCAL_VISION_FALLBACK_RUNTIME
        or str(value.get('rejectedReason') or '').strip() == 'local_vision_fallback_non_authoritative'
    )


def _raw_ai_vision_no_data_should_retry(entity: Dict) -> bool:
    filename = str(entity.get('RowKey') or '').strip()
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    if ext not in RAW_EXTENSIONS_RAWPY and ext not in RAW_EXTENSIONS_CINEMA:
        return False
    try:
        processing_metadata = json.loads(entity.get('processing_metadata') or '{}')
    except Exception:
        processing_metadata = {}
    if not isinstance(processing_metadata, dict):
        return False

    accepted_ai = processing_metadata.get('client_ai_vision')
    if isinstance(accepted_ai, dict) and str(accepted_ai.get('source') or '') == 'browser':
        if _is_local_vision_fallback_metadata(accepted_ai):
            return True
        return False

    report = processing_metadata.get('clientProcessingReport')
    report_items = report.get('items') if isinstance(report, dict) else report
    if not isinstance(report_items, list):
        return False

    for item in report_items:
        if str(item.get('step') or '').strip() != 'ai_vision':
            continue
        status = str(item.get('status') or '').strip().lower()
        reason = str(item.get('reason') or '').strip().lower()
        if status in {'failed', 'skipped', 'timeout'} and reason in RAW_AI_VISION_RETRY_REASONS:
            return True
    return False


def _browser_processing_pending_item(entity: Dict) -> Optional[Dict]:
    filename = str(entity.get('RowKey') or '').strip()
    if not filename:
        return None
    if str(entity.get('processing_state') or '').strip().lower() == 'deleted':
        return None

    statuses = {}
    has_pending_status = False
    lease_expired = _browser_processing_lease_expired(entity)
    for field, payload_key in BROWSER_PROCESSING_STATUS_FIELDS:
        raw_status = entity.get(field)
        status = str(raw_status or '').strip().lower()
        if status == 'running' and not lease_expired:
            statuses[payload_key] = raw_status
            continue
        if status == 'running' and lease_expired:
            raw_status = 'pending'
            status = 'pending'
        if field == 'ai_vision_status' and status in {'failed', 'no_data', 'skipped', 'timeout'} and _raw_ai_vision_no_data_should_retry(entity):
            raw_status = 'pending'
            status = 'pending'
        if status:
            statuses[payload_key] = raw_status
            if status not in BROWSER_PROCESSING_TERMINAL_STATUSES:
                has_pending_status = True

    if _browser_processing_face_background_throttled(entity):
        face_status = str(statuses.get('face') or '').strip().lower()
        if face_status != 'done':
            statuses['face'] = 'pending'
            has_pending_status = True

    if not has_pending_status:
        return None
    return {
        'filename': filename,
        'statuses': statuses,
        'lastProcessingUpdate': entity.get('last_processing_update') or '',
        'rotation': _normalize_rotation(entity.get('rotation', 0)),
    }


@app.route('/upload/processing/pending', methods=['GET'])
@app.route('/upload/processing/pending/', methods=['GET'])
@app.route('/api/upload/processing/pending', methods=['GET'])
@app.route('/api/upload/processing/pending/', methods=['GET'])
def upload_processing_pending():
    user_id, error = _require_user_id()
    if error:
        return error

    if metadata_table_client is None:
        app.logger.warning('Browser processing pending requested before metadata table was configured.')
        return jsonify({'pending': []})

    try:
        limit = max(1, min(int(request.args.get('limit', '1') or 1), 25))
    except ValueError:
        return jsonify({'error': 'Invalid limit'}), 400

    try:
        entities = _query_metadata_rows_for_user(
            user_id,
            select=BROWSER_PROCESSING_PENDING_SELECT,
            purpose='browser_processing_pending',
        )
    except Exception as exc:
        app.logger.warning('Browser processing pending scan failed for %s: %s', user_id, exc, exc_info=True)
        return jsonify({'pending': []})

    pending = []
    for entity in entities:
        item = _browser_processing_pending_item(entity)
        if item:
            pending.append(item)
    pending.sort(key=lambda item: str(item.get('lastProcessingUpdate') or ''))
    bounded = pending[:limit]
    for item in bounded:
        try:
            url, expires_at = _create_scoped_blob_url(BLOB_IMAGE_CONTAINER, item['filename'], minutes=10)
            item['sourceUrl'] = url
            item['sourceExpiresAt'] = expires_at
        except Exception:
            app.logger.warning('Failed to mint browser processing source URL for %s', item.get('filename'), exc_info=True)
        try:
            thumbnail_url, thumbnail_expires_at = _create_direct_thumbnail_upload_blob_url(item['filename'])
            item['thumbnailUploadUrl'] = thumbnail_url
            item['thumbnailUploadExpiresAt'] = thumbnail_expires_at
        except Exception:
            app.logger.warning('Failed to mint browser thumbnail upload URL for %s', item.get('filename'), exc_info=True)
    return jsonify({'pending': bounded, 'totalPending': len(pending)})


@app.route('/upload/processing/claim', methods=['POST'])
@app.route('/upload/processing/claim/', methods=['POST'])
@app.route('/api/upload/processing/claim', methods=['POST'])
@app.route('/api/upload/processing/claim/', methods=['POST'])
def upload_processing_claim():
    user_id, error = _require_user_id()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    filename = secure_filename(str(data.get('filename') or ''))
    if not filename:
        return jsonify({'error': 'Missing filename'}), 400
    lease_owner = str(data.get('leaseId') or data.get('ownerId') or f'browser-{uuid.uuid4()}').strip()
    requested_steps = data.get('steps')
    steps = [str(step or '').strip() for step in requested_steps] if isinstance(requested_steps, list) else None
    try:
        lease = claim_processing_lease(user_id, filename, lease_owner, lease_seconds=120, steps=steps)
    except Exception as exc:
        message = str(exc)
        if 'already held by another client' in message.lower() or 'lease is already held' in message.lower():
            return jsonify({'claimed': False, 'reason': 'lease_active', 'detail': message}), 200
        return jsonify({'claimed': False, 'reason': 'lease_active', 'detail': message}), 409
    lease_expires_at = lease.get('leaseExpiresAt') or ''
    response = {
        'claimed': True,
        'leaseId': lease_owner,
        'expiresAt': lease_expires_at,
    }
    try:
        thumbnail_url, thumbnail_expires_at = _create_direct_thumbnail_upload_blob_url(filename)
        response['thumbnailUploadUrl'] = thumbnail_url
        response['thumbnailUploadExpiresAt'] = thumbnail_expires_at
    except Exception:
        app.logger.warning('Failed to mint browser thumbnail upload URL for claimed photo %s', filename, exc_info=True)
    return jsonify(response)


@app.route('/upload/processing/heartbeat', methods=['POST'])
@app.route('/upload/processing/heartbeat/', methods=['POST'])
@app.route('/api/upload/processing/heartbeat', methods=['POST'])
@app.route('/api/upload/processing/heartbeat/', methods=['POST'])
def upload_processing_heartbeat():
    user_id, error = _require_user_id()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    filename = secure_filename(str(data.get('filename') or ''))
    lease_id = str(data.get('leaseId') or '')
    try:
        lease = heartbeat_processing_lease(user_id, filename, lease_id, lease_seconds=120)
    except Exception as exc:
        return jsonify({'ok': False, 'reason': 'lease_missing', 'detail': str(exc)}), 409
    return jsonify({'ok': True, 'expiresAt': lease.get('leaseExpiresAt') or ''})


@app.route('/upload/processing/release', methods=['POST'])
@app.route('/upload/processing/release/', methods=['POST'])
@app.route('/api/upload/processing/release', methods=['POST'])
@app.route('/api/upload/processing/release/', methods=['POST'])
def upload_processing_release():
    user_id, error = _require_user_id()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    filename = secure_filename(str(data.get('filename') or ''))
    lease_id = str(data.get('leaseId') or '')
    release_processing_lease(user_id, filename, lease_id)
    return jsonify({'ok': True})


UPLOAD_TRACKING_FIELDS = (
    'received_ranges',
    'upload_total_size',
    'upload_started_at',
    'upload_last_chunk_at',
    'upload_sha256_expected',
    'upload_sha256_actual',
    'upload_sha256_match',
)

THUMBNAIL_RETRY_COUNT_FIELD = 'thumbnail_retry_count'


def _is_not_found_storage_error(exc: Exception) -> bool:
    message = str(exc)
    return (
        '404' in message
        or 'ResourceNotFound' in message
        or 'BlobNotFound' in message
        or 'does not exist' in message.lower()
        or 'not found' in message.lower()
    )


def _delete_blob_if_present(container_name: str, blob_name: str) -> Optional[str]:
    if not container_name or blob_service_client is None:
        return None
    try:
        blob_service_client.get_blob_client(container=container_name, blob=blob_name).delete_blob()
    except Exception as exc:
        if _is_not_found_storage_error(exc):
            return None
        return str(exc)
    return None


def _delete_photo_blobs_if_present(blob_name: str) -> List[str]:
    errors: List[str] = []
    for label, container_name in (('blob image', BLOB_IMAGE_CONTAINER), ('blob thumbnail', BLOB_THUMBNAIL_CONTAINER)):
        error = _delete_blob_if_present(container_name, blob_name)
        if error:
            errors.append(f'{label}: {error}')
    return errors


def _mark_processing_deleted_for_file(user_id: str, filename: str) -> None:
    try:
        entity = metadata_table_client.get_entity(partition_key=user_id, row_key=filename)
    except Exception:
        return
    entity['processing_state'] = 'deleted'
    for step in ('thumbnail', 'face', 'ai_vision', 'map_detection', 'verify'):
        entity[f'{step}_status'] = 'deleted'
    entity['processing_lease_owner'] = ''
    entity['processing_lease'] = ''
    entity['processing_lease_expires_at'] = ''
    entity['last_processing_update'] = datetime.now(timezone.utc).isoformat()
    metadata_table_client.upsert_entity(entity)
    touch_user_vector_index_state(user_id)


def _delete_upload_temp_files_for_filename(filename: str, upload_id: str = '') -> Tuple[List[str], List[str]]:
    deleted: List[str] = []
    errors: List[str] = []
    temp_dir = os.path.abspath(UPLOAD_TMP_DIR)
    try:
        if not os.path.isdir(temp_dir):
            return deleted, errors
        suffix = f"__{filename}"
        for entry in os.listdir(temp_dir):
            if entry.endswith('.lock'):
                continue
            if not entry.endswith(suffix):
                continue
            if upload_id and not entry.startswith(f"{upload_id}__"):
                continue
            path = os.path.abspath(os.path.join(temp_dir, entry))
            if not path.startswith(temp_dir + os.sep):
                errors.append(f'{entry}: invalid temp path')
                continue
            try:
                os.remove(path)
                deleted.append(entry)
            except OSError as exc:
                errors.append(f'{entry}: {str(exc)}')
    except OSError as exc:
        errors.append(str(exc))
    return deleted, errors


def _cleanup_failed_upload(user_id: str, filename: str, upload_id: str = '') -> Dict:
    cleanup = {
        'filename': filename,
        'tempFileDeleted': False,
        'tempFilesDeleted': [],
        'partialFilesDeleted': [],
        'metadataAction': 'none',
        'errors': [],
    }

    temp_entries, temp_errors = _delete_upload_temp_files_for_filename(filename, upload_id)
    cleanup['tempFilesDeleted'] = temp_entries
    cleanup['tempFileDeleted'] = len(temp_entries) > 0
    for temp_error in temp_errors:
        cleanup['errors'].append(f'temp: {temp_error}')

    metadata = None
    try:
        metadata = metadata_table_client.get_entity(partition_key=user_id, row_key=filename)
    except Exception:
        metadata = None

    has_upload_tracking = bool(metadata) and any(field in metadata for field in UPLOAD_TRACKING_FIELDS)
    has_completed_metadata = bool(metadata) and bool(
        metadata.get('fileHash')
        or metadata.get('perceptualHash')
        or metadata.get('mimeType')
        or metadata.get('thumbnail_status')
        or metadata.get('verification_status')
    )

    if metadata and has_upload_tracking:
        if has_completed_metadata:
            for field in UPLOAD_TRACKING_FIELDS:
                metadata.pop(field, None)
            try:
                metadata_table_client.upsert_entity(metadata)
                cleanup['metadataAction'] = 'trackingCleared'
            except Exception as exc:
                cleanup['errors'].append(f'metadata: {str(exc)}')
        else:
            cleanup['errors'].extend(_delete_photo_blobs_if_present(filename))

            try:
                metadata_table_client.delete_entity(partition_key=user_id, row_key=filename)
                cleanup['metadataAction'] = 'deleted'
            except Exception as exc:
                cleanup['errors'].append(f'metadata: {str(exc)}')

    return cleanup


@app.route('/upload/cancel', methods=['POST'])
@app.route('/upload/cancel/', methods=['POST'])
@app.route('/api/upload/cancel', methods=['POST'])
@app.route('/api/upload/cancel/', methods=['POST'])
def cancel_uploads():
    user_id, error = _require_user_id()
    if error:
        return error

    data = request.get_json(silent=True) or {}
    files = data.get('files', [])
    if not isinstance(files, list) or not files:
        return jsonify({'error': 'files must be a non-empty list'}), 400

    cleaned = []
    errors = []
    for item in files:
        if not isinstance(item, dict):
            errors.append({'filename': '<unknown>', 'error': 'Invalid file entry'})
            continue

        original_name = str(item.get('filename') or '')
        safe_name = secure_filename(original_name)
        if not safe_name or safe_name != original_name or not allowed_file(safe_name):
            errors.append({'filename': original_name or '<unknown>', 'error': 'Invalid filename'})
            continue

        result = _cleanup_failed_upload(user_id, safe_name, str(item.get('uploadId') or ''))
        cleaned.append(result)
        if result['errors']:
            errors.append({'filename': safe_name, 'error': '; '.join(result['errors'])})

    return jsonify({
        'success': len(errors) == 0,
        'cleaned': cleaned,
        'errors': errors,
    }), 200 if len(errors) == 0 else 207


@app.route('/photos', methods=['GET'])
@app.route('/photos/', methods=['GET'])
@app.route('/api/photos', methods=['GET'])
@app.route('/api/photos/', methods=['GET'])
def list_photos():
    try:
        sort = request.args.get('sort', 'date')
        offset = int(request.args.get('offset', '0'))
        limit = int(request.args.get('limit', '24'))
    except ValueError:
        return jsonify({'error': 'Invalid paging parameters.'}), 400

    capture_start = _parse_capture_filter(request.args.get('captureStart', '') or '')
    capture_end = _parse_capture_filter(request.args.get('captureEnd', '') or '')

    user_id, error = _require_user_id()
    if error:
        return error
    try:
        metadata_rows = _query_metadata_rows_for_user(user_id, purpose='photos.list')
        entries = [row['RowKey'] for row in metadata_rows if row.get('RowKey')]
        metadata_map = {row['RowKey']: row for row in metadata_rows if row.get('RowKey')}
    except Exception as exc:
        return jsonify({'error': 'Unable to read photo metadata.', 'details': str(exc)}), 503

    if sort == 'location':
        entries.sort(key=lambda name: name.lower())
    elif sort == 'rating':
        entries.sort(key=lambda name: metadata_map.get(name, {}).get('rating', 0), reverse=True)
    elif sort == 'likes':
        entries.sort(key=lambda name: metadata_map.get(name, {}).get('likes', 0), reverse=True)
    elif sort == 'capture':
        entries.sort(key=lambda name: _metadata_capture_date(metadata_map.get(name, {})), reverse=True)
    elif sort == 'date':
        entries.sort(key=lambda name: _metadata_upload_date(metadata_map.get(name, {})), reverse=True)
    else:
        entries.sort(key=lambda name: _metadata_upload_date(metadata_map.get(name, {})), reverse=True)

    if capture_start or capture_end:
        entries = [name for name in entries if _capture_in_range(metadata_map.get(name, {}), capture_start, capture_end)]

    selected = entries[offset:offset + limit]
    photos = []
    for filename in selected:
        metadata = metadata_map.get(filename, {})
        photos.append(_build_photo_summary(user_id, filename, metadata, include_props=True))

    return jsonify({'photos': photos, 'total': len(entries)})


@app.route('/uploads/corrupted', methods=['GET'])
@app.route('/uploads/corrupted/', methods=['GET'])
@app.route('/api/uploads/corrupted', methods=['GET'])
@app.route('/api/uploads/corrupted/', methods=['GET'])
def list_corrupted_uploads():
    user_id, error = _require_user_id()
    if error:
        return error
    try:
        rows = _query_metadata_rows_for_user(user_id, purpose='uploads.corrupted')
    except Exception as exc:
        return jsonify({'error': 'Unable to read photo metadata.', 'details': str(exc)}), 503

    items = []
    for row in rows:
        if row.get('verification_status') != 'failed' and not row.get('corrupted'):
            continue
        filename = row.get('RowKey')
        if not filename:
            continue

        reason = row.get('verification_error') or row.get('last_error') or ''
        sha256_match = row.get('upload_sha256_match')
        ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
        reason_lower = str(reason).lower()
        raw_integrity_error = (
            'too small' in reason_lower
            or 'header' in reason_lower
            or 'signature' in reason_lower
            or 'decode' in reason_lower
            or 'embedded preview' in reason_lower
            or 'sha256' in reason_lower
        )
        if ext in RAW_EXTENSIONS_RAWPY and not raw_integrity_error and not (sha256_match is False or sha256_match == 'false'):
            continue
        if sha256_match is False or sha256_match == 'false':
            corruption_type = 'hash_mismatch'
        elif reason:
            corruption_type = 'parse_error'
        else:
            corruption_type = 'unknown'

        media_urls = _private_photo_media_urls(filename)
        items.append({
            'filename': filename,
            'reason': reason,
            'corruptionType': corruption_type,
            'uploadedAt': row.get('uploadDate') or '',
            'mimeType': row.get('mimeType') or '',
            'thumbnailUrl': media_urls['thumbnailUrl'],
            'url': media_urls['url'],
            'rotation': _normalize_rotation(row.get('rotation', 0)),
            'verificationStatus': row.get('verification_status') or '',
            'sha256Match': sha256_match,
        })

    items.sort(key=lambda item: item.get('uploadedAt') or '', reverse=True)
    return jsonify({'items': items, 'count': len(items)})


@app.route('/uploads/corrupted/<path:filename>/clear', methods=['POST'])
@app.route('/api/uploads/corrupted/<path:filename>/clear', methods=['POST'])
def clear_corrupted_upload(filename: str):
    user_id, error = _require_user_id()
    if error:
        return error
    safe_name = secure_filename(filename)
    if not safe_name or not allowed_file(safe_name) or safe_name != filename:
        return jsonify({'error': 'Invalid filename'}), 400

    metadata = _get_metadata_entity(user_id, safe_name)
    if metadata is None:
        return jsonify({'error': 'Not found'}), 404

    try:
        download_media_bytes('image', safe_name)
    except Exception:
        return jsonify({'error': 'Image file not found'}), 404

    metadata['corrupted'] = False
    metadata.pop('verification_error', None)
    metadata.pop('corrupted_at', None)
    if metadata.get('verification_status') == 'failed':
        metadata['verification_status'] = 'pending'
    metadata_table_client.upsert_entity(metadata)

    return jsonify({
        'filename': safe_name,
        'corrupted': False,
        'thumbnailRegenerated': False,
    })


@app.route('/performance/throughput', methods=['GET'])
@app.route('/performance/throughput/', methods=['GET'])
@app.route('/api/performance/throughput', methods=['GET'])
@app.route('/api/performance/throughput/', methods=['GET'])
def performance_throughput():
    return jsonify(_get_throughput_metrics())


@app.route('/photos/search', methods=['GET'])
@app.route('/photos/search/', methods=['GET'])
@app.route('/api/photos/search', methods=['GET'])
@app.route('/api/photos/search/', methods=['GET'])
def search_photos():
    query = (request.args.get('q') or '').strip()
    if not query:
        return jsonify({'photos': [], 'total': 0})

    try:
        offset = int(request.args.get('offset', '0'))
        limit = int(request.args.get('limit', '24'))
    except ValueError:
        return jsonify({'error': 'Invalid paging parameters.'}), 400

    capture_start = _parse_capture_filter(request.args.get('captureStart', '') or '')
    capture_end = _parse_capture_filter(request.args.get('captureEnd', '') or '')

    user_id, error = _require_user_id()
    if error:
        return error

    try:
        rows = _query_metadata_rows_for_user(user_id, purpose='photos.search')
    except Exception as exc:
        return jsonify({'error': 'Unable to read photo metadata.', 'details': str(exc)}), 503

    pid_to_name, name_to_ids = _load_people_name_index(user_id)
    matched_person_groups = _matched_query_people_groups(query, name_to_ids)
    matched_location_terms = _matched_query_locations(query, rows)
    tokens = parse_search_query(query)
    query_embedding = vision_utils.encode_text_embedding(build_expanded_query_text(query, tokens))
    current_embedding_version = vision_utils.get_text_embedding_version()
    vector_scores: Dict[str, float] = {}
    if query_embedding:
        for row_key, score in vector_search_candidates(user_id, query_embedding, top_k=max(limit * 25, 500), allow_refresh=False):
            if row_key:
                vector_scores[row_key] = score
    semantic_threshold = float(os.getenv('SEMANTIC_SEARCH_THRESHOLD', '0.16'))
    has_context_intent = bool(tokens.get('required_object') and tokens.get('modifiers'))
    scored: List[Tuple[float, str, Dict]] = []
    fallback_scored: List[Tuple[float, str, Dict]] = []

    for row in rows:
        filename = row.get('RowKey')
        if not filename:
            continue
        row = _metadata_with_people_names(row, pid_to_name)
        if capture_start or capture_end:
            if not _capture_in_range(row, capture_start, capture_end):
                continue
        if matched_person_groups:
            try:
                people_ids = set(str(pid) for pid in json.loads(row.get('peopleIds', '[]') or '[]'))
            except Exception:
                people_ids = set()
            # Every distinct queried person must appear (at least one id from
            # each group) -- "alice and bob" means both, not either.
            if not all(any(pid in people_ids for pid in group) for group in matched_person_groups):
                continue
        if not _metadata_matches_locations(row, matched_location_terms):
            continue

        exif_data = parse_exif_data(row.get('exifData', '{}'))
        semantic_text = build_semantic_text(filename, row)
        lexical_score = lexical_search_score(tokens, filename, row, exif_data)
        if has_context_intent and lexical_score <= 0:
            continue
        semantic_score = 0.0
        if query_embedding:
            # Blend semantic similarity into every candidate's score, not just as a
            # fallback when lexical matching finds nothing -- otherwise embeddings
            # (image or text) never influence ranking for queries that also happen
            # to hit a tag/filename keyword.
            semantic_score = vector_scores.get(filename, 0.0)
            if semantic_score <= 0 and not vector_scores:
                row_embedding, semantic_text = _semantic_embedding_for_row(
                    filename,
                    row,
                    current_embedding_version,
                    allow_compute=SEMANTIC_SEARCH_ALLOW_QUERYTIME_ROW_EMBEDDINGS,
                )
                semantic_score = cosine_similarity(query_embedding, row_embedding)

        score = lexical_score
        if semantic_score >= semantic_threshold:
            score += semantic_score * 10.0
        if matched_person_groups:
            # Reward matching more of the named people more, so "alice and bob"
            # ranks a photo with both above one that merely passed the AND gate.
            score += 8.0 * len(matched_person_groups)
        if matched_location_terms:
            score += 5.0

        if score <= 0:
            continue
        if has_context_intent and tokens.get('modifiers'):
            searchable_text = ' '.join([
                filename,
                row.get('caption', ''),
                semantic_text,
                row.get('ocrText', ''),
                ' '.join(parse_json_list(row.get('objects', '[]'))),
                ' '.join(parse_json_list(row.get('peopleNames', '[]'))),
                row.get('address', ''),
                row.get('locationCity', ''),
                row.get('locationCountry', ''),
            ]).lower()
            exact_modifier_match = any(
                modifier and (
                    modifier in searchable_text
                    or modifier in ' '.join(parse_tags(row.get('tags', '[]'))).lower()
                )
                for modifier in tokens.get('modifiers', [])
            )
            if not exact_modifier_match:
                fallback_scored.append((score, filename, row))
                continue
        if has_context_intent and lexical_score < 12.0:
            fallback_scored.append((score, filename, row))
        else:
            scored.append((score, filename, row))

    fallback_notice = None
    if has_context_intent and not scored and fallback_scored:
        modifier = tokens.get('modifiers', [''])[0]
        obj = tokens.get('required_object', [''])[0]
        fallback_notice = f"No {modifier} {obj} found. Showing {obj} results instead."
        scored = fallback_scored

    scored.sort(key=lambda item: item[0], reverse=True)
    total = len(scored)
    selected = scored[offset:offset + limit]

    photos = []
    for _, filename, metadata in selected:
        photos.append(_build_photo_summary(user_id, filename, metadata, include_props=True))

    response_payload = {'photos': photos, 'total': total}
    if fallback_notice:
        response_payload['searchNotice'] = fallback_notice
    return jsonify(response_payload)


@app.route('/photos/metadata', methods=['POST'])
@app.route('/photos/metadata/', methods=['POST'])
@app.route('/api/photos/metadata', methods=['POST'])
@app.route('/api/photos/metadata/', methods=['POST'])
def photos_metadata():
    user_id, error = _require_user_id()
    if error:
        return error

    data = request.get_json(silent=True) or {}
    filenames = data.get('filenames', [])
    if not isinstance(filenames, list):
        return jsonify({'error': 'Invalid request'}), 400

    metadata = {}
    for filename in filenames:
        safe_name = secure_filename(filename)
        if not safe_name or not allowed_file(safe_name) or safe_name != filename:
            metadata[filename] = {'error': 'Invalid filename'}
            continue

        if not _get_metadata_entity(user_id, safe_name):
            metadata[filename] = {'error': 'Not found'}
            continue

        try:
            props = get_media_properties('image', safe_name)
            metadata[filename] = {
                'size': props.get('size'),
                'lastModified': props.get('last_modified').isoformat() if props.get('last_modified') else None,
            }
        except Exception:
            metadata[filename] = {'error': 'Not found'}

    return jsonify(metadata)


@app.route('/photos/delete', methods=['POST'])
@app.route('/photos/delete/', methods=['POST'])
@app.route('/api/photos/delete', methods=['POST'])
@app.route('/api/photos/delete/', methods=['POST'])
def delete_multiple_photos():
    user_id, error = _require_user_id()
    if error:
        return error

    data = request.get_json(silent=True) or {}
    filenames = data.get('filenames', [])
    if not isinstance(filenames, list) or len(filenames) == 0:
        return jsonify({'error': 'Invalid request'}), 400

    deleted = []
    errors = []

    for filename in filenames:
        safe_name = secure_filename(filename)
        if not safe_name or not allowed_file(safe_name) or safe_name != filename:
            errors.append(f'{filename}: Invalid filename')
            continue

        metadata = _get_metadata_entity(user_id, safe_name)
        shared_with_other_user = _is_filename_shared(safe_name, user_id)

        removed_any = False
        file_errors = []
        temp_deleted, temp_errors = _delete_upload_temp_files_for_filename(safe_name)
        if temp_deleted:
            removed_any = True
        for temp_error in temp_errors:
            file_errors.append(f'temp: {temp_error}')

        if not shared_with_other_user:
            _mark_processing_deleted_for_file(user_id, safe_name)
            blob_errors = _delete_photo_blobs_if_present(safe_name)
            removed_any = True
            file_errors.extend(blob_errors)

        people_ids = []
        if metadata is not None:
            try:
                people_ids = json.loads(metadata.get('peopleIds', '[]') or '[]')
            except Exception:
                people_ids = []

            try:
                metadata_table_client.delete_entity(partition_key=user_id, row_key=safe_name)
                removed_any = True
            except Exception as exc:
                file_errors.append(f'metadata: {str(exc)}')
        elif not removed_any:
            errors.append(f'{filename}: Not found')
            continue

        try:
            _remove_faces_for_filename(user_id, safe_name)
        except Exception as exc:
            file_errors.append(f'faces: {str(exc)}')
        try:
            removed_jobs = _remove_job_rows_for_filename(user_id, safe_name)
            if removed_jobs:
                app.logger.info('Removed %s stale job row(s) for %s/%s', removed_jobs, user_id, safe_name)
        except Exception as exc:
            file_errors.append(f'jobs: {str(exc)}')
        try:
            _remove_filename_from_albums(user_id, safe_name)
        except Exception as exc:
            file_errors.append(f'albums: {str(exc)}')

        if people_ids and person_table_client is not None:
            try:
                all_rows = list(metadata_table_client.query_entities(f"PartitionKey eq '{_escape_odata(user_id)}'"))
            except Exception:
                all_rows = []
            for person_id in people_ids:
                try:
                    person_table_client.get_entity(partition_key=user_id, row_key=person_id)
                    continue
                except Exception:
                    pass

                for row in all_rows:
                    try:
                        pids = json.loads(row.get('peopleIds', '[]') or '[]')
                        if person_id not in pids:
                            continue
                        row['peopleIds'] = json.dumps([pid for pid in pids if pid != person_id])
                        metadata_table_client.upsert_entity(row)
                    except Exception:
                        pass

        if file_errors:
            errors.append(f'{filename}: {"; ".join(file_errors)}')
        elif removed_any:
            deleted.append(filename)
        else:
            errors.append(f'{filename}: Not found')

    return jsonify({'deleted': deleted, 'errors': errors, 'success': len(deleted) > 0})


@app.route('/albums/delete-multiple', methods=['POST'])
@app.route('/api/albums/delete-multiple', methods=['POST'])
def delete_multiple_albums_people():
    user_id, error = _require_user_id()
    if error:
        return error
    if not _albums_feature_available():
        return jsonify({'error': 'Albums/people features not configured'}), 503
    data = request.get_json(silent=True) or {}
    album_ids = data.get('albumIds', [])
    person_ids = data.get('personIds', [])
    if not isinstance(album_ids, list) or not isinstance(person_ids, list):
        return jsonify({'error': 'albumIds and personIds must be lists'}), 400

    deleted_albums = []
    album_errors = []
    deleted_persons = []
    person_errors = []
    updated_files = []

    for album_id in album_ids:
        try:
            albums_table_client.delete_entity(partition_key=user_id, row_key=str(album_id))
            deleted_albums.append(album_id)
        except Exception as exc:
            album_errors.append({'albumId': album_id, 'error': str(exc)})

    if person_ids:
        person_set = set(str(pid) for pid in person_ids)
        for pid in list(person_set):
            try:
                person_table_client.delete_entity(partition_key=user_id, row_key=pid)
                deleted_persons.append(pid)
            except Exception as exc:
                person_errors.append({'personId': pid, 'error': str(exc)})

        try:
            rows = list(metadata_table_client.query_entities(f"PartitionKey eq '{_escape_odata(user_id)}'"))
        except Exception:
            rows = []

        for row in rows:
            try:
                people_ids = json.loads(row.get('peopleIds', '[]') or '[]')
            except Exception:
                people_ids = []
            updated = [pid for pid in people_ids if pid not in person_set]
            if updated != people_ids:
                row['peopleIds'] = json.dumps(updated)
                try:
                    metadata_table_client.upsert_entity(row)
                    updated_files.append(row.get('RowKey'))
                except Exception:
                    pass

    return jsonify({
        'deletedAlbums': deleted_albums,
        'albumErrors': album_errors,
        'deletedPersonIds': deleted_persons,
        'personErrors': person_errors,
        'updatedFiles': updated_files,
        'success': len(album_errors) == 0 and len(person_errors) == 0,
    })


@app.route('/albums', methods=['GET'])
@app.route('/api/albums', methods=['GET'])
def list_albums():
    user_id, error = _require_user_id()
    if error:
        return error
    if not _albums_table_available():
        return jsonify({'error': 'Albums not configured'}), 503
    try:
        rows = list(albums_table_client.query_entities(f"PartitionKey eq '{_escape_odata(user_id)}'"))
    except Exception:
        rows = []
    albums = [_album_entity_to_payload(row) for row in rows]
    return jsonify({'albums': albums})


@app.route('/albums', methods=['POST'])
@app.route('/api/albums', methods=['POST'])
def create_album():
    user_id, error = _require_user_id()
    if error:
        return error
    if not _albums_table_available():
        return jsonify({'error': 'Albums not configured'}), 503
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'Album name is required'}), 400
    album_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    entity = {
        'PartitionKey': user_id,
        'RowKey': album_id,
        'name': name,
        'filenames': json.dumps([]),
        'createdAt': now,
        'updatedAt': now,
        'isPublic': False,
        'publicToken': '',
        'publicExpiresAt': '',
        'accessCode': '',
    }
    _save_album_entity(entity)
    return jsonify({'album': _album_entity_to_payload(entity)})


@app.route('/albums/<album_id>', methods=['GET'])
@app.route('/api/albums/<album_id>', methods=['GET'])
def get_album(album_id: str):
    user_id, error = _require_user_id()
    if error:
        return error
    if not _albums_table_available():
        return jsonify({'error': 'Albums not configured'}), 503
    entity = _load_album_entity(user_id, album_id)
    if not entity:
        return jsonify({'error': 'Album not found'}), 404
    payload = _album_entity_to_payload(entity)
    photos = _load_photos_for_filenames(user_id, payload.get('filenames', []))
    return jsonify({'album': payload, 'photos': photos})


@app.route('/albums/<album_id>/photos/add', methods=['POST'])
@app.route('/api/albums/<album_id>/photos/add', methods=['POST'])
def add_photos_to_album(album_id: str):
    user_id, error = _require_user_id()
    if error:
        return error
    if not _albums_table_available():
        return jsonify({'error': 'Albums not configured'}), 503
    entity = _load_album_entity(user_id, album_id)
    if not entity:
        return jsonify({'error': 'Album not found'}), 404
    data = request.get_json(silent=True) or {}
    filenames = data.get('filenames', [])
    if not isinstance(filenames, list):
        return jsonify({'error': 'filenames must be a list'}), 400

    current = set(_album_filenames(entity))
    added = []
    errors = []
    for filename in filenames:
        safe = secure_filename(str(filename))
        if not safe or not allowed_file(safe) or safe != filename:
            errors.append(f'{filename}: Invalid filename')
            continue
        if not _get_metadata_entity(user_id, safe):
            errors.append(f'{filename}: Not found')
            continue
        if safe not in current:
            current.add(safe)
            added.append(safe)

    entity['filenames'] = json.dumps(list(current))
    entity['updatedAt'] = datetime.now(timezone.utc).isoformat()
    _save_album_entity(entity)
    return jsonify({'success': True, 'added': added, 'errors': errors, 'album': _album_entity_to_payload(entity)})


@app.route('/albums/<album_id>/photos/remove', methods=['POST'])
@app.route('/api/albums/<album_id>/photos/remove', methods=['POST'])
def remove_photos_from_album(album_id: str):
    user_id, error = _require_user_id()
    if error:
        return error
    if not _albums_table_available():
        return jsonify({'error': 'Albums not configured'}), 503
    entity = _load_album_entity(user_id, album_id)
    if not entity:
        return jsonify({'error': 'Album not found'}), 404
    data = request.get_json(silent=True) or {}
    filenames = data.get('filenames', [])
    if not isinstance(filenames, list):
        return jsonify({'error': 'filenames must be a list'}), 400

    current = set(_album_filenames(entity))
    removed = []
    for filename in filenames:
        if filename in current:
            current.remove(filename)
            removed.append(filename)

    entity['filenames'] = json.dumps(list(current))
    entity['updatedAt'] = datetime.now(timezone.utc).isoformat()
    _save_album_entity(entity)
    return jsonify({'success': True, 'removed': removed, 'album': _album_entity_to_payload(entity)})


@app.route('/albums/<album_id>/rename', methods=['POST'])
@app.route('/api/albums/<album_id>/rename', methods=['POST'])
def rename_album(album_id: str):
    user_id, error = _require_user_id()
    if error:
        return error
    if not _albums_table_available():
        return jsonify({'error': 'Albums not configured'}), 503
    entity = _load_album_entity(user_id, album_id)
    if not entity:
        return jsonify({'error': 'Album not found'}), 404
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'Album name is required'}), 400
    entity['name'] = name
    entity['updatedAt'] = datetime.now(timezone.utc).isoformat()
    _save_album_entity(entity)
    return jsonify({'album': _album_entity_to_payload(entity)})


@app.route('/albums/<album_id>/delete', methods=['POST'])
@app.route('/api/albums/<album_id>/delete', methods=['POST'])
def delete_album(album_id: str):
    user_id, error = _require_user_id()
    if error:
        return error
    if not _albums_table_available():
        return jsonify({'error': 'Albums not configured'}), 503
    try:
        albums_table_client.delete_entity(partition_key=user_id, row_key=album_id)
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500
    return jsonify({'success': True})


@app.route('/albums/autocreate', methods=['POST'])
@app.route('/api/albums/autocreate', methods=['POST'])
def autocreate_albums():
    user_id, error = _require_user_id()
    if error:
        return error
    if not _albums_table_available():
        return jsonify({'error': 'Albums not configured'}), 503

    data = request.get_json(silent=True) or {}
    requested_rule = str(data.get('rule') or 'recent-upload').strip().lower()
    rule = SMART_ALBUM_RULES.get(requested_rule)
    if not rule:
        return jsonify({
            'error': 'Invalid smart album rule',
            'rules': sorted(set(SMART_ALBUM_RULES.values())),
        }), 400

    try:
        metadata_rows = _query_metadata_rows_for_user(user_id, purpose='albums.smart_create')
    except Exception as exc:
        return jsonify({'error': 'Unable to read photo metadata.', 'details': str(exc)}), 503

    try:
        existing_rows = list(albums_table_client.query_entities(f"PartitionKey eq '{_escape_odata(user_id)}'"))
    except Exception:
        existing_rows = []

    existing_names = {row.get('name') for row in existing_rows if row.get('name')}
    candidates = _smart_album_candidates(user_id, rule, metadata_rows)

    for candidate in candidates:
        name = candidate.get('name') or ''
        filenames = candidate.get('filenames') or []
        if name in existing_names:
            continue
        album_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        entity = {
            'PartitionKey': user_id,
            'RowKey': album_id,
            'name': name,
            'filenames': json.dumps(filenames),
            'createdAt': now,
            'updatedAt': now,
            'isPublic': False,
            'publicToken': '',
            'publicExpiresAt': '',
            'accessCode': '',
        }
        _save_album_entity(entity)
        payload = _album_entity_to_payload(entity)
        return jsonify({
            'count': 1,
            'rule': rule,
            'album': payload,
        })

    return jsonify({
        'count': 0,
        'rule': rule,
        'album': None,
        'message': 'No new matching smart album could be created for this rule.',
    })


@app.route('/albums/<album_id>/share', methods=['POST'])
@app.route('/api/albums/<album_id>/share', methods=['POST'])
def share_album(album_id: str):
    user_id, error = _require_user_id()
    if error:
        return error
    if not _albums_table_available():
        return jsonify({'error': 'Albums not configured'}), 503
    entity = _load_album_entity(user_id, album_id)
    if not entity:
        return jsonify({'error': 'Album not found'}), 404

    data = request.get_json(silent=True) or {}
    enabled = _coerce_bool(data.get('enabled', True))
    expires_in_days = int(data.get('expiresInDays', 0) or 0)
    access_code = (data.get('accessCode') or '').strip()
    clear_access_code = _coerce_bool(data.get('clearAccessCode', False))

    entity['isPublic'] = enabled
    if enabled and not entity.get('publicToken'):
        entity['publicToken'] = str(uuid.uuid4())
    if not enabled:
        entity['publicToken'] = ''

    if expires_in_days > 0:
        expires = datetime.now(timezone.utc) + timedelta(days=expires_in_days)
        entity['publicExpiresAt'] = expires.isoformat()
    else:
        entity['publicExpiresAt'] = ''

    if clear_access_code:
        entity['accessCode'] = ''
    elif access_code:
        entity['accessCode'] = access_code

    entity['updatedAt'] = datetime.now(timezone.utc).isoformat()
    _save_album_entity(entity)
    return jsonify({'album': _album_entity_to_payload(entity)})


@app.route('/albums/<album_id>/revoke', methods=['POST'])
@app.route('/api/albums/<album_id>/revoke', methods=['POST'])
def revoke_album_share(album_id: str):
    user_id, error = _require_user_id()
    if error:
        return error
    if not _albums_table_available():
        return jsonify({'error': 'Albums not configured'}), 503
    entity = _load_album_entity(user_id, album_id)
    if not entity:
        return jsonify({'error': 'Album not found'}), 404

    entity['isPublic'] = False
    entity['publicToken'] = ''
    entity['publicExpiresAt'] = ''
    entity['accessCode'] = ''
    entity['updatedAt'] = datetime.now(timezone.utc).isoformat()
    _save_album_entity(entity)
    return jsonify({'album': _album_entity_to_payload(entity)})


@app.route('/people/recluster', methods=['POST'])
@app.route('/api/people/recluster', methods=['POST'])
def recluster_people():
    try:
        user_id, error = _require_user_id()
        if error:
            return error
        if not _people_features_available():
            return jsonify({'error': 'People features not configured'}), 503
        data = request.get_json(silent=True) or {}
        if data.get('repair') is not True or data.get('confirm') != 'RECLUSTER_REPAIR':
            return jsonify({'error': 'repair confirmation required', 'code': 'protected_repair_required'}), 403
        queued = _enqueue_clustering_job(
            user_id,
            force=False,
            job_type='people_recluster',
            allow_reassign_confirmed=_coerce_bool(data.get('allowReassignConfirmed', False)),
        )
        response = _clustering_queue_response(queued)
        if queued.get('status') == 'unavailable':
            return jsonify(response), 503
        if queued.get('status') == 'failed':
            return jsonify(response), 500
        return jsonify(response)
    except Exception as exc:
        app.logger.exception('People recluster route failed')
        return jsonify({'error': 'People recluster failed', 'detail': str(exc)}), 500


@app.route('/api/people/assign-unclustered', methods=['POST'])
@app.route('/people/assign-unclustered', methods=['POST'])
def assign_unclustered_people():
    try:
        user_id, error = _require_user_id()
        if error:
            return error
        if not _people_features_available():
            return jsonify({'error': 'People features not configured'}), 503
        return jsonify(_assign_unclustered_faces(user_id))
    except Exception as exc:
        app.logger.exception('Assign unclustered faces route failed')
        return jsonify({'error': 'Assign unclustered faces failed', 'detail': str(exc)}), 500


@app.route('/api/admin/people/recluster', methods=['POST'])
@app.route('/admin/people/recluster', methods=['POST'])
def admin_recluster_people():
    try:
        user_id, error = _require_user_id()
        if error:
            return error
        if not _people_features_available():
            return jsonify({'error': 'People features not configured'}), 503
        data = request.get_json(silent=True) or {}
        if data.get('repair') is not True or data.get('confirm') != 'RECLUSTER_REPAIR':
            return jsonify({'error': 'repair confirmation required', 'code': 'protected_repair_required'}), 403
        queued = _enqueue_clustering_job(
            user_id,
            force=False,
            job_type='people_recluster',
            allow_reassign_confirmed=_coerce_bool(data.get('allowReassignConfirmed', False)),
        )
        response = _clustering_queue_response(queued)
        if queued.get('status') == 'unavailable':
            return jsonify(response), 503
        if queued.get('status') == 'failed':
            return jsonify(response), 500
        return jsonify(response)
    except Exception as exc:
        app.logger.exception('Admin people recluster route failed')
        return jsonify({'error': 'Admin people recluster failed', 'detail': str(exc)}), 500


@app.route('/api/admin/people/recluster/restore', methods=['POST'])
@app.route('/admin/people/recluster/restore', methods=['POST'])
def restore_people_recluster_snapshot():
    user_id, error = _require_user_id()
    if error:
        return error
    if not _people_features_available():
        return jsonify({'error': 'People features not configured'}), 503
    data = request.get_json(silent=True) or {}
    snapshot_id = str(data.get('snapshotId') or '').strip()
    if not snapshot_id:
        return jsonify({'error': 'snapshotId required'}), 400
    result = _restore_people_repair_snapshot(user_id, snapshot_id)
    if not result.get('success'):
        return jsonify(result), 404
    return jsonify(result)


@app.route('/api/admin/people/dedupe-faces', methods=['POST'])
@app.route('/admin/people/dedupe-faces', methods=['POST'])
def admin_dedupe_faces():
    user_id, error = _require_user_id()
    if error:
        return error
    if not _people_features_available():
        return jsonify({'error': 'People features not configured'}), 503
    data = request.get_json(silent=True) or {}
    if data.get('repair') is not True or data.get('confirm') != 'DEDUPE_FACES':
        return jsonify({'error': 'repair confirmation required', 'code': 'protected_repair_required'}), 403
    dry_run = _coerce_bool(data.get('dryRun', True))
    return jsonify(_dedupe_duplicate_faces(user_id, dry_run=dry_run))


@app.route('/api/admin/people/suppress-suspicious-faces', methods=['POST'])
@app.route('/admin/people/suppress-suspicious-faces', methods=['POST'])
def admin_suppress_suspicious_faces():
    user_id, error = _require_user_id()
    if error:
        return error
    if not _people_features_available():
        return jsonify({'error': 'People features not configured'}), 503
    data = request.get_json(silent=True) or {}
    if data.get('repair') is not True or data.get('confirm') != 'SUPPRESS_SUSPICIOUS_FACES':
        return jsonify({'error': 'repair confirmation required', 'code': 'protected_repair_required'}), 403
    dry_run = _coerce_bool(data.get('dryRun', True))
    return jsonify(_suppress_suspicious_faces(user_id, dry_run=dry_run))


@app.route('/api/admin/people/rebuild-photo-people-index', methods=['POST'])
@app.route('/admin/people/rebuild-photo-people-index', methods=['POST'])
def admin_rebuild_photo_people_index():
    user_id, error = _require_user_id()
    if error:
        return error
    if not _people_features_available():
        return jsonify({'error': 'People features not configured'}), 503
    data = request.get_json(silent=True) or {}
    if data.get('repair') is not True or data.get('confirm') != 'REBUILD_PEOPLE_INDEX':
        return jsonify({'error': 'repair confirmation required', 'code': 'protected_repair_required'}), 403
    dry_run = _coerce_bool(data.get('dryRun', True))
    return jsonify(_rebuild_photo_people_index(user_id, dry_run=dry_run))


@app.route('/api/admin/vector-index/rebuild', methods=['POST'])
@app.route('/admin/vector-index/rebuild', methods=['POST'])
def admin_rebuild_vector_index():
    try:
        user_id, error = _require_user_id()
        if error:
            return error
        data = request.get_json(silent=True) or {}
        if data.get('repair') is not True or data.get('confirm') != 'REBUILD_VECTOR_INDEX':
            return jsonify({'error': 'repair confirmation required', 'code': 'protected_repair_required'}), 403
        snapshot = refresh_user_vector_index(user_id)
        if snapshot is None:
            return jsonify({
                'status': 'empty',
                'userId': user_id,
                'rowCount': 0,
                'message': 'No face embeddings were available to rebuild a vector index.',
            })
        return jsonify({
            'status': 'rebuilt',
            'userId': user_id,
            'rowCount': len(snapshot.row_keys),
            'sourceVersion': snapshot.source_version,
            'embeddingVersion': snapshot.embedding_version,
            'updatedAt': snapshot.updated_at,
        })
    except Exception as exc:
        app.logger.exception('Admin vector index rebuild failed')
        return jsonify({'error': 'Admin vector index rebuild failed', 'detail': str(exc)}), 500


@app.route('/api/admin/people/repair-stale-memberships', methods=['POST'])
@app.route('/admin/people/repair-stale-memberships', methods=['POST'])
def admin_repair_stale_people_memberships():
    user_id, error = _require_user_id()
    if error:
        return error
    if not _people_features_available():
        return jsonify({'error': 'People features not configured'}), 503
    data = request.get_json(silent=True) or {}
    if data.get('repair') is not True or data.get('confirm') != 'REPAIR_STALE_MEMBERSHIPS':
        return jsonify({'error': 'repair confirmation required', 'code': 'protected_repair_required'}), 403
    dry_run = _coerce_bool(data.get('dryRun', True))
    return jsonify(_repair_face_memberships(user_id, dry_run=dry_run))


def _handle_clustering_queue_payload(payload: Dict, job_id: str, user_id: str, job_type: str) -> None:
    if not (user_id and job_type in _clustering_job_types() and _people_features_available()):
        return
    if job_id:
        _upsert_job_status(job_id, user_id, 'clustering', 'running')
    if job_type == 'people_cluster':
        eps, min_samples = _resolve_people_cluster_job_params(payload.get('eps', PEOPLE_CLUSTER_EPS), payload.get('minSamples', 2))
        result = cluster_user_faces(user_id, eps=eps, min_samples=min_samples)
        if job_id:
            if result.get('error'):
                _upsert_job_status(job_id, user_id, 'clustering', 'failed', error=str(result.get('error')), result=result)
            else:
                clusters = result.get('clusters') or {}
                stale_cleanup = _cleanup_stale_people_state(user_id)
                summary = {
                    'createdPeople': len(result.get('created', [])),
                    'clusterCount': len(clusters) if isinstance(clusters, dict) else 0,
                    'faceCount': sum(len(value) for value in clusters.values()) if isinstance(clusters, dict) else 0,
                    'stalePeopleRemoved': int(stale_cleanup.get('deletedEmptyPeople') or 0),
                    'staleReferencesRemoved': int(stale_cleanup.get('removedStaleReferences') or 0),
                    'orphanedOwnersCleared': int(stale_cleanup.get('orphanedFaceOwnersCleared') or 0),
                }
                _upsert_job_status(job_id, user_id, 'clustering', 'done', result=summary)
    elif job_type == 'people_recluster':
        plan = _build_people_recluster_plan(user_id, allow_reassign_confirmed=bool(payload.get('allowReassignConfirmed', False)))
        if plan.get('error'):
            if job_id:
                _upsert_job_status(job_id, user_id, 'clustering', 'failed', error=str(plan.get('error')), result=plan)
        else:
            apply_result = {'processed': 0, 'failed': 0}
            if plan.get('assignments') and plan.get('people'):
                apply_result = _apply_people_recluster_plan(user_id, plan)
            stale_cleanup = _cleanup_stale_people_state(user_id)
            if job_id:
                result_summary = {
                    'processed': int(apply_result.get('processed') or 0),
                    'failed': int(apply_result.get('failed') or 0),
                    'peopleAlbums': len(plan.get('created', [])),
                    'detectedFaces': len(plan.get('assignments', {})),
                    'candidateFaces': int(plan.get('candidateFaces') or 0),
                    'skippedConfirmedFaces': int(plan.get('skippedConfirmedFaces') or 0),
                    'stalePeopleRemoved': int(stale_cleanup.get('deletedEmptyPeople') or 0),
                    'staleReferencesRemoved': int(stale_cleanup.get('removedStaleReferences') or 0),
                    'orphanedOwnersCleared': int(stale_cleanup.get('orphanedFaceOwnersCleared') or 0),
                }
                if int(apply_result.get('failed') or 0) > 0:
                    _upsert_job_status(
                        job_id,
                        user_id,
                        'clustering',
                        'failed',
                        error='Failed to apply recluster plan',
                        result=result_summary,
                    )
                else:
                    _upsert_job_status(
                        job_id,
                        user_id,
                        'clustering',
                        'done',
                        result=result_summary,
                    )


def run_clustering_worker() -> None:
    """Poll clustering queue jobs in a standalone container."""
    logging.basicConfig(
        level=os.getenv('LOG_LEVEL', 'INFO').upper(),
        format='%(asctime)s %(levelname)s %(name)s %(message)s',
    )
    poll_seconds = float(os.getenv('CLUSTERING_WORKER_POLL_SECONDS', '2'))
    queue_service_client_local = queue_service_client
    if queue_service_client_local is None:
        _init_storage_clients()
        queue_service_client_local = queue_service_client
    if queue_service_client_local is None:
        raise RuntimeError('Queue service client unavailable')
    queue_client = queue_service_client_local.get_queue_client(CLUSTERING_QUEUE_NAME)
    for ensure_client in (queue_client,):
        try:
            ensure_client.create_queue()
        except Exception:
            pass
    worker_logger.info(
        'Worker polling queue %s every %ss',
        CLUSTERING_QUEUE_NAME,
        poll_seconds,
    )
    while True:
        processed_any = False
        try:
            messages = list(queue_client.receive_messages(messages_per_page=1))
            for message in messages:
                processed_any = True
                payload = {}
                job_id = ''
                user_id = ''
                job_type = ''
                try:
                    payload = json.loads(message.content or '{}')
                    if isinstance(payload, dict):
                        job_id = str(payload.get('jobId') or payload.get('correlationId') or '').strip()
                        user_id = str(payload.get('user_id') or payload.get('userId') or '').strip()
                        job_type = str(payload.get('type') or '').strip()
                        _handle_clustering_queue_payload(payload, job_id, user_id, job_type)
                except Exception as exc:
                    if job_id and user_id:
                        try:
                            _upsert_job_status(job_id, user_id, 'clustering', 'failed', error=str(exc))
                        except Exception:
                            pass
                    worker_logger.exception('Failed to process clustering queue message')
                finally:
                    try:
                        queue_client.delete_message(message)
                    except Exception:
                        worker_logger.exception('Failed to delete clustering queue message')
            if not processed_any:
                time.sleep(poll_seconds)
        except Exception:
            worker_logger.exception('Queue polling iteration failed')
            time.sleep(poll_seconds)


@app.route('/upload/processing/status', methods=['GET'])
@app.route('/upload/processing/status/', methods=['GET'])
@app.route('/api/upload/processing/status', methods=['GET'])
@app.route('/api/upload/processing/status/', methods=['GET'])
def processing_status():
    user_id, error = _require_user_id()
    if error:
        return error
    counts = _count_processing_statuses(user_id, ['thumbnail', 'exif', 'ocr', 'ai_vision', 'map_detection', 'face'])

    def _pending(summary: Dict[str, int]) -> int:
        return int(summary.get('queued', 0) or 0) + int(summary.get('pending', 0) or 0)

    def _build(key: str) -> Dict:
        summary = counts.get(key, {})
        return {
            'queued': int(summary.get('queued', 0) or 0),
            'pending': int(summary.get('pending', 0) or 0),
            'pendingTotal': _pending(summary),
            'running': int(summary.get('running', 0) or 0),
            'failed': int(summary.get('failed', 0) or 0),
            'noData': int(summary.get('no_data', 0) or 0),
        }

    response = jsonify({
        'generatedAt': datetime.now(timezone.utc).isoformat(),
        'thumbnail': _build('thumbnail'),
        'exif': _build('exif'),
        'ocr': _build('ocr'),
        'ai_vision': _build('ai_vision'),
        'map_detection': _build('map_detection'),
        'face': _build('face'),
    })
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

@app.route('/public/albums/<token>', methods=['GET'])
@app.route('/public/albums/<token>', methods=['POST'])
@app.route('/api/public/albums/<token>', methods=['GET'])
@app.route('/api/public/albums/<token>', methods=['POST'])
def public_album(token: str):
    entity = _find_public_album_by_token(token)
    if not entity:
        return jsonify({'error': 'Album not found'}), 404
    if not _coerce_bool(entity.get('isPublic', False)):
        return jsonify({'error': 'Album not public'}), 404
    if _album_is_expired(entity):
        return jsonify({'error': 'Album expired'}), 404

    access_code = _album_access_code(entity)
    provided = ''
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        provided = (data.get('accessCode') or '').strip()

    if access_code and not (
        (provided and hmac.compare_digest(access_code, provided))
        or _album_grant_valid(entity, token)
    ):
        return jsonify({'codeRequired': True, 'retryAfterSeconds': 0}), 401

    filenames = _album_filenames(entity)
    owner_id = str(entity.get('PartitionKey') or '')
    photos = []
    for name in filenames:
        urls = _public_photo_urls(token, name)
        metadata = _get_metadata_entity(owner_id, name) if owner_id else {}
        photos.append({
            'filename': name,
            'url': urls['url'],
            'thumbnailUrl': urls['thumbnailUrl'],
            'previewUrl': urls.get('previewUrl') or '',
            'rotation': _normalize_rotation((metadata or {}).get('rotation', 0)),
        })

    resp = make_response(jsonify({
        'album': {
            'name': entity.get('name', ''),
            'photoCount': len(filenames),
        },
        'photos': photos,
    }))
    # Issue a signed grant so the browser can subsequently load the (code-protected)
    # media, which are fetched as <img src> and cannot carry the access code themselves.
    if access_code:
        resp.set_cookie(
            _album_grant_cookie_name(token),
            _sign_album_grant(token, access_code),
            httponly=True,
            secure=request.is_secure,
            samesite='Lax',
            max_age=60 * 60 * 6,
            path='/',
        )
    return resp


@app.route('/public/photos/<token>/thumbnail/<path:filename>', methods=['GET'])
def public_thumbnail(token: str, filename: str):
    entity = _find_public_album_by_token(token)
    if not entity or not _coerce_bool(entity.get('isPublic', False)) or _album_is_expired(entity):
        return jsonify({'error': 'Not found'}), 404
    if not _album_grant_valid(entity, token):
        return jsonify({'error': 'Not found'}), 404
    safe_name = secure_filename(filename)
    if not safe_name or not allowed_file(safe_name) or safe_name != filename:
        return jsonify({'error': 'Invalid filename'}), 400
    if safe_name not in _album_filenames(entity):
        return jsonify({'error': 'Not found'}), 404

    if safe_name.lower().endswith(tuple(ext.lower() for ext in RAW_EXTENSIONS_RAWPY | RAW_EXTENSIONS_CINEMA)):
        return public_preview(token, filename)
    if not blob_service_client:
        return jsonify({'error': 'Thumbnail service not configured'}), 503

    try:
        props = get_media_properties('thumbnail', safe_name)
        content_type = props.get('content_type') or 'image/jpeg'
        data = download_media_bytes('thumbnail', safe_name)
        resp = Response(data, mimetype=content_type)
        resp.headers['Cache-Control'] = 'public, max-age=3600'
        return resp
    except Exception as exc:
        print(f"Unexpected error serving public thumbnail for {safe_name}: {str(exc)}", flush=True)
        return jsonify({'error': 'Thumbnail not found'}), 404


@app.route('/public/photos/<token>/image/<path:filename>', methods=['GET'])
def public_image(token: str, filename: str):
    entity = _find_public_album_by_token(token)
    if not entity or not _coerce_bool(entity.get('isPublic', False)) or _album_is_expired(entity):
        return jsonify({'error': 'Not found'}), 404
    if not _album_grant_valid(entity, token):
        return jsonify({'error': 'Not found'}), 404
    safe_name = secure_filename(filename)
    if not safe_name or not allowed_file(safe_name) or safe_name != filename:
        return jsonify({'error': 'Invalid filename'}), 400
    if safe_name not in _album_filenames(entity):
        return jsonify({'error': 'Not found'}), 404

    try:
        try:
            props = get_media_properties('image', safe_name)
            content_type = props.get('content_type') or 'image/jpeg'
        except Exception:
            content_type = 'image/jpeg'
        data = download_media_bytes('image', safe_name)
        resp = Response(data, mimetype=content_type)
        resp.headers['Cache-Control'] = 'public, max-age=3600'
        return resp
    except Exception as exc:
        if _is_missing_media_error(exc):
            return jsonify({'error': 'File not found in storage'}), 404
        app.logger.exception('Failed to serve public image for %s', safe_name)
        return jsonify({'error': 'Failed to retrieve image'}), 500


@app.route('/public/photos/<token>/preview/<path:filename>', methods=['GET'])
def public_preview(token: str, filename: str):
    entity = _find_public_album_by_token(token)
    if not entity or not _coerce_bool(entity.get('isPublic', False)) or _album_is_expired(entity):
        return jsonify({'error': 'Not found'}), 404
    if not _album_grant_valid(entity, token):
        return jsonify({'error': 'Not found'}), 404
    safe_name = secure_filename(filename)
    if not safe_name or not allowed_file(safe_name) or safe_name != filename:
        return jsonify({'error': 'Invalid filename'}), 400
    if safe_name not in _album_filenames(entity):
        return jsonify({'error': 'Not found'}), 404

    try:
        image_bytes = download_media_bytes('image', safe_name)
        preview_bytes = convert_image_to_jpeg(image_bytes, safe_name)
        if not preview_bytes:
            return jsonify({'error': 'Preview not available'}), 404
        if _filename_requires_backend_preview(safe_name) and not _looks_like_jpeg(preview_bytes):
            return jsonify({'error': 'Preview not available'}), 404
        resp = Response(preview_bytes, mimetype='image/jpeg')
        resp.headers['Cache-Control'] = 'public, max-age=3600'
        return resp
    except Exception as exc:
        if _is_missing_media_error(exc):
            return jsonify({'error': 'File not found in storage'}), 404
        app.logger.exception('Failed to create public preview for %s', safe_name)
        return jsonify({'error': 'Failed to create preview'}), 503


@app.route('/public/albums/<token>/download', methods=['POST'])
def public_album_download(token: str):
    entity = _find_public_album_by_token(token)
    if not entity or not _coerce_bool(entity.get('isPublic', False)) or _album_is_expired(entity):
        return jsonify({'error': 'Not found'}), 404

    access_code = _album_access_code(entity)
    if access_code:
        data_for_auth = request.form.to_dict(flat=True) if request.form else (request.get_json(silent=True) or {})
        provided = (data_for_auth.get('accessCode') or '').strip()
        if not (
            (provided and hmac.compare_digest(access_code, provided))
            or _album_grant_valid(entity, token)
        ):
            return jsonify({'codeRequired': True, 'retryAfterSeconds': 0}), 401

    data = request.form.to_dict(flat=True) if request.form else (request.get_json(silent=True) or {})
    raw_filenames = data.get('filenames', [])
    filenames: List[str]
    if isinstance(raw_filenames, str) and raw_filenames.strip():
        try:
            parsed = json.loads(raw_filenames)
            filenames = [str(item) for item in parsed if isinstance(item, (str, int, float))]
        except Exception:
            filenames = [item.strip() for item in raw_filenames.split(',') if item.strip()]
    elif isinstance(raw_filenames, list):
        filenames = [str(item) for item in raw_filenames]
    else:
        filenames = _album_filenames(entity)

    album_filenames = set(_album_filenames(entity))
    selected = [name for name in filenames if name in album_filenames]
    if not selected:
        selected = _album_filenames(entity)

    zip_buffer = io.BytesIO()
    written_count = 0
    with zipfile.ZipFile(zip_buffer, 'w', compression=zipfile.ZIP_DEFLATED) as zip_file:
        for name in selected:
            try:
                data_bytes = download_media_bytes('image', name)
                zip_file.writestr(name, data_bytes)
                written_count += 1
            except Exception as exc:
                print(f"Skipping {name} while creating public album download: {str(exc)}", flush=True)

    if written_count == 0:
        return jsonify({'error': 'No files could be downloaded'}), 404

    zip_buffer.seek(0)
    resp = Response(zip_buffer.read(), mimetype='application/zip')
    resp.headers['Content-Disposition'] = f'attachment; filename=public-album-{token}.zip'
    resp.headers['Cache-Control'] = 'no-store'
    return resp


@app.route('/photos/<filename>/rating', methods=['POST'])
@app.route('/photos/<filename>/rating/', methods=['POST'])
@app.route('/api/photos/<filename>/rating', methods=['POST'])
@app.route('/api/photos/<filename>/rating/', methods=['POST'])
def set_photo_rating(filename: str):
    user_id, error = _require_user_id()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    rating = data.get('rating', 0)

    if not isinstance(rating, int) or rating < 0 or rating > 5:
        return jsonify({'error': 'Rating must be between 0 and 5'}), 400

    try:
        safe_name = secure_filename(filename)
        if not safe_name or not allowed_file(safe_name) or safe_name != filename:
            return jsonify({'error': 'Invalid filename'}), 400
        metadata = _get_metadata_entity(user_id, safe_name)
        if not metadata:
            return jsonify({'error': 'Not found'}), 404
        _update_metadata_entity_fields(user_id, safe_name, {'rating': rating})
        return jsonify({'success': True, 'filename': filename, 'rating': rating})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/photos/<filename>/like', methods=['POST'])
@app.route('/photos/<filename>/like/', methods=['POST'])
@app.route('/api/photos/<filename>/like', methods=['POST'])
@app.route('/api/photos/<filename>/like/', methods=['POST'])
def toggle_like_photo(filename: str):
    user_id, error = _require_user_id()
    if error:
        return error

    try:
        safe_name = secure_filename(filename)
        if not safe_name or not allowed_file(safe_name) or safe_name != filename:
            return jsonify({'error': 'Invalid filename'}), 400
        metadata = _get_metadata_entity(user_id, safe_name)
        if not metadata:
            return jsonify({'error': 'Not found'}), 404
        liked_by = json.loads(metadata.get('likedBy', '[]'))

        if user_id in liked_by:
            liked_by.remove(user_id)
        else:
            liked_by.append(user_id)

        _update_metadata_entity_fields(user_id, safe_name, {
            'likes': len(liked_by),
            'likedBy': json.dumps(liked_by),
        })

        return jsonify({
            'success': True,
            'filename': filename,
            'likes': metadata['likes'],
            'liked': user_id in liked_by,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/photos/<filename>/rotation', methods=['POST'])
@app.route('/photos/<filename>/rotation/', methods=['POST'])
@app.route('/api/photos/<filename>/rotation', methods=['POST'])
@app.route('/api/photos/<filename>/rotation/', methods=['POST'])
def set_photo_rotation(filename: str):
    user_id, error = _require_user_id()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    rotation = _normalize_rotation(data.get('rotation', 0))

    try:
        safe_name = secure_filename(filename)
        if not safe_name or not allowed_file(safe_name) or safe_name != filename:
            return jsonify({'error': 'Invalid filename'}), 400
        metadata = _get_metadata_entity(user_id, safe_name)
        if not metadata:
            return jsonify({'error': 'Not found'}), 404
        previous_rotation = _normalize_rotation(metadata.get('rotation', 0))
        updates = {'rotation': rotation}
        if rotation != previous_rotation:
            updates['thumbnail_status'] = 'pending'
        _update_metadata_entity_fields(user_id, safe_name, {
            **updates,
        })
        return jsonify({'success': True, 'filename': filename, 'rotation': rotation})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/photos/<filename>/metadata', methods=['GET'])
@app.route('/photos/<filename>/metadata/', methods=['GET'])
@app.route('/api/photos/<filename>/metadata', methods=['GET'])
@app.route('/api/photos/<filename>/metadata/', methods=['GET'])
def get_photo_metadata(filename: str):
    user_id, error = _require_user_id()
    if error:
        return error

    try:
        safe_name = secure_filename(filename)
        if not safe_name or not allowed_file(safe_name) or safe_name != filename:
            return jsonify({'error': 'Invalid filename'}), 400
        metadata = _get_metadata_entity(user_id, safe_name)
        if not metadata:
            return jsonify({'error': 'Not found'}), 404
        liked_by = json.loads(metadata.get('likedBy', '[]'))
        exif_data = parse_exif_data(metadata.get('exifData', '{}'))
        resolution = _resolution_from_exif(exif_data)
        if not resolution['width'] or not resolution['height']:
            # Not every camera/re-encoder writes EXIF dimension tags. This
            # endpoint is only called once per photo when the info panel is
            # opened (not in bulk listing), so a lazy header-only image read
            # is an acceptable fallback cost here where it wouldn't be in the
            # main photo list endpoint.
            try:
                image_bytes = download_media_bytes('image', filename)
                with Image.open(io.BytesIO(image_bytes)) as img:
                    resolution = {'width': img.width, 'height': img.height}
            except Exception:
                pass
        return jsonify({
            'filename': filename,
            'rating': metadata.get('rating', 0),
            'likes': metadata.get('likes', 0),
            'liked': user_id in liked_by,
            'tags': json.loads(metadata.get('tags', '[]')),
            'rotation': _normalize_rotation(metadata.get('rotation', 0)),
            'objects': parse_json_list(metadata.get('objects', '[]')),
            'ocrText': metadata.get('ocrText', ''),
            'caption': metadata.get('caption', ''),
            'exifData': exif_data,
            'exifSummary': exif_summary(exif_data) if exif_data else {},
            'resolution': resolution,
            'faces': json.loads(metadata.get('faces', '[]') or '[]'),
            'faceCount': metadata.get('faceCount', 0),
            'peopleIds': json.loads(metadata.get('peopleIds', '[]') or '[]'),
            'location': _location_from_metadata(metadata, exif_data),
            'uploadDate': metadata.get('uploadDate'),
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/photos/filter', methods=['GET'])
@app.route('/photos/filter/', methods=['GET'])
@app.route('/api/photos/filter', methods=['GET'])
@app.route('/api/photos/filter/', methods=['GET'])
def filter_photos():
    user_id, error = _require_user_id()
    if error:
        return error

    try:
        min_rating = int(request.args.get('minRating', 0))
        min_likes = int(request.args.get('minLikes', 0))
        latitude = request.args.get('latitude', '')
        longitude = request.args.get('longitude', '')
        radius_km = float(request.args.get('radius', 0))
        offset = int(request.args.get('offset', 0))
        limit = int(request.args.get('limit', 24))
    except ValueError:
        return jsonify({'error': 'Invalid filter parameters'}), 400

    capture_start = _parse_capture_filter(request.args.get('captureStart', '') or '')
    capture_end = _parse_capture_filter(request.args.get('captureEnd', '') or '')

    try:
        all_photos = _query_metadata_rows_for_user(user_id, purpose='photos.filter')
    except Exception as exc:
        return jsonify({'error': 'Unable to read photo metadata.', 'details': str(exc)}), 503

    try:
        filtered = []

        for photo in all_photos:
            if photo.get('rating', 0) < min_rating:
                continue
            if photo.get('likes', 0) < min_likes:
                continue

            if capture_start or capture_end:
                if not _capture_in_range(photo, capture_start, capture_end):
                    continue

            if latitude and longitude:
                try:
                    photo_lat = float(photo.get('latitude', 0))
                    photo_lon = float(photo.get('longitude', 0))
                    user_lat = float(latitude)
                    user_lon = float(longitude)
                    distance = ((photo_lat - user_lat) ** 2 + (photo_lon - user_lon) ** 2) ** 0.5
                    if distance > radius_km * 0.01:
                        continue
                except Exception:
                    pass

            filtered.append(photo)

        filtered.sort(key=lambda p: (-p.get('rating', 0), -p.get('likes', 0)))
        selected = filtered[offset:offset + limit]
        photos = []

        for photo in selected:
            name = photo['RowKey']
            photos.append(_build_photo_summary(user_id, name, photo, include_props=True))

        return jsonify({'photos': photos, 'total': len(filtered), 'offset': offset, 'limit': limit})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# Guard other optional startup helpers to avoid import-time failures
for _fn in ('create_blob_containers', 'create_metadata_table', 'create_albums_table', 'create_face_table', 'create_person_table', 'create_merge_table'):
    if _fn in globals() and callable(globals().get(_fn)):
        try:
            globals().get(_fn)()
        except Exception:
            # Ignore errors during optional startup actions
            pass
