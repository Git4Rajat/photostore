import base64
import hashlib
import hmac
import html
import io
import json
import os
import random
import re
import secrets
import signal
import tempfile
import zipfile
import time
import logging
import threading
try:
    import resource  # POSIX-only; ipworker always runs in a Linux container
except ImportError:
    resource = None
import unicodedata
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import quote as _urlquote, urlparse

from azure.core import MatchConditions
from azure.core.exceptions import AzureError, ResourceExistsError, ResourceModifiedError
from azure.data.tables import TableServiceClient, UpdateMode
from azure.identity import DefaultAzureCredential
from azure.storage.blob import (
    BlobSasPermissions, BlobServiceClient, ContainerSasPermissions, UserDelegationKey,
    generate_blob_sas, generate_container_sas,
)
from azure.storage.queue import QueueServiceClient
from flask import Flask, Response, jsonify, make_response, request, stream_with_context
from auth_utils import get_request_user_id as resolve_request_user_id
from auth_utils import validate_bearer_token as validate_entra_bearer_token
import password_auth
import email_utils
import library_utils
from ordering_utils import (
    order_photo_entries,
    metadata_capture_datetime,
    metadata_upload_datetime,
    epoch_millis_to_iso,
)
from timeline_metadata import build_timeline_summary
from image_utils import (
    BROWSER_UNVIEWABLE_EXTENSIONS,
    RAW_EXTENSIONS_CINEMA,
    RAW_EXTENSIONS_RAWPY,
    allowed_file,
    convert_image_to_jpeg,
    create_placeholder_thumbnail,
    extract_raw_native_preview_bytes,
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
    upload_file_to_blob,
    download_file_from_blob,
    finalize_uploaded_file,
    get_media_properties,
    claim_processing_lease,
    heartbeat_processing_lease,
    PhotoNotFoundError,
    reset_received_ranges,
    reset_upload_tracking_and_reserve_blob,
    reset_upload_tracking_and_reserve_blobs_batch,
    release_processing_lease,
    update_processing_status,
    upload_media_file,
    prime_available_vector_indexes,
    refresh_user_vector_index,
    get_vector_index_manifest_summary,
    get_vector_index_blob_location,
    invalidate_user_vector_index_cache,
    delete_user_vector_index_data,
    touch_user_search_indexes_state,
    metadata_updates_affect_search_indexes,
    get_user_lexical_index,
    get_lexical_index_blob_location,
    invalidate_user_lexical_index_cache,
    delete_user_lexical_index_data,
    get_user_tag_embedding_index,
    delete_user_tag_embedding_index_data,
    nearest_tags_for_word,
    vector_search_candidates,
    reserve_pending_anonymous_blob,
    read_pending_anonymous_blob,
    resolve_physical_blob_name,
    original_filename_for_anonymous_id,
    invalidate_image_names_cache,
    delete_image_name_mapping,
    delete_hash_index_entry,
    delete_filename_owner_entry,
    list_known_file_hashes,
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
# run_clustering_worker/run_ipworker each call this themselves, but the main
# "backend" role (gunicorn running this Flask app directly) never did --
# Flask's default app.logger has no handler/level configured until something
# sets one up, so every app.logger.info(...) call (including the per-request
# phase-timing logs on the three upload endpoints) was being silently
# dropped in production. WARNING/ERROR calls still appeared because of
# Werkzeug/gunicorn's own default handling, which masked the gap until a
# 2026-08-30 Log Analytics query came back with zero "timings" lines despite
# a live, active upload. Matches run_clustering_worker/run_ipworker's exact
# LOG_LEVEL env var pattern.
#
# force=True because a first attempt at this fix (without it) still produced
# zero INFO lines live: gunicorn configures its own logging before importing
# the WSGI app, which leaves the root logger already holding handlers by the
# time this module-level call runs -- plain basicConfig() silently no-ops
# whenever the root logger already has any handler, regardless of level.
# force=True tears those down and installs this configuration instead.
logging.basicConfig(
    level=os.getenv('LOG_LEVEL', 'INFO').upper(),
    format='%(asctime)s %(levelname)s %(name)s %(message)s',
    force=True,
)
# The azure-sdk HTTP pipeline logs a full request/response dump (URL, every
# header, one log line each) at INFO on every single blob/table/queue call --
# previously invisible because the root logger defaulted to WARNING, so this
# fix's INFO level silently turned it on everywhere (backend/worker/ipworker
# all share this module). A job doing thousands of SDK calls back-to-back
# (e.g. _execute_library_download's per-file blob downloads and per-block ZIP
# part uploads) was found live generating tens of thousands of these lines in
# minutes -- real, unrelated resource pressure on top of the job's own work.
# Setting the level on the shared 'azure' parent logger (not each per-role
# basicConfig call above/below) survives every basicConfig(force=True) call
# in this module, since force=True only resets the root logger's handlers,
# not another logger's already-set level.
logging.getLogger('azure').setLevel(logging.WARNING)
app = Flask(__name__)
# Belt-and-suspenders alongside the basicConfig(force=True) above: Flask's
# own app.logger can carry an independent level/handler (attached lazily by
# Flask/Werkzeug) that would otherwise keep filtering out INFO regardless of
# the root logger's configuration.
app.logger.setLevel(os.getenv('LOG_LEVEL', 'INFO').upper())
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
    # Calibrated against the real browser-hybrid-arcface-faceapi-v2 similarity
    # distribution (see backend/scripts/calibrate_face_thresholds.py). On real
    # data, the SAME person across photos sits at cosine sim ~0.65-0.95, while
    # DIFFERENT people (two faces in one photo) top out around ~0.59. So a link
    # threshold in the 0.68-0.78 band (eps = 1 - sim, i.e. distance 0.22-0.32)
    # separates them. ``eps`` gathers DBSCAN candidates; ``absolute_max_pair_distance``
    # is the complete-linkage ceiling that then splits any chained cluster so a
    # bigger eps can never fuse two identities. The old 0.03/0.99 values were
    # tuned for the v1 double-normalization bug (unrelated faces scored ~0.98)
    # and left every face in its own singleton once v2 fixed the embeddings.
    presets = {
        'strictest': {
            # Prefer false negatives: only very confident same-person links.
            'eps': 0.24,
            'absolute_max_pair_distance': 0.22,
            'match_threshold': 0.80,
            'match_margin': 0.08,
            'assign_threshold': 0.80,
            'assign_margin': 0.08,
        },
        'strict': {
            # Favor false negatives over false merges.
            'eps': 0.28,
            'absolute_max_pair_distance': 0.26,
            'match_threshold': 0.76,
            'match_margin': 0.07,
            'assign_threshold': 0.78,
            'assign_margin': 0.07,
        },
        'balanced': {
            'eps': 0.32,
            'absolute_max_pair_distance': 0.30,
            'match_threshold': 0.72,
            'match_margin': 0.06,
            'assign_threshold': 0.74,
            'assign_margin': 0.06,
        },
        'loose': {
            'eps': 0.38,
            'absolute_max_pair_distance': 0.36,
            'match_threshold': 0.66,
            'match_margin': 0.05,
            'assign_threshold': 0.68,
            'assign_margin': 0.05,
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
# Separate, looser DBSCAN epsilon for landmark-2pt-tier faces (extreme head
# poses where 5-point alignment was measured to actively hurt the embedding --
# see faceAlignment history). Calibrated directly against real data: 3 real
# confirmed-same-person 2pt embeddings scored cosine distance 0.45-0.49 from
# each other, while 2 different-person comparisons scored 0.88+ -- a wide,
# safe gap. 0.60 sits comfortably in the middle. This is intentionally NOT
# comparable to PEOPLE_CLUSTER_EPS (which is calibrated for 5-point's own,
# much tighter distance range) -- the two tiers are clustered in separate
# DBSCAN passes specifically because cross-tier distances were measured to be
# unreliable (same person, 5pt-vs-2pt, ranged 0.44-0.91 -- indistinguishable
# from noise), so they must never be compared directly against one eps.
PEOPLE_CLUSTER_EPS_2PT = float(os.getenv('PEOPLE_CLUSTER_EPS_2PT', '0.60'))
# DBSCAN epsilon for landmark-5pt-mp (ipworker's MediaPipe-aligned tier --
# same AdaFace weights as landmark-5pt, different landmark source; see
# ipwork_face.py). Deliberately its OWN tier, not merged into
# PEOPLE_CLUSTER_EPS, because real calibration data showed messier separation
# than the browser's own tier. Calibrated twice against real photos (via
# backend/scripts/calibrate_ipworker_face_tier.py's sibling analysis):
#   - first pass: 4 people, 9 photos -- same-person 0.53-0.98 cosine
#     similarity, different-person up to 0.80 (3 of the 4 people happened to
#     look visually similar: young men, dark hair, beards).
#   - second pass, broadened specifically to de-risk the first pass's small/
#     skewed sample: 7 people, 13 photos (added a woman and more head-angle
#     variety, including one full-profile shot MediaPipe couldn't align at
#     all -- see landmark-2pt-mp note below). Same-person 8 pairs: min 0.53,
#     median 0.80, max 0.98. Different-person 83 pairs: p95 0.75, max 0.80.
#     The 0.18 eps below (needs >=0.82 similarity) clears BOTH passes' worst
#     different-person pair with margin, while still auto-clustering the
#     easy/burst-shot same-person pairs (4 of 8 in the second pass).
# No single eps cleanly separates hard same-person pairs from the hardest
# different-person pairs in either sample. Set conservatively tight so this
# tier starts by under-clustering (same person split across singletons --
# safe, user can manually merge) never over-clustering (different people
# wrongly merged -- unsafe, hard to notice) -- matching this codebase's
# existing bias for automatic merges (see MIN_AUTO_FACE_MERGE_SIMILARITY).
# Alignment crops were visually inspected and looked correctly centered/
# upright, so the overlap reflects genuine embedding-space demographic
# similarity in a still-small sample, not a pipeline bug.
# Revisit with calibrate_face_thresholds.py (filtering on this tier's distinct
# modelTaxonomyVersion, FACE_EMBEDDING_MODEL_TAXONOMY_VERSION in
# ipwork_face.py) once enough real landmark-5pt-mp faces accumulate to
# calibrate from a larger, more representative sample than 7 people.
PEOPLE_CLUSTER_EPS_MP = float(os.getenv('PEOPLE_CLUSTER_EPS_MP', '0.18'))

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
# Public-share-link index: PartitionKey=publicToken, RowKey='owner' -> (userId,
# albumId), so a public share view is an O(1) point read instead of an
# unscoped `publicToken eq '...'` scan of every account's albums.
ALBUM_TOKEN_INDEX_TABLE = os.getenv('ALBUM_TOKEN_INDEX_TABLE', 'photoalbumtokens')
PEOPLE_TABLE = os.getenv('PEOPLE_TABLE', 'photopeople')
FACE_TABLE = os.getenv('FACE_TABLE', 'photofaces')
MERGE_TABLE = os.getenv('MERGE_TABLE', 'personmerges')
# Job status/progress rows: PartitionKey=userId (or libraryId for
# library_clean/library_download, which any member of a shared library must
# be able to check regardless of who started the job), RowKey=jobId. Used to
# live as PartitionKey='jobs' inside METADATA_TABLE -- one shared partition
# across every user, which forced every "is there an active job" check to
# scan and Python-filter the whole thing (213k+ rows, 17-33s/call). See
# _upsert_job_status.
JOBS_TABLE = os.getenv('JOBS_TABLE', 'photojobs')
# One row per user-triggered Workbench processing run (action, steps, scope,
# filename count/list, timestamp) -- durable history, modeled on MERGE_TABLE.
WORKBENCH_ACTIONS_TABLE = os.getenv('WORKBENCH_ACTIONS_TABLE', 'workbenchactions')
# Image-name anonymization: maps opaque UUID blob names <-> original filenames,
# partitioned per library. See storage_utils anonymization helpers.
IMAGE_NAMES_TABLE = os.getenv('IMAGE_NAMES_TABLE', 'photoimagenames')
# Upload dedup index: PartitionKey=library_id, RowKey=fileHash -> filename, for an
# O(1) exact-duplicate lookup on every finalize instead of a per-partition scan.
HASH_INDEX_TABLE = os.getenv('HASH_INDEX_TABLE', 'photofilehashes')
# Cross-tenant filename-collision index: PartitionKey=filename, RowKey=library_id,
# so /upload/finalize can check "does any OTHER library already own this filename"
# without scanning the entire metadata table.
FILENAME_OWNERS_TABLE = os.getenv('FILENAME_OWNERS_TABLE', 'photofilenameowners')
# Multi-tenant library sharing (accounts, libraries, memberships, invites, audit).
USERS_TABLE = os.getenv('USERS_TABLE', 'photousers')
LIBRARIES_TABLE = os.getenv('LIBRARIES_TABLE', 'photolibraries')
MEMBERSHIPS_TABLE = os.getenv('MEMBERSHIPS_TABLE', 'photomemberships')
INVITES_TABLE = os.getenv('INVITES_TABLE', 'photoinvites')
AUDIT_TABLE = os.getenv('AUDIT_TABLE', 'photoaudit')
CLEAN_REQUESTS_TABLE = os.getenv('CLEAN_REQUESTS_TABLE', 'photolibraryclean')
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
# Holds "download entire library" ZIP exports. Blobs are library- and
# part-scoped (see _execute_library_download/_library_export_part_blob_name),
# overwritten on each re-export so this container never accumulates more than
# the current run's parts per library (stale extra parts from a shrinking
# export are swept by _cleanup_stale_library_export_parts).
BLOB_EXPORTS_CONTAINER = os.getenv('BLOB_EXPORTS_CONTAINER', 'library-exports').strip()
# Person-merge undo payloads (base + merged person snapshots, faceMap): these
# can run large enough to threaten Table Storage's 64KB-per-property /
# 1MB-per-entity caps, so they live here instead of inline on the
# personmerges row -- see _write_merge_record.
BLOB_MERGE_PAYLOADS_CONTAINER = os.getenv('BLOB_MERGE_PAYLOADS_CONTAINER', 'merge-payloads').strip()
# Each export "part" ZIP is capped at roughly this many bytes (measured from
# each photo's actual downloaded size as it's added) before it's closed and
# uploaded and a new part is started. Keeps very large libraries from
# producing one impractically large ZIP, bounds peak temp-disk usage to one
# part at a time, and gives natural progress checkpoints.
LIBRARY_EXPORT_PART_MAX_BYTES = int(os.getenv('LIBRARY_EXPORT_PART_MAX_BYTES', str(2 * 1024 ** 3)))
# How many photo downloads _execute_library_download runs concurrently.
# Downloading is pure network I/O wait, so overlapping several at once
# (rather than one full blob round-trip at a time) is a large, low-risk
# throughput win -- results are still consumed in strict row order (see
# _execute_library_download), so this changes nothing about ordering,
# part boundaries, or resumability, only how much wall-clock time each
# batch of downloads actually takes.
LIBRARY_EXPORT_DOWNLOAD_CONCURRENCY = int(os.getenv('LIBRARY_EXPORT_DOWNLOAD_CONCURRENCY', '8'))
# delete_multiple_photos does several point-reads/deletes (metadata, blobs,
# hash/filename-owner index rows) per file, previously run one file at a time
# -- pure network I/O wait each time, same shape as the library-export
# downloads above, so overlapping them is the same low-risk win.
DELETE_IO_CONCURRENCY = int(os.getenv('DELETE_IO_CONCURRENCY', '16'))
# The azure-core SDK's default requests-based transport caps its underlying
# urllib3 connection pool at 10 per host. That's invisible under sequential
# per-file calls, but DELETE_IO_CONCURRENCY (and any other concurrent callers
# sharing these same process-wide client singletons, e.g. library export's
# LIBRARY_EXPORT_DOWNLOAD_CONCURRENCY) can easily have more than 10 requests
# in flight to the same storage account at once. Once the pool is full,
# urllib3 doesn't queue -- it silently opens a new, unpooled connection and
# discards it after the response ("Connection pool is full, discarding
# connection" in the logs), paying a fresh TCP+TLS handshake on every such
# call instead of reusing a warm one. Confirmed live: this made the
# parallelized delete slower per-file than the sequential version it
# replaced. Sized comfortably above the largest concurrency user in this
# process so pooling stays effective under concurrent requests too.
STORAGE_CONNECTION_POOL_MAXSIZE = int(os.getenv('STORAGE_CONNECTION_POOL_MAXSIZE', '64'))
# 'sas' hands the browser day-stable read SAS URLs pointing straight at blob
# storage so media bytes never stream through this container; 'proxy' serves
# every byte through the backend. 'sas' silently degrades to proxy URLs when
# minting is impossible (no AAD credential, e.g. Azurite/local dev).
MEDIA_URL_MODE = os.getenv('MEDIA_URL_MODE', 'sas').strip().lower()
BLOB_VECTOR_INDEX_CONTAINER = os.getenv('BLOB_VECTOR_INDEX_CONTAINER', 'vector-index').strip()
BLOB_LEXICAL_INDEX_CONTAINER = os.getenv('BLOB_LEXICAL_INDEX_CONTAINER', 'lexical-index').strip()
VECTOR_INDEX_PRIME_ON_STARTUP = os.getenv('VECTOR_INDEX_PRIME_ON_STARTUP', 'false').lower() in ('1', 'true', 'yes')
VECTOR_INDEX_PRIME_MAX_USERS = max(0, int(os.getenv('VECTOR_INDEX_PRIME_MAX_USERS', '200')))
SEMANTIC_SEARCH_ALLOW_QUERYTIME_ROW_EMBEDDINGS = os.getenv(
    'SEMANTIC_SEARCH_ALLOW_QUERYTIME_ROW_EMBEDDINGS',
    'false',
).lower() in ('1', 'true', 'yes')

# Feature toggles
MAPS_ENABLED = os.getenv('MAPS_ENABLED', 'true').lower() in ('1', 'true', 'yes')
MAPS_ON_UPLOAD = os.getenv('MAPS_ON_UPLOAD', 'false').lower() in ('1', 'true', 'yes')
MAPS_QUEUE_ON_UPLOAD = os.getenv('MAPS_QUEUE_ON_UPLOAD', 'true').lower() in ('1', 'true', 'yes')
# 'browser' (default): only the client runs OCR/face/vision/geo, matching today's
# behavior. 'backend': the client skips AI entirely and every upload is queued for
# the ipworker container to process server-side. 'both': the client attempts AI
# locally AND the upload is queued for ipworker; whichever result lands first for
# a given step wins (see _step_locked_done in storage_utils.py) and the loser is
# discarded. Deploy-time only -- see the `processingMode` bicep parameter.
PROCESSING_MODE = os.getenv('PROCESSING_MODE', 'browser').strip().lower()
if PROCESSING_MODE not in ('browser', 'backend', 'both'):
    PROCESSING_MODE = 'browser'
# Derived from PROCESSING_MODE rather than a second independent flag, so the two
# can't drift out of sync the way hand-edited bicep literals have before.
BROWSER_ONLY_PROCESSING = PROCESSING_MODE == 'browser'
CLUSTERING_QUEUE_NAME = os.getenv('CLUSTERING_QUEUE_NAME', 'photostore-clustering')
# receive_messages() with no visibility_timeout defaults to Azure's 30s -- a
# full DBSCAN pass over a large library (or a multi-hour library_download
# export) can exceed that, making Azure redeliver the same message before
# run_clustering_worker's finally-block delete runs, causing duplicate
# processing. This used to be a large fixed value (1800s) justified by
# "only one consumer, so this just prevents self-redelivery" -- but live
# maxReplicas is actually in the hundreds (KEDA queueLength-based scaling),
# and a fixed long lease meant any scale-down that killed a replica
# mid-job stranded its message, completely dead, for up to the rest of
# that 1800s window regardless of how much work remained (confirmed live:
# one scale-down cost ~12 minutes of zero progress on a library_download
# export). Now short + actively renewed instead: run_clustering_worker
# renews the lease via QueueClient.update_message() every
# CLUSTERING_WORKER_LEASE_RENEWAL_SECONDS while a message is being
# processed, so a healthy worker's message never actually expires no
# matter how long the job runs, while a killed worker's message becomes
# reclaimable in at most this many seconds instead of up to 1800.
CLUSTERING_WORKER_VISIBILITY_TIMEOUT_SECONDS = int(os.getenv('CLUSTERING_WORKER_VISIBILITY_TIMEOUT_SECONDS', '120'))
CLUSTERING_WORKER_LEASE_RENEWAL_SECONDS = int(os.getenv('CLUSTERING_WORKER_LEASE_RENEWAL_SECONDS', '40'))
# Active lease renewal (above) means a healthy replica never loses a message
# mid-job -- but it can't help a message whose processing crashes the whole
# replica every time it's attempted (a "poison" job: a payload that reliably
# OOMs or hard-kills the process before any exception handler runs), or one
# that's redelivered over and over across many separate replica
# restarts/redeploys for unrelated reasons. Without a ceiling, Azure just
# keeps redelivering such a message forever, each attempt burning a full
# replica's worth of compute. Confirmed live 2026-09-03: a batch of
# duplicate clustering jobs (see the atomic-claim fix on
# _clustering_maintenance_due above) kept getting redelivered across
# unrelated restarts for hours. message.dequeue_count is Azure's own
# per-message attempt counter (already used below for IPWORK_LEASE_RETRY_LIMIT);
# once it exceeds this, the message is dropped and its job marked 'failed'
# instead of retried again -- the user can retry manually from the UI if the
# job is still wanted.
CLUSTERING_WORKER_MAX_RETRIES = int(os.getenv('CLUSTERING_WORKER_MAX_RETRIES', '5'))
# library_clean/library_download used to share the clustering queue with every
# auto-triggered clustering job (people_cluster, recluster, propagate...) --
# a user-initiated destructive wipe had no priority over routine backfill
# traffic and could sit FIFO behind it. Their own queue means the worker
# checks for one before ever touching the general clustering backlog (see
# run_clustering_worker), so a clean/download request gets picked up within
# one poll cycle regardless of how deep the clustering queue is.
LIBRARY_OPS_QUEUE_NAME = os.getenv('LIBRARY_OPS_QUEUE_NAME', 'photostore-library-ops')
# _execute_library_clean (and _execute_library_download) are naturally
# idempotent/resumable -- they walk-and-delete/export whatever's still there,
# so redelivering the same message is always safe, unlike a generic
# clustering job. CLUSTERING_WORKER_MAX_RETRIES's 5-strike ceiling exists to
# stop a genuinely poisoned message from retrying forever, but on this queue
# most redeliveries come from worker restarts/redeploys/KEDA scale-down
# SIGTERMs killing an in-flight job (see clustering-worker-sigterm-job-loss),
# not from the clean logic itself failing -- a slow clean can plausibly
# survive more than 5 of those across a busy deploy day. Give it a much
# higher ceiling instead of none, so a truly poisoned library (every attempt
# throws immediately) still eventually stops instead of burning compute
# forever.
LIBRARY_CLEAN_MAX_RETRIES = int(os.getenv('LIBRARY_CLEAN_MAX_RETRIES', '30'))
IPWORKER_QUEUE_NAME = os.getenv('IPWORKER_QUEUE_NAME', 'photostore-ipwork')
# ipworker's job: thumbnail, exif, ocr, geo (map_detection), vision (ai_vision),
# face -- the full set the browser can do client-side. Thumbnail used to be a
# permanent browser-only exception (cheap canvas resize, no model needed, so
# there was "nothing for ipworker to take over") until it became clear that
# reasoning only covers the fresh-upload case: reprocessing/backfill still
# needs a live browser tab to download each photo and redo it, defeating the
# point of 'backend' mode for unattended bulk reprocessing. ipwork_thumbnail.py
# reuses the same PIL/rawpy/ffmpeg path as storage_utils's existing reactive
# server-side thumbnail fallback, so this needed no new ipworker-only deps.
IPWORK_STEPS = ('preview', 'thumbnail', 'exif', 'ocr', 'face', 'ai_vision', 'map_detection')
# How long ipworker holds the per-photo processing lease while it works.
# Generous relative to the browser's 120s because a single ipworker pass runs
# every step server-side inference in sequence (face + OCR + vision + geo)
# rather than one step at a time.
IPWORKER_LEASE_SECONDS = int(os.getenv('IPWORKER_LEASE_SECONDS', '300'))
# How many times ipworker will let a queue message be redelivered (via Azure
# Queue's own visibility timeout) while it keeps losing the per-photo lease
# race to another owner, before giving up and deleting the message. This is
# what lets a photo whose browser tab closed mid-processing still get
# finished by ipworker on its own -- see _handle_ipwork_queue_payload. Each
# retry costs one IPWORKER_VISIBILITY_TIMEOUT_SECONDS wait (below), so keep
# this small -- the browser's own lease (CLIENT_PROCESSING_LEASE_SECONDS,
# 120s) has long since expired by the first retry if the browser really did
# abandon the photo, so more than a couple of retries mainly extends the
# worst-case abandon window (limit x visibility timeout) without actually
# improving the odds of success.
IPWORK_LEASE_RETRY_LIMIT = int(os.getenv('IPWORK_LEASE_RETRY_LIMIT', '3'))
# Same bug class as CLUSTERING_WORKER_VISIBILITY_TIMEOUT_SECONDS above: with no
# visibility_timeout, receive_messages() defaults to Azure's 30s, and one
# ipwork pass (download + YOLO face detection + MediaPipe landmarks + AdaFace
# embedding + CLIP tagging + tesseract OCR, run in sequence) can plausibly
# exceed that on a larger image -- Azure would then redeliver the same
# message to a second replica while the first is still working it, and
# because the redelivered copy carries the same jobId (so the same
# processing-lease owner string), claim_processing_lease's ownership check
# doesn't block the second attempt: two replicas can genuinely run the full
# model pipeline concurrently on one photo. Unlike the clustering worker this
# can't just use a very long window -- ipworker runs maxReplicas=4, so a
# window much longer than one photo's worst-case processing time would delay
# recovery if a replica crashes mid-job while other replicas sit idle.
# Matches IPWORKER_LEASE_SECONDS, the same "how long is one photo allowed to
# take" budget already used for the app-level lease.
IPWORKER_VISIBILITY_TIMEOUT_SECONDS = int(os.getenv('IPWORKER_VISIBILITY_TIMEOUT_SECONDS', '300'))
# Ceiling on total redeliveries for one message regardless of outcome --
# distinct from IPWORK_LEASE_RETRY_LIMIT above, which only bounds the
# lease_busy case. A message whose processing reliably crashes the whole
# replica (e.g. a corrupt/poison image -- see the tesserocr in-process
# migration's blast-radius note) never reaches _process_ipwork_message's own
# except block, so it has no chance to mark itself 'failed' and stop being
# redelivered; without this it would retry forever, one full replica-worth of
# compute per attempt. Checked against the same message.dequeue_count Azure
# already tracks. Same 5-retry default as CLUSTERING_WORKER_MAX_RETRIES.
IPWORKER_MAX_RETRIES = int(os.getenv('IPWORKER_MAX_RETRIES', '5'))
# Bounded worker-thread pool inside a single ipworker replica -- lets one
# replica process several photos' synchronous I/O (blob download/upload,
# table reads/writes, the geocode HTTP call) concurrently instead of one
# photo at a time, without raising replica count or size. Defaults to 1
# (today's exact sequential behavior); raise only after benchmarking --
# see the ipworker intra-replica concurrency plan for the gated rollout
# (Azure Monitor showed real CPU headroom, ~45% avg per replica, but
# memory was already the tighter constraint at ~66-77% peak at
# concurrency=1, so this isn't guessed higher without measurement).
IPWORKER_CONCURRENCY = max(1, int(os.getenv('IPWORKER_CONCURRENCY', '1')))
# How long run_ipworker's SIGTERM handler waits for in-flight messages to
# finish (and their queue messages to be deleted) before force-exiting. Azure
# Container Apps' default terminationGracePeriodSeconds is 30s -- a replica
# that hasn't exited by then gets SIGKILLed with no further chance to clean
# up, so this must stay comfortably under 30 to leave margin for the exit
# itself. Without this, a replica killed while holding an already-completed
# message (result written, just not yet deleted from the queue) orphans that
# message: it sits invisible until IPWORKER_VISIBILITY_TIMEOUT_SECONDS
# elapses, then gets redelivered and reprocessed forever, since scale-down
# during a backlog drain (KEDA shrinking replica count as visible messages
# drop) sends SIGTERM constantly, not just on deploys.
IPWORKER_SHUTDOWN_GRACE_SECONDS = max(1, int(os.getenv('IPWORKER_SHUTDOWN_GRACE_SECONDS', '25')))
LIBRARY_CLEAN_MAX_IN_PROGRESS_SECONDS = max(60, int(os.getenv('LIBRARY_CLEAN_MAX_IN_PROGRESS_SECONDS', '14400')))
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

# Hard floor for automatic face merges into existing people/clusters. This is a
# safety clamp against a misconfigured-too-loose override, NOT the operating
# point (the preset thresholds above are). It was 0.98 — which silently clamped
# every auto path (match/assign/propagate) up to 0.98 and, once v2 embeddings
# made genuine same-person pairs score ~0.65-0.95, blocked all automatic merges.
MIN_AUTO_FACE_MERGE_SIMILARITY = float(os.getenv('MIN_AUTO_FACE_MERGE_SIMILARITY', '0.60'))

# Keep person matching conservative so clustering does not collapse distinct faces into one cluster.
PEOPLE_MATCH_THRESHOLD = max(float(_PEOPLE_CLUSTER_CONFIG['match_threshold']), MIN_AUTO_FACE_MERGE_SIMILARITY)
PEOPLE_MATCH_MARGIN = float(_PEOPLE_CLUSTER_CONFIG['match_margin'])
PEOPLE_CLUSTER_ASSIGN_THRESHOLD = max(float(_PEOPLE_CLUSTER_CONFIG['assign_threshold']), MIN_AUTO_FACE_MERGE_SIMILARITY)
PEOPLE_CLUSTER_ASSIGN_MARGIN = float(_PEOPLE_CLUSTER_CONFIG['assign_margin'])
# Hard floor for merge suggestions shown to users. Suggestions are user-reviewed
# (not auto-applied), so this can sit a touch below the auto-merge floor to
# surface plausible same-person candidates for confirmation.
MIN_PEOPLE_SUGGEST_THRESHOLD = float(os.getenv('MIN_PEOPLE_SUGGEST_THRESHOLD', '0.62'))
PEOPLE_SUGGEST_THRESHOLD = max(float(os.getenv('PEOPLE_SUGGEST_THRESHOLD', '0.70')), MIN_PEOPLE_SUGGEST_THRESHOLD)
PEOPLE_SUGGEST_LIMIT = int(os.getenv('PEOPLE_SUGGEST_LIMIT', '20'))
PEOPLE_SUGGEST_PER_PERSON = int(os.getenv('PEOPLE_SUGGEST_PER_PERSON', '2'))
# Suggestion quality guardrails: only trusted clusters participate in merge
# suggestions to avoid obvious non-face false positives (e.g. flowers) from
# polluting representative embeddings.
PEOPLE_SUGGEST_INCLUDE_UNNAMED = os.getenv('PEOPLE_SUGGEST_INCLUDE_UNNAMED', 'false').lower() in ('1', 'true', 'yes')
PEOPLE_SUGGEST_MIN_FACES = int(os.getenv('PEOPLE_SUGGEST_MIN_FACES', '2'))
PEOPLE_SUGGEST_MIN_CONFIRMED_FACES = int(os.getenv('PEOPLE_SUGGEST_MIN_CONFIRMED_FACES', '1'))
PEOPLE_SUGGEST_MIN_REP_FACE_CONFIDENCE = float(os.getenv('PEOPLE_SUGGEST_MIN_REP_FACE_CONFIDENCE', '0.85'))

# Identity propagation: once a person cluster is named/merged, use its learned
# representative embedding to pull that person's faces out of *unnamed* clusters.
# These thresholds are intentionally looser than the strict base-clustering
# match threshold (which stays high to avoid false merges at detection time),
# because a named person's confirmed rep is a much stronger, user-vetted anchor.
# ``AUTO`` faces are moved in automatically; faces between ``REVIEW`` and
# ``AUTO`` are surfaced as a per-face review queue for manual accept/decline.
PEOPLE_PROPAGATE_AUTO_THRESHOLD = max(
    float(os.getenv('PEOPLE_PROPAGATE_AUTO_THRESHOLD', '0.74')),
    MIN_AUTO_FACE_MERGE_SIMILARITY,
)
PEOPLE_PROPAGATE_REVIEW_THRESHOLD = float(os.getenv('PEOPLE_PROPAGATE_REVIEW_THRESHOLD', '0.62'))
# A candidate face must beat its best match to any *other* named person by this
# margin before it is auto-assigned, so faces ambiguous between two known people
# are never silently moved.
PEOPLE_PROPAGATE_MARGIN = float(os.getenv('PEOPLE_PROPAGATE_MARGIN', '0.05'))
# Require the target person to have at least this many active faces so a single
# stray face cannot become an over-eager magnet for the whole library.
PEOPLE_PROPAGATE_MIN_FACES = int(os.getenv('PEOPLE_PROPAGATE_MIN_FACES', '2'))
PEOPLE_PROPAGATE_MAX_SUGGESTIONS = int(os.getenv('PEOPLE_PROPAGATE_MAX_SUGGESTIONS', '60'))
# Identity propagation scans the whole face table. Materialising every row (each
# carries an inline ~512-dim embedding) at once spiked RSS enough to OOM-kill the
# replica on a large library. Stream the scan and score the embeddings in bounded
# chunks so peak memory is one batch, not the entire table.
PEOPLE_PROPAGATE_SCAN_BATCH = int(os.getenv('PEOPLE_PROPAGATE_SCAN_BATCH', '1024'))
SUSPICIOUS_FACE_CONFIDENCE = float(os.getenv('SUSPICIOUS_FACE_CONFIDENCE', '0.60'))
FACE_MIN_STORE_CONFIDENCE = float(os.getenv('FACE_MIN_STORE_CONFIDENCE', '0.24'))
FACE_LOW_CONFIDENCE_REJECT_BELOW = float(os.getenv('FACE_LOW_CONFIDENCE_REJECT_BELOW', '0.32'))
FACE_LOW_CONFIDENCE_MAX_AREA_RATIO = float(os.getenv('FACE_LOW_CONFIDENCE_MAX_AREA_RATIO', '0.08'))
FACE_LOW_CONFIDENCE_MAX_SIDE_RATIO = float(os.getenv('FACE_LOW_CONFIDENCE_MAX_SIDE_RATIO', '0.42'))
FACE_CLUSTER_EMBEDDING_VERSION = (
    os.getenv('FACE_CLUSTER_EMBEDDING_VERSION')
    # v3 drops the 128-d face-api descriptor that was concatenated onto
    # ArcFace's 512-d output (it diluted ArcFace's own signal) and feeds
    # ArcFace a 5-point-landmark-aligned crop instead of a plain padded box.
    # See backend/scripts/ for the calibration behind this change: cross-day
    # same-person similarity was landing right at the different-people
    # ceiling under the old unaligned hybrid embedding.
    # -guarded: same v3 model/alignment pipeline; cropFaceCanvas now rejects a
    # geometrically-implausible 5-point solve (bad landmarks producing a
    # garbage transform) and falls back instead of trusting it blindly, and
    # each face is tagged with which alignment tier it got.
    # -diag: real-world testing showed alignmentMethod='none' on 100% of
    # faces post-guard, with zero visibility into why, because
    # detectFaceLandmarks silently swallowed its own errors. That catch is
    # gone now and detectFiveFaceLandmarks records the real reason into
    # alignmentFailureReason.
    # -fixed: -diag caught the real cause via alignmentFailureReason:
    # "faceapi_model_load_failed: No backend found in registry." — tfjs
    # backends only self-register via importing '@tensorflow/tfjs-backend-cpu'
    # as a side effect, and loadFaceApiSession called tf.setBackend('cpu')
    # without ever doing that import, silently relying on an unrelated
    # initialization path (the browser-AI tagging feature) to have already
    # done it. Fixed by importing it directly.
    # -fixed2: -fixed got past that error but hit a new one one layer deeper,
    # again via alignmentFailureReason: "e.toFloat is not a function".
    # face-api.js's bundled code (built against tfjs-core@1.7.0) calls legacy
    # convenience cast methods removed from the app's deduped tfjs-core@4.22.0
    # (only .cast(dtype) remains). Added a one-time compat shim restoring
    # toFloat/toInt/toBool as thin .cast() wrappers (faceApiRuntime.ts).
    # -fixed3: -fixed2's shim only covered casts; the very next call in the
    # same chain hit "e.as4D is not a function". tfjs-core@4.22.0 actually
    # removed essentially ALL chainable Tensor op methods (257 of them), not
    # just casts. faceApiRuntime.ts now generically restores every tf.<op> as
    # an instance method forwarding to its top-level call, plus explicit
    # mappings for the few with no same-named top-level equivalent.
    # -fixed4: -fixed3 was verified against a real production crop before
    # shipping, yet the real deploy still hit a 3rd error: "Size(136) must
    # match the product of shape" (shape stringified to '' -- it was []).
    # Root cause, isolated directly: as1D() is called with ZERO arguments in
    # legacy usage ("flatten to 1D, infer the size"), unlike as2D..as5D which
    # always take explicit dims. The generic shim forwarded the empty args
    # array straight to tf.reshape(this, []), targeting a scalar instead.
    # Fixed by special-casing as1D to reshape to [this.size].
    # -fixed5: -fixed4 finally got real landmarks end-to-end (no more
    # crashes), but 24/25 faces landed on alignmentMethod='landmark-2pt' --
    # the ARC_FACE_MIN_SCALE/MAX_SCALE guard bounds (frontend
    # PhotoGallery.tsx) were copied from the old 2-point path's different
    # crop convention and rejected essentially every real 5-point solve.
    # Recalibrated to 0.03-0.6 after confirming real solved scales (0.08-
    # 0.25) against 6 production faces.
    # -fixed6: cross-photo testing on real confirmed-same-person faces
    # showed the 2-point eye-only fallback actively hurts matches -- mixing
    # it with 5-point-aligned embeddings in the same clustering pool scored
    # near-zero similarity for genuinely identical people, purely from the
    # alignment-tier mismatch. Removed the 2-point fallback from the browser
    # pipeline; _face_embedding_allowed_for_clustering now also requires
    # alignmentMethod == 'landmark-5pt' -- one embedding quality tier in the
    # matching pool, not several silently mixed together.
    # -fixed7: -fixed6's alignment guard (ARC_FACE_MAX_SCALE=0.6) was too
    # tight -- a confirmed-real, downward-tilted face measured 0.68 and was
    # wrongly rejected. Raised to 0.9.
    # -fixed8: -fixed7 was reverted. Real ArcFace embedding testing (actual
    # model inference, not just checking the transform's numbers) proved
    # 5-point alignment for that same case scored only 0.19-0.28 same-person
    # similarity -- worse than plain (0.56-0.68) or 2-point (0.51-0.55).
    # MAX_SCALE reverted to 0.6; the 2-point fallback is restored as a real,
    # separate quality tier. _face_embedding_allowed_for_clustering now
    # accepts both landmark-5pt and landmark-2pt, and
    # _build_people_recluster_plan clusters each tier in its own DBSCAN pass
    # (PEOPLE_CLUSTER_EPS_2PT for 2pt) -- cross-tier comparisons were
    # measured unreliable (0.09-0.56, indistinguishable from noise) so the
    # two tiers are never compared directly.
    # -adaface1: swapped the embedding model itself (ArcFace resnet100 ->
    # AdaFace IR-101/WebFace4M) -- even -fixed8's tier-aware clustering can't
    # fix a case where the pose gap is real rather than an alignment
    # artifact (a confirmed same-person pair at genuinely different
    # head-turn scored only 0.29 on ArcFace's best tier). Head-to-head on
    # identical crops: AdaFace scored 0.68 on that pair vs ArcFace's 0.31,
    # and 0.82-0.85 on a moderate-pose trio vs 0.56-0.67, while an easy
    # frontal pair stayed near ceiling for both (0.89 vs 0.86) -- gain is
    # concentrated in hard-pose cases. Unlike every prior bump on this
    # constant, this is a different model producing a different embedding
    # space, not a different alignment/guard behavior on the same one --
    # AdaFace and ArcFace vectors are both 512-d (so no dimension-mismatch
    # guard would catch mixing them) but are NOT comparable by cosine
    # distance. _face_embedding_allowed_versions() deliberately does NOT
    # carry any ArcFace-family version forward this time (see there).
    # -adaface1-fixed: -adaface1's browser rollout never actually took effect
    # -- the model URL used at inference time is injected at container start
    # from window.__APP_CONFIG__.arcFaceModelUrl (docker-entrypoint.sh),
    # which fell back to the OLD arcface path because
    # APP_CONFIG_ARC_FACE_MODEL_URL was never pinned in resources.bicep. The
    # browser kept loading the old ArcFace model the whole time; confirmed
    # directly by re-fetching "re-embedded" faces post-deploy and finding
    # cosine similarities bit-identical (4 decimals, 4 independent pairs) to
    # their pre-swap values. Re-bumping rather than just fixing the config
    # because every face already carries the -adaface1 label, so the
    # staleness check alone would never trigger a real re-embed.
    or 'browser-adaface-ir101-v1-fixed'
).strip()
FACE_CLUSTER_EMBEDDING_DIMENSIONS = int(os.getenv('FACE_CLUSTER_EMBEDDING_DIMENSIONS', '512'))
FACE_CLUSTER_LEGACY_EMBEDDING_DIMENSIONS = 512
# ipworker's embeddingVersion string (must match FACE_EMBEDDING_MODEL_TAXONOMY_VERSION
# in ipwork_face.py verbatim -- duplicated here rather than imported because
# ipwork_face.py pulls in onnxruntime/opencv/mediapipe, which the plain
# backend/worker roles must not import). Unlike the ArcFace->AdaFace jump
# above, this IS the same AdaFace model/weights as FACE_CLUSTER_EMBEDDING_VERSION
# -- only the landmark source differs (MediaPipe vs face-api.js) -- so it's
# safe to allow into the same clustering pool; the alignment-tier split
# ('landmark-5pt-mp' in PEOPLE_CLUSTER_ALIGNMENT_TIERS) is what keeps it from
# ever being compared directly against browser-computed distances.
IPWORKER_FACE_CLUSTER_EMBEDDING_VERSION = os.getenv(
    'IPWORKER_FACE_CLUSTER_EMBEDDING_VERSION',
    'adaface-ir101-webface4m-512d-v1+mediapipe-landmark-478',
).strip()
# The v1->v2->...->fixed8 ArcFace-era legacy version constants that used to
# live here (each kept temporarily in _face_embedding_allowed_versions() so
# faces on the previous version kept clustering among themselves during a
# re-embed sweep) are gone as of adaface1: that whole chain was one model
# (ArcFace) evolving its alignment/guard behavior, so carrying the previous
# version forward was safe. adaface1 is a different model entirely -- see
# the comment on FACE_CLUSTER_EMBEDDING_VERSION above and on
# _face_embedding_allowed_versions() below for why none of them carry over
# this time. Full history of each prior version is preserved in that same
# comment block.
# When true, a photo whose faces were embedded under an older embedding version
# is re-queued for browser face processing so its embeddings get recomputed
# under the current model. This is what makes an embedding-version bump
# self-healing across an existing library (e.g. the v1 -> v2 -> v3 fixes). Set
# to false to freeze re-embedding (e.g. to stagger a large reprocessing wave).
FACE_REEMBED_STALE_VERSION = os.getenv('FACE_REEMBED_STALE_VERSION', 'true').lower() in ('1', 'true', 'yes')


# adaface1 breaks the "carry the previous version forward" pattern every
# bump above followed: those were all the same ArcFace model with a
# different alignment/guard behavior, so an older version's embeddings were
# still meaningfully comparable (just a different quality tier). AdaFace is
# a different model producing a different 512-d embedding space -- same
# dimension as ArcFace (so the dim-mismatch guard in
# _build_people_recluster_plan would NOT catch mixing them), but cosine
# distance between an ArcFace vector and an AdaFace vector is meaningless,
# not just lower-quality. None of the ArcFace-family legacy versions below
# are included in the allowed set for that reason -- every existing face
# needs a real re-embed under adaface1, the same self-healing sweep
# (FACE_REEMBED_STALE_VERSION) used for every prior bump, not a pass-through.
def _face_embedding_allowed_versions() -> set:
    return {
        version
        for version in {
            FACE_CLUSTER_EMBEDDING_VERSION,
            IPWORKER_FACE_CLUSTER_EMBEDDING_VERSION,
        }
        if version
    }
PHOTO_TABLE_SCAN_PAGE_SIZE = int(os.getenv('PHOTO_TABLE_SCAN_PAGE_SIZE', '1000'))
PHOTO_TABLE_SCAN_MAX_ROWS = int(os.getenv('PHOTO_TABLE_SCAN_MAX_ROWS', '250000'))
# Max legacy rows to stamp with a derived uploadDate per /photos request; the
# backfill converges over a few loads without slowing any single one down much.
UPLOAD_DATE_BACKFILL_MAX_PER_REQUEST = int(os.getenv('UPLOAD_DATE_BACKFILL_MAX_PER_REQUEST', '100'))
# Max legacy rows to stamp with a persisted blob size per /photos request. New
# uploads get their size stamped at finalize; this converges pre-existing rows so
# the gallery stops doing a per-photo blob HEAD (24 serial round trips per page).
PHOTO_PROPS_BACKFILL_MAX_PER_REQUEST = int(os.getenv('PHOTO_PROPS_BACKFILL_MAX_PER_REQUEST', '12'))

# Module-level storage/credential defaults (set during startup if available)
account_name = None
credential = None
metadata_table_client = None
blob_service_client = None
albums_table_client = None
face_table_client = None
person_table_client = None
merge_table_client = None
jobs_table_client = None
album_token_index_table_client = None
workbench_actions_table_client = None
image_names_table_client = None
hash_index_table_client = None
filename_owners_table_client = None
config_table_client = None
users_table_client = None
libraries_table_client = None
memberships_table_client = None
invites_table_client = None
audit_table_client = None
clean_requests_table_client = None
library_store = None
clustering_queue_client = None
queue_service_client = None
ipwork_queue_client = None
library_ops_queue_client = None


class _UserScanCache:
    """Short-TTL cache + per-user coalescing for a full per-user partition scan.

    Several listing endpoints each need the user's entire partition from a
    given table. Without this, back-to-back or concurrent calls (e.g. a page
    load that hits /photos, /photos/search-adjacent people lookups, and the
    People page in quick succession) each re-scan the same partition from
    Azure Table Storage, and concurrent requests pile up doing duplicate
    scans instead of sharing one. The first caller performs the scan while
    others for the same user wait on a lock and reuse the result; writes
    invalidate the entry, and the TTL bounds staleness for anything
    invalidation misses.

    Cache-invalidation decision for service splits (2026-09-15): this cache
    and _InvalidatingTableClient's write-triggered invalidation are both
    strictly per-process. That gap is NOT new as of the `tools` service
    split (see backend-cpu-optimization-2026-09 memory) -- it already exists
    today across sibling replicas of one service (backend's own maxReplicas
    can be >1; a write landing on replica 2 never invalidates replica 1's
    copy of this same dict), bounded only by this TTL, and that has been the
    accepted behavior all along. Splitting a route group into its own
    container app is the same risk at a different granularity, not a new
    one -- deliberately NOT adding a shared cache (e.g. Redis) or a
    cross-process invalidation event (e.g. over an existing queue) for this
    reason. Revisit only if a real correctness complaint ties back to this
    TTL window specifically (nothing has, across however long multi-replica
    scale-out has already been live).
    """

    def __init__(self, ttl_seconds: float):
        self._ttl = ttl_seconds
        self._cache: Dict[str, Tuple[float, List[Dict]]] = {}
        self._locks: Dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def _lock_for(self, key: str) -> threading.Lock:
        with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock

    def _fresh(self, key: str) -> Optional[List[Dict]]:
        with self._guard:
            entry = self._cache.get(key)
        if entry and entry[0] > time.monotonic():
            return [dict(row) for row in entry[1]]
        return None

    def get(self, key: str, fetch_fn: Callable[[], List[Dict]]) -> List[Dict]:
        cached = self._fresh(key)
        if cached is not None:
            return cached
        lock = self._lock_for(key)
        with lock:
            # Re-check after acquiring: another request may have scanned while we waited.
            cached = self._fresh(key)
            if cached is not None:
                return cached
            rows = fetch_fn()
            if self._ttl > 0:
                with self._guard:
                    self._cache[key] = (time.monotonic() + self._ttl, [dict(row) for row in rows])
            return rows

    def invalidate(self, key: str) -> None:
        with self._guard:
            self._cache.pop(key, None)

    def set(self, key: str, rows: List[Dict]) -> None:
        """Install a fresh, already-known-correct value directly, bypassing
        fetch_fn. Used by hot paths that just wrote the underlying data and
        already have the resulting state in memory (see
        _assign_faces_to_people_incrementally) -- avoids the write triggering
        _InvalidatingTableClient's invalidate() only to have the very next
        call rebuild via a full Table Storage scan for data the caller could
        hand back directly."""
        with self._guard:
            self._cache[key] = (time.monotonic() + self._ttl, [dict(row) for row in rows])


# Person/face partitions are read in full by every People/Faces page load and
# by every photo listing (for name lookups) -- see _cached_person_rows_for_user
# and _load_user_face_summary_by_id below -- so they get the same treatment as
# the metadata cache (_cached_metadata_rows_for_user, defined further down).
PEOPLE_SCAN_CACHE_TTL_SECONDS = float(os.getenv('PEOPLE_SCAN_CACHE_TTL_SECONDS', '20'))
_person_scan_cache = _UserScanCache(PEOPLE_SCAN_CACHE_TTL_SECONDS)
_face_summary_scan_cache = _UserScanCache(PEOPLE_SCAN_CACHE_TTL_SECONDS)
# Caches _load_people_embedding_index's built (parsed + normalized) result --
# see that function for why. Same TTL/invalidation semantics as the two
# caches above since it's derived entirely from their underlying data.
_people_embedding_index_cache = _UserScanCache(PEOPLE_SCAN_CACHE_TTL_SECONDS)


def _invalidate_people_scan_cache(user_id: str) -> None:
    if not user_id:
        return
    _person_scan_cache.invalidate(user_id)
    _face_summary_scan_cache.invalidate(user_id)
    _people_embedding_index_cache.invalidate(user_id)


def _partition_key_from_write_call(method_name: str, args: tuple, kwargs: dict) -> str:
    """Best-effort PartitionKey extraction from a table-client write call, so
    _InvalidatingTableClient can invalidate the right user's cache entry
    without every one of the many call sites having to do it explicitly.
    """
    try:
        if method_name == 'delete_entity':
            pk = kwargs.get('partition_key')
            if pk is None and args:
                pk = args[0]
            return str(pk or '')
        if method_name == 'submit_transaction':
            operations = args[0] if args else kwargs.get('entity_operations')
            if operations:
                first = operations[0]
                entity = first[1] if isinstance(first, (list, tuple)) and len(first) > 1 else None
                if isinstance(entity, dict):
                    return str(entity.get('PartitionKey') or '')
            return ''
        entity = kwargs.get('entity')
        if entity is None and args:
            entity = args[0]
        if isinstance(entity, dict):
            return str(entity.get('PartitionKey') or '')
    except Exception:
        pass
    return ''


class _InvalidatingTableClient:
    """Proxy around a Table Storage client that invalidates the read caches for
    the affected user's partition on every write.

    Person/face rows are written from dozens of call sites across app.py and
    storage_utils.py (merges, labels, clustering, deletes, ...). Requiring
    each one to remember to invalidate the cache is exactly how a stale-name
    or vanished-cluster-after-refresh bug creeps back in; wrapping the client
    once at construction makes it impossible to write to the table without
    invalidating, regardless of which function does the writing.
    """

    _MUTATING_METHODS = {'upsert_entity', 'delete_entity', 'create_entity', 'update_entity', 'submit_transaction'}

    def __init__(self, table_client, on_write: Callable[[str], None]):
        self._table_client = table_client
        self._on_write = on_write

    def __getattr__(self, name):
        attr = getattr(self._table_client, name)
        if name not in self._MUTATING_METHODS or not callable(attr):
            return attr

        def _wrapped(*args, **kwargs):
            try:
                self._on_write(_partition_key_from_write_call(name, args, kwargs))
            except Exception:
                pass
            return attr(*args, **kwargs)

        return _wrapped


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


def _bootstrap_owner_account() -> None:
    """Idempotently mirror the seeded password-mode owner into the account and
    library tables, giving them ``user_id == library_id == OWNER_USER_ID``.

    Prep for multi-account password auth: the credential hash is copied into the
    ``photousers`` row and an email->id lookup created so login-by-email works,
    while the legacy config-table credential remains the source of truth until
    the multi-account cutover. No-op after the first run.
    """
    if library_store is None or AUTH_MODE != 'password':
        return
    try:
        cred = password_auth.get_owner_credential(config_table_client) or {}
        # Prefer an explicitly-configured OWNER_EMAIL so an operator can set it
        # after the fact to recover login-by-email; fall back to the seeded value.
        email = OWNER_EMAIL or str(cred.get('email') or '')
        owner = library_store.get_user(password_auth.OWNER_USER_ID)
        if owner is None:
            library_store.create_user(
                email=email,
                password_hash=str(cred.get('passwordHash') or '') or None,
                user_id=password_auth.OWNER_USER_ID,
            )
        elif email and library_utils.normalize_email(owner.get('emailNorm')) != library_utils.normalize_email(email):
            # Reconcile a changed/newly-set OWNER_EMAIL onto the existing account.
            library_store.set_user_email(password_auth.OWNER_USER_ID, email)
        library_store.ensure_personal_library(
            password_auth.OWNER_USER_ID,
            name=email or 'My Library',
        )
        if not email:
            app.logger.warning(
                'Owner account has no email; login-by-email will fail until '
                'OWNER_EMAIL is set. Set OWNER_EMAIL and restart to enable sign-in.'
            )
    except Exception as exc:
        app.logger.warning('Owner account bootstrap failed: %s', exc)


def _init_storage_clients():
    global account_name, credential
    global metadata_table_client
    global blob_service_client, albums_table_client, face_table_client, person_table_client, merge_table_client
    global album_token_index_table_client
    global jobs_table_client
    global workbench_actions_table_client
    global image_names_table_client
    global hash_index_table_client, filename_owners_table_client
    global config_table_client
    global users_table_client, libraries_table_client, memberships_table_client
    global invites_table_client, audit_table_client, clean_requests_table_client, library_store
    global clustering_queue_client, queue_service_client, ipwork_queue_client, library_ops_queue_client

    account_name = STORAGE_ACCOUNT_NAME or os.getenv('AZURE_STORAGE_ACCOUNT_NAME')

    # Prefer local/Azurite connection string when provided.
    if STORAGE_CONNECTION_STRING:
        tbl_svc = TableServiceClient.from_connection_string(STORAGE_CONNECTION_STRING)
        metadata_table_client_local = tbl_svc.get_table_client(METADATA_TABLE)
        albums_table_client_local = tbl_svc.get_table_client(ALBUMS_TABLE)
        album_token_index_table_client_local = tbl_svc.get_table_client(ALBUM_TOKEN_INDEX_TABLE)
        face_table_client_local = tbl_svc.get_table_client(FACE_TABLE)
        person_table_client_local = tbl_svc.get_table_client(PEOPLE_TABLE)
        merge_table_client_local = tbl_svc.get_table_client(MERGE_TABLE)
        jobs_table_client_local = tbl_svc.get_table_client(JOBS_TABLE)
        workbench_actions_table_client_local = tbl_svc.get_table_client(WORKBENCH_ACTIONS_TABLE)
        image_names_table_client_local = tbl_svc.get_table_client(IMAGE_NAMES_TABLE)
        hash_index_table_client_local = tbl_svc.get_table_client(HASH_INDEX_TABLE)
        filename_owners_table_client_local = tbl_svc.get_table_client(FILENAME_OWNERS_TABLE)
        config_table_client_local = tbl_svc.get_table_client(CONFIG_TABLE)
        users_table_client_local = tbl_svc.get_table_client(USERS_TABLE)
        libraries_table_client_local = tbl_svc.get_table_client(LIBRARIES_TABLE)
        memberships_table_client_local = tbl_svc.get_table_client(MEMBERSHIPS_TABLE)
        invites_table_client_local = tbl_svc.get_table_client(INVITES_TABLE)
        audit_table_client_local = tbl_svc.get_table_client(AUDIT_TABLE)
        clean_requests_table_client_local = tbl_svc.get_table_client(CLEAN_REQUESTS_TABLE)

        if BLOB_CONNECTION_STRING:
            blob_service_client_local = BlobServiceClient.from_connection_string(BLOB_CONNECTION_STRING)
        else:
            blob_service_client_local = BlobServiceClient.from_connection_string(STORAGE_CONNECTION_STRING)
        queue_service_client_local = QueueServiceClient.from_connection_string(STORAGE_CONNECTION_STRING)
        clustering_queue_client_local = queue_service_client_local.get_queue_client(CLUSTERING_QUEUE_NAME)
        ipwork_queue_client_local = queue_service_client_local.get_queue_client(IPWORKER_QUEUE_NAME)
        library_ops_queue_client_local = queue_service_client_local.get_queue_client(LIBRARY_OPS_QUEUE_NAME)
    else:
        # Managed identity mode (Azure)
        credential = DefaultAzureCredential()
        if not account_name:
            raise RuntimeError('STORAGE_ACCOUNT_NAME must be set for managed identity authentication.')

        if BLOB_CONNECTION_STRING:
            blob_service_client_local = BlobServiceClient.from_connection_string(
                BLOB_CONNECTION_STRING, connection_pool_maxsize=STORAGE_CONNECTION_POOL_MAXSIZE,
            )
        else:
            blob_service_client_local = BlobServiceClient(
                account_url=f'https://{account_name}.blob.core.windows.net',
                credential=credential,
                connection_pool_maxsize=STORAGE_CONNECTION_POOL_MAXSIZE,
            )
        queue_service_client_local = QueueServiceClient(
            account_url=f'https://{account_name}.queue.core.windows.net',
            credential=credential,
        )
        clustering_queue_client_local = queue_service_client_local.get_queue_client(CLUSTERING_QUEUE_NAME)
        ipwork_queue_client_local = queue_service_client_local.get_queue_client(IPWORKER_QUEUE_NAME)
        library_ops_queue_client_local = queue_service_client_local.get_queue_client(LIBRARY_OPS_QUEUE_NAME)

        # Table clients
        tbl_svc = TableServiceClient(
            endpoint=f'https://{account_name}.table.core.windows.net',
            credential=credential,
            connection_pool_maxsize=STORAGE_CONNECTION_POOL_MAXSIZE,
        )
        metadata_table_client_local = tbl_svc.get_table_client(METADATA_TABLE)
        albums_table_client_local = tbl_svc.get_table_client(ALBUMS_TABLE)
        album_token_index_table_client_local = tbl_svc.get_table_client(ALBUM_TOKEN_INDEX_TABLE)
        face_table_client_local = tbl_svc.get_table_client(FACE_TABLE)
        person_table_client_local = tbl_svc.get_table_client(PEOPLE_TABLE)
        merge_table_client_local = tbl_svc.get_table_client(MERGE_TABLE)
        jobs_table_client_local = tbl_svc.get_table_client(JOBS_TABLE)
        workbench_actions_table_client_local = tbl_svc.get_table_client(WORKBENCH_ACTIONS_TABLE)
        image_names_table_client_local = tbl_svc.get_table_client(IMAGE_NAMES_TABLE)
        hash_index_table_client_local = tbl_svc.get_table_client(HASH_INDEX_TABLE)
        filename_owners_table_client_local = tbl_svc.get_table_client(FILENAME_OWNERS_TABLE)
        config_table_client_local = tbl_svc.get_table_client(CONFIG_TABLE)
        users_table_client_local = tbl_svc.get_table_client(USERS_TABLE)
        libraries_table_client_local = tbl_svc.get_table_client(LIBRARIES_TABLE)
        memberships_table_client_local = tbl_svc.get_table_client(MEMBERSHIPS_TABLE)
        invites_table_client_local = tbl_svc.get_table_client(INVITES_TABLE)
        audit_table_client_local = tbl_svc.get_table_client(AUDIT_TABLE)
        clean_requests_table_client_local = tbl_svc.get_table_client(CLEAN_REQUESTS_TABLE)

    # assign to globals
    metadata_table_client = metadata_table_client_local
    config_table_client = config_table_client_local
    blob_service_client = blob_service_client_local
    albums_table_client = albums_table_client_local
    album_token_index_table_client = album_token_index_table_client_local
    # Wrap so every write (from anywhere in app.py or storage_utils.py) auto-invalidates
    # the people/faces scan cache -- see _InvalidatingTableClient.
    face_table_client = _InvalidatingTableClient(face_table_client_local, _invalidate_people_scan_cache)
    person_table_client = _InvalidatingTableClient(person_table_client_local, _invalidate_people_scan_cache)
    merge_table_client = merge_table_client_local
    jobs_table_client = jobs_table_client_local
    workbench_actions_table_client = workbench_actions_table_client_local
    image_names_table_client = image_names_table_client_local
    hash_index_table_client = hash_index_table_client_local
    filename_owners_table_client = filename_owners_table_client_local
    users_table_client = users_table_client_local
    libraries_table_client = libraries_table_client_local
    memberships_table_client = memberships_table_client_local
    invites_table_client = invites_table_client_local
    audit_table_client = audit_table_client_local
    clean_requests_table_client = clean_requests_table_client_local
    clustering_queue_client = clustering_queue_client_local
    ipwork_queue_client = ipwork_queue_client_local
    library_ops_queue_client = library_ops_queue_client_local
    queue_service_client = queue_service_client_local

    # Ensure the multi-tenant tables exist and wire up the library store.
    for tbl in (users_table_client, libraries_table_client, memberships_table_client,
                invites_table_client, audit_table_client, clean_requests_table_client):
        try:
            tbl.create_table()
        except Exception as exc:
            app.logger.debug('Library table ensure skipped: %s', exc)
    library_store = library_utils.LibraryStore(
        users_table=users_table_client,
        libraries_table=libraries_table_client,
        memberships_table=memberships_table_client,
        invites_table=invites_table_client,
        audit_table=audit_table_client,
        clean_requests_table=clean_requests_table_client,
    )

    try:
        clustering_queue_client.create_queue()
    except Exception as exc:
        app.logger.debug('Queue ensure skipped for %s: %s', CLUSTERING_QUEUE_NAME, exc)
    try:
        ipwork_queue_client.create_queue()
    except Exception as exc:
        app.logger.debug('Queue ensure skipped for %s: %s', IPWORKER_QUEUE_NAME, exc)
    try:
        library_ops_queue_client.create_queue()
    except Exception as exc:
        app.logger.debug('Queue ensure skipped for %s: %s', LIBRARY_OPS_QUEUE_NAME, exc)

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

    # Backfill the account + library tables so existing single-owner data maps to
    # a library whose id equals the legacy user id (no photo/face/album data moves).
    # Entra users are bootstrapped lazily on first authenticated request instead.
    _bootstrap_owner_account()

    # Configure storage_utils (do not pass account keys or SAS keys)
    configure_storage(
        metadata_table_client=metadata_table_client,
        face_table_client=face_table_client,
        person_table_client=person_table_client,
        blob_service_client=blob_service_client,
        blob_image_container=BLOB_IMAGE_CONTAINER,
        blob_thumbnail_container=BLOB_THUMBNAIL_CONTAINER,
        blob_cover_container=BLOB_COVER_CONTAINER,
        blob_vector_index_container=BLOB_VECTOR_INDEX_CONTAINER,
        blob_lexical_index_container=BLOB_LEXICAL_INDEX_CONTAINER,
        image_names_table_client=image_names_table_client,
        hash_index_table_client=hash_index_table_client,
        filename_owners_table_client=filename_owners_table_client,
        queue_map_on_upload=(MAPS_QUEUE_ON_UPLOAD and not MAPS_ON_UPLOAD),
        # Lambda, not a direct reference: _load_user_face_summary_by_id is
        # defined later in this module than this call runs at import time --
        # deferring the name lookup to call time (long after the module has
        # finished importing) sidesteps that ordering issue.
        face_summary_lookup=lambda uid: _load_user_face_summary_by_id(uid),
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


def create_album_token_index_table() -> None:
    try:
        svc = _ensure_table_service_client()
        svc.create_table_if_not_exists(table_name=ALBUM_TOKEN_INDEX_TABLE)
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


def create_jobs_table() -> None:
    try:
        svc = _ensure_table_service_client()
        svc.create_table_if_not_exists(table_name=JOBS_TABLE)
    except AzureError:
        pass


def create_workbench_actions_table() -> None:
    try:
        svc = _ensure_table_service_client()
        svc.create_table_if_not_exists(table_name=WORKBENCH_ACTIONS_TABLE)
    except AzureError:
        pass


def create_image_names_table() -> None:
    try:
        svc = _ensure_table_service_client()
        svc.create_table_if_not_exists(table_name=IMAGE_NAMES_TABLE)
    except AzureError:
        pass


def create_hash_index_table() -> None:
    try:
        svc = _ensure_table_service_client()
        svc.create_table_if_not_exists(table_name=HASH_INDEX_TABLE)
    except AzureError:
        pass


def create_filename_owners_table() -> None:
    try:
        svc = _ensure_table_service_client()
        svc.create_table_if_not_exists(table_name=FILENAME_OWNERS_TABLE)
    except AzureError:
        pass


def create_blob_containers() -> None:
    if blob_service_client is None:
        return
    for container_name in (BLOB_IMAGE_CONTAINER, BLOB_THUMBNAIL_CONTAINER, BLOB_VECTOR_INDEX_CONTAINER, BLOB_LEXICAL_INDEX_CONTAINER, BLOB_EXPORTS_CONTAINER, BLOB_MERGE_PAYLOADS_CONTAINER):
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
    # A frontend/backend pair from the same deployment shares an identical host
    # except that the app-name label contains 'frontend' vs 'backend' (e.g.
    # `<appName>-frontend` and `<appName>-backend`). Accept the origin when
    # swapping that token reproduces this backend's own host, regardless of the
    # chosen app-name prefix/suffix. The rest of the host — the Container Apps
    # environment subdomain (unique per environment) and region — must match, so
    # an attacker cannot forge a matching origin under a different environment.
    origin_label = origin_parts[0]
    if 'frontend' not in origin_label:
        return False
    return (
        origin_label.replace('frontend', 'backend') == request_parts[0]
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


def _parse_capture_filter(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.strptime(value, '%Y-%m-%d')
        return parsed.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _parse_capture_range_args() -> Tuple[Optional[datetime], Optional[datetime]]:
    """captureStart/captureEnd query params, shared by /photos, /photos/search
    and /photos/filter (each accepts the same date-range filter)."""
    return (
        _parse_capture_filter(request.args.get('captureStart', '') or ''),
        _parse_capture_filter(request.args.get('captureEnd', '') or ''),
    )


def _build_photo_summaries_page(
    user_id: str,
    filename_row_pairs: List[Tuple[str, Dict]],
    pid_to_name: Dict[str, str],
) -> List[Dict]:
    """Shared page-building step for /photos, /photos/search and /photos/filter:
    turn a page's (filename, metadata row) pairs into response photo dicts.
    Size comes from the metadata row (stamped at finalize / backfilled by the
    caller) -- never HEADs a blob per result.
    """
    return [
        _build_photo_summary(user_id, filename, row, include_props=True, head_missing=False, pid_to_name=pid_to_name)
        for filename, row in filename_row_pairs
    ]


def _metadata_upload_date(metadata: Dict) -> datetime:
    # Delegates to ordering_utils so listing and any other caller share one
    # definition of "upload date". The datetime.min fallback keeps callers that
    # expect a non-optional datetime (e.g. range comparisons) working.
    return metadata_upload_datetime(metadata) or datetime.min.replace(tzinfo=timezone.utc)


def _metadata_capture_date(metadata: Dict) -> datetime:
    # Capture date with upload date as the documented fallback (see ordering_utils).
    return metadata_capture_datetime(metadata) or datetime.min.replace(tzinfo=timezone.utc)


def _capture_in_range(metadata: Dict, capture_start: Optional[datetime], capture_end: Optional[datetime]) -> bool:
    if not capture_start and not capture_end:
        return True
    # Falls back to upload date when EXIF capture date is absent, matching the
    # gallery's default sort (see _metadata_capture_date) — otherwise undated
    # photos silently vanish from date-filtered results even though they still
    # sort into the gallery by the same fallback date.
    captured = _metadata_capture_date(metadata)
    if captured == datetime.min.replace(tzinfo=timezone.utc):
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
MIN_ALBUM_ACCESS_CODE_LENGTH = 4


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


def _album_access_code_gate(entity: Dict, token: str, provided: str):
    """None if the caller may proceed past a public album's access code, else an
    (response, status) tuple to return as-is.

    Wrong-code attempts are throttled per album token with the same
    exponential-backoff lockout `password_auth` uses for login attempts, so a
    public album's access code (which has no format/complexity requirement --
    it's a plain user-chosen string) can't be brute-forced by an automated
    caller that already knows the (unguessable) share token.
    """
    access_code = _album_access_code(entity)
    if not access_code or _album_grant_valid(entity, token):
        return None
    row_key = f'album-code-throttle:{token}'
    if not password_auth.login_attempt_allowed(config_table_client, row_key):
        retry_after = password_auth.seconds_until_unlocked(config_table_client, row_key)
        return jsonify({'codeRequired': True, 'retryAfterSeconds': retry_after}), 429
    if provided and hmac.compare_digest(access_code, provided):
        password_auth.record_login_success(config_table_client, row_key)
        return None
    if provided:
        password_auth.record_login_failure(config_table_client, row_key=row_key)
    return jsonify({'codeRequired': True, 'retryAfterSeconds': 0}), 401


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
        # Points at this backend's own /public/album/<token> share page (not
        # directly at the SPA) so link-preview bots see the album's real
        # name/thumbnail; that page then redirects human visitors into the SPA.
        public_url = f"{request.host_url.rstrip('/')}/public/album/{token}"
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


def _thumbnail_rotation_from_metadata(metadata: Optional[Dict]) -> int:
    """Rotation baked into the stored thumbnail blob at generation time (e.g. RAW/HEIC
    orientation correction), as opposed to `rotation` (the user's manual rotate action)."""
    try:
        processing_metadata = json.loads((metadata or {}).get('processing_metadata') or '{}')
    except Exception:
        return 0
    client_thumbnail = processing_metadata.get('client_thumbnail') if isinstance(processing_metadata, dict) else {}
    if isinstance(client_thumbnail, dict):
        return _normalize_rotation(client_thumbnail.get('rotationDegrees', 0))
    return 0


def _blob_name_from_metadata(metadata: Optional[Dict], filename: str) -> str:
    """Physical blob name (anonymous UUID) for a photo, or the filename if not
    anonymized. Reads only the metadata already in hand — no extra table call."""
    if metadata:
        anonymous_id = str(metadata.get('anonymousImageId') or '').strip()
        if anonymous_id:
            return anonymous_id
    return filename


def _thumbnail_url_from_metadata(metadata: Dict, filename: str) -> str:
    """Return a thumbnail URL when a real thumbnail or backend preview can be served."""
    if str((metadata or {}).get('thumbnail_status') or '').strip().lower() != 'done':
        if _filename_requires_backend_preview(filename):
            # No thumbnail blob exists yet; the proxy route falls through to the
            # server-side RAW/HEIC preview converter, which a direct blob URL can't.
            return make_proxy_url(filename, 'thumbnail')
        return ''
    return make_media_url(filename, 'thumbnail', blob_name=_blob_name_from_metadata(metadata, filename))


def _private_photo_media_urls(filename: str, metadata: Optional[Dict] = None) -> Dict[str, str]:
    blob_name = _blob_name_from_metadata(metadata, filename)
    return {
        'url': make_media_url(filename, 'image', blob_name=blob_name),
        'thumbnailUrl': make_media_url(filename, 'thumbnail', blob_name=blob_name),
    }


def _photo_people_list(metadata: Dict, pid_to_name: Optional[Dict[str, str]]) -> List[Dict[str, str]]:
    try:
        people_ids = json.loads(metadata.get('peopleIds', '[]') or '[]')
    except Exception:
        people_ids = []
    names = pid_to_name or {}
    people = []
    for pid in people_ids:
        pid_str = str(pid or '').strip()
        if not pid_str:
            continue
        people.append({'personId': pid_str, 'name': names.get(pid_str, '')})
    return people


def _active_processing_worker(metadata: Dict) -> Optional[str]:
    """Returns 'ipworker' if ipworker currently holds an unexpired processing
    lease on this photo, else None. Used by the gallery to show a
    "processing on server" icon distinct from the browser's own in-tab work.
    """
    lease_owner = str(metadata.get('processing_lease_owner') or '').strip()
    if not lease_owner.startswith('ipworker-'):
        return None
    expires_at = str(metadata.get('processing_lease_expires_at') or '').strip()
    if not expires_at:
        return None
    try:
        if datetime.fromisoformat(expires_at.replace('Z', '+00:00')) <= datetime.now(timezone.utc):
            return None
    except Exception:
        return None
    return 'ipworker'


def _build_photo_summary(user_id: str, filename: str, metadata: Dict, include_props: bool = True,
                         head_missing: bool = True, pid_to_name: Optional[Dict[str, str]] = None) -> Dict:
    # Prefer the size/last-modified persisted on the metadata row (stamped at
    # finalize / backfilled). Only fall back to a blob HEAD when a caller allows
    # it (head_missing) and the value is absent — the gallery list path passes
    # head_missing=False so it never fans out a HEAD per tile.
    size = 0
    try:
        size = int(metadata.get('size') or 0)
    except Exception:
        size = 0
    last_modified_iso = metadata.get('lastModified') or None
    if include_props and head_missing and not size:
        try:
            props = get_media_properties('image', _blob_name_from_metadata(metadata, filename))
            size = int(props.get('size') or 0)
            lm = props.get('last_modified')
            if lm is not None:
                last_modified_iso = lm.isoformat()
        except Exception:
            pass

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

    # Dates the gallery sorts and groups by. captureDate follows the documented
    # fallback rule (EXIF capture time, else the uploading device's own file-
    # modified time, else upload time -- see metadata_capture_datetime) so
    # every photo has a chronology anchor even without EXIF.
    upload_dt = metadata_upload_datetime(metadata)
    capture_dt = metadata_capture_datetime(metadata)

    media_urls = _private_photo_media_urls(filename, metadata)
    return {
        'filename': filename,
        'url': media_urls['url'],
        'thumbnailUrl': _thumbnail_url_from_metadata(metadata, filename),
        'size': size,
        'lastModified': last_modified_iso,
        'uploadDate': upload_dt.isoformat() if upload_dt else None,
        'captureDate': capture_dt.isoformat() if capture_dt else None,
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
        'people': _photo_people_list(metadata, pid_to_name),
        'processing': {
            'preview': metadata.get('preview_status'),
            'thumbnail': metadata.get('thumbnail_status'),
            'exif': metadata.get('exif_status'),
            'ocr': metadata.get('ocr_status'),
            'face': metadata.get('face_status'),
            'faceSource': face_source or None,
            'aiVision': metadata.get('ai_vision_status'),
            'mapDetection': metadata.get('map_detection_status'),
            # Which side currently holds the active processing lease on this
            # photo, if any -- lets the gallery show a "processing on server"
            # icon (see PhotoGallery.tsx tile-badges) distinct from the
            # browser's own in-tab processing. Origin is inferred from the
            # lease-owner id prefix set at claim time (_queue_ipwork_processing
            # uses 'ipworker-<jobId>', /upload/processing/claim uses
            # 'browser-<uuid>' by default).
            'activeWorker': _active_processing_worker(metadata),
        },
    }


def _ensure_account_bootstrapped(user_id: str, email: Optional[str] = None) -> Optional[Dict]:
    """Idempotently ensure an account + its personal library/membership exist,
    returning the account row.

    Password-mode accounts are created at invite acceptance; Entra users are
    bootstrapped here on first authenticated request (they have no prior row).
    An existing account implies its personal library/membership already exist
    (they are created together), so this reads once and writes only on first use.
    """
    if library_store is None or not user_id:
        return None
    try:
        account = library_store.get_user(user_id)
        if account is None:
            library_store.create_user(email=email or '', user_id=user_id)
            library_store.ensure_personal_library(user_id, name=(email or 'My Library'))
            account = library_store.get_user(user_id)
        return account
    except Exception as exc:
        app.logger.warning('Account bootstrap failed for %s: %s', user_id, exc)
        return None


def _resolve_session_payload(require_auth: bool):
    """Validate the Photostore-issued session token (both auth modes).

    Returns (payload|None, error_response|None). ``payload is None`` with no
    error means no token was presented and auth is not being enforced (the
    local-dev convenience path).
    """
    auth_header = str(request.headers.get('Authorization', '') or '')
    if auth_header.lower().startswith('bearer '):
        token = auth_header.split(' ', 1)[1].strip()
        try:
            return password_auth.validate_session_token(SESSION_SECRET, token), None
        except Exception as exc:
            app.logger.warning('Session token validation failed: %s', exc)
            return None, (jsonify({'error': 'Invalid or expired session'}), 401)
    if AUTH_REQUIRED or require_auth:
        return None, (jsonify({'error': 'Authorization token is required.'}), 401)
    return None, None


def _auth_lookup_with_retry(fn, attempts: int = 3):
    """Run an auth-critical table lookup, retrying transient failures with a
    short backoff. The backend runs several gunicorn threads that share the
    Azure table clients and managed-identity credential; an occasional storage
    or token blip under that concurrency should be absorbed here rather than
    surface as a spurious 401/403. Only raises if every attempt fails; a
    definitively-absent row returns None without retrying (the *_checked lookups
    return None for not-found and raise only on real errors)."""
    last_exc: Optional[Exception] = None
    for attempt in range(max(1, attempts)):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if attempt < attempts - 1:
                time.sleep(0.05 * (2 ** attempt))
    raise last_exc if last_exc is not None else RuntimeError('auth lookup failed')


def _require_library_context(require_auth: bool = False):
    """The single tenant-isolation boundary.

    Resolves the authenticated account and its *active* library from the signed
    session token, then, per request: (1) enforces the token version so a reset/
    removal kills outstanding tokens, and (2) confirms the caller is a live
    member of the active library, so access revocation is immediate. The active
    library is taken ONLY from the signed token (``lib`` claim) — never from
    client input — so a caller cannot point requests at a library they are not a
    member of.

    Returns (account_user_id, library_id, None) or (None, None, error_response).
    """
    payload, error = _resolve_session_payload(require_auth)
    if error:
        return None, None, error

    if payload is None:
        # Unauthenticated dev convenience: behave as the single owner identity.
        if AUTH_MODE == 'password':
            uid = password_auth.OWNER_USER_ID
            _ensure_account_bootstrapped(uid, email=None)
            return uid, uid, None
        return None, None, (jsonify({'error': 'Authorization token is required.'}), 401)

    user_id = str(payload.get('sub') or '').strip()
    if not user_id:
        return None, None, (jsonify({'error': 'Invalid session (no subject).'}), 401)

    # Accounts are created at login / token-exchange / invite-acceptance, not
    # here: a valid token whose account row is gone means the account was
    # deleted, so reject rather than silently resurrecting it from the token.
    # The *_checked lookups raise on a transient storage error (rather than
    # returning None), so a storage blip under concurrency can't be mistaken for
    # "account deleted"/"not a member" — that surfaces as a retryable 503 instead
    # of a spurious 401/403 that reads as being logged out.
    account = None
    if library_store is not None:
        try:
            account = _auth_lookup_with_retry(lambda: library_store.get_user_checked(user_id))
        except Exception:
            app.logger.warning('Account lookup failed transiently for %s', user_id, exc_info=True)
            return None, None, (jsonify({'error': 'Account verification is temporarily unavailable. Please retry.'}), 503)
        if account is None:
            return None, None, (jsonify({'error': 'This account no longer exists. Please sign in again.'}), 401)

    # Session-kill: the token's version must match the account's current version.
    token_ver = payload.get('ver')
    if token_ver is not None and account is not None:
        current_ver = int(account.get('tokenVersion', 1) or 1)
        if int(token_ver) != current_ver:
            return None, None, (jsonify({'error': 'Session expired. Please sign in again.'}), 401)

    library_id = str(payload.get('lib') or user_id).strip() or user_id

    # Membership check: the caller must currently belong to the active library.
    # A user's own personal-library membership is never removed while the account
    # exists, so we only need the lookup when acting in a *different* library.
    if library_id != user_id and library_store is not None:
        try:
            is_member = _auth_lookup_with_retry(
                lambda: library_store.get_membership_checked(user_id, library_id)
            ) is not None
        except Exception:
            app.logger.warning('Membership lookup failed transiently for %s/%s', user_id, library_id, exc_info=True)
            return None, None, (jsonify({'error': 'Library access check is temporarily unavailable. Please retry.'}), 503)
        if not is_member:
            return None, None, (jsonify({'error': 'You no longer have access to this library.'}), 403)

    return user_id, library_id, None


def _is_safe_path_segment(name: str) -> bool:
    """True if name is a bare path segment safe to use as a blob/metadata key
    (no path traversal, separators, or null bytes)."""
    if not name or name in ('.', '..'):
        return False
    if '/' in name or '\\' in name or '\x00' in name:
        return False
    return os.path.basename(name) == name


def _validate_media_filename(filename: str) -> Optional[str]:
    """Validate a user-supplied photo/video filename for use as a metadata
    key. Returns the filename unchanged if it is safe (no path traversal)
    and has an allowed extension, else None.

    Deliberately does NOT use werkzeug's secure_filename() for this check:
    secure_filename() strips leading '.'/'_' characters, which silently
    mangles legitimate camera filenames such as Canon's "_MG_1234.CR3"
    (the AdobeRGB-color-space naming convention), either rejecting them
    outright (round-trip equality checks) or renaming them out from under
    the caller (bare sanitize-and-continue call sites).
    """
    name = (filename or '').strip()
    if not _is_safe_path_segment(name) or not allowed_file(name):
        return None
    return name


def _require_user_id(require_auth: bool = False):
    """Compatibility shim: returns the *active library id* (the data partition
    key) for the current request, so every data endpoint transparently operates
    on the active library. Use _require_library_context() where the account
    identity (attribution, permissions, audit actor) is needed."""
    _account_id, library_id, error = _require_library_context(require_auth=require_auth)
    if error:
        return None, error
    return library_id, None


def _resolve_user_role(user_id: str) -> str:
    """Role lookup for the authenticated identity (admin allow-list only)."""
    if user_id and str(user_id).strip().lower() in ADMIN_USER_IDS:
        return 'admin'
    return ''


def _require_admin(require_auth: bool = True):
    """Return (library_id, None) for admins, or (None, error_response) otherwise.

    The admin allow-list is checked against the authenticated *account* id, but
    the returned id is the active library so admin data operations stay scoped.
    """
    account_id, library_id, error = _require_library_context(require_auth=require_auth)
    if error:
        return None, error
    if _resolve_user_role(account_id) != 'admin':
        return None, (jsonify({'error': 'Administrator privileges are required.'}), 403)
    return library_id, None


def _issue_session_for(
    user_id: str,
    *,
    library_id: Optional[str] = None,
    email: str = '',
    mode: str = 'password',
    ttl_seconds: Optional[int] = None,
) -> str:
    """Mint a session token for a user, defaulting the active library to their
    own and stamping the account's current token version."""
    ver = library_store.token_version(user_id) if library_store is not None else 1
    return password_auth.issue_session_token(
        SESSION_SECRET,
        user_id=user_id,
        library_id=library_id or user_id,
        token_version=ver or 1,
        email=email,
        mode=mode,
        ttl_seconds=SESSION_TTL_SECONDS if ttl_seconds is None else ttl_seconds,
    )


def _get_metadata_entity(user_id: str, filename: str) -> Optional[Dict]:
    try:
        return metadata_table_client.get_entity(partition_key=user_id, row_key=filename)
    except Exception:
        return None


def _resolve_media_blob_name(user_id: str, filename: str, metadata: Optional[Dict] = None) -> str:
    """Resolve the physical blob name for a photo.

    Anonymized uploads store their blob under a UUID (metadata['anonymousImageId']);
    pre-anonymization photos are stored under the original filename. Callers that
    already hold the metadata row should pass it to avoid a table read; otherwise
    the row is fetched here. Falls back to the original filename when no anonymous
    id is present, keeping old photos serving correctly."""
    entity = metadata
    if entity is None:
        entity = _get_metadata_entity(user_id, filename)
    if entity:
        anonymous_id = str(entity.get('anonymousImageId') or '').strip()
        if anonymous_id:
            return anonymous_id
    return filename


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


def _cached_person_rows_for_user(user_id: str) -> List[Dict]:
    """Every person row for user_id, from the short-TTL cache when fresh.

    Shared by every caller that needs the full person partition (name index,
    People/Faces page listings, ...) so they scan Azure Table Storage once per
    TTL window instead of once per call. See _UserScanCache / _person_scan_cache.
    """
    if person_table_client is None:
        return []

    def _fetch() -> List[Dict]:
        try:
            return list(person_table_client.query_entities(f"PartitionKey eq '{_escape_odata(user_id)}'"))
        except Exception:
            return []

    return _person_scan_cache.get(user_id, _fetch)


def _load_people_name_index(user_id: str) -> Tuple[Dict[str, str], Dict[str, List[str]]]:
    pid_to_name: Dict[str, str] = {}
    name_to_ids: Dict[str, List[str]] = {}
    if person_table_client is None:
        return pid_to_name, name_to_ids
    rows = _cached_person_rows_for_user(user_id)
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
        for field in ('locationCity', 'locationRegion', 'locationCountry', 'address'):
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
        str(metadata.get('locationRegion', '')),
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


# Legacy RowKey shape for the old shared 'jobs' partition in METADATA_TABLE
# (see _upsert_job_status's fallback-read note below). New rows in
# JOBS_TABLE key by the raw job_id instead -- it only ever contains
# `[prefix]:[id]:[hex]`, all valid Table Storage RowKey characters, so this
# sanitize-through-secure_filename round-trip (which silently turned ':'
# into '_') is no longer needed.
def _job_row_key(job_id: str) -> str:
    return secure_filename(job_id) or str(uuid.uuid4())


_LIBRARY_SCOPED_JOB_TYPES = ('library_clean', 'library_download')


def _job_partition_key(user_id: str, job_type: str, fields: Dict) -> str:
    """Jobs are partitioned by their natural scope so an "is there an active
    job" check is a cheap single-partition query instead of a fleet-wide scan:
    userId for personal jobs, libraryId for library_clean/library_download
    since any member of a shared library must be able to check those
    regardless of who started the job (see _active_library_cleanup_job)."""
    if job_type in _LIBRARY_SCOPED_JOB_TYPES:
        library_id = fields.get('libraryId')
        if library_id:
            return str(library_id)
    return user_id


def _upsert_job_status(job_id: str, user_id: str, job_type: str, status: str, **fields) -> None:
    if jobs_table_client is None:
        return
    partition_key = _job_partition_key(user_id, job_type, fields)
    entity = {
        'PartitionKey': partition_key,
        'RowKey': job_id,
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
        jobs_table_client.upsert_entity(entity)
    except Exception:
        return
    if partition_key != user_id:
        # jobs_status() (the notification-bell poller) only ever queries the
        # caller's own userId partition. library_clean/library_download jobs
        # are authoritatively keyed by libraryId instead (so any member of a
        # shared library can see an in-progress cleanup, not just whoever
        # started it) -- mirror the same row under the initiator's userId
        # partition too, purely so their own bell/toast still sees it. Same
        # hand-rolled-secondary-index shape as _store_hash_index/
        # _store_filename_owner.
        try:
            jobs_table_client.upsert_entity({**entity, 'PartitionKey': user_id})
        except Exception:
            pass


def _get_job_row(partition_key: str, job_id: str) -> Optional[Dict]:
    """Point-read a job row by its scope (userId or libraryId) and jobId.

    Falls back to the old shared 'jobs' partition in METADATA_TABLE if not
    found in JOBS_TABLE, so a job that was already fully terminal (and thus
    never wrote another update) at the moment JOBS_TABLE went live is still
    findable for a bridging period. TODO(remove ~2 weeks after this ships):
    every legitimate job will have cycled through the new table by then.
    """
    if jobs_table_client is not None:
        try:
            return jobs_table_client.get_entity(partition_key=partition_key, row_key=job_id)
        except Exception:
            pass
    if metadata_table_client is not None:
        try:
            return metadata_table_client.get_entity(partition_key='jobs', row_key=_job_row_key(job_id))
        except Exception:
            pass
    return None


# How far back the /api/jobs/status endpoint looks for finished jobs. In-flight
# jobs are always returned; terminal ones only while this fresh, so a client
# opening the app long after a job completed does not get a stale notification.
JOB_STATUS_WINDOW_MINUTES = 60


def _humanize_job(row: Dict) -> Dict:
    """Turn a raw ``jobs`` table row into a client-facing summary the in-app
    notifier can surface on completion.

    The stored ``jobType`` is coarse — ``clustering`` covers recluster, initial
    clustering, and identity propagation ("find more faces") alike — so the
    specific operation is inferred from the shape of the ``result`` payload each
    of those code paths writes.
    """
    job_type = str(row.get('jobType') or '')
    status = str(row.get('status') or 'unknown').lower()
    error = str(row.get('error') or '') or None
    result = row.get('result')
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except Exception:
            result = {}
    if not isinstance(result, dict):
        result = {}

    kind = 'job'
    title = 'Background task'
    message = ''

    def _plural(count: int, word: str) -> str:
        return f"{count} {word}" if count == 1 else f"{count} {word}s"

    if job_type == 'library_clean':
        kind = 'library_clean'
        if status == 'done':
            title = 'Library cleanup finished'
            message = f"Removed {_plural(int(result.get('photosDeleted') or 0), 'photo')}."
        elif status == 'failed':
            title = 'Library cleanup failed'
    elif job_type == 'library_download':
        kind = 'library_download'
        if status == 'done':
            title = 'Library export ready'
            parts = result.get('parts')
            part_count = len(parts) if isinstance(parts, list) else 0
            message = f"{_plural(int(result.get('photosIncluded') or 0), 'photo')} ready to download"
            message += f" across {_plural(part_count, 'part')}." if part_count > 1 else '.'
        elif status == 'failed':
            title = 'Library export failed'
    elif job_type == 'ipwork':
        # One of these per photo in backend/both processing mode -- the gallery
        # tile's own "processing on server" badge (_active_processing_worker)
        # already gives per-photo feedback, so this must not also surface a
        # bell/toast per file (see kind='ipwork' exclusion in the frontend
        # poller) or a bulk backend upload spams one "Background task
        # finished" per photo.
        kind = 'ipwork'
        if status == 'done':
            title = 'Photo processed'
        elif status == 'failed':
            title = 'Photo processing failed'
    elif job_type == PREVIEW_JOB_TYPE:
        kind = 'preview'
        name = str(row.get('filename') or '').rsplit('/', 1)[-1]
        if status == 'done':
            title = 'Preview ready'
            message = f"{name} is ready to view." if name else 'A preview finished generating.'
        elif status == 'failed':
            title = 'Preview generation failed'
    elif job_type == 'clustering':
        recluster_keys = {'peopleAlbums', 'detectedFaces', 'candidateFaces', 'skippedConfirmedFaces', 'assignments'}
        cluster_keys = {'createdPeople', 'clusterCount', 'faceCount'}
        if recluster_keys & set(result.keys()):
            kind = 'recluster'
            if status == 'done':
                title = 'Reclustering finished'
                processed = int(result.get('processed') or 0)
                new_groups = int(result.get('peopleAlbums') or 0)
                parts = []
                if processed:
                    parts.append(f"{_plural(processed, 'face')} reorganized")
                if new_groups:
                    parts.append(f"{_plural(new_groups, 'new group')}")
                message = (', '.join(parts) + '.') if parts else 'No changes were needed.'
            elif status == 'failed':
                title = 'Reclustering failed'
        elif cluster_keys & set(result.keys()):
            kind = 'cluster'
            if status == 'done':
                title = 'People grouping finished'
                created = int(result.get('createdPeople') or 0)
                faces = int(result.get('faceCount') or 0)
                person = 'person' if created == 1 else 'people'
                message = f"{created} new {person}, {_plural(faces, 'face')} grouped."
            elif status == 'failed':
                title = 'People grouping failed'
        else:
            kind = 'find_faces'
            found = int(result.get('autoAssignedFaces') or 0)
            people_count = result.get('peopleCount')
            if status == 'done':
                title = 'Find more faces finished'
                if isinstance(people_count, int) and people_count > 1:
                    # A batched pass from bulk-approving several merge
                    # suggestions at once — see _enqueue_propagate_batch_job.
                    people_phrase = f"{people_count} people"
                    message = (f"Updated {people_phrase}, added {_plural(found, 'matching face')}."
                               if found > 0 else f"Checked {people_phrase} — no new matching faces found.")
                else:
                    message = (f"Added {_plural(found, 'matching face')}."
                               if found > 0 else 'No new matching faces found.')
            elif status == 'failed':
                title = 'Find more faces failed'
    else:
        if status == 'done':
            title = 'Background task finished'
        elif status == 'failed':
            title = 'Background task failed'

    if status == 'failed' and not message:
        message = error or 'Something went wrong.'

    suppress_notification = bool(result.get('isIntermediate')) and status == 'done'

    return {
        'jobId': str(row.get('jobId') or ''),
        'status': status,
        'kind': kind,
        'title': title,
        'message': message,
        'error': error,
        'updatedAt': str(row.get('updatedAt') or ''),
        'suppressNotification': suppress_notification,
        'snapshotId': str(result.get('snapshotId') or ''),
    }


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
            _invalidate_metadata_scan_cache(user_id)
            if metadata_updates_affect_search_indexes(updates or {}):
                touch_user_search_indexes_state(user_id)
            return entity
        except Exception as exc:
            last_exc = exc
            time.sleep(0.05 * (2 ** attempt))
    if last_exc:
        app.logger.warning('Failed to update metadata entity %s/%s: %s', user_id, filename, last_exc)
    return None


def _clustering_job_types() -> set:
    return {'people_recluster', 'people_cluster', 'people_propagate', 'people_propagate_batch'}


# A queued/running clustering job older than this is treated as dead (worker
# killed mid-job, or its queue message was dropped) and no longer blocks new
# enqueues. Without this, one orphaned job row would suppress all future
# clustering for a user forever now that the de-dupe guard actually matches.
CLUSTERING_ACTIVE_JOB_STALE_MINUTES = int(os.getenv('CLUSTERING_ACTIVE_JOB_STALE_MINUTES', '15'))

# How often _handle_clustering_queue_payload refreshes a job row's updatedAt
# while cluster_user_faces/_build_people_recluster_plan/etc. are still
# computing. Without this, a full recluster over a large-enough face table
# can legitimately take longer than CLUSTERING_ACTIVE_JOB_STALE_MINUTES --
# confirmed live 2026-09-04, a healthy worker (0 restarts) still got its
# in-progress people_cluster job force-flipped to 'failed' ("worker
# restarted or timed out") by /api/jobs/status's staleness sweep, purely
# because nothing had touched updatedAt since the single write at dispatch
# time. Mirrors the fix already applied to _execute_library_download for
# the identical failure mode (see LIBRARY_EXPORT_PART_MAX_BYTES's sibling
# heartbeat, _live_progress_heartbeat).
CLUSTERING_JOB_HEARTBEAT_SECONDS = int(os.getenv('CLUSTERING_JOB_HEARTBEAT_SECONDS', '120'))

# _has_active_clustering_job used to run query_entities("PartitionKey eq
# 'jobs'") -- an unfiltered scan of the SAME shared 'jobs' partition
# _active_library_cleanup_job was found scanning on every upload request
# (213k+ rows and growing, confirmed live to cost 17-33s/call -- see that
# fix's own comments). Jobs now live in their own JOBS_TABLE partitioned by
# userId (or libraryId for library_clean/library_download -- see
# _job_partition_key), so this is a normal scoped partition query instead.
def _has_active_clustering_job(user_id: str) -> Optional[str]:
    if jobs_table_client is None:
        return None
    try:
        rows = list(jobs_table_client.query_entities(f"PartitionKey eq '{_escape_odata(user_id)}'"))
    except Exception:
        return None
    stale_before = datetime.now(timezone.utc) - timedelta(minutes=CLUSTERING_ACTIVE_JOB_STALE_MINUTES)
    for row in rows:
        # Job rows store the coarse category (see _upsert_job_status), so every
        # people_cluster/people_recluster/people_propagate job is written with
        # jobType='clustering'. Comparing against the fine-grained message types
        # in _clustering_job_types() never matched, so the force=False de-dupe
        # guard never fired and each upload enqueued a redundant full recluster.
        if str(row.get('jobType') or '') != 'clustering':
            continue
        if str(row.get('status') or '').lower() not in {'queued', 'running'}:
            continue
        # Ignore rows that never reached a terminal state but are old enough that
        # the worker clearly is not still working them, so they cannot wedge the
        # de-dupe guard shut and starve the user of clustering indefinitely.
        updated = _parse_iso_date(str(row.get('updatedAt') or ''))
        if updated is not None and updated < stale_before:
            continue
        return str(row.get('jobId') or '')
    return None


def _mark_clustering_job_rerun_requested(job_id: str, user_id: str) -> None:
    """Flag an in-flight clustering job so the worker fires exactly one
    follow-up job once it finishes, instead of the caller enqueueing its own.

    Used to coalesce a burst of per-photo triggers (e.g. every photo in a big
    upload finishing face detection) into a single clustering pass — and a
    single completion notification — rather than one job per photo.
    """
    if jobs_table_client is None:
        return
    try:
        jobs_table_client.upsert_entity({
            'PartitionKey': user_id,
            'RowKey': job_id,
            'rerunRequested': True,
        })
    except Exception:
        pass


# Minimum time between automatic full-recluster (DBSCAN) maintenance passes
# for a given user. New faces are assigned synchronously via
# _assign_faces_to_people_incrementally (no worker involved) as they arrive;
# this pass exists only to merge fragmented unnamed-person clusters that the
# greedy matcher can leave behind, so it doesn't need to run on every upload
# -- it previously did (via unconditional coalesced reruns), which is what
# kept ownphotostore-worker alive continuously during a sustained backfill.
PEOPLE_CLUSTER_MAINTENANCE_COOLDOWN_SECONDS = int(os.getenv('PEOPLE_CLUSTER_MAINTENANCE_COOLDOWN_SECONDS', '1800'))

# Caps how many maintenance passes can auto-fire back-to-back (each still
# individually cooldown-gated above) without a genuinely fresh upload in
# between. The cooldown alone only bounds *frequency* -- it doesn't stop an
# indefinite drip of non-upload triggers (the ipwork sweep recovering old
# stuck/stale-face-version photos, client-processing resubmissions after a
# rotation, coalesced reruns) from re-arming this full ~9000+-face DBSCAN
# pass every 30 minutes forever, even when the user hasn't uploaded anything
# in weeks -- pure wasted worker compute. Once the cap is hit, maintenance
# stays paused until _mark_fresh_upload_activity resets the counter.
PEOPLE_CLUSTER_MAX_MAINTENANCE_RUNS_WITHOUT_UPLOAD = int(os.getenv('PEOPLE_CLUSTER_MAX_MAINTENANCE_RUNS_WITHOUT_UPLOAD', '3'))


def _clustering_maintenance_due(user_id: str) -> bool:
    """Atomic cooldown check + claim in one round trip (create-then-
    conditional-update, mirroring _try_claim_ipwork_sweep_lock below), gating
    the automatic maintenance recluster (not the explicit user-triggered
    endpoints, which call _enqueue_clustering_job directly and must stay
    immediate).

    Used to be a blind read-then-upsert with no concurrency control, on the
    documented assumption that _has_active_clustering_job's de-dupe bounded
    any race to "one extra job enqueue, not a repeating chain." Confirmed
    live 2026-09-03 that assumption was wrong: a burst of uploads whose
    face-processing lands within the same _jobs_partition_scan_cache TTL
    window (20s) can each independently read this row as "not due yet"
    before any of their own writes lands, and each proceeds to
    _enqueue_clustering_job -- observed 9 concurrent full-library clustering
    jobs for one user from a single upload batch, not one. The etag-
    conditional update here closes that race: only one concurrent caller can
    win the claim no matter how many check within the same window.
    """
    if metadata_table_client is None:
        return False
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    claim_row = {
        'PartitionKey': 'clustering_maintenance',
        'RowKey': user_id,
        'lastStartedAt': now_iso,
        'updatedAt': now_iso,
        'runsSinceUpload': 1,
    }
    try:
        metadata_table_client.create_entity(dict(claim_row))
        return True
    except ResourceExistsError:
        pass
    except Exception:
        return False

    try:
        existing = metadata_table_client.get_entity('clustering_maintenance', user_id)
    except Exception:
        return False

    last = _parse_iso_date(str(existing.get('lastStartedAt') or ''))
    cutoff = now - timedelta(seconds=PEOPLE_CLUSTER_MAINTENANCE_COOLDOWN_SECONDS)
    if last is not None and last >= cutoff:
        return False

    try:
        runs_since_upload = int(existing.get('runsSinceUpload') or 0)
    except Exception:
        runs_since_upload = 0
    if runs_since_upload >= PEOPLE_CLUSTER_MAX_MAINTENANCE_RUNS_WITHOUT_UPLOAD:
        return False
    claim_row['runsSinceUpload'] = runs_since_upload + 1

    try:
        metadata_table_client.update_entity(
            claim_row, etag=existing.metadata['etag'], match_condition=MatchConditions.IfNotModified,
        )
        return True
    except Exception:
        return False  # lost the race to claim the next window


def _mark_fresh_upload_activity(user_id: str) -> None:
    """Resets the PEOPLE_CLUSTER_MAX_MAINTENANCE_RUNS_WITHOUT_UPLOAD counter
    on a genuinely fresh upload -- called only from /upload/finalize and
    /upload/finalize-batch, not from client-processing resubmissions or the
    ipwork sweep's recovery of old stuck photos, so those non-upload triggers
    can't extend the maintenance-pass budget. Deliberately leaves
    lastStartedAt untouched: this only affects the run-count cap, not the
    per-pass cooldown timer, so an upload can't force an early recluster.

    Reads the existing row (if any) and writes every field back rather than
    upserting just {runsSinceUpload: 0} -- upsert_entity's merge-vs-replace
    behavior shouldn't be relied on here (a blind partial upsert would risk
    wiping lastStartedAt/updatedAt entirely under replace semantics). No
    etag/conditional-update needed despite the read-then-write shape: unlike
    _clustering_maintenance_due's claim, concurrent resets all want the same
    outcome (runsSinceUpload=0, lastStartedAt unchanged), so there's no
    lost-update case to guard against; the worst a race with a concurrent
    claim above can do is make this cycle's maintenance pass skip once,
    which is harmless.
    """
    if metadata_table_client is None:
        return
    try:
        existing = metadata_table_client.get_entity('clustering_maintenance', user_id)
    except Exception:
        existing = None
    row = dict(existing) if isinstance(existing, dict) else {}
    row['PartitionKey'] = 'clustering_maintenance'
    row['RowKey'] = user_id
    row['runsSinceUpload'] = 0
    try:
        metadata_table_client.upsert_entity(row)
    except Exception:
        app.logger.exception('Failed to reset clustering maintenance run counter for %s', user_id)


def _enqueue_clustering_job(
    user_id: str,
    *,
    force: bool = False,
    job_type: str = 'people_recluster',
    allow_reassign_confirmed: bool = False,
    payload: Optional[Dict] = None,
    coalesce_on_conflict: bool = False,
) -> Dict[str, str]:
    if not force:
        existing_job_id = _has_active_clustering_job(user_id)
        if existing_job_id:
            if coalesce_on_conflict:
                _mark_clustering_job_rerun_requested(existing_job_id, user_id)
                return {'status': 'coalesced', 'jobId': existing_job_id}
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


def _enqueue_admin_repair_job(user_id: str, *, action: str, dry_run: bool) -> Dict[str, str]:
    """Queue one of the Tools/Workbench admin repair actions (dedupe, suppress-
    suspicious, unblock-low-confidence, rebuild-people-index, repair-stale-
    memberships, purge-orphaned) instead of running its full-account scan
    inline on a backend request thread. force=True: each is an explicit,
    one-off click, not a recurring background trigger, so it shouldn't be
    silently coalesced against an unrelated in-flight clustering job the way
    automatic re-cluster triggers are."""
    return _enqueue_clustering_job(
        user_id,
        force=True,
        job_type='people_admin_repair',
        payload={'action': action, 'dryRun': dry_run},
    )


def _enqueue_library_purge_job(library_id: str) -> bool:
    """Fire-and-forget: queue the full-account data purge for a just-deleted
    library (see library_delete) instead of running it inline on the request
    thread -- _purge_library_data does a full row-by-row scan/delete across
    6 tables, the same worst-case-sizing shape as the admin repair actions
    (see _enqueue_admin_repair_job). No job_id/status bookkeeping: by the
    time this could finish, the library and account rows are already gone,
    so nothing can poll for a result and there's no jobs-table partition key
    left to scope one to -- same reasoning as _enqueue_incremental_assign_job."""
    if clustering_queue_client is None:
        app.logger.warning('Clustering queue client is unavailable; purge for %s was not enqueued', library_id)
        return False
    message = {'user_id': library_id, 'type': 'library_delete_purge', 'libraryId': library_id}
    try:
        clustering_queue_client.send_message(json.dumps(message, separators=(',', ':')))
    except Exception:
        app.logger.exception('Failed to enqueue library purge for %s', library_id)
        return False
    return True


def _enqueue_incremental_assign_job(user_id: str, filename: str) -> Dict[str, str]:
    """Queue asynchronous face-to-person assignment for one just-processed
    photo, run by the standalone clustering worker instead of inline in the
    upload request path (see _queue_people_clustering_after_face_processing
    for why: this used to run synchronously in-process, and moving it here
    was what made /upload/finalize and /upload/client-processing responses
    balloon from ms to tens-of-seconds under a large burst -- an unvectorized
    per-photo embedding-index rebuild competing for the same GIL as every
    other concurrent upload request on the replica).

    Deliberately skips _enqueue_clustering_job's active-job de-dupe and
    _upsert_job_status bookkeeping: that guard exists so a slow full-library
    DBSCAN maintenance pass doesn't get duplicated, but here every photo
    needs its own assignment pass -- coalescing them would silently drop
    faces, and a status row per photo would add exactly the kind of
    per-upload Table Storage write churn this fix is trying to get off the
    request path. No jobId means the worker's own status/coalesced-rerun
    bookkeeping (which all key off a truthy job_id) is a no-op for these.
    """
    if clustering_queue_client is None:
        app.logger.warning('Clustering queue client is unavailable; incremental-assign for %s/%s was not enqueued', user_id, filename)
        return {'status': 'unavailable'}
    message = {
        'user_id': user_id,
        'type': 'people_incremental_assign',
        'filename': filename,
    }
    try:
        clustering_queue_client.send_message(json.dumps(message, separators=(',', ':')))
    except Exception:
        app.logger.exception('Failed to enqueue incremental-assign job for %s/%s', user_id, filename)
        return {'status': 'failed'}
    return {'status': 'queued'}


def _enqueue_propagate_job(user_id: str, person_id: str) -> Dict[str, str]:
    """Queue an asynchronous identity-propagation pass for a named person.

    Reclaiming a person's faces from unnamed clusters scans the whole face table
    (a past OOM driver), so it runs on the queue-scaled worker instead of blocking
    the request that triggered it (e.g. a merge or label).

    Unlike _enqueue_clustering_job, this had no de-dupe guard: every call (e.g.
    repeated "Find more faces" clicks, since the button re-enables as soon as
    the job is *queued*, well before it finishes) fired its own full-table-scan
    job. They queue up and run serially, so the "Finding more faces…" indicator
    stays lit for the whole backlog and the worker burns time re-scanning.
    Reuse whatever clustering-family job is already in flight for the user
    instead, same as the guard _enqueue_clustering_job already has.
    """
    existing_job_id = _has_active_clustering_job(user_id)
    if existing_job_id:
        return {'status': 'queued', 'jobId': existing_job_id}
    job_id = f"propagate:{user_id}:{uuid.uuid4().hex}"
    if clustering_queue_client is None:
        app.logger.warning('Clustering queue client is unavailable; propagate job %s was not enqueued', job_id)
        return {'status': 'unavailable', 'jobId': job_id}
    message = {
        'jobId': job_id,
        'correlationId': job_id,
        'user_id': user_id,
        'type': 'people_propagate',
        'personId': person_id,
    }
    try:
        clustering_queue_client.send_message(json.dumps(message, separators=(',', ':')))
    except Exception:
        app.logger.exception('Failed to enqueue propagate job %s', job_id)
        return {'status': 'failed', 'jobId': job_id}
    _upsert_job_status(job_id, user_id, 'clustering', 'queued', payload={'personId': person_id})
    return {'status': 'queued', 'jobId': job_id}


def _enqueue_propagate_batch_job(user_id: str, person_ids: List[str]) -> Dict[str, str]:
    """Queue one identity-propagation pass covering several named people.

    Used by the bulk merge-suggestion approval flow so approving N suggestions
    at once produces a single background job (and a single completion
    notification) instead of N — see _merge_persons_core's callers.

    Same de-dupe guard as _enqueue_propagate_job — see its docstring.
    """
    existing_job_id = _has_active_clustering_job(user_id)
    if existing_job_id:
        return {'status': 'queued', 'jobId': existing_job_id}
    job_id = f"propagate-batch:{user_id}:{uuid.uuid4().hex}"
    if clustering_queue_client is None:
        app.logger.warning('Clustering queue client is unavailable; batch propagate job %s was not enqueued', job_id)
        return {'status': 'unavailable', 'jobId': job_id}
    message = {
        'jobId': job_id,
        'correlationId': job_id,
        'user_id': user_id,
        'type': 'people_propagate_batch',
        'personIds': list(person_ids),
    }
    try:
        clustering_queue_client.send_message(json.dumps(message, separators=(',', ':')))
    except Exception:
        app.logger.exception('Failed to enqueue batch propagate job %s', job_id)
        return {'status': 'failed', 'jobId': job_id}
    _upsert_job_status(job_id, user_id, 'clustering', 'queued', payload={'personIds': list(person_ids)})
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


# Short-TTL cache + coalescing for full metadata scans. Listing endpoints
# (/photos, search, filter, …) each scan the user's entire metadata partition;
# back-to-back or concurrent calls used to repeat that scan and starve the
# server. The first request performs the scan while identical concurrent
# requests wait on a per-user lock and reuse the result; writes invalidate the
# user's entry, and the TTL bounds staleness for anything invalidation misses.
# (Uses the same _UserScanCache as the person/face caches defined near
# _init_storage_clients -- metadata writes go through many call sites rather
# than a wrapped client, so this table still invalidates explicitly.)
#
# Must stay comfortably above the scan's own elapsed time or the cache can
# never actually stay warm -- live 2026-09-16 on a 36,633-row account: even
# the narrow-column scan below (PHOTO_LIST_SELECT_FIELDS) took 17-20s against
# a 20s TTL, so entries were expiring before the next request could reuse
# them, collapsing into the exact back-to-back full-partition-scan thrash
# this cache exists to prevent. Same fix already applied once for ipwork's
# people-scan cache (20s TTL < 77s/photo cadence -> 120s).
METADATA_SCAN_CACHE_TTL_SECONDS = float(os.getenv('METADATA_SCAN_CACHE_TTL_SECONDS', '120'))
_metadata_scan_cache = _UserScanCache(METADATA_SCAN_CACHE_TTL_SECONDS)
# /photos/filter's default sort order (rating/likes -> recency -> filename) never
# depends on the request's minRating/minLikes/capture-range/location filter values
# -- those only decide which rows are *included*, not how included rows are
# ordered relative to each other. Without this, every single pagination page
# (offset=0, 24, 48, ...) of an infinite-scroll session re-sorted the user's
# entire library 3x from scratch even though 23 of that request's 24 results
# were already correctly ordered by the previous page's work. Cached and
# invalidated the same way/at the same time as _metadata_scan_cache below so it
# can't go stale relative to it.
_photo_default_sort_cache = _UserScanCache(METADATA_SCAN_CACHE_TTL_SECONDS)

# Narrow-column counterpart of the scans above, for the highest-traffic
# gallery-loading purposes (list/access_batch/timeline/filter). Found live
# 2026-09-16: a 36,633-row account's full-column scan (every field, including
# large ones like photoEmbedding/semanticEmbedding/tagMetadata/weakTags/
# objects/ocrText/faces that these four purposes never read) took 60-80s --
# far longer than METADATA_SCAN_CACHE_TTL_SECONDS, so the cache could never
# actually stay warm: each scan was stale before the next request needed it,
# collapsing into back-to-back full scans and making scrolling/loading feel
# broken. select= cuts the transferred/parsed payload to just what
# _build_photo_summary, order_photo_entries, and filter_photos's own
# criteria actually read. Kept as a SEPARATE cache (not a select= parameter
# on the caches above) deliberately: photos.search's fallback path (lexical/
# semantic scoring over tags/ocrText/caption/objects/embeddings) and the
# rarer albums.smart_create/admin.backfill/uploads.corrupted purposes
# genuinely need the wider field set, and sharing one cache/select between
# them and the hot path would either re-bloat the hot path or silently drop
# fields those purposes rely on.
PHOTO_LIST_SELECT_FIELDS = [
    'PartitionKey', 'RowKey',
    'size', 'lastModified', 'exifData', 'likedBy', 'processing_metadata',
    'rating', 'likes', 'tags', 'rotation',
    'latitude', 'longitude', 'address', 'locationCity', 'locationCountry',
    'exifCount', 'faceCount', 'peopleIds',
    'preview_status', 'thumbnail_status', 'exif_status', 'ocr_status',
    'face_status', 'ai_vision_status', 'map_detection_status',
    'processing_lease_owner', 'processing_lease_expires_at',
    'anonymousImageId',
    'uploadDate', 'upload_started_at', 'last_processing_update', 'clientLastModified',
]
_metadata_list_scan_cache = _UserScanCache(METADATA_SCAN_CACHE_TTL_SECONDS)
_photo_list_default_sort_cache = _UserScanCache(METADATA_SCAN_CACHE_TTL_SECONDS)


def _invalidate_metadata_scan_cache(user_id: str) -> None:
    _metadata_scan_cache.invalidate(user_id)
    _photo_default_sort_cache.invalidate(user_id)
    _metadata_list_scan_cache.invalidate(user_id)
    _photo_list_default_sort_cache.invalidate(user_id)


def _cached_metadata_rows_for_user(user_id: str, purpose: str) -> List[Dict]:
    """Full metadata scan for a user, served from the short-TTL cache when fresh.

    Each caller gets its own shallow copy of the rows (via _UserScanCache) so
    request handlers can annotate them (e.g. the uploadDate backfill) without
    mutating shared state.
    """
    return _metadata_scan_cache.get(user_id, lambda: _query_metadata_rows_for_user(user_id, purpose=purpose))


def _cached_sorted_metadata_rows_for_user(user_id: str, purpose: str) -> List[Dict]:
    """Same rows as _cached_metadata_rows_for_user, pre-sorted once in the
    canonical rating/likes -> recency -> filename order and cached separately
    (see _photo_default_sort_cache above) so /photos/filter's pagination
    doesn't pay for a fresh triple-sort of the whole library on every page."""
    def _compute() -> List[Dict]:
        rows = list(_cached_metadata_rows_for_user(user_id, purpose=purpose))
        rows.sort(key=lambda p: p.get('RowKey', ''))
        rows.sort(key=lambda p: _metadata_upload_date(p), reverse=True)
        rows.sort(key=lambda p: (p.get('rating', 0), p.get('likes', 0)), reverse=True)
        return rows
    return _photo_default_sort_cache.get(user_id, _compute)


def _cached_metadata_list_rows_for_user(user_id: str, purpose: str) -> List[Dict]:
    """Narrow-column counterpart of _cached_metadata_rows_for_user -- see
    PHOTO_LIST_SELECT_FIELDS above for which purposes this is safe for.

    Tries the same lazily-rebuilt, blob-persisted lexical index
    /photos/search already relies on (get_user_lexical_index) before ever
    falling back to a live Table scan. That index's rows are a superset of
    PHOTO_LIST_SELECT_FIELDS (it excludes only the embedding columns), and
    its staleness/rebuild state lives in a blob manifest rather than this
    process's own memory -- so unlike _metadata_list_scan_cache below, it
    stays correctly invalidated even when the write (upload/admin) and read
    (backend/tools) paths run in different container-app processes after the
    tools/upload/admin service split (see backend-cpu-optimization-2026-09
    memory). This also removes the ~20s synchronous full-partition scan that
    a cold _metadata_list_scan_cache used to force onto every first gallery
    request after a replica restart -- observed live to be the trigger for a
    ContainerBackOff crash loop under sustained upload traffic (2026-09-17).
    Only a genuinely cold account (no index has ever been built) still pays
    the live-scan cost here, matching search_photos's own fallback.
    """
    try:
        lexical_index = get_user_lexical_index(user_id, allow_refresh=True)
    except Exception:
        lexical_index = None
        app.logger.exception('Lexical index lookup failed purpose=%s user=%s, falling back to full scan', purpose, user_id)
    if lexical_index is not None:
        return lexical_index.get('rows') or []
    return _metadata_list_scan_cache.get(
        user_id,
        lambda: _query_metadata_rows_for_user(user_id, select=list(PHOTO_LIST_SELECT_FIELDS), purpose=purpose),
    )


def _cached_sorted_metadata_list_rows_for_user(user_id: str, purpose: str) -> List[Dict]:
    """Narrow-column counterpart of _cached_sorted_metadata_rows_for_user, for
    /photos/filter (see PHOTO_LIST_SELECT_FIELDS above)."""
    def _compute() -> List[Dict]:
        rows = list(_cached_metadata_list_rows_for_user(user_id, purpose=purpose))
        rows.sort(key=lambda p: p.get('RowKey', ''))
        rows.sort(key=lambda p: _metadata_upload_date(p), reverse=True)
        rows.sort(key=lambda p: (p.get('rating', 0), p.get('likes', 0)), reverse=True)
        return rows
    return _photo_list_default_sort_cache.get(user_id, _compute)


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


def _face_is_propagation_assigned(face: Dict) -> bool:
    """True when a face was auto-attached to a named person by identity
    propagation. Such assignments are treated as sticky so a later recluster
    does not scatter faces the user's named-person anchor already pulled in."""
    return _coerce_bool(face.get('assignedByPropagation', False))


def _face_assignment_is_sticky(face: Dict) -> bool:
    return _face_is_confirmed(face) or _face_is_propagation_assigned(face)


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
    for key in ('qualityScore', 'detector', 'alignmentMethod', 'alignmentFailureReason', 'model', 'modelVersion', 'embeddingVersion', 'runtime'):
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
    _defer_into: Optional[Dict[str, Dict]] = None,
) -> str:
    """_defer_into lets a caller doing many of these in one pass (see
    cluster_user_faces) stage entities into a dict keyed by person_id instead
    of writing immediately -- last write per person_id naturally wins, same
    final state as calling this repeatedly, but the caller can then flush
    everything in one _batch_upsert_entities call instead of one network
    round-trip per cluster."""
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
    if _defer_into is not None:
        _defer_into[person_id] = entity
        return person_id
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


def _face_alignment_tier(face: Dict) -> str:
    return str(face.get('alignmentMethod') or '').strip()


# 'landmark-5pt-mp' (ipworker, MediaPipe-aligned) added after real-data
# calibration -- see PEOPLE_CLUSTER_EPS_MP's comment. 'landmark-2pt-mp'
# (ipworker's own eyes-only fallback) is deliberately NOT included yet: no
# real landmark-2pt-mp faces have been observed to calibrate against. Of 13
# real photos used across two calibration passes, all 13 that produced a
# usable face landed in the 5pt path; the one deliberately-extreme
# full-profile shot included specifically to probe the 2pt fallback instead
# produced NO detection at all (crop_and_align_face returned None -- YOLO
# found a candidate box, but MediaPipe couldn't resolve landmarks in it well
# enough for either the 5pt or 2pt path). So it's not just unobserved, it may
# be rare for this detector/landmarker pairing. Faces landing there stay
# stored-but-excluded from clustering until real data exists.
PEOPLE_CLUSTER_ALIGNMENT_TIERS = ('landmark-5pt', 'landmark-2pt', 'landmark-5pt-mp')


def _face_embedding_allowed_for_clustering(face: Dict) -> bool:
    versions = _face_embedding_allowed_versions()
    if versions and _face_embedding_version(face) not in versions:
        return False
    # Two embedding quality tiers are allowed into the matching pool:
    # landmark-5pt (real 5-point alignment, near-frontal faces) and
    # landmark-2pt (eyes-only alignment, restored for extreme head poses
    # where forcing a 5-point frontal-template fit was measured to actively
    # HURT the embedding -- a confirmed-same-person face scored 0.19-0.28
    # aligned via 5-point vs 0.51-0.55 via 2-point, with noise floors of
    # -0.03-0.15 and 0.00-0.12 respectively; 5-point barely clears its own
    # noise floor for this case while 2-point clears it with a wide margin).
    # Crucially, the two tiers are NEVER compared directly against each
    # other: a same-person cross-tier check (5pt embedding vs 2pt embedding)
    # measured 0.09-0.56 -- indistinguishable from noise. See
    # _build_people_recluster_plan, which clusters each tier in its own
    # DBSCAN pass with its own epsilon, precisely to avoid ever forming a
    # cross-tier neighbor link. 'none' (no alignment could be solved at all)
    # stays excluded -- there's no calibrated distance metric for it.
    return _face_alignment_tier(face) in PEOPLE_CLUSTER_ALIGNMENT_TIERS


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


def _attach_normalized_embeddings_batched(entries: List[Dict], raw_reps: List[List[float]], np) -> None:
    """Same per-entry result as calling _normalized_embedding(rep, np) once for
    each entries[i]/raw_reps[i] pair, but batches same-dimension embeddings
    into one vectorized norm+divide instead of one numpy call per entry --
    same technique _best_two_person_matches already uses for the comparison
    step (see its docstring), applied to the other half of
    _load_people_embedding_index's per-photo rebuild cost. A minority of
    entries on a different (legacy) embedding dimension are simply grouped
    into their own smaller batch, so correctness for mixed-dimension
    libraries is unaffected."""
    groups: Dict[int, List[int]] = {}
    for pos, rep in enumerate(raw_reps):
        groups.setdefault(len(rep), []).append(pos)

    dtype = _embedding_precision_dtype(np)
    for dim, positions in groups.items():
        if dim == 0:
            continue
        matrix = np.asarray([raw_reps[pos] for pos in positions], dtype=dtype)
        norms = np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-12
        normalized = matrix / norms
        for row_idx, pos in enumerate(positions):
            entries[pos]['_normalized_rep_embedding'] = normalized[row_idx]


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


def _best_two_person_matches(
    face_norm,
    session_embedding_index: List[Dict],
    np,
) -> Tuple[float, float, Optional[Dict]]:
    """Same result as scanning session_embedding_index in order and tracking
    best/second-best via strict '>' comparisons (first entry wins an exact
    tie) -- but computes it with one batched numpy matmul across every
    same-dimension entry instead of one _embedding_similarity_between_normalized
    Python call (with its own per-call numpy<->list round trip) per entry.
    That per-entry call overhead, multiplied by every person in the library
    on every uploaded photo, was a real, unvectorized CPU cost -- see
    _load_people_embedding_index's docstring for the sibling fix addressing
    the other half of that same bottleneck (the GIL contention documented in
    deploy/resources.bicep's reverted GUNICORN_THREADS 4->12 experiment).

    Entries whose normalized embedding has a different length than
    face_norm (a person still on an older embedding-taxonomy version) are
    scored individually via the original per-entry path, preserving its
    truncate-without-renormalize behavior for that legacy comparison.
    """
    best_score = 0.0
    second_best_score = 0.0
    best_person = None
    if face_norm is None or not session_embedding_index:
        return best_score, second_best_score, best_person

    face_len = len(face_norm)
    same_dim_positions: List[int] = []
    same_dim_norms = []
    scores: List[Optional[float]] = [None] * len(session_embedding_index)

    for idx, entry in enumerate(session_embedding_index):
        if not (entry.get('repEmbedding') or []):
            continue
        existing_norm = _normalized_embedding_for_entry(entry, np)
        if existing_norm is None:
            continue
        if len(existing_norm) == face_len:
            same_dim_positions.append(idx)
            same_dim_norms.append(existing_norm)
        else:
            scores[idx] = _embedding_similarity_between_normalized(face_norm, existing_norm, np)

    if same_dim_norms:
        batch_scores = np.vstack(same_dim_norms) @ face_norm
        for pos, idx in enumerate(same_dim_positions):
            scores[idx] = float(batch_scores[pos])

    for idx, score in enumerate(scores):
        if score is None:
            continue
        if score > best_score:
            second_best_score = best_score
            best_score = score
            best_person = session_embedding_index[idx]
        elif score > second_best_score:
            second_best_score = score
    return best_score, second_best_score, best_person


def _split_cluster_by_max_pair_distance(indices: List[int], dist_matrix, max_distance: float) -> List[List[int]]:
    if len(indices) <= 1:
        return [list(indices)]

    import numpy as np

    threshold = max(0.0, float(max_distance))
    remaining = np.asarray(indices, dtype=np.int64)
    split_clusters: List[List[int]] = []

    while remaining.size:
        # Seed = point with the smallest total distance to everything else
        # still unclustered. Vectorized: was an O(M^2) Python-level loop
        # (sum() over a generator per candidate, for every candidate) --
        # cheap at a few hundred faces, but every sub-cluster formed pays
        # this cost, so it compounds badly once a library's largest DBSCAN
        # cluster reaches the thousands.
        sub = dist_matrix[np.ix_(remaining, remaining)]
        seed_pos = int(np.argmin(sub.sum(axis=1)))
        seed = int(remaining[seed_pos])
        cluster = [seed]
        remaining = np.delete(remaining, seed_pos)

        # dist_to_cluster[i] / max_dist_to_cluster[i] are running aggregates
        # of dist_matrix[remaining[i], member] over members already accepted
        # into `cluster`, updated incrementally as one member is added per
        # inner-loop step. This replaces recomputing every pairwise distance
        # in the growing candidate_cluster from scratch on every candidate
        # check -- that used to be O(cluster_size^2) work per candidate,
        # repeated for every remaining candidate, every step, which made the
        # whole function roughly O(cluster_size^3): fine for the few hundred
        # faces this was calibrated against, but an effective multi-hour
        # hang once one person's cluster grew into the low thousands (a real
        # 2026-09-04 incident: a single clustering job pegged a worker's CPU
        # at 100% for 7+ hours, masked from the 15-minute stale-job sweep by
        # the heartbeat added the same day, once this account's face count
        # reached ~15k). The candidate_cluster's max pairwise distance is
        # exactly max(cluster's existing diameter, max distance from the
        # candidate to each existing member) since every accepted member
        # already satisfies the threshold against the rest of the cluster by
        # construction -- no need to ever recheck pairs already in `cluster`.
        cluster_diam = 0.0
        if remaining.size:
            dist_to_cluster = dist_matrix[seed, remaining].astype(np.float64)
            max_dist_to_cluster = dist_to_cluster.copy()

        while remaining.size:
            candidate_diam = np.maximum(max_dist_to_cluster, cluster_diam)
            ok = candidate_diam <= threshold
            if not np.any(ok):
                break
            ok_idx = np.nonzero(ok)[0]
            # Same tie-break as the original: smallest resulting diameter,
            # then smallest summed distance to the cluster, then smallest
            # index. np.lexsort's last key is primary.
            order = np.lexsort((remaining[ok_idx], dist_to_cluster[ok_idx], candidate_diam[ok_idx]))
            chosen_pos = int(ok_idx[order[0]])
            next_idx = int(remaining[chosen_pos])
            cluster_diam = float(candidate_diam[chosen_pos])
            cluster.append(next_idx)

            remaining = np.delete(remaining, chosen_pos)
            dist_to_cluster = np.delete(dist_to_cluster, chosen_pos)
            max_dist_to_cluster = np.delete(max_dist_to_cluster, chosen_pos)
            if remaining.size:
                new_dists = dist_matrix[next_idx, remaining].astype(np.float64)
                dist_to_cluster = dist_to_cluster + new_dists
                max_dist_to_cluster = np.maximum(max_dist_to_cluster, new_dists)

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


def _update_person_rep_embedding(user_id: str, person_id: str) -> List[float]:
    if face_table_client is None or person_table_client is None:
        return []
    try:
        person = person_table_client.get_entity(partition_key=user_id, row_key=person_id)
        face_ids = json.loads(person.get('faceIds', '[]') or '[]')
    except Exception:
        return []

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
    return rep


def _confirmed_face_count(user_id: str, face_ids: List[str], person_id: str = '') -> int:
    # Was one face_table_client.get_entity() per face_id -- for
    # _load_people_embedding_index (called once per photo now that face
    # assignment is synchronous, see _queue_people_clustering_after_face_processing)
    # that's ~2x(all faces owned by all the user's people) point-read RPCs per
    # photo. _load_user_face_summary_by_id already scans+caches this same data
    # (PEOPLE_SCAN_CACHE_TTL_SECONDS-window shared across calls in a backfill).
    summary = _load_user_face_summary_by_id(user_id)
    count = 0
    for face_id in face_ids:
        face = summary.get(str(face_id))
        if face is None:
            continue
        if person_id and not _face_is_owned_by_person(face, person_id):
            continue
        if _face_is_rejected(face):
            continue
        if _coerce_bool(face.get('confirmedByUser', False)):
            count += 1
    return count


def _load_people_embedding_index(user_id: str) -> List[Dict]:
    if person_table_client is None:
        return []

    # Was rebuilt from scratch (JSON-decode every person's repEmbedding + a
    # fresh numpy normalize) on every single call -- and this runs once per
    # uploaded photo, synchronously, inline in /upload/finalize and
    # /upload/client-processing (see _queue_people_clustering_after_face_processing).
    # For a library with hundreds of people that's real, unvectorized
    # Python-level CPU work (not I/O wait) repeated per photo; under a burst
    # of many photos finishing face detection near-simultaneously, that many
    # gthread threads doing this at once is genuine GIL contention -- the
    # same class of bottleneck that made raising GUNICORN_THREADS 4->12 make
    # things WORSE instead of better (see deploy/resources.bicep's comment on
    # that reverted experiment). Cache the built index the same way
    # _cached_person_rows_for_user already caches the raw rows it's built
    # from -- correctness is unaffected since this index is entirely derived
    # from _cached_person_rows_for_user + the face-summary cache, both
    # already governed by the same PEOPLE_SCAN_CACHE_TTL_SECONDS/
    # _invalidate_people_scan_cache (via _InvalidatingTableClient on every
    # person/face table write), so this adds no new staleness window beyond
    # what those two already tolerate.
    def _build() -> List[Dict]:
        rows = _cached_person_rows_for_user(user_id)
        try:
            import numpy as np
        except Exception:
            np = None

        index = []
        raw_reps: List[List[float]] = []
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
            entry = {
                'personId': person_id,
                'name': row.get('name', ''),
                'faceIds': active_face_ids,
                'repEmbedding': rep,
                'confirmedFaceCount': _confirmed_face_count(user_id, active_face_ids, person_id),
            }
            index.append(entry)
            raw_reps.append(rep)

        if np is not None and index:
            # Precomputed once per cache build instead of once per caller
            # (_normalized_embedding_for_entry would otherwise redo this on
            # every fresh copy of the entry) -- read-only downstream, so
            # sharing the same array across cached copies is safe. Batched
            # across all entries (see _attach_normalized_embeddings_batched)
            # instead of one numpy call per person -- that per-person call
            # overhead, multiplied by every person in the library on every
            # uploaded photo, was the other half of this function's
            # unvectorized CPU cost (see module docstring above).
            _attach_normalized_embeddings_batched(index, raw_reps, np)
        return index

    return _people_embedding_index_cache.get(user_id, _build)


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


def _person_entity_is_named(person: Dict) -> bool:
    """True when the user explicitly named this cluster (not a placeholder).

    Named clusters must never be silently auto-deleted when they transiently
    lose their last face to a merge / identity-propagation reassignment — that
    discards the user's naming work. Callers keep such a person (empty) instead.
    """
    name = str((person or {}).get('name') or '').strip()
    return bool(name) and not _is_unnamed_name(name)


def _update_person_entity_with_retry(
    user_id: str,
    person_id: str,
    mutate_fn: Callable[[Dict], Optional[Dict]],
    *,
    max_attempts: int = 5,
) -> Optional[Dict]:
    """Read-modify-write a person entity using ETag optimistic concurrency.

    Person entities are read-modify-written from several places
    (_add_face_to_person, _remove_face_from_other_people, _update_person_entity)
    and, unlike the per-(user_id, filename) metadata entity, are keyed only
    by (user_id, person_id) -- coarser-grained, since one person aggregates
    faces from many photos. Concurrent ipworker threads/replicas processing
    two different photos for the same user can genuinely both match the
    same existing person, so an unconditional upsert_entity here silently
    drops whichever thread's write lost the race. mutate_fn(person_dict) ->
    mutated dict, or None to skip the write entirely (e.g. the mutation
    turned out to be a no-op).
    """
    if person_table_client is None:
        return None
    for _ in range(max_attempts):
        try:
            person = person_table_client.get_entity(partition_key=user_id, row_key=person_id)
        except Exception:
            return None
        mutated = mutate_fn(dict(person))
        if mutated is None:
            return None
        try:
            person_table_client.update_entity(
                mutated, etag=person.metadata['etag'], match_condition=MatchConditions.IfNotModified,
            )
            return mutated
        except ResourceModifiedError:
            continue  # someone else wrote first -- re-read and retry
        except Exception:
            return None
    worker_logger.warning('person entity update retries exhausted for %s/%s', user_id, person_id)
    return None


def _update_person_entity(user_id: str, person_id: str, updates: Dict) -> bool:
    result = _update_person_entity_with_retry(user_id, person_id, lambda person: {**person, **updates})
    return result is not None


def _batch_upsert_entities(table_client, entities: List[Dict], *, chunk_size: int = 100) -> None:
    """Upsert entities in transactional batches instead of one round-trip each.

    Azure Table transactions require every entity in a batch to share the same
    PartitionKey and cap out at 100 operations, so callers must pass entities that
    all live in one partition. Uses the same MERGE semantics as ``upsert_entity``
    and falls back to per-entity upserts if a batch is rejected, so a single bad
    row can never drop the rest.
    """
    if table_client is None or not entities:
        return
    for start in range(0, len(entities), chunk_size):
        chunk = entities[start:start + chunk_size]
        try:
            table_client.submit_transaction([('upsert', entity) for entity in chunk])
        except Exception:
            for entity in chunk:
                try:
                    table_client.upsert_entity(entity)
                except Exception:
                    pass


def _load_searchable_person_name_index(user_id: str) -> Dict[str, str]:
    if person_table_client is None:
        return {}
    rows = _cached_person_rows_for_user(user_id)
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
        # Keep a user-named cluster even when it loses its last face here (only
        # remove unnamed clusters); deleting it would silently discard the name.
        try:
            if _person_entity_is_named(person):
                person['faceIds'] = json.dumps([])
                person_table_client.upsert_entity(person)
            else:
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


def _remove_face_from_person_with_retry(
    user_id: str, person_id: str, face_id: str, *, max_attempts: int = 5,
) -> Optional[str]:
    """Removes face_id from one person's faceIds, re-reading fresh state on
    every attempt -- an etag conflict means another thread/replica just
    changed this same person, so which branch (update / keep-empty-named /
    delete) applies may have changed too, not just the faceIds list.
    Returns 'updated', 'kept_empty', 'deleted', or None if nothing needed
    to change (face_id was already gone by the time this ran)."""
    if person_table_client is None:
        return None
    for _ in range(max_attempts):
        try:
            person = person_table_client.get_entity(partition_key=user_id, row_key=person_id)
        except Exception:
            return None
        try:
            face_ids = json.loads(person.get('faceIds', '[]') or '[]')
        except Exception:
            face_ids = []
        if face_id not in face_ids:
            return None
        next_face_ids = [fid for fid in face_ids if fid != face_id]
        etag = person.metadata['etag']
        try:
            if next_face_ids:
                person['faceIds'] = json.dumps(next_face_ids)
                person_table_client.update_entity(person, etag=etag, match_condition=MatchConditions.IfNotModified)
                return 'updated'
            if _person_entity_is_named(person):
                # Preserve a user-named cluster that loses its last face to this
                # reassignment; keep it empty rather than silently deleting it.
                person['faceIds'] = json.dumps([])
                person_table_client.update_entity(person, etag=etag, match_condition=MatchConditions.IfNotModified)
                return 'kept_empty'
            person_table_client.delete_entity(partition_key=user_id, row_key=person_id, etag=etag, match_condition=MatchConditions.IfNotModified)
            return 'deleted'
        except ResourceModifiedError:
            continue  # someone else wrote first -- re-read and retry
        except Exception:
            return None
    worker_logger.warning('person face-removal retries exhausted for %s/%s', user_id, person_id)
    return None


def _remove_face_from_other_people(user_id: str, face_id: str, keep_person_id: str) -> Dict:
    if person_table_client is None or not face_id:
        return {'removed': 0, 'deletedPeople': 0, 'touchedPeople': []}
    # Was an always-live person_table_client.query_entities call, bypassing
    # _person_scan_cache entirely -- unlike the reads elsewhere in this file
    # that share that cache, this one re-scanned the full person partition on
    # every call regardless of TTL. _add_face_to_person's only caller
    # (_assign_faces_to_people_incrementally) only ever passes face_ids
    # already confirmed ownerless by _face_ids_awaiting_person_assignment, so
    # in the common case every row here has to be examined just to find
    # nothing to remove. Reading through the cache costs nothing when a
    # concurrent read already warmed it (e.g. the same call's own
    # _load_people_embedding_index at the top of _assign_faces_to_people_incrementally),
    # and still self-heals within PEOPLE_SCAN_CACHE_TTL_SECONDS otherwise --
    # same staleness tolerance every other reader of this cache already
    # accepts; the removal below still re-reads fresh state per-candidate via
    # _remove_face_from_person_with_retry before writing, so a stale
    # candidate list can only cost a wasted no-op retry, never a missed
    # removal it would have caught anyway.
    rows = _cached_person_rows_for_user(user_id)

    removed = 0
    deleted_people = 0
    touched_people = []
    for row in rows:
        person_id = str(row.get('RowKey') or '')
        if not person_id or person_id == keep_person_id:
            continue
        try:
            face_ids = json.loads(row.get('faceIds', '[]') or '[]')
        except Exception:
            face_ids = []
        if face_id not in face_ids:
            continue
        # row is a possibly-stale snapshot from the query above -- the retry
        # helper re-reads fresh state (and re-checks face_id is still
        # present) before writing, so a race with another thread/replica
        # touching this same person is handled there, not here.
        outcome = _remove_face_from_person_with_retry(user_id, person_id, face_id)
        if outcome is None:
            continue
        removed += 1
        touched_people.append(person_id)
        if outcome == 'deleted':
            deleted_people += 1
        elif outcome == 'updated':
            _update_person_rep_embedding(user_id, person_id)
    return {'removed': removed, 'deletedPeople': deleted_people, 'touchedPeople': touched_people}


def _add_face_to_person(user_id: str, person_id: str, face_id: str) -> bool:
    """Returns whether the person's faceIds actually changed AND its
    repEmbedding was refreshed as a result -- callers that need a refreshed
    rep embedding (e.g. _assign_faces_to_people_incrementally) use this to
    avoid a redundant second _update_person_rep_embedding call for a person
    this function already just refreshed."""
    if person_table_client is None or not person_id or not face_id:
        return False
    if face_table_client is not None:
        try:
            face = face_table_client.get_entity(partition_key=user_id, row_key=face_id)
            if _face_is_rejected(face) or (_face_is_suspicious(face) and not _face_is_confirmed(face)):
                return False
        except Exception:
            pass
    _remove_face_from_other_people(user_id, face_id, person_id)

    def _mutate(person: Dict) -> Optional[Dict]:
        try:
            face_ids = json.loads(person.get('faceIds', '[]') or '[]')
        except Exception:
            face_ids = []
        next_face_ids = _dedupe_face_ids_preserving_order([*face_ids, face_id])
        if next_face_ids == face_ids:
            return None
        person['faceIds'] = json.dumps(next_face_ids)
        return person

    result = _update_person_entity_with_retry(user_id, person_id, _mutate)
    if result is not None:
        _update_person_rep_embedding(user_id, person_id)
        return True
    return False


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
    # Per-file jobs (preview, ipwork, clustering) are all userId-partitioned
    # (see _job_partition_key) -- library-scoped job types (library_clean/
    # library_download) never correlate to an individual filename, so scoping
    # this to the user's own partition covers every realistic match.
    if jobs_table_client is None or not filename:
        return 0
    try:
        rows = list(jobs_table_client.query_entities(f"PartitionKey eq '{_escape_odata(user_id)}'"))
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
        correlation_id = str(row.get('correlationId') or '')
        if not (
            row_filename == filename
            or filename == correlation_id
            or any(token and (job_id.startswith(token) or row_key.startswith(token) or correlation_id.startswith(token)) for token in job_prefixes)
        ):
            continue
        try:
            jobs_table_client.delete_entity(partition_key=user_id, row_key=row_key)
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
    if not user_id or not person_id:
        return []
    # See _confirmed_face_count for why this uses the cached face summary
    # instead of a per-face_id get_entity() call.
    summary = _load_user_face_summary_by_id(user_id)
    active_face_ids = []
    for face_id in face_ids:
        face = summary.get(str(face_id))
        if face is None:
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


def _assign_faces_to_people_incrementally(user_id: str, filename: str, face_ids: List[str]) -> Tuple[Dict[str, str], set]:
    if not face_ids or face_table_client is None or person_table_client is None:
        return {}, set()
    try:
        import numpy as np
    except Exception:
        return {}, set()

    session_embedding_index = [dict(entry) for entry in _load_people_embedding_index(user_id)]
    # Keyed view of the same entries session_embedding_index holds, so the
    # people_to_refresh loop below can patch a person's repEmbedding back
    # into its entry in O(1) instead of re-scanning the list. Entries are the
    # same dict objects in both structures, so mutating via this map mutates
    # what _best_two_person_matches sees too.
    index_by_person_id = {
        str(entry.get('personId') or ''): entry for entry in session_embedding_index
    }
    assignments: Dict[str, str] = {}
    created_person_ids: set = set()
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

        best_score, second_best_score, best_person = _best_two_person_matches(
            face_norm, session_embedding_index, np,
        )

        person_id = ''
        # Whether _add_face_to_person already ran _update_person_rep_embedding
        # for this person as part of this same write -- if so, the
        # people_to_refresh loop below must not redo it (that used to happen
        # unconditionally for every matched face: get person + get every one
        # of its face entities + upsert, all a second time for no new data).
        rep_already_refreshed = False
        if (
            best_person
            and best_score >= PEOPLE_CLUSTER_ASSIGN_THRESHOLD
            and (best_score - second_best_score) >= PEOPLE_CLUSTER_ASSIGN_MARGIN
        ):
            person_id = str(best_person.get('personId') or '')
            rep_already_refreshed = _add_face_to_person(user_id, person_id, face_id)
            if rep_already_refreshed:
                best_person['faceIds'] = [*best_person.get('faceIds', []), face_id]
        else:
            name = next_unnamed_person_name()
            person_id = _create_person_entity(user_id, [face_id], emb, name=name)
            if person_id:
                created_person_ids.add(person_id)
            new_entry = {
                'personId': person_id,
                'name': name,
                'faceIds': [face_id],
                'repEmbedding': emb,
                '_normalized_rep_embedding': face_norm,
                'confirmedFaceCount': 0,
            }
            session_embedding_index.append(new_entry)
            index_by_person_id[person_id] = new_entry

        if not person_id:
            continue
        face_ent['personId'] = person_id
        try:
            face_table_client.upsert_entity(face_ent)
            if not rep_already_refreshed:
                people_to_refresh.add(person_id)
        except Exception:
            pass
        assignments[face_id] = person_id

    for person_id in people_to_refresh:
        new_rep = _update_person_rep_embedding(user_id, person_id)
        entry = index_by_person_id.get(person_id)
        if entry is not None and new_rep:
            entry['repEmbedding'] = new_rep
            entry['_normalized_rep_embedding'] = _normalized_embedding(new_rep, np)

    if assignments:
        # Pass the names already sitting in session_embedding_index instead
        # of letting _rebuild_metadata_faces_for_filename fall back to
        # _load_searchable_person_name_index -- that helper reads through
        # _person_scan_cache, which the person-table writes above (via
        # _add_face_to_person/_create_person_entity/_update_person_rep_embedding)
        # just invalidated for this user, so the fallback would otherwise
        # force yet another full person-partition scan this call already has
        # the answer to in memory.
        searchable_person_index = {
            str(pid): str(entry.get('name') or '')
            for pid, entry in index_by_person_id.items()
            if entry.get('name') and not _is_unnamed_name(str(entry.get('name') or ''))
        }
        _rebuild_metadata_faces_for_filename(
            user_id, filename, searchable_person_index=searchable_person_index,
        )

    # session_embedding_index now reflects every write this call just made
    # (new persons appended, matched persons' faceIds/repEmbedding patched
    # above) -- hand it straight back to the cache instead of leaving it
    # invalidated by the writes above. Without this, the very next
    # people_incremental_assign message for this user (typically seconds
    # away during a backfill/upload burst, well under
    # PEOPLE_SCAN_CACHE_TTL_SECONDS) would re-derive the exact same index
    # from scratch: a full person-partition scan, a full face-summary scan
    # for _confirmed_face_count, and renormalizing every person's embedding
    # -- all Table Storage round-trips this process already has the answer
    # to in memory.
    _people_embedding_index_cache.set(user_id, session_embedding_index)
    return assignments, created_person_ids


def _load_existing_people_for_matching(user_id: str) -> List[Dict]:
    if person_table_client is None:
        return []
    existing_rows = _cached_person_rows_for_user(user_id)

    existing_people = []
    for row in existing_rows:
        person_id = str(row.get('RowKey') or '')
        if not person_id:
            continue
        try:
            face_ids = json.loads(row.get('faceIds', '[]') or '[]')
        except Exception:
            face_ids = []
        active_face_ids = _active_face_ids_for_person(user_id, person_id, face_ids)
        if not active_face_ids:
            continue
        try:
            rep_embedding = json.loads(row.get('repEmbedding', '[]') or '[]')
        except Exception:
            rep_embedding = []
        existing_people.append({
            'personId': person_id,
            'name': row.get('name', ''),
            'faceIds': active_face_ids,
            'repEmbedding': rep_embedding,
            'confirmedFaceCount': _confirmed_face_count(user_id, active_face_ids, person_id),
        })
    return existing_people


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

    # Faces that must never be re-clustered away from their current person:
    # confirmed / propagation-assigned (sticky), or any face already owned by a
    # user-named cluster. This is the upload path (people_cluster runs on every
    # new photo); without this guard DBSCAN re-pooled a named person's faces into
    # fresh unnamed clusters, then stale-membership repair emptied the named
    # person — the "named cluster gets cleaned up after upload" bug. Faces stay
    # glued to their person here; only genuinely free faces get (re)clustered,
    # mirroring the guard in _build_people_recluster_plan.
    try:
        named_person_ids = {
            str(row.get('RowKey') or '')
            for row in person_table_client.query_entities(f"PartitionKey eq '{_escape_odata(user_id)}'")
            if _person_entity_is_named(row)
        }
    except Exception:
        named_person_ids = set()

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
            owner_id = str(row.get('personId') or '')
            if owner_id and (_face_assignment_is_sticky(row) or owner_id in named_person_ids):
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

    if preserve_people is None:
        preserve_people = _load_existing_people_for_matching(user_id)
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
    # Staged, not written yet -- _create_person_entity(_defer_into=...) below
    # stashes each cluster's person entity here instead of upserting inline.
    # 2026-09-04: this loop runs once per DBSCAN cluster (thousands, at this
    # account's ~15k-face scale) and used to call upsert_entity() inline on
    # every iteration -- one sequential network round-trip per cluster, the
    # dominant cost of a full recluster once _split_cluster_by_max_pair_distance's
    # O(n^3) hang (fixed the same day) was no longer masking it. Deduping by
    # person_id here also means a person touched by multiple clusters gets
    # written once with its final combined faceIds, not once per touch.
    person_entities_to_write: Dict[str, Dict] = {}

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
                _defer_into=person_entities_to_write,
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
                _defer_into=person_entities_to_write,
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

    # Flush every cluster's staged person entity now, deduped by person_id,
    # in real Table Storage transactional batches instead of the sequential
    # upsert-per-cluster this used to be.
    _batch_upsert_entities(person_table_client, list(person_entities_to_write.values()))

    # Batch update faces -- was a sequential upsert-per-face loop despite the
    # comment; see _batch_upsert_entities for why this is now a real batch.
    _batch_upsert_entities(face_table_client, faces_to_update)

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

    # Batch update metadata -- was a sequential upsert-per-photo loop despite
    # the comment (same class of bug as the two loops above: fine at a
    # handful of touched photos, thousands of sequential round-trips once a
    # full recluster touches most of a ~15k-face library).
    if metadata_updates:
        try:
            query = f"PartitionKey eq '{_escape_odata(user_id)}'"
            metadata_rows = list(metadata_table_client.query_entities(query))
            metadata_entities_to_update = []
            for metadata in metadata_rows:
                if metadata.get('RowKey') in metadata_updates:
                    people_ids = parse_json_list(metadata.get('peopleIds', '[]'))
                    for person_id in metadata_updates[metadata.get('RowKey')]:
                        if person_id not in people_ids:
                            people_ids.append(person_id)
                    metadata['peopleIds'] = json.dumps(people_ids)
                    metadata_entities_to_update.append(metadata)
            _batch_upsert_entities(metadata_table_client, metadata_entities_to_update)
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

        # Track newly-created person ids as they're created instead of diffing two
        # full person-table scans (before/after) -- each of those was a full
        # partition scan paid just to compute a count.
        assignments: Dict[str, str] = {}
        created_person_ids: set = set()
        for filename, face_ids in candidates_by_filename.items():
            filename_assignments, filename_created = _assign_faces_to_people_incrementally(user_id, filename, face_ids)
            assignments.update(filename_assignments)
            created_person_ids.update(filename_created)

        return {
            'success': True,
            'queued': False,
            'candidateFaces': sum(len(face_ids) for face_ids in candidates_by_filename.values()),
            'assignedFaces': len(assignments),
            'createdPeople': len(created_person_ids),
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
    if not str(manifest.get('kind') or '').endswith('_snapshot'):
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
        app.logger.exception('Clustering dependencies unavailable')
        return {'error': 'clustering unavailable'}

    try:
        rows = list(face_table_client.query_entities(f"PartitionKey eq '{_escape_odata(user_id)}'"))
    except Exception:
        rows = []
    existing_rows = _cached_person_rows_for_user(user_id)

    existing_people = []
    existing_face_ids_by_person: Dict[str, List[str]] = {}
    # Track user-named clusters so a plain recluster never re-pools (and thus
    # never scatters/renames) them — naming is explicit user intent. The explicit
    # repair path (allow_reassign_confirmed) can still override this.
    named_person_ids: set = set()
    for row in existing_rows:
        try:
            face_ids = json.loads(row.get('faceIds', '[]') or '[]')
        except Exception:
            face_ids = []
        person_id = str(row.get('RowKey') or '')
        if person_id and _person_entity_is_named(row):
            named_person_ids.add(person_id)
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
    skip_reasons = {'no_id_or_emb': 0, 'not_clusterable': 0, 'embedding_version': 0, 'dim_mismatch': 0, 'sticky': 0}
    embedding_versions_seen = set()
    for row in rows:
        face_id = str(row.get('RowKey') or '')
        emb = _face_embedding_from_entity(row)
        if not face_id or not emb:
            skip_reasons['no_id_or_emb'] += 1
            continue
        if not _face_is_clusterable(row):
            skip_reasons['not_clusterable'] += 1
            continue
        version = _face_embedding_version(row)
        embedding_versions_seen.add(version)
        if not _face_embedding_allowed_for_clustering(row):
            skip_reasons['embedding_version'] += 1
            continue
        if expected_embedding_dim == 0:
            expected_embedding_dim = len(emb)
        elif len(emb) != expected_embedding_dim:
            skip_reasons['dim_mismatch'] += 1
            continue
        owner_id = str(row.get('personId') or '')
        # Keep a face glued to its person when it is sticky (confirmed / propagation
        # assigned) OR belongs to a user-named cluster. Without the named-cluster
        # guard, reclustering re-pooled a named person's unconfirmed faces and
        # scattered them into fresh unnamed clusters — silently "un-merging" and
        # un-naming the person. The explicit repair path can still reassign.
        if owner_id and not allow_reassign_confirmed and (
            _face_assignment_is_sticky(row) or owner_id in named_person_ids
        ):
            skipped_confirmed += 1
            skip_reasons['sticky'] += 1
            continue
        embeddings.append(emb)
        face_ids.append(face_id)
        face_entities[face_id] = row

    if not embeddings:
        app.logger.warning('Recluster plan: no clusterable faces. Total rows: %d, skip_reasons: %s, skipped_confirmed: %d, embedding_versions: %s', len(rows), skip_reasons, skipped_confirmed, embedding_versions_seen)
        return {
            'created': [],
            'assignments': {},
            'people': {},
            'candidateFaces': 0,
            'skippedConfirmedFaces': skipped_confirmed,
            'debugSkipReasons': skip_reasons,
            'debugEmbeddingVersions': sorted(list(embedding_versions_seen)),
        }

    X = np.asarray(embeddings, dtype=_embedding_precision_dtype(np))
    Xn = X / (np.linalg.norm(X, axis=1, keepdims=True) + 1e-12)

    def _dbscan_pass(global_indices: List[int], eps: float, max_pair_distance: float) -> Dict[int, List[int]]:
        # Runs DBSCAN + max-pair-distance refinement on a SUBSET of faces
        # (one alignment tier), entirely in that subset's own local index
        # space, then maps the result back to indices into the shared
        # embeddings/face_ids arrays. Keeping each tier's distance matrix
        # separate is the whole point: a tier-appropriate eps only makes
        # sense if it's never applied to another tier's differently-scaled
        # distances (see PEOPLE_CLUSTER_EPS_2PT for the calibration this
        # protects).
        if not global_indices:
            return {}
        sub_Xn = Xn[global_indices]
        sub_dist = np.clip(1.0 - (sub_Xn @ sub_Xn.T), 0.0, 2.0)
        sub_labels = DBSCAN(eps=eps, min_samples=2, metric='precomputed').fit(sub_dist).labels_
        local_clusters: Dict[int, List[int]] = {}
        next_noise_label = int(np.max(sub_labels)) + 1 if len(sub_labels) else 0
        for local_idx, label in enumerate(sub_labels):
            if label == -1:
                local_clusters[next_noise_label] = [local_idx]
                next_noise_label += 1
            else:
                local_clusters.setdefault(int(label), []).append(local_idx)
        local_clusters = _refine_clusters_by_max_pair_distance(local_clusters, sub_dist, max_pair_distance)
        return {label: [global_indices[li] for li in local_idxs] for label, local_idxs in local_clusters.items()}

    tier_indices: Dict[str, List[int]] = {tier: [] for tier in PEOPLE_CLUSTER_ALIGNMENT_TIERS}
    for idx, face_id in enumerate(face_ids):
        tier = _face_alignment_tier(face_entities[face_id])
        if tier in tier_indices:
            tier_indices[tier].append(idx)

    clusters: Dict[int, List[int]] = {}
    next_label = 0
    for tier_clusters in (
        _dbscan_pass(
            tier_indices['landmark-5pt'],
            PEOPLE_CLUSTER_EPS,
            min(PEOPLE_CLUSTER_EPS, PEOPLE_CLUSTER_MAX_PAIR_DISTANCE, PEOPLE_CLUSTER_ABSOLUTE_MAX_PAIR_DISTANCE),
        ),
        _dbscan_pass(tier_indices['landmark-2pt'], PEOPLE_CLUSTER_EPS_2PT, PEOPLE_CLUSTER_EPS_2PT),
        _dbscan_pass(tier_indices['landmark-5pt-mp'], PEOPLE_CLUSTER_EPS_MP, PEOPLE_CLUSTER_EPS_MP),
    ):
        for _, global_idxs in tier_clusters.items():
            clusters[next_label] = global_idxs
            next_label += 1

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
        # Never delete a user-named cluster that transiently lost its faces to a
        # recluster reassignment — deleting it silently discards the user's name
        # (the "cluster lost its name after find-faces/refresh" bug). Keep it empty
        # like every other membership path does; only unnamed clusters are removed.
        if _person_entity_is_named(person):
            try:
                person['faceIds'] = json.dumps([])
                person_table_client.upsert_entity(person)
            except Exception:
                pass
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
                elif len(next_face_ids) != len(face_ids):
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


def _unblock_low_confidence_faces(user_id: str, *, dry_run: bool = True) -> Dict:
    """Un-reject faces that were previously rejected as low-confidence but now
    meet the current FACE_LOW_CONFIDENCE_REJECT_BELOW / FACE_MIN_STORE_CONFIDENCE
    thresholds. This is the counterpart to _suppress_suspicious_faces and is
    needed when the operator *lowers* the rejection threshold to accept more faces.
    """
    if face_table_client is None:
        return {'success': False, 'error': 'People features not configured'}
    try:
        rows = list(face_table_client.query_entities(
            f"PartitionKey eq '{_escape_odata(user_id)}'"
        ))
    except Exception:
        rows = []

    candidates = []
    affected_files: set = set()
    for row in rows:
        # Only consider faces that were auto-rejected for low confidence reasons.
        # Leave user-confirmed rejections alone.
        if not _face_is_rejected(row):
            continue
        rejected_reason = str(row.get('rejectedReason') or '').strip()
        review_status = str(row.get('reviewStatus') or '').strip().lower()
        # Only un-reject faces that were auto-suppressed for low confidence,
        # not faces the user manually rejected.
        if review_status == 'rejected' and rejected_reason not in (
            'low_confidence_false_positive', 'low_confidence', ''
        ):
            continue
        if _face_is_confirmed(row):
            continue
        try:
            confidence = float(row.get('confidence', 0.0) or 0.0)
        except Exception:
            confidence = 0.0
        # This face would now pass quality — it should be un-rejected.
        normalized = _normalize_face_bbox(row)
        if not _face_passes_auto_store_quality(row, confidence, normalized):
            continue
        face_id = str(row.get('RowKey') or '')
        filename = str(row.get('filename') or '')
        if filename:
            affected_files.add(filename)
        candidates.append({
            'faceId': face_id,
            'filename': filename,
            'confidence': confidence,
            'newStatus': 'suspicious' if confidence < SUSPICIOUS_FACE_CONFIDENCE else 'pending',
        })

    result: Dict = {
        'success': True,
        'dryRun': dry_run,
        'rejectThreshold': FACE_LOW_CONFIDENCE_REJECT_BELOW,
        'minStoreThreshold': FACE_MIN_STORE_CONFIDENCE,
        'suspiciousThreshold': SUSPICIOUS_FACE_CONFIDENCE,
        'candidateFaces': len(candidates),
        'affectedFiles': len(affected_files),
        'faces': candidates[:100],
    }
    if dry_run or not candidates:
        return result

    snapshot_id = _create_people_repair_snapshot(
        user_id,
        snapshot_prefix='unblock-faces-snapshot',
        kind='unblock_faces_snapshot',
    )

    unblocked = 0
    for item in candidates:
        face_id = item['faceId']
        try:
            face = face_table_client.get_entity(partition_key=user_id, row_key=face_id)
        except Exception:
            continue
        face['rejected'] = False
        face.pop('rejectedReason', None)
        face.pop('rejectedAt', None)
        try:
            confidence = float(face.get('confidence', 0.0) or 0.0)
        except Exception:
            confidence = 0.0
        if confidence < SUSPICIOUS_FACE_CONFIDENCE:
            face['reviewStatus'] = 'suspicious'
            face['suspiciousReason'] = 'low_confidence'
        else:
            face.pop('reviewStatus', None)
            face.pop('suspiciousReason', None)
        try:
            face_table_client.upsert_entity(face)
            unblocked += 1
        except Exception:
            pass

    rebuild = _rebuild_metadata_faces_for_filenames(user_id, affected_files)

    result.update({
        'snapshotId': snapshot_id,
        'unblockedFaces': unblocked,
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
        if face_ids or person_id not in people_by_id:
            continue
        # Keep user-named clusters even when they become empty so recluster/
        # repair passes never discard explicit naming work.
        if _person_entity_is_named(people_by_id[person_id]):
            continue
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
                person = people_by_id[person_id]
                if _person_entity_is_named(person):
                    person['faceIds'] = json.dumps([])
                    person_table_client.upsert_entity(person)
                else:
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
    # Read by _store_client_face_entities (storage_utils.py) via
    # face_summary_lookup to decide whether a re-detected face's
    # propagation-assigned personId should be preserved -- added when that
    # function switched from its own uncached full-table scan to this shared
    # cached summary, so this projection needs to carry everything that
    # decision already depended on.
    'assignedByPropagation',
]


def _is_not_found_error(exc: Exception) -> bool:
    message = str(exc)
    return '404' in message or 'ResourceNotFound' in message or 'does not exist' in message.lower()


def _load_user_face_summary_by_id(user_id: str) -> Dict[str, Dict]:
    if face_table_client is None:
        return {}

    def _fetch() -> List[Dict]:
        query = f"PartitionKey eq '{_escape_odata(user_id)}'"
        try:
            return list(face_table_client.query_entities(query, select=FACE_SUMMARY_COLUMNS))
        except TypeError:
            try:
                return list(face_table_client.query_entities(query))
            except Exception:
                return []
        except Exception:
            return []

    rows = _face_summary_scan_cache.get(user_id, _fetch)
    return {str(row.get('RowKey') or ''): row for row in rows if row.get('RowKey')}


def _scan_person_and_face_rows(user_id: str) -> Tuple[List[Dict], Dict[str, Dict]]:
    """Shared cheap scan used by list_persons/list_faces: every person row (sorted
    by RowKey) plus the bulk face-summary map. Neither call does per-item work
    (SAS minting, individual face lookups) -- callers do that only for the page
    they're about to return.
    """
    rows = sorted(_cached_person_rows_for_user(user_id), key=lambda r: str(r.get('RowKey', '')))
    face_by_id = _load_user_face_summary_by_id(user_id)
    return rows, face_by_id


def _face_thumbnail_url(filename: str, user_id: str = '') -> str:
    """Direct-blob thumbnail URL for a face's source photo, for the People page.

    A face tile renders the photo's thumbnail cropped to the face bbox, so it can
    load straight from storage via a read SAS instead of streaming through the
    backend proxy (the slow, memory-heavy path). Returns '' when no direct URL can
    be served — RAW/HEIC needs the server-side preview converter, and non-SAS mode
    yields a proxy path — so the frontend keeps its existing proxy fallback there.

    For anonymized photos the thumbnail blob lives under the anonymous UUID, so we
    resolve the physical blob name (O(1) via the warm reverse cache) before minting.
    """
    if not filename:
        return ''
    if _filename_requires_backend_preview(filename):
        return ''
    blob_name = resolve_physical_blob_name(user_id, filename, 'image') if user_id else filename
    url = make_media_url(filename, 'thumbnail', blob_name=blob_name)
    return url if url.startswith('http') else ''


def _face_summary_for_person_list(face_id: str, face: Dict, user_id: str = '') -> Dict:
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
        'thumbnailUrl': _face_thumbnail_url(str(face.get('filename') or ''), user_id),
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
        person_id = str(row.get('RowKey') or '')
        person_name = str(row.get('name', '') or '')
        if not person_id:
            continue
        if not PEOPLE_SUGGEST_INCLUDE_UNNAMED and _is_unnamed_name(person_name):
            continue
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
        confirmed_face_count = 0
        rep_face = None
        rep_face_score = None
        for face_id in face_ids:
            try:
                face = face_by_id.get(str(face_id))
                if face is None and face_table_client is not None:
                    face = face_table_client.get_entity(partition_key=user_id, row_key=face_id)
                if (
                    face
                    and _face_is_owned_by_person(face, person_id)
                    and not _face_is_rejected(face)
                ):
                    active_face_ids.append(face_id)
                    if _face_is_confirmed(face):
                        confirmed_face_count += 1
                    score = _face_preview_priority(face)
                    if rep_face is None or rep_face_score is None or score > rep_face_score:
                        rep_face = _face_summary_for_person_list(str(face_id), face, user_id)
                        rep_face_score = score
            except Exception:
                continue
        if len(active_face_ids) < PEOPLE_SUGGEST_MIN_FACES:
            continue
        if confirmed_face_count < PEOPLE_SUGGEST_MIN_CONFIRMED_FACES:
            continue
        if rep_face is None:
            continue
        try:
            rep_confidence = float(rep_face.get('confidence', 0.0) or 0.0)
        except Exception:
            rep_confidence = 0.0
        if rep_confidence < PEOPLE_SUGGEST_MIN_REP_FACE_CONFIDENCE:
            continue
        try:
            declined = json.loads(row.get('declinedSuggestions', '[]') or '[]')
            declined = {str(pid) for pid in declined} if isinstance(declined, list) else set()
        except Exception:
            declined = set()
        people.append({
            'personId': person_id,
            'name': person_name,
            'faceCount': len(active_face_ids),
            'confirmedFaceCount': confirmed_face_count,
            'repEmbedding': rep,
            'representativeFace': rep_face,
            'declined': declined,
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
            if str(other['personId']) in person['declined'] or str(person['personId']) in other['declined']:
                continue
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
                'sourceFace': source.get('representativeFace'),
                'targetPersonId': target.get('personId'),
                'targetName': target.get('name', ''),
                'targetFaceCount': target.get('faceCount', 0),
                'targetFace': target.get('representativeFace'),
                'similarity': score,
            })
            if len(suggestions) >= limit:
                break
        if len(suggestions) >= limit:
            break

    suggestions.sort(key=lambda s: s.get('similarity', 0.0), reverse=True)
    return suggestions


def _add_declined_suggestion(user_id: str, person_id: str, other_person_id: str) -> bool:
    """Record that ``person_id`` should no longer be suggested to merge with
    ``other_person_id``. The declined partner list is stored on the person
    entity so declined pairs stay hidden across future suggestion recomputes."""
    if person_table_client is None or not person_id or not other_person_id:
        return False
    try:
        entity = person_table_client.get_entity(partition_key=user_id, row_key=person_id)
    except Exception:
        return False
    try:
        declined = json.loads(entity.get('declinedSuggestions', '[]') or '[]')
        if not isinstance(declined, list):
            declined = []
    except Exception:
        declined = []
    declined = [str(pid) for pid in declined]
    if str(other_person_id) not in declined:
        declined.append(str(other_person_id))
    entity['declinedSuggestions'] = json.dumps(declined)
    try:
        person_table_client.upsert_entity(entity)
        return True
    except Exception:
        return False


def _person_declined_face_ids(person: Dict) -> set:
    try:
        declined = json.loads(person.get('declinedFaceSuggestions', '[]') or '[]')
    except Exception:
        declined = []
    return {str(fid) for fid in declined} if isinstance(declined, list) else set()


def _add_declined_face_suggestions(user_id: str, person_id: str, face_ids: List[str]) -> int:
    """Record that ``face_ids`` should no longer be suggested for ``person_id``
    so a declined per-face suggestion stays hidden across future propagations."""
    if person_table_client is None or not person_id:
        return 0
    try:
        person = person_table_client.get_entity(partition_key=user_id, row_key=person_id)
    except Exception:
        return 0
    declined = _person_declined_face_ids(person)
    before = len(declined)
    for face_id in face_ids or []:
        value = str(face_id or '').strip()
        if value:
            declined.add(value)
    if len(declined) == before:
        return 0
    person['declinedFaceSuggestions'] = json.dumps(sorted(declined))
    try:
        person_table_client.upsert_entity(person)
    except Exception:
        return 0
    return len(declined) - before


def _propagate_person_identity(
    user_id: str,
    person_id: str,
    *,
    apply: bool = True,
    collect_suggestions: bool = True,
    auto_threshold: float = PEOPLE_PROPAGATE_AUTO_THRESHOLD,
    review_threshold: float = PEOPLE_PROPAGATE_REVIEW_THRESHOLD,
    margin: float = PEOPLE_PROPAGATE_MARGIN,
    max_suggestions: int = PEOPLE_PROPAGATE_MAX_SUGGESTIONS,
) -> Dict:
    """Use a named person's learned representative embedding to reclaim that
    person's faces from *unnamed* clusters (and truly unclustered faces).

    High-confidence matches (>= ``auto_threshold`` with a margin over the best
    rival named person) are moved in automatically when ``apply`` is set;
    borderline matches (>= ``review_threshold``) are returned as a per-face
    review queue. Faces confirmed to, or owned by, another *named* person are
    never touched."""
    empty = {'autoAssigned': [], 'autoAssignedCount': 0, 'suggestions': [], 'candidateFaces': 0}
    if face_table_client is None or person_table_client is None:
        return {**empty, 'error': 'People features not configured'}
    try:
        import numpy as np
    except Exception:
        return {**empty, 'error': 'clustering unavailable'}

    try:
        target = person_table_client.get_entity(partition_key=user_id, row_key=person_id)
    except Exception:
        return {**empty, 'error': 'person not found'}

    target_norm = _normalized_embedding(_parse_embedding(target.get('repEmbedding', '[]')), np)
    if target_norm is None:
        return {**empty, 'skipped': 'no representative embedding'}
    target_dim = len(target_norm)

    try:
        target_face_ids = json.loads(target.get('faceIds', '[]') or '[]')
    except Exception:
        target_face_ids = []
    if len(_active_face_ids_for_person(user_id, person_id, target_face_ids)) < PEOPLE_PROPAGATE_MIN_FACES:
        return {**empty, 'skipped': 'not enough anchor faces'}

    declined = _person_declined_face_ids(target)

    # Other named people: protect their faces and reject candidates that are a
    # better match for a different known person.
    try:
        person_rows = list(person_table_client.query_entities(f"PartitionKey eq '{_escape_odata(user_id)}'"))
    except Exception:
        person_rows = []
    named_person_ids = set()
    other_named_reps = []
    for row in person_rows:
        pid = str(row.get('RowKey') or '')
        if not pid or _is_unnamed_name(str(row.get('name') or '')):
            continue
        named_person_ids.add(pid)
        if pid == person_id:
            continue
        other_norm = _normalized_embedding(_parse_embedding(row.get('repEmbedding', '[]')), np)
        if other_norm is not None and len(other_norm) == target_dim:
            other_named_reps.append(other_norm)
    other_matrix = np.vstack(other_named_reps) if other_named_reps else None

    # Stream the face table and score it in bounded batches. Loading every row at
    # once (each with an inline embedding) plus the full numpy matrix was the OOM
    # driver; here peak memory is one PEOPLE_PROPAGATE_SCAN_BATCH chunk. Per-face
    # decisions are independent, so batching yields identical matches.
    dtype = _embedding_precision_dtype(np)
    # Cap the retained review queue so a magnet face can't grow it without bound;
    # we only ever surface the top ``max_suggestions`` anyway.
    review_retain_cap = max(max_suggestions * 4, max_suggestions)

    auto_face_ids: List[str] = []
    review_candidates: List[Tuple[str, Dict, float]] = []
    candidate_face_count = 0

    batch_ids: List[str] = []
    batch_rows: List[Optional[Dict]] = []
    batch_embeddings: List[List[float]] = []

    def _flush_batch() -> None:
        if not batch_embeddings:
            return
        X = np.asarray(batch_embeddings, dtype=dtype)
        Xn = X / (np.linalg.norm(X, axis=1, keepdims=True) + 1e-12)
        target_sim = Xn @ target_norm
        if other_matrix is not None:
            other_best = np.max(Xn @ other_matrix.T, axis=1)
        else:
            other_best = np.full(target_sim.shape, -1.0)
        for i in range(len(batch_ids)):
            sim = float(target_sim[i])
            if sim < review_threshold:
                continue
            rival = float(other_best[i])
            # A face closer to a different named person belongs to them.
            if rival >= sim:
                continue
            if sim >= auto_threshold and (sim - rival) >= margin:
                auto_face_ids.append(batch_ids[i])
            elif collect_suggestions:
                review_candidates.append((batch_ids[i], batch_rows[i], sim))
        # Release the chunk (and its embeddings) before scanning the next one.
        batch_ids.clear()
        batch_rows.clear()
        batch_embeddings.clear()
        if collect_suggestions and len(review_candidates) > review_retain_cap:
            review_candidates.sort(key=lambda item: item[2], reverse=True)
            del review_candidates[max_suggestions:]

    try:
        face_iter = face_table_client.query_entities(f"PartitionKey eq '{_escape_odata(user_id)}'")
    except Exception:
        face_iter = []

    for row in face_iter:
        face_id = str(row.get('RowKey') or '')
        if not face_id or face_id in declined:
            continue
        owner_id = str(row.get('personId') or '')
        if owner_id == person_id:
            continue
        # Only pull from unclustered faces or *unnamed* clusters; never steal a
        # face that already belongs to (or was confirmed for) another named person.
        if owner_id and owner_id in named_person_ids:
            continue
        if _face_is_confirmed(row):
            continue
        if not _face_is_clusterable(row):
            continue
        if not _face_embedding_allowed_for_clustering(row):
            continue
        emb = _face_embedding_from_entity(row)
        if not emb or len(emb) != target_dim:
            continue
        candidate_face_count += 1
        batch_ids.append(face_id)
        # Only retain the row when suggestions are collected (it feeds the review
        # summary); the apply path re-reads the live row, so drop it to save RAM.
        batch_rows.append(row if collect_suggestions else None)
        batch_embeddings.append(emb)
        if len(batch_embeddings) >= PEOPLE_PROPAGATE_SCAN_BATCH:
            _flush_batch()
    _flush_batch()

    if candidate_face_count == 0:
        return {**empty, 'candidateFaces': 0}

    result = {
        'autoAssigned': [],
        'autoAssignedCount': len(auto_face_ids),
        'suggestions': [],
        'candidateFaces': candidate_face_count,
    }

    if apply and auto_face_ids:
        affected_files = set()
        applied: List[str] = []
        for face_id in auto_face_ids:
            try:
                face_ent = face_table_client.get_entity(partition_key=user_id, row_key=face_id)
            except Exception:
                continue
            # Re-check against the live row: the bulk snapshot may be stale, and we
            # must never override a face confirmed/rejected in the meantime.
            if _face_is_confirmed(face_ent) or _face_is_rejected(face_ent):
                continue
            old_owner = str(face_ent.get('personId') or '')
            if old_owner and old_owner != person_id:
                _remove_face_from_person(user_id, old_owner, face_id)
            _remove_face_from_other_people(user_id, face_id, person_id)
            face_ent['personId'] = person_id
            face_ent['assignedByPropagation'] = True
            try:
                face_table_client.upsert_entity(face_ent)
            except Exception:
                continue
            filename = str(face_ent.get('filename') or '')
            if filename:
                affected_files.add(filename)
            applied.append(face_id)
        if applied:
            # Batch the target-person membership update so its rep embedding is
            # recomputed once, not once per newly attached face.
            try:
                target_entity = person_table_client.get_entity(partition_key=user_id, row_key=person_id)
                existing_ids = json.loads(target_entity.get('faceIds', '[]') or '[]')
            except Exception:
                existing_ids = []
            merged_ids = _dedupe_face_ids_preserving_order([*existing_ids, *applied])
            _update_person_entity(user_id, person_id, {'faceIds': json.dumps(merged_ids)})
            _update_person_rep_embedding(user_id, person_id)
            _rebuild_metadata_faces_for_filenames(user_id, affected_files)
        result['autoAssigned'] = applied
        result['autoAssignedCount'] = len(applied)

    if collect_suggestions and review_candidates:
        review_candidates.sort(key=lambda item: item[2], reverse=True)
        for face_id, face_row, sim in review_candidates[:max_suggestions]:
            summary = _face_summary_for_person_list(face_id, face_row, user_id)
            summary['similarity'] = round(sim, 4)
            summary['currentPersonId'] = str(face_row.get('personId') or '')
            result['suggestions'].append(summary)

    return result


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


def _store_album_token_index(token: str, user_id: str, album_id: str) -> None:
    """Record token -> (userId, albumId) so a public share view is an O(1)
    point read instead of an unscoped `publicToken eq '...'` scan of every
    account's albums."""
    if album_token_index_table_client is None or not token:
        return
    try:
        album_token_index_table_client.upsert_entity({
            'PartitionKey': token,
            'RowKey': 'owner',
            'userId': user_id,
            'albumId': album_id,
        })
    except Exception:
        pass


def _delete_album_token_index(token: str) -> None:
    if album_token_index_table_client is None or not token:
        return
    try:
        album_token_index_table_client.delete_entity(partition_key=token, row_key='owner')
    except Exception:
        pass


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


def _public_album_share_meta(entity: Optional[Dict], token: str) -> Dict[str, str]:
    """Build the OG/Twitter preview values for a public album's share page.

    Crawlers (iMessage/WhatsApp/Slack link unfurlers) fetch this page without
    running JS, so an access-code-protected album must stay fully generic here
    -- a bot can never supply the code, so it must not learn the album's name
    or see a real photo either.
    """
    spa_base = _get_spa_base_url()
    fallback_image = f'{spa_base}/og-image.png'
    valid = bool(entity) and _coerce_bool(entity.get('isPublic', False)) and not _album_is_expired(entity)
    if not valid:
        return {
            'title': 'Shared album',
            'description': 'This shared album link is no longer available.',
            'image': fallback_image,
            'image_is_fallback': 'true',
        }
    if _album_access_code(entity):
        return {
            'title': 'Shared album (locked)',
            'description': 'This album is protected by an access code.',
            'image': fallback_image,
            'image_is_fallback': 'true',
        }
    name = str(entity.get('name') or '').strip() or 'Shared album'
    filenames = _album_filenames(entity)
    photo_count = len(filenames)
    description = f'{photo_count} photo{"s" if photo_count != 1 else ""} shared on Keepsake.'
    image = fallback_image
    image_is_fallback = True
    if filenames:
        # /public/photos/<token>/share-preview/<filename> always returns a
        # properly link-preview-sized JPEG (2048px/~3.9MB bound) regardless of
        # source format/resolution -- the raw thumbnail (120x120) is too small
        # for WhatsApp/Facebook's crawler and the full original can be 10MB+.
        first_name = filenames[0]
        image = f"{request.host_url.rstrip('/')}/public/photos/{token}/share-preview/{first_name}"
        image_is_fallback = False
    return {
        'title': name,
        'description': description,
        'image': image,
        'image_is_fallback': 'true' if image_is_fallback else '',
    }


def _render_public_album_share_page(meta: Dict[str, str], redirect_url: str) -> str:
    title = html.escape(meta['title'])
    description = html.escape(meta['description'])
    image = html.escape(meta['image'], quote=True)
    redirect = html.escape(redirect_url, quote=True)
    # The branded fallback image is a fixed 1200x630 asset; a real photo
    # thumbnail's rendered size varies with its source aspect ratio (thumbnails
    # are generated bounded within 120x120, not cropped to a fixed box), so we
    # only advertise dimensions when we know them.
    dimensions_html = ''
    if meta.get('image_is_fallback'):
        dimensions_html = (
            '\n<meta property="og:image:width" content="1200" />'
            '\n<meta property="og:image:height" content="630" />'
        )
    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<meta http-equiv="refresh" content="0; url={redirect}" />
<title>{title}</title>
<meta name="description" content="{description}" />
<meta property="og:type" content="website" />
<meta property="og:site_name" content="Keepsake" />
<meta property="og:title" content="{title}" />
<meta property="og:description" content="{description}" />
<meta property="og:image" content="{image}" />{dimensions_html}
<meta name="twitter:card" content="summary_large_image" />
<meta name="twitter:title" content="{title}" />
<meta name="twitter:description" content="{description}" />
<meta name="twitter:image" content="{image}" />
</head>
<body>
<p>Opening the shared album&hellip; if nothing happens, <a href="{redirect}">tap here</a>.</p>
</body>
</html>'''


def _find_public_album_by_token(token: str) -> Optional[Dict]:
    if not albums_table_client or not token:
        return None
    index_row = None
    if album_token_index_table_client is not None:
        try:
            index_row = album_token_index_table_client.get_entity(partition_key=token, row_key='owner')
        except Exception:
            index_row = None
    if index_row is not None:
        entity = _load_album_entity(str(index_row.get('userId') or ''), str(index_row.get('albumId') or ''))
        if entity is not None and str(entity.get('publicToken') or '') == token:
            return entity
        # Stale index row: the album was deleted, un-shared, or re-shared with a
        # new token since this row was written. Self-heal instead of trusting it
        # forever, then fall through to the scan below in case the token is
        # actually valid but predates this index (pre-backfill).
        _delete_album_token_index(token)
    # Fallback: unscoped scan, for tokens created before this index existed and
    # not yet covered by the backfill script.
    safe = _escape_odata(token)
    try:
        rows = list(albums_table_client.query_entities(f"publicToken eq '{safe}'"))
    except Exception:
        rows = []
    if not rows:
        return None
    row = rows[0]
    _store_album_token_index(token, str(row.get('PartitionKey') or ''), str(row.get('RowKey') or ''))
    return row


def _public_photo_urls(token: str, filename: str, blob_name: Optional[str] = None) -> Dict[str, str]:
    # The shrunk preview is the default lightbox image for every non-video photo now
    # (see getMainMediaPath in PhotoViewer.tsx), not just RAW/HEIC/JXL. previewUrl must
    # therefore be populated here for every image, or the frontend falls back to its
    # hardcoded `/api/photos/preview/...` path -- an authenticated route anonymous
    # album visitors can't reach, which 404s and silently downgrades the lightbox to
    # the low-res thumbnail instead.
    preview_url = f'/public/photos/{token}/preview/{filename}' if not is_video_file(filename) else ''
    # Direct SAS URLs point at storage, so they must name the physical blob (the
    # anonymous UUID for anonymized photos). The proxy fallbacks keep the original
    # filename since the public routes resolve the anonymous id internally.
    physical_name = blob_name or filename
    # Day-stable SAS so shared-album thumbnails stay browser-cacheable; the
    # album link itself is the long-lived bearer secret, so a day-scoped blob
    # URL doesn't widen exposure.
    try:
        image_url, _ = _create_stable_read_sas_url(BLOB_IMAGE_CONTAINER, physical_name, download_filename=filename)
    except Exception:
        image_url = f'/public/photos/{token}/image/{filename}'
    try:
        thumbnail_url, _ = _create_stable_read_sas_url(BLOB_THUMBNAIL_CONTAINER, physical_name)
    except Exception:
        thumbnail_url = f'/public/photos/{token}/thumbnail/{filename}'
    return {
        'url': image_url,
        'thumbnailUrl': thumbnail_url,
        'previewUrl': preview_url,
    }


def _load_photos_for_filenames(user_id: str, filenames: List[str]) -> List[Dict]:
    pid_to_name, _ = _load_people_name_index(user_id)
    photos = []
    for name in filenames:
        metadata = _get_metadata_entity(user_id, name)
        if metadata is None:
            continue
        photos.append(_build_photo_summary(user_id, name, metadata, include_props=False, pid_to_name=pid_to_name))
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
        # Without this, the browser re-preflights every method+headers
        # combination on every call (observed live: OPTIONS was 39% of all
        # backend requests during a bulk upload), doubling load on the same
        # thread pool that's already contended. Browsers clamp this to their
        # own ceiling (Chromium 7200s, Firefox 86400s) regardless of the
        # value sent, so one high number is safe everywhere.
        resp.headers['Access-Control-Max-Age'] = '86400'
        return resp


# ---------------------------------------------------------------------------
# Single-owner password authentication endpoints (AUTH_MODE=password).
# ---------------------------------------------------------------------------
def _password_mode_guard():
    if AUTH_MODE != 'password':
        return jsonify({'error': 'Password authentication is not enabled on this deployment.'}), 400
    return None


# ---------------------------------------------------------------------------
# Shared-library membership: invites, acceptance, switching, and management.
# All owner-gated actions operate on the caller's *active* library and require
# the caller to be that library's owner.
# ---------------------------------------------------------------------------
def _require_owner_context(require_auth: bool = True):
    """(account_id, library_id, None) if the caller owns their active library,
    else (None, None, error_response)."""
    account_id, library_id, error = _require_library_context(require_auth=require_auth)
    if error:
        return None, None, error
    if library_store is None or not library_store.is_owner(account_id, library_id):
        return None, None, (jsonify({'error': 'Only the library owner can do that.'}), 403)
    return account_id, library_id, None


def _member_view(library_id: str, account_id: str) -> List[Dict]:
    members = library_store.list_library_members(library_id)
    out = []
    for m in members:
        account = library_store.get_user(m['userId']) or {}
        out.append({
            'userId': m['userId'],
            'email': str(account.get('email') or ''),
            'isOwner': m['isOwner'],
            'isSelf': m['userId'] == account_id,
        })
    out.sort(key=lambda m: (not m['isOwner'], m['email'].lower()))
    return out


def _purge_library_data(library_id: str) -> None:
    """Best-effort delete of every data row in a library's partition across the
    photo tables. Image/thumbnail blobs are content-addressed (and may be shared
    across libraries), so they are intentionally left to a separate GC pass.

    The image_names table is included so no anonymous_id -> original_filename
    mapping (which still holds the plaintext filename) outlives the library."""
    pk = _escape_odata(library_id)
    for client in (metadata_table_client, face_table_client, person_table_client,
                   albums_table_client, merge_table_client, image_names_table_client):
        if client is None:
            continue
        try:
            for row in list(client.query_entities(f"PartitionKey eq '{pk}'")):
                try:
                    client.delete_entity(partition_key=row['PartitionKey'], row_key=row['RowKey'])
                except Exception:
                    pass
        except Exception as exc:
            app.logger.warning('Purge skipped a table for %s: %s', library_id, exc)
    try:
        invalidate_image_names_cache(library_id)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Library "clean": wipe all photo/video content (metadata, faces, people,
# albums, merges, blobs, vector index) for a library while leaving the
# library, its membership, and every account intact. Distinct from
# `/api/library` DELETE above, which tears down the library + owner account.
#
# Gated behind an emailed, single-use token per required approver so a
# compromised session alone can't trigger it: the owner always confirms, and
# if the library has other members, one of them (picked at random) must also
# confirm before the wipe runs.
# ---------------------------------------------------------------------------
def _delete_cover_blobs_for_library(library_id: str) -> None:
    """Face-cover crops are namespaced under a per-library hash prefix (unlike
    image/thumbnail blobs, they are never content-shared across libraries), so
    the whole prefix can be safely deleted."""
    if blob_service_client is None or not BLOB_COVER_CONTAINER:
        return
    prefix = hashlib.sha256(str(library_id).encode('utf-8')).hexdigest()[:16] + '/'
    try:
        container = blob_service_client.get_container_client(BLOB_COVER_CONTAINER)
        for blob in container.list_blobs(name_starts_with=prefix):
            try:
                container.delete_blob(blob.name)
            except Exception:
                pass
    except Exception as exc:
        app.logger.warning('Cover blob cleanup skipped for %s: %s', library_id, exc)


def _notify_cleanup_completed(library_id: str, summary: Dict) -> None:
    """Send cleanup completion notifications to all library members."""
    try:
        if not email_utils.is_configured():
            return
        meta = library_store.get_library(library_id) or {}
        members = library_store.list_library_members(library_id) or []
        photos_deleted = summary.get('photosDeleted', 0)

        for member in members:
            try:
                member_email = str((library_store.get_user(member.get('userId')) or {}).get('email') or '')
                if not member_email:
                    continue
                email_utils.send_library_cleanup_complete_email(
                    member_email,
                    library_name=str(meta.get('name') or ''),
                    photos_deleted=photos_deleted,
                )
            except Exception as exc:
                app.logger.warning('Failed to send cleanup completion email to %s: %s', member.get('userId'), exc)
    except Exception as exc:
        app.logger.warning('Cleanup completion notification failed for %s: %s', library_id, exc)


def _reconcile_stale_library_cleanup(library_id: str, *, job_id: str = '', job_row: Optional[Dict] = None) -> Optional[str]:
    """Convert very old in-progress cleanup state into a failed terminal state.

    Returns the failure reason when a stale state is reconciled, else ``None``.
    """
    if library_store is None:
        return None
    meta = library_store.get_library(library_id) or {}
    if str(meta.get('lastCleanupStatus') or '') != 'in_progress':
        return None
    started_at = int(meta.get('lastCleanupStartTime') or 0)
    if started_at <= 0:
        # A row stuck 'in_progress' with no start time can never age out on the
        # elapsed check below, so it would block uploads forever. Fall back to the
        # last recorded cleanup time; if there is none either, treat it as stale
        # immediately — a genuinely running job always records a start time.
        started_at = int(meta.get('lastCleanupTime') or 0)
        if started_at <= 0:
            started_at = int(time.time()) - LIBRARY_CLEAN_MAX_IN_PROGRESS_SECONDS
    elapsed = int(time.time()) - started_at
    if elapsed < LIBRARY_CLEAN_MAX_IN_PROGRESS_SECONDS:
        return None
    reason = (
        'Cleanup timed out after '
        f'{LIBRARY_CLEAN_MAX_IN_PROGRESS_SECONDS} seconds. Please retry cleanup.'
    )
    library_store.set_cleanup_failed(library_id, reason)
    if job_id:
        try:
            row_user_id = str((job_row or {}).get('userId') or '')
            _upsert_job_status(
                job_id,
                row_user_id or str(meta.get('ownerUserId') or ''),
                'library_clean',
                'failed',
                error=reason,
                libraryId=library_id,
            )
        except Exception:
            app.logger.debug('Could not mark stale cleanup job %s as failed', job_id)
    return reason


def _active_library_cleanup_job(library_id: str) -> Optional[Dict]:
    """Return the active queued/running cleanup job row for a library, if any.

    library_clean jobs are partitioned by libraryId (see _job_partition_key)
    rather than the initiating user's userId, since any member of a shared
    library must be able to check this regardless of who started the job --
    so this is a scoped partition query, not a fleet-wide scan.
    """
    if jobs_table_client is None or not library_id:
        return None
    safe_library_id = str(library_id)
    try:
        rows = list(jobs_table_client.query_entities(f"PartitionKey eq '{_escape_odata(safe_library_id)}'"))
    except Exception:
        return None
    for row in rows:
        if str(row.get('jobType') or '') != 'library_clean':
            continue
        if str(row.get('status') or '').lower() not in {'queued', 'running'}:
            continue
        return row
    return None


def _reconcile_in_progress_from_job_row(library_id: str, meta: Dict) -> bool:
    """Self-correct a library cached as 'in_progress' that has no active job.

    Trusts the authoritative ``jobs`` row recorded in ``lastCleanupJobId``: if
    that job already finished (done/failed), the library row is moved to the
    matching terminal state and ``True`` is returned so the caller stops blocking
    uploads. Returns ``False`` when the job row is missing or not yet terminal,
    leaving the (conservative) time-based backstop in charge.

    This closes the window where a worker's terminal write lost a race with a
    stale in-progress write and left uploads blocked until the 4h timeout.
    """
    if library_store is None:
        return False
    job_id = str((meta or {}).get('lastCleanupJobId') or '')
    if not job_id:
        return False
    row = _get_job_row(library_id, job_id)
    if row is None:
        return False
    status = str(row.get('status') or '').lower()
    if status == 'done':
        result = row.get('result')
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except Exception:
                result = {}
        if not isinstance(result, dict):
            result = {}
        library_store.set_cleanup_completed(
            library_id,
            int(result.get('photosDeleted') or 0),
            int(result.get('blobsDeleted') or 0),
        )
        return True
    if status == 'failed':
        library_store.set_cleanup_failed(library_id, str(row.get('error') or 'cleanup failed'))
        return True
    return False


def _library_cleanup_block_reason(library_id: str) -> Optional[str]:
    """Return a user-facing reason when uploads must be blocked for cleanup.

    Gated on the library's own lastCleanupStatus (one cheap point-read)
    before touching _active_library_cleanup_job's full 'jobs'-partition scan
    below: that scan pulls and deserializes every job-status row for the
    whole account (213k+ rows and growing on a long-running account,
    confirmed live via Log Analytics phase-timing logs to cost 17-33s per
    call), and this function ran on EVERY init-batch/finalize-batch/
    client-processing request -- dominating upload latency end to end, not
    any of the actual per-file work. set_cleanup_in_progress (the sole
    writer of 'in_progress') is called synchronously in the same request
    that enqueues a cleanup job, before that request returns, so this gate
    can't race a job that's genuinely active: lastCleanupStatus always
    reflects reality by the time any *other* request observes it.
    """
    meta = library_store.get_library(library_id) if library_store is not None else {}
    if str((meta or {}).get('lastCleanupStatus') or '') != 'in_progress':
        return None

    stale_reason = _reconcile_stale_library_cleanup(library_id)
    if stale_reason:
        return None

    active_job = _active_library_cleanup_job(library_id)
    if active_job is None:
        # Cached 'in_progress' but nothing is actually queued/running. Trust the
        # recorded job row and reconcile, rather than blocking uploads on a stale
        # flag until the timeout fires.
        if _reconcile_in_progress_from_job_row(library_id, meta or {}):
            return None
        return 'Cleanup is still running for this library. Please wait until it finishes before uploading.'

    updated_at = _parse_iso_date(str(active_job.get('updatedAt') or ''))
    if updated_at is None:
        reason = (
            'Cleanup timed out after '
            f'{LIBRARY_CLEAN_MAX_IN_PROGRESS_SECONDS} seconds. Please retry cleanup.'
        )
        try:
            row_library_id = str(active_job.get('libraryId') or library_id)
            _upsert_job_status(
                str(active_job.get('jobId') or ''),
                str(active_job.get('userId') or ''),
                'library_clean',
                'failed',
                error=reason,
                libraryId=row_library_id,
            )
            if library_store is not None:
                library_store.set_cleanup_failed(row_library_id, reason)
        except Exception:
            pass
        return None

    elapsed = (datetime.now(timezone.utc) - updated_at).total_seconds()
    if elapsed >= LIBRARY_CLEAN_MAX_IN_PROGRESS_SECONDS:
        reason = (
            'Cleanup timed out after '
            f'{LIBRARY_CLEAN_MAX_IN_PROGRESS_SECONDS} seconds. Please retry cleanup.'
        )
        try:
            row_library_id = str(active_job.get('libraryId') or library_id)
            _upsert_job_status(
                str(active_job.get('jobId') or ''),
                str(active_job.get('userId') or ''),
                'library_clean',
                'failed',
                error=reason,
                libraryId=row_library_id,
            )
            if library_store is not None:
                library_store.set_cleanup_failed(row_library_id, reason)
        except Exception:
            pass
        return None

    # NOTE: deliberately do NOT write cleanup state here. An upload attempt must
    # never move the library *into* in_progress: `_active_library_cleanup_job`
    # reads the jobs partition, so between that read and a write here the job can
    # finish, and re-stamping in_progress would clobber the worker's terminal
    # write (and re-arm the timeout), stranding uploads. The enqueue path is the
    # sole writer that sets in_progress.
    return 'Cleanup is still running for this library. Please wait until it finishes before uploading.'


def _execute_library_clean(library_id: str) -> Dict:
    """Delete every photo/video and its derived data for a library. Runs on the
    queue-scaled worker (see _enqueue_library_clean_job) since it walks the
    library's full metadata partition, mirroring the existing clustering jobs'
    "don't do full scans inline" rule."""
    pk = _escape_odata(library_id)
    try:
        # _clean_one_photo below only reads RowKey/anonymousImageId per row --
        # narrow select= avoids pulling every photo's full metadata (tags,
        # embeddings, OCR text, etc.) just to delete it.
        metadata_rows = list(metadata_table_client.query_entities(
            f"PartitionKey eq '{pk}'", select=['PartitionKey', 'RowKey', 'anonymousImageId'],
        )) if metadata_table_client else []
    except Exception:
        metadata_rows = []

    # Was a per-photo call to _is_filename_shared, an *unscoped* `RowKey eq X`
    # query -- no PartitionKey means Table Storage can't restrict it to this
    # library, so it's a full scan of the entire multi-tenant metadata table,
    # repeated once per photo. On a large account this dwarfed every other
    # cost in this function. _shared_names_in_batch (added for bulk delete,
    # see its docstring) answers the same question from the filename_owners
    # index with one partition-scoped point query per name, run concurrently
    # -- reusing it here instead of re-deriving the same fix twice.
    filenames = {str(row.get('RowKey') or '') for row in metadata_rows if row.get('RowKey')}
    shared_names = _shared_names_in_batch(filenames, library_id)

    def _clean_one_photo(row: Dict) -> Tuple[int, int]:
        filename = str(row.get('RowKey') or '')
        if not filename:
            return (0, 0)
        _delete_upload_temp_files_for_filename(filename)
        # Drop this library's filename-ownership row regardless of the shared
        # check below -- it tracks "does THIS library have a row under this
        # name", which is going away here even when the underlying blob (and
        # its anonymous-id mapping) survives for another library that shares it.
        if filename_owners_table_client is not None:
            try:
                filename_owners_table_client.delete_entity(partition_key=filename, row_key=library_id)
            except Exception:
                pass
        anonymous_id = str(row.get('anonymousImageId') or '').strip()
        if filename in shared_names:
            # Another library still references this content-addressed blob.
            return (0, 0)
        # Anonymized photos are stored under the anonymous UUID; delete that blob
        # (plus the original name as a safety net) and drop the name mapping.
        physical_name = anonymous_id or filename
        extra = [filename] if anonymous_id else None
        errors = _delete_photo_blobs_if_present(physical_name, extra)
        if anonymous_id:
            try:
                delete_image_name_mapping(library_id, anonymous_id)
            except Exception:
                pass
        return (len(errors), 0) if errors else (0, 1)

    blobs_deleted = 0
    blob_errors = 0
    if metadata_rows:
        # Each photo's cleanup is independent, pure network I/O wait (temp
        # files, a table delete, blob deletes) -- same reasoning as every
        # other DELETE_IO_CONCURRENCY call site in the delete path.
        with ThreadPoolExecutor(max_workers=DELETE_IO_CONCURRENCY) as executor:
            for errors, deleted in executor.map(_clean_one_photo, metadata_rows):
                blob_errors += errors
                blobs_deleted += deleted

    for client in (metadata_table_client, face_table_client, person_table_client,
                   albums_table_client, merge_table_client, image_names_table_client,
                   hash_index_table_client):
        if client is None:
            continue
        # select=[keys only] (+payloadBlobName for merges): merge_table_client's
        # partition can hold tens of thousands of face_membership_snapshot_chunk
        # rows carrying a ~24KB `payload` blob each (see
        # _create_people_repair_snapshot). Fetching full rows just to read
        # PartitionKey/RowKey materializes all of that into memory at once and
        # OOMs the worker (same bug class fixed for list_merges) -- adding the
        # small payloadBlobName string doesn't reintroduce that risk.
        select_fields = ['PartitionKey', 'RowKey']
        if client is merge_table_client:
            select_fields.append('payloadBlobName')
        try:
            rows_to_delete = list(client.query_entities(f"PartitionKey eq '{pk}'", select=select_fields))
        except Exception as exc:
            app.logger.warning('Library clean skipped a table for %s: %s', library_id, exc)
            continue

        def _delete_row(row: Dict, client=client) -> None:
            if client is merge_table_client:
                blob_name = str(row.get('payloadBlobName') or '')
                if blob_name:
                    _delete_blob_if_present(BLOB_MERGE_PAYLOADS_CONTAINER, blob_name)
            try:
                client.delete_entity(partition_key=row['PartitionKey'], row_key=row['RowKey'])
            except Exception:
                pass

        if rows_to_delete:
            with ThreadPoolExecutor(max_workers=DELETE_IO_CONCURRENCY) as executor:
                list(executor.map(_delete_row, rows_to_delete))

    try:
        invalidate_image_names_cache(library_id)
    except Exception:
        pass
    _delete_cover_blobs_for_library(library_id)
    delete_user_vector_index_data(library_id)
    delete_user_lexical_index_data(library_id)
    delete_user_tag_embedding_index_data(library_id)
    _invalidate_metadata_scan_cache(library_id)

    return {'photosDeleted': len(metadata_rows), 'blobsDeleted': blobs_deleted, 'blobErrors': blob_errors}


def _enqueue_library_clean_job(library_id: str, actor_user_id: str, request_id: str) -> Dict[str, str]:
    job_id = f"libclean:{library_id}:{uuid.uuid4().hex}"
    if library_ops_queue_client is None:
        # No queue configured (e.g. local dev) — run inline rather than silently
        # dropping a destructive action the caller believes is in progress.
        app.logger.warning('Library-ops queue client is unavailable; running library clean %s inline', job_id)
        try:
            library_store.set_cleanup_in_progress(library_id, job_id)
            summary = _execute_library_clean(library_id)
            library_store.set_cleanup_completed(library_id, summary.get('photosDeleted', 0), summary.get('blobsDeleted', 0))
            _upsert_job_status(job_id, actor_user_id, 'library_clean', 'done', result=summary, libraryId=library_id)
            _notify_cleanup_completed(library_id, summary)
            return {'status': 'done', 'jobId': job_id}
        except Exception as exc:
            app.logger.exception('Inline library clean failed for %s', library_id)
            library_store.set_cleanup_failed(library_id, 'Library clean failed')
            _upsert_job_status(job_id, actor_user_id, 'library_clean', 'failed', error='Library clean failed', libraryId=library_id)
            return {'status': 'failed', 'jobId': job_id}
    message = {
        'jobId': job_id,
        'correlationId': job_id,
        'user_id': actor_user_id,
        'libraryId': library_id,
        'type': 'library_clean',
        'requestId': request_id,
    }
    try:
        library_store.set_cleanup_in_progress(library_id, job_id)
        library_ops_queue_client.send_message(json.dumps(message, separators=(',', ':')))
    except Exception:
        app.logger.exception('Failed to enqueue library clean job %s', job_id)
        library_store.set_cleanup_failed(library_id, 'Failed to queue cleanup job')
        return {'status': 'failed', 'jobId': job_id}
    _upsert_job_status(job_id, actor_user_id, 'library_clean', 'queued', libraryId=library_id)
    return {'status': 'queued', 'jobId': job_id}


def _library_export_part_blob_name(library_id: str, part_index: int) -> str:
    # Deterministic, library- and part-scoped path: overwriting on each
    # re-export means this container never accumulates more than the current
    # run's parts per library. A run that produces fewer parts than the
    # previous one has its extra stale parts swept by
    # _cleanup_stale_library_export_parts.
    return f'{library_id}/library-export-part-{part_index}.zip'


def _cleanup_stale_library_export_parts(library_id: str, keep_count: int) -> None:
    """Delete previously-uploaded export part blobs beyond keep_count -- a
    library whose export shrinks from e.g. 5 parts to 3 would otherwise leave
    stale, still-downloadable blobs from the prior run."""
    if blob_service_client is None:
        return
    prefix = f'{library_id}/library-export-part-'
    try:
        container_client = blob_service_client.get_container_client(BLOB_EXPORTS_CONTAINER)
        for blob in container_client.list_blobs(name_starts_with=prefix):
            name = str(getattr(blob, 'name', '') or '')
            suffix = name[len(prefix):]
            if not suffix.endswith('.zip'):
                continue
            try:
                index = int(suffix[:-len('.zip')])
            except ValueError:
                continue
            if index > keep_count:
                try:
                    container_client.delete_blob(name)
                except Exception:
                    app.logger.warning('Failed to delete stale export part %s for %s', name, library_id)
    except Exception:
        app.logger.warning('Failed to sweep stale export parts for %s', library_id)


def _execute_library_download(library_id: str, library_name: str, job_id: Optional[str] = None, user_id: Optional[str] = None) -> Dict:
    """Build one or more size-capped ZIP parts covering every photo/video in a
    library and upload them to BLOB_EXPORTS_CONTAINER. Runs on the
    queue-scaled worker, same as _execute_library_clean, since it walks the
    library's full metadata partition and reads every photo's full-size
    bytes.

    Splitting into LIBRARY_EXPORT_PART_MAX_BYTES-capped parts (instead of one
    ZIP) keeps a very large library from producing one impractically large
    file and bounds peak temp-disk usage to a single part. It also gives a
    resume point: if job_id identifies a job row from a PRIOR attempt (this
    queue message was redelivered after the worker died mid-run), rows
    already accounted for by that attempt's durably-uploaded parts
    (exportRowsProcessed/exportPartsSummary) are skipped rather than
    reprocessed. A fresh user-initiated re-export always gets a new job_id,
    so this never resumes across unrelated export runs -- only across
    retries of the same one.
    """
    pk = _escape_odata(library_id)
    try:
        # select=[the 2 fields actually read below]: a full row carries heavy
        # columns (exifData, backgroundTags, aiPersonLabel, ...) never touched
        # by this function -- materializing all of that for every row in a
        # 17k+-photo library is the same bug class already fixed in
        # _execute_library_clean/list_merges (see that comment), just never
        # applied here since this function was written later. Confirmed live
        # 2026-09-04: the unprojected version OOMKilled (exit 137) a 2vCPU/4Gi
        # worker repeatedly, well before the executor's own download buffers
        # could plausibly account for it.
        metadata_rows = list(metadata_table_client.query_entities(
            f"PartitionKey eq '{pk}'", select=['PartitionKey', 'RowKey', 'processing_state'],
        )) if metadata_table_client else []
    except Exception:
        metadata_rows = []

    # Stable order so part boundaries -- and the resume-skip count below --
    # are consistent across retries of the same job.
    metadata_rows.sort(key=lambda row: str(row.get('RowKey') or ''))
    candidate_rows = [
        row for row in metadata_rows
        if str(row.get('RowKey') or '') and str(row.get('processing_state') or '').strip().lower() != 'deleted'
    ]
    photos_total = len(candidate_rows)

    rows_processed = 0
    written_count = 0
    skipped_count = 0
    part_index = 0
    parts_summary: List[Dict] = []
    if job_id:
        prior_row = _get_job_row(library_id, job_id)
        if prior_row is not None:
            try:
                rows_processed = max(0, min(int(prior_row.get('exportRowsProcessed') or 0), photos_total))
                written_count = int(prior_row.get('exportPhotosWritten') or 0)
                skipped_count = int(prior_row.get('exportPhotosSkipped') or 0)
                part_index = int(prior_row.get('exportPartsCompleted') or 0)
                raw_summary = prior_row.get('exportPartsSummary')
                if isinstance(raw_summary, str) and raw_summary:
                    parts_summary = json.loads(raw_summary)
            except Exception:
                rows_processed, written_count, skipped_count, part_index, parts_summary = 0, 0, 0, 0, []

    def _durable_checkpoint() -> None:
        # Only called right after a part's ZIP has actually been uploaded --
        # this is what a retry's resume-skip logic above trusts, so it must
        # never advance past data that isn't safely in blob storage yet.
        if not job_id:
            return
        _upsert_job_status(
            job_id, user_id, 'library_download', 'running',
            libraryId=library_id,
            exportRowsProcessed=rows_processed,
            exportPhotosWritten=written_count,
            exportPhotosSkipped=skipped_count,
            exportPartsCompleted=part_index,
            exportPartsSummary=parts_summary,
            result={'photosCompleted': rows_processed, 'photosTotal': photos_total},
        )

    def _live_progress_heartbeat() -> None:
        # Refreshes updatedAt (so /api/jobs/status's 15-minute stale-job
        # cutoff doesn't flip a still-running export to 'failed') and lets the
        # UI show real counts. Deliberately does NOT touch the durable
        # exportRowsProcessed/exportPartsSummary checkpoint above -- a photo
        # counted here could still be lost if the worker dies before its part
        # finishes uploading, so advancing the resume pointer this early
        # would let a retry skip data that was never actually made durable.
        if not job_id:
            return
        _upsert_job_status(
            job_id, user_id, 'library_download', 'running', libraryId=library_id,
            result={'photosCompleted': rows_processed, 'photosTotal': photos_total},
        )

    def _download_row(row: Dict) -> Tuple[str, Optional[bytes], Optional[Exception]]:
        filename = str(row.get('RowKey') or '')
        try:
            blob_name = resolve_physical_blob_name(library_id, filename, 'image')
            return filename, download_media_bytes('image', blob_name), None
        except Exception as exc:
            return filename, None, exc

    tmp = tempfile.NamedTemporaryFile(prefix='libexport-', suffix='.zip', delete=False)
    tmp_path = tmp.name
    # ZIP_STORED, not ZIP_DEFLATED: JPEG/HEIC/RAW are already entropy-coded,
    # so DEFLATE spends real CPU compressing this content for near-zero size
    # reduction -- pure waste that would otherwise compete with the
    # network-bound downloads below for the same core.
    zip_file = zipfile.ZipFile(tmp, 'w', compression=zipfile.ZIP_STORED)
    photos_in_part = 0
    part_bytes = 0
    try:
        # Bounded read-ahead: a fixed-size sliding window of at most
        # LIBRARY_EXPORT_DOWNLOAD_CONCURRENCY in-flight-or-completed futures,
        # not executor.map(). map() submits every remaining row's download
        # up front -- with max_workers=8 that only bounds how many run
        # *concurrently*, not how many completed results can pile up waiting
        # to be consumed: one slow straggler (a large RAW/video) blocks
        # in-order consumption while the other 7 threads race ahead through
        # the rest of the list, each completed download's full bytes sitting
        # in memory until the straggler finally clears. A sliding window
        # only ever has LIBRARY_EXPORT_DOWNLOAD_CONCURRENCY outstanding
        # futures (submits the next one only after popping+consuming the
        # oldest), so peak memory is bounded regardless of file-size mix.
        # Submission/consumption order is still strictly FIFO, so the zip's
        # contents, size-cap part boundaries, and durable resume checkpoint
        # below stay exactly as deterministic as the sequential version.
        with ThreadPoolExecutor(max_workers=LIBRARY_EXPORT_DOWNLOAD_CONCURRENCY) as executor:
            remaining_rows = candidate_rows[rows_processed:]
            next_row_index = 0
            in_flight = []
            for _ in range(min(LIBRARY_EXPORT_DOWNLOAD_CONCURRENCY, len(remaining_rows))):
                in_flight.append(executor.submit(_download_row, remaining_rows[next_row_index]))
                next_row_index += 1

            while in_flight:
                future = in_flight.pop(0)
                if next_row_index < len(remaining_rows):
                    in_flight.append(executor.submit(_download_row, remaining_rows[next_row_index]))
                    next_row_index += 1
                filename, data_bytes, exc = future.result()
                if exc is not None:
                    skipped_count += 1
                    app.logger.warning('Skipping %s while building library export for %s: %s', filename, library_id, exc)
                else:
                    zip_file.writestr(filename, data_bytes)
                    written_count += 1
                    photos_in_part += 1
                    part_bytes += len(data_bytes)
                rows_processed += 1
                is_last_row = rows_processed >= photos_total

                if photos_in_part and (part_bytes >= LIBRARY_EXPORT_PART_MAX_BYTES or is_last_row):
                    zip_file.close()
                    tmp.close()
                    part_index += 1
                    part_size = os.path.getsize(tmp_path)
                    try:
                        with open(tmp_path, 'rb') as fh:
                            upload_file_to_blob(BLOB_EXPORTS_CONTAINER, _library_export_part_blob_name(library_id, part_index), fh, 'application/zip')
                    finally:
                        _remove_file_quietly(tmp_path)
                    parts_summary.append({'partIndex': part_index, 'photosIncluded': photos_in_part, 'sizeBytes': part_size})
                    _durable_checkpoint()
                    if not is_last_row:
                        tmp = tempfile.NamedTemporaryFile(prefix='libexport-', suffix='.zip', delete=False)
                        tmp_path = tmp.name
                        zip_file = zipfile.ZipFile(tmp, 'w', compression=zipfile.ZIP_STORED)
                        photos_in_part = 0
                        part_bytes = 0
                elif rows_processed % 25 == 0:
                    _live_progress_heartbeat()
    except Exception:
        try:
            zip_file.close()
        except Exception:
            pass
        try:
            tmp.close()
        finally:
            _remove_file_quietly(tmp_path)
        raise

    if photos_in_part == 0:
        # Either there was nothing left to process (a fully-resumed retry) or
        # the trailing row(s) all failed -- either way the currently-open part
        # is empty and was never uploaded above; discard it.
        try:
            zip_file.close()
        except Exception:
            pass
        tmp.close()
        _remove_file_quietly(tmp_path)

    if part_index == 0:
        # Nothing was ever produced (empty library, or every row failed) --
        # leave any blobs from a prior successful export alone, matching this
        # function's pre-chunking behavior.
        return {'photosIncluded': written_count, 'photosSkipped': skipped_count, 'sizeBytes': 0, 'parts': []}

    _cleanup_stale_library_export_parts(library_id, part_index)

    multi_part = len(parts_summary) > 1

    def _finalize_part(part: Dict) -> Dict:
        idx = part['partIndex']
        blob_name = _library_export_part_blob_name(library_id, idx)
        download_name = (
            f"{library_name or library_id}-export-part-{idx}.zip" if multi_part
            else f"{library_name or library_id}-export.zip"
        )
        download_url, expires_at = _create_stable_read_sas_url(BLOB_EXPORTS_CONTAINER, blob_name, download_filename=download_name)
        return {
            'partIndex': idx,
            'downloadUrl': download_url,
            'expiresAt': expires_at,
            'photosIncluded': part['photosIncluded'],
            'sizeBytes': part['sizeBytes'],
        }

    # Each call is independent (day-cached delegation key + local HMAC
    # signing, no shared mutable state beyond that already-thread-safe
    # cache), and results are tiny strings -- unlike the per-file download
    # loop above, there's no memory-buildup risk from running all of these
    # concurrently rather than with a bounded sliding window. Confirmed live
    # 2026-09-04: run sequentially, this loop over a few hundred parts
    # (large libraries at LIBRARY_EXPORT_PART_MAX_BYTES's smaller end) took
    # long enough to repeatedly get caught mid-loop by routine replica
    # restarts (deploys/scale-downs), and since this loop wasn't itself
    # checkpointed, every interruption meant redoing the whole thing from
    # part 1 again on redelivery.
    with ThreadPoolExecutor(max_workers=LIBRARY_EXPORT_DOWNLOAD_CONCURRENCY) as executor:
        parts_result = list(executor.map(_finalize_part, parts_summary))
    total_size = sum(int(part.get('sizeBytes') or 0) for part in parts_summary)

    return {
        'parts': parts_result,
        'photosIncluded': written_count,
        'photosSkipped': skipped_count,
        'sizeBytes': total_size,
    }


def _has_active_library_download_job(library_id: str) -> Optional[str]:
    """Return the jobId of an in-flight 'library_download' job for this
    library, if any -- de-dupes button-mash/multi-tab clicks. library_download
    jobs are partitioned by libraryId (see _job_partition_key), so this is a
    scoped partition query instead of the fleet-wide 'jobs'-partition scan
    this used to share with _has_active_clustering_job."""
    if jobs_table_client is None:
        return None
    try:
        rows = list(jobs_table_client.query_entities(f"PartitionKey eq '{_escape_odata(library_id)}'"))
    except Exception:
        return None
    stale_before = datetime.now(timezone.utc) - timedelta(minutes=CLUSTERING_ACTIVE_JOB_STALE_MINUTES)
    for row in rows:
        if str(row.get('jobType') or '') != 'library_download':
            continue
        if str(row.get('status') or '').lower() not in {'queued', 'running'}:
            continue
        updated = _parse_iso_date(str(row.get('updatedAt') or ''))
        if updated is not None and updated < stale_before:
            continue
        return str(row.get('jobId') or '')
    return None


def _enqueue_library_download_job(library_id: str, actor_user_id: str, library_name: str) -> Dict[str, str]:
    job_id = f"libdownload:{library_id}:{uuid.uuid4().hex}"
    if library_ops_queue_client is None:
        # No queue configured (e.g. local dev) — run inline rather than
        # silently dropping the request.
        app.logger.warning('Library-ops queue client is unavailable; running library download %s inline', job_id)
        try:
            summary = _execute_library_download(library_id, library_name, job_id=job_id, user_id=actor_user_id)
            _upsert_job_status(job_id, actor_user_id, 'library_download', 'done', result=summary, libraryId=library_id)
            return {'status': 'done', 'jobId': job_id}
        except Exception as exc:
            app.logger.exception('Inline library download failed for %s', library_id)
            _upsert_job_status(job_id, actor_user_id, 'library_download', 'failed', error='Library download failed', libraryId=library_id)
            return {'status': 'failed', 'jobId': job_id}
    message = {
        'jobId': job_id,
        'correlationId': job_id,
        'user_id': actor_user_id,
        'libraryId': library_id,
        'libraryName': library_name,
        'type': 'library_download',
    }
    try:
        library_ops_queue_client.send_message(json.dumps(message, separators=(',', ':')))
    except Exception:
        app.logger.exception('Failed to enqueue library download job %s', job_id)
        _upsert_job_status(job_id, actor_user_id, 'library_download', 'failed', error='Failed to queue download job', libraryId=library_id)
        return {'status': 'failed', 'jobId': job_id}
    _upsert_job_status(job_id, actor_user_id, 'library_download', 'queued', libraryId=library_id)
    return {'status': 'queued', 'jobId': job_id}


# ---------------------------------------------------------------------------
# Client-orchestrated library export: the browser downloads every original
# file directly from blob storage (see frontend/src/services/
# libraryExportDownloader.ts) instead of the server building a zip. This
# endpoint's only job is handing out {filename, url} pages -- no zipping, no
# queue, no worker -- which is what keeps it viable at up to ~500k photos
# where /api/library/download/* (kept only as a rollback path) would take
# multi-day, fully-serial worker time to build the equivalent zip parts.
# ---------------------------------------------------------------------------
LIBRARY_EXPORT_MANIFEST_PAGE_SIZE = int(os.getenv('LIBRARY_EXPORT_MANIFEST_PAGE_SIZE', '500'))


def _encode_export_manifest_cursor(token: Optional[Dict]) -> Optional[str]:
    # azure-data-tables' continuation_token is a {'PartitionKey', 'RowKey'}
    # dict, not the plain string azure.core.paging.PageIterator's own type
    # hint implies (confirmed live 2026-09-05: a bare token.encode() 500'd
    # every request with "'dict' object has no attribute 'encode'") -- JSON
    # round-trip it before base64ing so the opaque cursor stays a single
    # string for the client to carry in a query param.
    if not token:
        return None
    return base64.urlsafe_b64encode(json.dumps(token).encode('utf-8')).decode('ascii')


def _decode_export_manifest_cursor(cursor: str) -> Optional[Dict]:
    try:
        token = json.loads(base64.urlsafe_b64decode(cursor.encode('ascii')).decode('utf-8'))
        return token if isinstance(token, dict) else None
    except Exception:
        return None


def _preview_proxy_url(filename: str) -> str:
    return f'/api/photos/preview/{filename}'


def _filename_requires_backend_preview(filename: str) -> bool:
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    return ext in BROWSER_UNVIEWABLE_EXTENSIONS


def _is_missing_media_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return '404' in text or 'resourcenotfound' in text or 'does not exist' in text or 'not found' in text


def _media_container_for_kind(kind: str) -> str:
    if kind == 'thumbnail':
        return BLOB_THUMBNAIL_CONTAINER
    if kind == 'cover':
        return BLOB_COVER_CONTAINER
    return BLOB_IMAGE_CONTAINER


def _stream_media_response(
    kind: str,
    blob_name: str,
    *,
    content_type: str,
    cache_control: str,
    content_length: Optional[int] = None,
    download_filename: Optional[str] = None,
):
    """Stream blob bytes in chunks to avoid buffering whole files in RAM.

    ``download_filename`` sets an 'inline' Content-Disposition carrying the
    original name, so a proxied download of an anonymized (UUID) blob restores the
    real filename instead of exposing the UUID.
    """
    container_name = _media_container_for_kind(kind)
    if not blob_service_client or not container_name:
        raise RuntimeError(f'{kind} storage is not configured')
    blob_client = blob_service_client.get_blob_client(container=container_name, blob=blob_name)
    downloader = blob_client.download_blob(max_concurrency=1)

    def _iter_chunks():
        for chunk in downloader.chunks():
            if chunk:
                yield chunk

    resp = Response(
        stream_with_context(_iter_chunks()),
        mimetype=(content_type or 'application/octet-stream'),
    )
    if content_length is not None:
        try:
            resp.headers['Content-Length'] = str(max(0, int(content_length)))
        except Exception:
            pass
    resp.headers['Cache-Control'] = cache_control
    if download_filename:
        resp.headers['Content-Disposition'] = _download_content_disposition(download_filename)
    return resp


def _looks_like_jpeg(data: bytes) -> bool:
    return bool(data) and data.startswith(b'\xff\xd8')


PREVIEW_JOB_TYPE = 'media_preview'


def _preview_cache_blob_name(blob_name: str) -> str:
    # Keyed on the physical blob name (the anonymous UUID for anonymized photos),
    # so the derived preview blob never embeds the original filename either.
    return f'preview/{blob_name}.jpg'


def _stream_cached_preview(filename: str, *, cache_control: str, blob_name: Optional[str] = None):
    preview_blob = _preview_cache_blob_name(blob_name or filename)
    try:
        props = get_media_properties('thumbnail', preview_blob)
    except Exception as exc:
        if _is_missing_media_error(exc):
            return None
        raise
    content_type = props.get('content_type') or 'image/jpeg'
    return _stream_media_response(
        'thumbnail',
        preview_blob,
        content_type=content_type,
        cache_control=cache_control,
        content_length=props.get('size'),
    )


def _active_preview_job_for_file(user_id: str, filename: str) -> Optional[str]:
    """Called on every proxy_preview cache-miss (i.e. every first view of a
    RAW/CR3 or other backend-preview-required file) -- a hot, user-facing read
    path. media_preview jobs are partitioned by userId (see
    _job_partition_key), so this is a scoped partition query instead of the
    (213k+ row and growing) fleet-wide 'jobs'-partition scan this used to
    share with _has_active_clustering_job."""
    if jobs_table_client is None:
        return None
    try:
        rows = list(jobs_table_client.query_entities(f"PartitionKey eq '{_escape_odata(user_id)}'"))
    except Exception:
        return None
    for row in rows:
        if str(row.get('jobType') or '') != PREVIEW_JOB_TYPE:
            continue
        if str(row.get('filename') or '') != filename:
            continue
        if str(row.get('status') or '').lower() in {'queued', 'running'}:
            return str(row.get('jobId') or '')
    return None


def _enqueue_preview_generation_job(user_id: str, filename: str) -> Dict[str, str]:
    existing_job_id = _active_preview_job_for_file(user_id, filename)
    if existing_job_id:
        return {'status': 'already_queued', 'jobId': existing_job_id}
    if clustering_queue_client is None:
        return {'status': 'unavailable', 'jobId': ''}
    job_id = f'preview:{user_id}:{uuid.uuid4().hex}'
    payload = {
        'jobId': job_id,
        'correlationId': job_id,
        'user_id': user_id,
        'type': PREVIEW_JOB_TYPE,
        'filename': filename,
    }
    try:
        clustering_queue_client.send_message(json.dumps(payload, separators=(',', ':')))
        _upsert_job_status(job_id, user_id, PREVIEW_JOB_TYPE, 'queued', filename=filename)
        _update_metadata_entity_fields(user_id, filename, {'preview_status': 'queued'})
        return {'status': 'queued', 'jobId': job_id}
    except Exception:
        app.logger.exception('Failed to enqueue preview generation for %s', filename)
        return {'status': 'failed', 'jobId': job_id}


def _preview_failure_payload(filename: str) -> dict:
    """Build a structured, user-facing explanation for why a preview could not be made."""
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    if ext in {'heic', 'heif'}:
        reason = 'heic_decode_failed'
        detail = ('This HEIC/HEIF image could not be converted into a viewable preview. '
                  'It may be damaged or use an unsupported variant.')
    elif ext == 'jxl':
        reason = 'jxl_decode_failed'
        detail = ('This JPEG XL image could not be converted into a viewable preview. '
                  'It may be damaged or use an unsupported variant.')
    elif ext in RAW_EXTENSIONS_RAWPY or ext in RAW_EXTENSIONS_CINEMA:
        reason = 'raw_decode_failed'
        detail = (f'This .{ext.upper()} file is a RAW format with no usable embedded preview, and it '
                  'could not be decoded on the server, so a preview could not be generated.')
    else:
        reason = 'preview_failed'
        detail = 'This image could not be converted into a viewable preview.'
    return {
        'error': 'Preview not available',
        'reason': reason,
        'detail': detail,
        'canDownloadOriginal': True,
    }


PHOTO_ACCESS_KINDS = {'thumbnail', 'image', 'preview'}


def _is_supported_photo_access_kind(kind: str) -> bool:
    return kind in PHOTO_ACCESS_KINDS


def _photo_access_container(kind: str) -> Optional[str]:
    if kind == 'image':
        return BLOB_IMAGE_CONTAINER
    if kind in ('thumbnail', 'preview'):
        # Preview blobs live in the same container as thumbnails, under a
        # preview/{blob}.jpg key -- see _preview_cache_blob_name.
        return BLOB_THUMBNAIL_CONTAINER
    return None


def _thumbnail_access_response(safe_name: str, metadata: Optional[Dict]) -> Optional[Dict]:
    """Route to preview/proxy when no real thumbnail blob exists yet.

    Decided from metadata already in hand (thumbnail_status) rather than a blob
    HEAD per file — the HEAD round-trips were the bulk of the cost of the
    access endpoints on large grids.
    """
    if str((metadata or {}).get('thumbnail_status') or '').strip().lower() == 'done':
        return None
    return {
        'url': make_proxy_url(safe_name, 'thumbnail'),
        'expiresAt': '',
        'filename': safe_name,
        'kind': 'thumbnail',
    }


def _preview_access_response(safe_name: str, metadata: Optional[Dict]) -> Optional[Dict]:
    """Route to the proxy (which caches + enqueues generation on a miss) when
    no real preview blob exists yet, same shape as _thumbnail_access_response.

    Every photo view used to always proxy through proxy_preview for RAW/HEIC
    only; now that preview is the default lightbox image for every photo,
    minting a real SAS whenever one's ready (like thumbnail already does)
    matters a lot more -- otherwise every single photo view round-trips
    through the Flask app instead of hitting blob storage directly.
    """
    if str((metadata or {}).get('preview_status') or '').strip().lower() == 'done':
        return None
    return {
        'url': _preview_proxy_url(safe_name),
        'expiresAt': '',
        'filename': safe_name,
        'kind': 'preview',
    }


def _access_url_response(url: str, expires_at: str, filename: str, kind: str) -> Dict:
    return {
        'url': url,
        'expiresAt': expires_at,
        'filename': filename,
        'kind': kind,
    }


 
# Helper to return backend proxy URLs instead of SAS URLs when using managed identity
def make_proxy_url(filename: str, kind: str = 'thumbnail') -> str:
    """Return a backend proxy URL instead of a SAS URL."""
    return f'/api/photos/{kind}/{filename}'


# ---------------------------------------------------------------------------
# Direct-to-blob media URLs (MEDIA_URL_MODE='sas').
#
# Streaming media bytes through this container dominated its compute bill, so
# in 'sas' mode the browser gets read SAS URLs pointing straight at blob
# storage. Two properties keep this cheap:
#   - the user-delegation key is minted once per UTC day and cached (in-process
#     plus a shared row in the metadata table so every worker/replica signs
#     with the SAME key), instead of one key round-trip per URL;
#   - SAS start/expiry are day-aligned, so a given blob's URL is byte-identical
#     across requests all day and the browser HTTP cache keeps working.
# The window is [day start - 15min, day start + 48h]: a URL minted just before
# midnight is still valid for a full day after.
# ---------------------------------------------------------------------------

_MEDIA_DELEGATION_KEY_PARTITION = '__system__'
_delegation_key_lock = threading.Lock()
_delegation_key_cached: Optional[Tuple[datetime, 'UserDelegationKey', datetime, datetime]] = None
# After a mint failure (e.g. Azurite has no user-delegation keys), fall back to
# proxy URLs without re-attempting SAS on every single URL for a while.
_media_sas_retry_after = 0.0


def _delegation_key_to_row(key: 'UserDelegationKey') -> Dict[str, str]:
    return {
        'signed_oid': key.signed_oid,
        'signed_tid': key.signed_tid,
        'signed_start': key.signed_start,
        'signed_expiry': key.signed_expiry,
        'signed_service': key.signed_service,
        'signed_version': key.signed_version,
        'value': key.value,
    }


def _delegation_key_from_row(entity: Dict) -> 'UserDelegationKey':
    key = UserDelegationKey()
    for field in ('signed_oid', 'signed_tid', 'signed_start', 'signed_expiry',
                  'signed_service', 'signed_version', 'value'):
        setattr(key, field, entity[field])
    return key


def _stable_delegation_key() -> Tuple['UserDelegationKey', datetime, datetime]:
    """Return (key, starts_on, expires_on) for the current UTC day, cached."""
    global _delegation_key_cached
    if blob_service_client is None:
        raise RuntimeError('Blob storage is not configured')
    now = datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    cached = _delegation_key_cached
    if cached and cached[0] == day_start:
        return cached[1], cached[2], cached[3]
    with _delegation_key_lock:
        cached = _delegation_key_cached
        if cached and cached[0] == day_start:
            return cached[1], cached[2], cached[3]
        starts_on = day_start - timedelta(minutes=15)
        expires_on = day_start + timedelta(hours=48)
        row_key = f"media_delegation_key_{day_start.strftime('%Y%m%d')}"
        key = None
        if metadata_table_client is not None:
            try:
                entity = metadata_table_client.get_entity(_MEDIA_DELEGATION_KEY_PARTITION, row_key)
                key = _delegation_key_from_row(entity)
            except Exception:
                key = None
        if key is None:
            key = blob_service_client.get_user_delegation_key(starts_on, expires_on)
            if metadata_table_client is not None:
                try:
                    metadata_table_client.create_entity({
                        'PartitionKey': _MEDIA_DELEGATION_KEY_PARTITION,
                        'RowKey': row_key,
                        **_delegation_key_to_row(key),
                    })
                except ResourceExistsError:
                    # Another replica won the race; sign with its key so URLs
                    # stay identical cluster-wide.
                    try:
                        entity = metadata_table_client.get_entity(_MEDIA_DELEGATION_KEY_PARTITION, row_key)
                        key = _delegation_key_from_row(entity)
                    except Exception:
                        pass
                except Exception:
                    app.logger.warning('Could not persist shared media delegation key', exc_info=True)
        _delegation_key_cached = (day_start, key, starts_on, expires_on)
        return key, starts_on, expires_on


def _download_content_disposition(original_filename: str) -> str:
    """Build an 'inline' Content-Disposition that carries the ORIGINAL filename.

    Anonymized blobs are named with an opaque UUID, so a direct 'Save As' would
    otherwise suggest the UUID (bad UX and it leaks the internal name). 'inline'
    keeps <img> display working while giving the real filename to an explicit
    download. Emits both a plain-ASCII filename and an RFC 5987 filename* so
    non-ASCII names survive.
    """
    safe = re.sub(r'[\r\n"\\]', '_', str(original_filename or '')).strip() or 'photo'
    ascii_fallback = safe.encode('ascii', 'ignore').decode('ascii') or 'photo'
    return f"inline; filename=\"{ascii_fallback}\"; filename*=UTF-8''{_urlquote(safe, safe='')}"


def _create_stable_read_sas_url(
    container_name: str,
    filename: str,
    *,
    download_filename: Optional[str] = None,
) -> Tuple[str, str]:
    """Read-only SAS with day-aligned validity, deterministic for the whole day.

    When ``download_filename`` is given, the SAS carries a Content-Disposition
    response override (``rscd``) so a direct download of an anonymized blob
    restores the original filename instead of exposing the UUID.
    """
    if not account_name:
        raise RuntimeError('Storage account name is not configured')
    key, starts_on, expires_on = _stable_delegation_key()
    extra_sas_kwargs = {}
    if download_filename:
        extra_sas_kwargs['content_disposition'] = _download_content_disposition(download_filename)
    sas = generate_blob_sas(
        account_name=account_name,
        container_name=container_name,
        blob_name=filename,
        user_delegation_key=key,
        permission=BlobSasPermissions(read=True),
        start=starts_on,
        expiry=expires_on,
        **extra_sas_kwargs,
    )
    blob_client = blob_service_client.get_blob_client(container=container_name, blob=filename)
    return f'{blob_client.url}?{sas}', expires_on.isoformat()


def _stable_container_read_sas(container_name: str) -> Tuple[str, str, str]:
    """One read-only SAS scoped to the whole container -- day-aligned and
    deterministic like _create_stable_read_sas_url above, but signed ONCE per
    call instead of once per blob. Returns (base_url, sas_query_string,
    expires_at_iso); the caller builds each blob's URL as
    f'{base_url}/{quote(blob_name)}?{sas}'.

    generate_container_sas is a pure local HMAC computation against the
    already-cached user-delegation-key (see _stable_delegation_key), so this
    costs the same as signing one blob URL -- the saving comes from
    library_export_manifest_page calling it once per page instead of once per
    row, which is what actually matters at up to ~500k files per library.
    """
    if not account_name:
        raise RuntimeError('Storage account name is not configured')
    key, starts_on, expires_on = _stable_delegation_key()
    sas = generate_container_sas(
        account_name=account_name,
        container_name=container_name,
        user_delegation_key=key,
        permission=ContainerSasPermissions(read=True),
        start=starts_on,
        expiry=expires_on,
    )
    container_client = blob_service_client.get_container_client(container_name)
    return container_client.url, sas, expires_on.isoformat()


_MEDIA_KIND_CONTAINERS = {
    'thumbnail': lambda: BLOB_THUMBNAIL_CONTAINER,
    'image': lambda: BLOB_IMAGE_CONTAINER,
    'cover': lambda: BLOB_COVER_CONTAINER,
}


def make_media_url(filename: str, kind: str = 'thumbnail', blob_name: Optional[str] = None) -> str:
    """Best URL for the browser to fetch a media blob: direct SAS, else proxy.

    ``filename`` is always the original (user-facing) name; the proxy route keys
    off it and resolves the physical blob internally. ``blob_name`` is the physical
    blob (the anonymous UUID for anonymized photos) and is used only when minting a
    direct SAS URL, which points at storage and therefore must name the real blob.
    """
    global _media_sas_retry_after
    if MEDIA_URL_MODE == 'sas' and blob_service_client is not None and account_name:
        container_getter = _MEDIA_KIND_CONTAINERS.get(kind)
        if container_getter and time.monotonic() >= _media_sas_retry_after:
            try:
                # Only full images are "saved"/downloaded by users; give those the
                # original filename via the SAS Content-Disposition override so an
                # anonymized (UUID) blob doesn't surface its UUID on Save As.
                download_filename = filename if kind == 'image' else None
                url, _ = _create_stable_read_sas_url(
                    container_getter(),
                    blob_name or filename,
                    download_filename=download_filename,
                )
                return url
            except Exception as exc:
                _media_sas_retry_after = time.monotonic() + 300
                app.logger.warning('SAS media URL minting failed; serving proxy URLs for 5 minutes: %s', exc)
    return make_proxy_url(filename, kind)


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
            # Deleting a cluster is explicit user intent to stop tracking these
            # faces. Without marking them rejected, they're simply "unclustered"
            # and the next upload's auto-cluster pass (or a manual recluster)
            # regroups them by embedding similarity — silently resurrecting the
            # deleted cluster under a new personId. Reuse the existing
            # rejected/reviewStatus mechanism (already respected by
            # _face_is_clusterable) so released faces stay out of clustering
            # for good, same as a manually-rejected face.
            face.pop('suspiciousReason', None)
            face['reviewStatus'] = 'rejected'
            face['rejected'] = True
            face['rejectedReason'] = 'person_cluster_deleted'
            face['rejectedAt'] = datetime.now(timezone.utc).isoformat()
            # upsert_entity defaults to MERGE mode, which only writes fields
            # present in the payload -- popping a key above only affects this
            # local dict, not the stored row. REPLACE actually clears the
            # popped fields server-side since `face` is the full entity we
            # just fetched, not a partial payload.
            face_table_client.upsert_entity(face, mode=UpdateMode.REPLACE)
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


def _merge_persons_core(user_id: str, person_id: str, merge_ids: List) -> Optional[Dict]:
    """Reassign faces from ``merge_ids`` into ``person_id`` and delete the source
    person rows. Returns ``{'mergeId': ...}``, or ``None`` if the base person
    doesn't exist.

    This is the data-mutation half of a merge only; callers own triggering
    identity propagation afterwards (the single-merge route does one propagate
    job immediately, the batch route coalesces one job across the whole batch)
    since that's the part worth doing once instead of once per pair.
    """
    try:
        base = person_table_client.get_entity(partition_key=user_id, row_key=person_id)
    except Exception:
        return None

    base_snapshot = dict(base)
    face_map = {}
    try:
        base_face_ids = set(json.loads(base.get('faceIds', '[]')))
    except Exception:
        base_face_ids = set()

    # Capture restore snapshots and persist an undo record BEFORE any destructive
    # change. merge_persons reassigns faces and deletes the merged person rows
    # in-place; if the worker is killed mid-merge (e.g. an OOM kill) after a
    # delete, a named cluster would be gone with no undo record to restore it.
    # Writing base + merged snapshots up front guarantees undo_merge can always
    # bring the original people (and their names) back; the record is finalised
    # with the real faceMap once reassignment completes.
    merge_id = str(uuid.uuid4())
    merged_snapshots = []
    for mid in merge_ids:
        try:
            merged_snapshots.append(dict(person_table_client.get_entity(partition_key=user_id, row_key=mid)))
        except Exception:
            continue

    merge_payload_blob_name = f'{user_id}/{merge_id}.json'

    def _write_merge_record(final_face_map, final_target_name):
        merged_names = [s['name'] for s in merged_snapshots if isinstance(s, dict) and s.get('name')]
        payload_json = json.dumps({
            'base': base_snapshot,
            'merged': merged_snapshots,
            'faceMap': final_face_map,
        })
        try:
            # Base+merged person snapshots can run large enough to threaten
            # Table Storage's 64KB-per-property/1MB-per-entity caps -- store
            # the payload in Blob Storage and keep only a reference on the row
            # (called twice per merge -- safety-net then final -- overwriting
            # the same blob each time is fine, it's keyed by merge_id).
            upload_file_to_blob(
                BLOB_MERGE_PAYLOADS_CONTAINER, merge_payload_blob_name,
                payload_json.encode('utf-8'), 'application/json',
            )
            merge_table_client.upsert_entity({
                'PartitionKey': user_id,
                'RowKey': merge_id,
                'targetPersonId': person_id,
                'mergedIds': json.dumps(merge_ids),
                'targetName': final_target_name or '',
                'mergedNames': json.dumps(merged_names),
                'payloadBlobName': merge_payload_blob_name,
                'createdAt': None,
            })
        except Exception:
            pass

    # Safety-net write before the destructive phase (faceMap filled in later).
    _write_merge_record({}, str(base_snapshot.get('name') or ''))

    # Collect every reassignment first, then flush the face writes in transactional
    # batches (all faces share the user's partition) rather than one round-trip per
    # face. Two things dominated the old per-face loop and are removed here:
    #   * unlinking faces from the source clusters via _remove_face_from_person,
    #     which recomputed each shrinking source's representative embedding once per
    #     face — O(faces^2) work on clusters that are deleted moments later;
    #   * a synchronous upsert per face.
    merge_id_set = set(str(mid) for mid in merge_ids)
    face_updates: Dict[str, Dict] = {}
    external_removals: List[Tuple[str, str]] = []

    owner_face_ids_by_person: Dict[str, set] = {}
    if face_table_client is not None:
        try:
            all_face_rows = list(face_table_client.query_entities(f"PartitionKey eq '{_escape_odata(user_id)}'"))
        except Exception:
            all_face_rows = []
        for face_row in all_face_rows:
            fid = str(face_row.get('RowKey') or '')
            owner = str(face_row.get('personId') or '')
            if not fid or owner not in merge_id_set:
                continue
            owner_face_ids_by_person.setdefault(owner, set()).add(fid)

    for mid in merge_ids:
        try:
            merged = person_table_client.get_entity(partition_key=user_id, row_key=mid)
        except Exception:
            continue
        try:
            merged_face_ids = json.loads(merged.get('faceIds', '[]'))
        except Exception:
            merged_face_ids = []
        # Merge must include every currently-owned face. If memberships were
        # stale, relying only on merged.faceIds can miss faces and let a later
        # recluster resurrect pre-merge clusters.
        merged_face_ids = list(dict.fromkeys([
            *[str(fid) for fid in merged_face_ids if fid],
            *sorted(owner_face_ids_by_person.get(str(mid), set())),
        ]))
        for fid in merged_face_ids:
            fid = str(fid)
            if fid in face_updates:
                continue
            try:
                face_ent = face_table_client.get_entity(partition_key=user_id, row_key=fid)
            except Exception:
                continue
            if _face_is_rejected(face_ent):
                continue
            current_owner = str(face_ent.get('personId') or '')
            face_map[fid] = mid
            base_face_ids.add(fid)
            # Only unlink faces that belong to some *other* person outside this
            # merge. The source clusters (merge_id_set) are deleted wholesale below,
            # so stripping faces off them individually is wasted work.
            if current_owner and current_owner != person_id and current_owner not in merge_id_set:
                external_removals.append((current_owner, fid))
            face_ent['personId'] = person_id
            face_ent['confirmedByUser'] = True
            face_ent['reviewStatus'] = 'confirmed'
            face_ent['rejected'] = False
            face_ent.pop('suspiciousReason', None)
            face_ent.pop('rejectedReason', None)
            face_ent.pop('rejectedAt', None)
            face_ent['confidence'] = max(float(face_ent.get('confidence', 0.0) or 0.0), 1.0)
            face_updates[fid] = face_ent

    for owner_id, fid in external_removals:
        _remove_face_from_person(user_id, owner_id, fid)

    _batch_upsert_entities(face_table_client, list(face_updates.values()))

    for mid in merge_ids:
        try:
            person_table_client.delete_entity(partition_key=user_id, row_key=mid)
        except Exception:
            pass

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

    # Finalise the restore record written before the destructive phase: same
    # RowKey (merge_id), now carrying the real faceMap so undo can revert face
    # ownership exactly, and the base's possibly-adopted name.
    try:
        final_base = person_table_client.get_entity(partition_key=user_id, row_key=person_id)
        final_target_name = str(final_base.get('name') or '')
    except Exception:
        final_target_name = str(base_snapshot.get('name') or '')
    _write_merge_record(face_map, final_target_name)

    return {'mergeId': merge_id}


def _person_is_named(user_id: str, person_id: str) -> bool:
    try:
        person = person_table_client.get_entity(partition_key=user_id, row_key=person_id)
    except Exception:
        return False
    return bool(str(person.get('name') or '').strip()) and not _is_unnamed_name(str(person.get('name') or ''))


# Sane upper bound on how many merge pairs one bulk-approve request can carry —
# each pair does a full per-user face-table query, so an unbounded batch is a
# single request doing unbounded work.
PEOPLE_MERGE_BATCH_MAX = 50


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


def _delete_faces_bulk(user_id: str, face_ids: List[str]) -> Dict:
    """Reject a batch of faces in one pass.

    Mirrors _mark_face_not_a_face for each face but batches the per-person
    faceIds update, representative-embedding refresh, empty-person cleanup and
    metadata rebuild so a bulk delete costs one rebuild pass instead of one per
    face.
    """
    if face_table_client is None or person_table_client is None:
        return {'deleted': [], 'errors': [], 'deletedPersonIds': [], 'status': 503}

    deleted: List[str] = []
    errors: List[Dict] = []
    affected_filenames = set()
    # person_id -> set(faceId) removed from that person in this batch
    person_face_removals: Dict[str, set] = {}
    now = datetime.now(timezone.utc).isoformat()

    seen = set()
    for raw_face_id in face_ids:
        face_id = str(raw_face_id or '').strip()
        if not face_id or face_id in seen:
            continue
        seen.add(face_id)
        try:
            face = face_table_client.get_entity(partition_key=user_id, row_key=face_id)
        except Exception:
            errors.append({'faceId': face_id, 'error': 'face not found'})
            continue
        person_id = str(face.get('personId') or '')
        filename = str(face.get('filename') or '')
        face['reviewStatus'] = 'rejected'
        face['rejected'] = True
        face['rejectedReason'] = 'not_a_face'
        face['rejectedAt'] = now
        face.pop('personId', None)
        face.pop('confirmedByUser', None)
        try:
            face_table_client.upsert_entity(face)
        except Exception as exc:
            app.logger.warning('Face reject upsert failed for %s: %s', face_id, exc)
            errors.append({'faceId': face_id, 'error': 'update failed'})
            continue
        deleted.append(face_id)
        if filename:
            affected_filenames.add(filename)
        if person_id:
            person_face_removals.setdefault(person_id, set()).add(face_id)

    deleted_person_ids: List[str] = []
    for person_id, removed in person_face_removals.items():
        try:
            person = person_table_client.get_entity(partition_key=user_id, row_key=person_id)
        except Exception:
            continue
        try:
            existing = json.loads(person.get('faceIds', '[]') or '[]')
        except Exception:
            existing = []
        next_face_ids = [fid for fid in existing if fid not in removed]
        if next_face_ids:
            person['faceIds'] = json.dumps(next_face_ids)
            try:
                person_table_client.upsert_entity(person)
                _update_person_rep_embedding(user_id, person_id)
            except Exception:
                pass
        else:
            try:
                person_table_client.delete_entity(partition_key=user_id, row_key=person_id)
                deleted_person_ids.append(person_id)
            except Exception:
                pass

    if affected_filenames:
        _rebuild_metadata_faces_for_filenames(user_id, affected_filenames)

    return {
        'deleted': deleted,
        'errors': errors,
        'deletedPersonIds': deleted_person_ids,
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
        app.logger.exception('Face split upsert failed for %s', face_id)
        try:
            person_table_client.delete_entity(partition_key=user_id, row_key=new_person_id)
        except Exception:
            pass
        return {'success': False, 'error': 'Failed to split face into new person', 'status': 500}

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


MAX_INIT_BATCH_FILES = 20


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
    # Reuse the cached day key when it covers the requested window (it always
    # extends >=24h out, so every standard lifetime fits); only unusually long
    # expiries pay for a dedicated key round-trip.
    try:
        delegation_key, _, key_expires_on = _stable_delegation_key()
        if expires_on > key_expires_on:
            delegation_key = blob_service_client.get_user_delegation_key(starts_on, expires_on)
    except Exception:
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


def _queue_ipwork_processing(user_id: str, filename: str, steps: Optional[List[str]] = None) -> Dict[str, str]:
    """Send a real queue message so ipworker (also) processes this upload.

    No-op in 'browser' mode (the default) -- ipworker never needs to be
    deployed at all in that mode. In 'both' mode this races the browser's own
    client-side pipeline; whichever result lands first for a given step wins
    (see _step_locked_done in storage_utils.py) and the other is discarded.

    `steps` defaults to every step ipworker owns (IPWORK_STEPS); callers that
    only want a subset re-processed (e.g. the admin backfill endpoint scoped
    to `{"steps": ["ocr"]}`) can pass it explicitly so ipworker doesn't
    silently redo more than was asked.
    """
    if PROCESSING_MODE == 'browser':
        return {'status': 'skipped', 'reason': 'browser_only_processing'}
    requested_steps = [s for s in (steps if steps is not None else IPWORK_STEPS) if s in IPWORK_STEPS]
    if not requested_steps:
        return {'status': 'skipped', 'reason': 'no_ipwork_steps_requested'}
    job_id = f'ipwork:{user_id}:{uuid.uuid4().hex}'
    if ipwork_queue_client is None:
        app.logger.warning('ipwork queue client is unavailable; job %s was not enqueued', job_id)
        return {'status': 'unavailable', 'jobId': job_id}
    message = {
        'jobId': job_id,
        'correlationId': job_id,
        'user_id': user_id,
        'filename': filename,
        'steps': requested_steps,
    }
    try:
        ipwork_queue_client.send_message(json.dumps(message, separators=(',', ':')))
    except Exception:
        app.logger.exception('Failed to enqueue ipwork job %s', job_id)
        return {'status': 'failed', 'jobId': job_id}
    _upsert_job_status(job_id, user_id, 'ipwork', 'queued')
    return {'status': 'queued', 'jobId': job_id}


def _queue_upload_processing(user_id: str, final_name: str) -> None:
    if is_video_file(final_name):
        return
    _enqueue_processing_steps(user_id, final_name, ['face'])
    _queue_ipwork_processing(user_id, final_name)


def _face_ids_awaiting_person_assignment(user_id: str, filename: str) -> List[str]:
    """Face rows for one photo that don't have a personId yet. metadata's own
    'faces' list never carries the server-assigned Table RowKey (it's built
    from the client-reported payload with the embedding stripped), so this is
    the only way to get face_ids for the incremental matcher below."""
    if face_table_client is None:
        return []
    try:
        rows = list(face_table_client.query_entities(
            f"PartitionKey eq '{_escape_odata(user_id)}' and filename eq '{_escape_odata(filename)}'"
        ))
    except Exception:
        return []
    return [str(r.get('RowKey') or '') for r in rows if r.get('RowKey') and not r.get('personId')]


def _queue_people_clustering_after_face_processing(user_id: str, filename: str, metadata: Optional[Dict]) -> Optional[Dict[str, str]]:
    """Queue face-to-person assignment for newly-detected faces, then queue a
    maintenance recluster if one's due.

    2026-08-19 (6696f27) promoted the incremental matcher from a
    worker-only fallback to running synchronously, in-process, right here --
    fixing a real bug (a runaway full-library DBSCAN loop that kept
    ownphotostore-worker alive re-reclustering every ~2 minutes during a
    backfill) by moving the work onto the request path instead. That traded
    one bug for another: the unvectorized per-photo embedding-index rebuild
    (see _load_people_embedding_index) now competes for the same
    GUNICORN_THREADS/GIL as every other concurrent upload request, which is
    what made /upload/finalize and /upload/client-processing responses
    balloon from ms to tens-of-seconds under a large burst. Back to queuing
    it for the standalone clustering worker (see
    _enqueue_incremental_assign_job) -- the maintenance-cooldown gate below,
    which is what actually fixed the runaway-loop bug, is untouched.
    """
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

    try:
        _enqueue_incremental_assign_job(user_id, filename)
    except Exception:
        app.logger.exception('Failed to queue incremental face-to-person assignment for %s/%s', user_id, filename)

    if not _clustering_maintenance_due(user_id):
        return {'status': 'cooldown_skipped'}

    return _enqueue_clustering_job(
        user_id,
        job_type='people_cluster',
        payload={
            'trigger': 'upload_face_ready',
            'filename': filename,
            'faceCount': face_count,
        },
        # A big upload calls this once per photo; if a clustering job is
        # already in flight for the user (e.g. from an earlier photo in the
        # same batch), coalesce into a single rerun after it finishes instead
        # of enqueueing a new job per photo.
        coalesce_on_conflict=True,
    )


_ANONYMOUS_BLOB_NAME_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.IGNORECASE)


def _validate_client_blob_name(value: object) -> Optional[str]:
    """Validate a client-echoed blobName from /upload/init's own response.

    Only accepts the exact UUID shape _generate_anonymous_id() produces --
    anything else (missing, malformed, or an attempt to point finalize at an
    arbitrary blob name) is rejected and the caller falls back to the
    metadata-row lookup instead.
    """
    candidate = str(value or '').strip()
    return candidate if _ANONYMOUS_BLOB_NAME_RE.match(candidate) else None


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
    # Required so the source-image / thumbnail-upload SAS URLs resolve to the
    # anonymized (UUID) blob. Without it the projection drops anonymousImageId,
    # _blob_name_from_metadata falls back to the original filename, and the
    # browser's reprocessing fetch 404s on a blob that doesn't exist.
    'anonymousImageId',
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


def _browser_processing_face_version_stale(entity: Dict) -> bool:
    """True when this photo's faces were embedded under an older embedding
    version than the one currently in force, so they should be recomputed."""
    if not FACE_REEMBED_STALE_VERSION:
        return False
    try:
        processing_metadata = json.loads(entity.get('processing_metadata') or '{}')
    except Exception:
        return False
    client_face = processing_metadata.get('client_face') if isinstance(processing_metadata, dict) else None
    if not isinstance(client_face, dict):
        return False
    # Only photos that actually stored faces carry a corrupt/stale embedding;
    # a 'no_data' result (no faces detected) has nothing to re-embed and the
    # detector output is independent of the embedding model.
    if not client_face.get('hasData'):
        return False
    stored_version = str(client_face.get('modelTaxonomyVersion') or '').strip()
    # Browser and ipworker tag faces with their own distinct version strings
    # for the same underlying AdaFace model (see IPWORKER_FACE_CLUSTER_EMBEDDING_VERSION's
    # definition) -- comparing only against FACE_CLUSTER_EMBEDDING_VERSION would
    # mark every ipworker-processed face permanently stale even right after a
    # successful re-embed, since it's never tagged with the browser's string.
    return stored_version not in _face_embedding_allowed_versions()


def _browser_processing_pending_item(entity: Dict) -> Optional[Dict]:
    filename = str(entity.get('RowKey') or '').strip()
    if not filename:
        return None
    if str(entity.get('processing_state') or '').strip().lower() == 'deleted':
        return None

    statuses = {}
    has_pending_status = False
    lease_expired = _browser_processing_lease_expired(entity)
    face_version_stale = _browser_processing_face_version_stale(entity)
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
        # Re-queue face processing when the stored embeddings are on an older
        # model version so a version bump recomputes them across the library.
        if field == 'face_status' and face_version_stale and status in BROWSER_PROCESSING_TERMINAL_STATUSES:
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
        # Physical blob name (anonymous UUID for anonymized photos) for minting the
        # reprocessing source-read / thumbnail-upload SAS URLs. Kept internal —
        # stripped from the item before it's returned to the browser.
        '_blobName': _blob_name_from_metadata(entity, filename),
        'statuses': statuses,
        'lastProcessingUpdate': entity.get('last_processing_update') or '',
        'rotation': _normalize_rotation(entity.get('rotation', 0)),
    }


IPWORK_SWEEP_INTERVAL_SECONDS = int(os.getenv('IPWORK_SWEEP_INTERVAL_SECONDS', '1200'))
IPWORK_SWEEP_STALE_QUEUED_SECONDS = int(os.getenv('IPWORK_SWEEP_STALE_QUEUED_SECONDS', '1800'))
_IPWORK_SWEEP_LOCK_ROW_KEY = 'ipwork_sweep_lock'


def _try_claim_ipwork_sweep_lock(owner_id: str, ttl_seconds: int) -> bool:
    """Every ipworker replica runs its own copy of _ipwork_sweep_loop on an
    independent timer -- without this, N replicas redundantly re-enqueue the
    same "stale" backlog on every cycle. Confirmed live 2026-08-28: with 4
    replicas this produced a ~5x queue-depth blowup during a single backfill
    (see docs/ipworker-architecture.md). Only the replica that wins this
    claim actually runs the sweep for a given cycle; the
    create-then-steal-if-expired shape mirrors the delegation-key claim
    above (_MEDIA_DELEGATION_KEY_PARTITION) so a crashed lock holder doesn't
    block the sweep forever.
    """
    if metadata_table_client is None:
        return False
    now = datetime.now(timezone.utc)
    lock_row = {
        'PartitionKey': _MEDIA_DELEGATION_KEY_PARTITION,
        'RowKey': _IPWORK_SWEEP_LOCK_ROW_KEY,
        'lease_owner': owner_id,
        'lease_expires_at': (now + timedelta(seconds=ttl_seconds)).isoformat(),
    }
    try:
        metadata_table_client.create_entity(dict(lock_row))
        return True
    except ResourceExistsError:
        pass
    except Exception:
        return False

    try:
        existing = metadata_table_client.get_entity(_MEDIA_DELEGATION_KEY_PARTITION, _IPWORK_SWEEP_LOCK_ROW_KEY)
    except Exception:
        return False

    expires_at = str(existing.get('lease_expires_at') or '')
    try:
        still_held = bool(expires_at) and datetime.fromisoformat(expires_at.replace('Z', '+00:00')) > now
    except Exception:
        still_held = False
    if still_held and str(existing.get('lease_owner') or '') != owner_id:
        return False  # another replica holds a live lease

    try:
        metadata_table_client.update_entity(
            lock_row, etag=existing.metadata['etag'], match_condition=MatchConditions.IfNotModified,
        )
        return True
    except Exception:
        return False  # lost the race to steal an expired lease


def _ipwork_sweep_eligible_steps(entity: Dict) -> List[str]:
    """Which IPWORK_STEPS on this photo are safe to hand ipworker right now.

    Mirrors _browser_processing_pending_item's notion of "not done yet"
    (same terminal-status set, same lease-expiry check, same ai_vision
    no-data retry case, same stale-face-embedding-version retry case) but
    adds one more guard that only matters for an *active* re-enqueue
    (unlike the browser poll, which is read-only): a 'queued' step is
    skipped unless it's been stuck long enough (IPWORK_SWEEP_STALE_QUEUED_SECONDS)
    that its original queue message was plausibly lost (ipworker was
    stopped, queue purged, etc.) rather than still legitimately in flight
    -- otherwise every sweep interval would pile a fresh duplicate message
    onto a perfectly healthy backlog.
    """
    if str(entity.get('processing_state') or '').strip().lower() == 'deleted':
        return []
    lease_expired = _browser_processing_lease_expired(entity)
    face_version_stale = _browser_processing_face_version_stale(entity)
    last_update = str(entity.get('last_processing_update') or '').strip()
    stale_enough = True
    if last_update:
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(last_update.replace('Z', '+00:00'))).total_seconds()
            stale_enough = age >= IPWORK_SWEEP_STALE_QUEUED_SECONDS
        except Exception:
            stale_enough = True

    eligible = []
    for step in IPWORK_STEPS:
        status = str(entity.get(f'{step}_status') or '').strip().lower()
        retryable_no_data = (
            step == 'ai_vision'
            and status in {'failed', 'no_data', 'skipped', 'timeout'}
            and _raw_ai_vision_no_data_should_retry(entity)
        )
        # A 'done' face_status doesn't mean this photo is actually done if
        # its stored embedding predates the current FACE_CLUSTER_EMBEDDING_VERSION
        # -- _browser_processing_pending_item already re-queues these for the
        # browser (see _browser_processing_face_version_stale); without this,
        # an embedding-version bump would only ever get re-embedded by
        # whichever browser tabs happen to be open, and ipworker's sweep
        # would silently skip this entire class of "pending" work forever.
        face_version_retry = step == 'face' and face_version_stale
        # Same reasoning as _handle_ipwork_queue_payload's runnable_steps carve-out:
        # a browser-reported 'skipped'/'no_data'/'unsupported' thumbnail only means
        # its lightweight embedded-preview scan gave up, not that no real thumbnail
        # exists -- ipworker's own exiftool-based extraction is strictly more
        # capable and must still get a chance via this safety-net sweep too.
        thumbnail_retry = step == 'thumbnail' and status in {'skipped', 'no_data', 'unsupported'}
        if status in BROWSER_PROCESSING_TERMINAL_STATUSES and not retryable_no_data and not face_version_retry and not thumbnail_retry:
            continue
        if status == 'running' and not lease_expired:
            continue
        if status == 'queued' and not stale_enough:
            continue
        eligible.append(step)
    return eligible


def _sweep_stale_processing_into_ipwork() -> Dict[str, int]:
    """Self-heal orphaned photos: ones with no ipwork queue message ever
    sent (or one that's long gone) sitting stuck pending/stale-queued/
    stale-running across EVERY library, not just whichever one library a
    currently-open browser tab happens to have active.

    Without this, a photo that misses the one-time upload-time race (e.g.
    uploaded while ipworker was admin-stopped, or PROCESSING_MODE was
    briefly 'browser') stays invisible to both consumers forever: ipworker
    only ever sees what's explicitly queued to it, and the browser's own
    /upload/processing/pending poll is scoped to one library per open tab.
    """
    stats = {'libraries': 0, 'photosQueued': 0, 'stepsQueued': 0}
    if library_store is None or metadata_table_client is None:
        return stats
    try:
        library_ids = library_store.list_all_library_ids()
    except Exception:
        worker_logger.exception('ipwork sweep: failed to list libraries')
        return stats

    for library_id in library_ids:
        stats['libraries'] += 1
        try:
            rows = _query_metadata_rows_for_user(library_id, select=BROWSER_PROCESSING_PENDING_SELECT, purpose='ipwork_sweep')
        except Exception:
            worker_logger.warning('ipwork sweep: metadata scan failed for library %s', library_id, exc_info=True)
            continue
        for row in rows:
            filename = str(row.get('RowKey') or '').strip()
            if not filename or is_video_file(filename):
                continue
            steps = _ipwork_sweep_eligible_steps(row)
            if not steps:
                continue
            try:
                _queue_ipwork_processing(library_id, filename, steps=steps)
                stats['photosQueued'] += 1
                stats['stepsQueued'] += len(steps)
            except Exception:
                worker_logger.exception('ipwork sweep: failed to enqueue %s/%s', library_id, filename)
    return stats


def _sweep_tag_embedding_indexes() -> Dict[str, int]:
    """Rebuild any library's tag-embedding index (see storage_utils.py's
    "Per-user tag-embedding index" section) that a tag-affecting write has
    marked dirty since the last rebuild. Only meaningful here: this loop only
    runs inside run_ipworker(), the one role that actually has real CLIP
    loaded (see docs/ipworker-architecture.md) -- the plain backend role that
    serves /photos/search deliberately never triggers this rebuild inline
    (get_user_tag_embedding_index(..., allow_refresh=False) there), the same
    reason its lexical/vector-index siblings avoid an expensive synchronous
    rebuild during an interactive request."""
    stats = {'librariesChecked': 0, 'indexesAvailable': 0}
    if library_store is None or not vision_utils.image_encoder_available():
        return stats
    try:
        library_ids = library_store.list_all_library_ids()
    except Exception:
        worker_logger.exception('tag-embedding sweep: failed to list libraries')
        return stats

    for library_id in library_ids:
        stats['librariesChecked'] += 1
        try:
            # Only actually re-embeds when the manifest is dirty or missing --
            # otherwise this is one cheap manifest blob read per library, same
            # cost shape as get_user_lexical_index's own lazy-rebuild check.
            index = get_user_tag_embedding_index(library_id, allow_refresh=True)
        except Exception:
            worker_logger.exception('tag-embedding sweep: rebuild failed for library %s', library_id)
            continue
        if index is not None:
            stats['indexesAvailable'] += 1
    return stats


def _ipwork_sweep_loop() -> None:
    """Runs for the lifetime of the ipworker process on its own daemon
    thread, independent of the queue-polling loop in run_ipworker, so a
    slow/large sweep never delays picking up fresh queue messages.

    Every replica runs this same loop, so each iteration first claims a
    cluster-wide lock (_try_claim_ipwork_sweep_lock) and skips the actual
    scan entirely if another replica already holds it -- see that
    function's docstring for why this matters."""
    owner_id = uuid.uuid4().hex
    time.sleep(min(60, IPWORK_SWEEP_INTERVAL_SECONDS))
    while True:
        try:
            if _try_claim_ipwork_sweep_lock(owner_id, ttl_seconds=IPWORK_SWEEP_INTERVAL_SECONDS):
                stats = _sweep_stale_processing_into_ipwork()
                if stats['photosQueued']:
                    worker_logger.info(
                        'ipwork sweep: released %d stale photo(s), %d step(s), across %d librar(y/ies)',
                        stats['photosQueued'], stats['stepsQueued'], stats['libraries'],
                    )
                tag_embedding_stats = _sweep_tag_embedding_indexes()
                if tag_embedding_stats['librariesChecked']:
                    worker_logger.info(
                        'tag-embedding sweep: %d/%d librar(y/ies) have a usable index',
                        tag_embedding_stats['indexesAvailable'], tag_embedding_stats['librariesChecked'],
                    )
        except Exception:
            worker_logger.exception('ipwork sweep iteration failed')
        time.sleep(IPWORK_SWEEP_INTERVAL_SECONDS)


def _claim_processing_lease_response(
    user_id: str,
    filename: str,
    lease_owner: str,
    steps: Optional[List[str]],
    client_blob_name: Optional[str] = None,
) -> Tuple[Dict, int]:
    """Shared body of upload_processing_claim, also used per-item by the
    batch route below -- same lease semantics either way, just fewer HTTP
    round trips when claiming several photos at once."""
    try:
        lease = claim_processing_lease(user_id, filename, lease_owner, lease_seconds=120, steps=steps)
    except Exception as exc:
        message = str(exc)
        app.logger.warning('Processing lease claim failed for %s: %s', filename, exc)
        if 'already held by another client' in message.lower() or 'lease is already held' in message.lower():
            return {'claimed': False, 'reason': 'lease_active'}, 200
        return {'claimed': False, 'reason': 'lease_active'}, 409
    response = {
        'claimed': True,
        'leaseId': lease_owner,
        'expiresAt': lease.get('leaseExpiresAt') or '',
    }
    try:
        # Thumbnail upload must target the same physical (anonymous) blob as the
        # image. Prefer a validated client-echoed blobName (the caller's own
        # /upload/init response, just like finalize's _validate_client_blob_name
        # use) over re-deriving it from the shared (user, filename) metadata row:
        # this claim call fires right after finalizeUploadedFile, and when several
        # in-flight files share an original filename (e.g. many photos named
        # "IMG_8771.jpeg" from different devices merged into one library), the row
        # lookup can return whichever same-named file's row state landed last --
        # not necessarily this one -- so the direct thumbnail PUT below would
        # silently overwrite a different, already-correct photo's thumbnail blob
        # (the image itself is untouched since that upload path already uses the
        # client-echoed blobName; only this thumbnail-claim path still had the gap).
        physical_name = _validate_client_blob_name(client_blob_name) or _resolve_media_blob_name(user_id, filename)
        thumbnail_url, thumbnail_expires_at = _create_direct_thumbnail_upload_blob_url(physical_name)
        response['thumbnailUploadUrl'] = thumbnail_url
        response['thumbnailUploadExpiresAt'] = thumbnail_expires_at
    except Exception:
        app.logger.warning('Failed to mint browser thumbnail upload URL for claimed photo %s', filename, exc_info=True)
    return response, 200


MAX_CLAIM_BATCH_ITEMS = 60


UPLOAD_TRACKING_FIELDS = (
    'received_ranges',
    'upload_total_size',
    'upload_started_at',
    'upload_last_chunk_at',
    'upload_sha256_expected',
    'upload_sha256_actual',
    'upload_sha256_match',
    # The anonymous-blob reservation is transient upload state: it is promoted to
    # anonymousImageId at finalize, so any leftover reservation should be cleared
    # alongside the other tracking fields.
    'pendingAnonymousBlob',
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
        app.logger.warning('Blob delete failed for %s/%s: %s', container_name, blob_name, exc)
        return 'delete failed'
    return None


def _delete_photo_blobs_if_present(blob_name: str, extra_blob_names: Optional[List[str]] = None) -> List[str]:
    """Delete the image + thumbnail blobs for a photo. ``blob_name`` is the physical
    blob (the anonymous UUID for anonymized photos, else the original filename);
    ``extra_blob_names`` lets callers also clear the original-filename blobs as a
    belt-and-suspenders cleanup when unsure which naming a photo used."""
    names = [blob_name]
    for extra in (extra_blob_names or []):
        if extra and extra not in names:
            names.append(extra)
    errors: List[str] = []
    for label, container_name in (('blob image', BLOB_IMAGE_CONTAINER), ('blob thumbnail', BLOB_THUMBNAIL_CONTAINER)):
        for name in names:
            error = _delete_blob_if_present(container_name, name)
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
    touch_user_search_indexes_state(user_id)


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
                app.logger.warning('Temp file delete failed for %s: %s', entry, exc)
                errors.append(f'{entry}: delete failed')
    except OSError as exc:
        app.logger.warning('Temp directory scan failed for %s: %s', temp_dir, exc)
        errors.append('temp directory scan failed')
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
    # thumbnail_status deliberately excluded: kickOffThumbnailForFile fires an
    # early, thumbnail-only processing claim concurrently with the raw upload
    # itself (before finalize), so thumbnail_status can be 'running' on a row
    # whose upload was then abandoned before finalize ever ran -- fooling this
    # into treating a genuinely incomplete upload as "completed", which left
    # the row's stale claim/lease fields (and its orphaned blob reservation)
    # behind forever instead of deleting them below. anonymousImageId,
    # fileHash, mimeType, and perceptualHash are only ever stamped together at
    # finalize (finalize_uploaded_file, storage_utils.py), so they're the
    # actual trustworthy "this upload really finished" signals.
    has_completed_metadata = bool(metadata) and bool(
        metadata.get('fileHash')
        or metadata.get('perceptualHash')
        or metadata.get('mimeType')
        or metadata.get('anonymousImageId')
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
                app.logger.warning('Cleanup metadata update failed for %s: %s', filename, exc)
                cleanup['errors'].append('metadata: update failed')
        else:
            # A failed/incomplete direct upload wrote its blob under the anonymous
            # UUID reserved at /upload/init. finalize may not have run, so read the
            # UUID from the metadata row: anonymousImageId if finalize got that far,
            # else the pendingAnonymousBlob reservation. Delete the real blob (and
            # the original filename too, as a safety net).
            anonymous_id = str(
                metadata.get('anonymousImageId')
                or metadata.get('pendingAnonymousBlob')
                or ''
            ).strip()
            physical_name = anonymous_id or filename

            # A cross-tenant filename clash at finalize (_resolve_filename_for_upload)
            # renames the upload but only touches the row keyed by the NEW name --
            # this row (still keyed by the pre-rename filename) is left behind
            # looking exactly like an abandoned upload, even though its blob
            # reservation was long since promoted to a live, finalized row under
            # the renamed filename. The name-mapping table is stamped with the
            # current owner at finalize, so it's the authoritative check before
            # deleting a blob by UUID: if some OTHER filename now owns this
            # anonymous_id, the blob is live and must be preserved -- only the
            # stale husk row below should go.
            blob_owned_elsewhere = False
            if anonymous_id:
                current_owner_filename = original_filename_for_anonymous_id(user_id, anonymous_id)
                blob_owned_elsewhere = bool(current_owner_filename) and current_owner_filename != filename

            if blob_owned_elsewhere:
                app.logger.warning(
                    'Skipped deleting blob %s during cleanup of stale row %s: '
                    'now owned by %s.', anonymous_id, filename, current_owner_filename,
                )
            else:
                extra = [filename] if anonymous_id else None
                cleanup['errors'].extend(_delete_photo_blobs_if_present(physical_name, extra))
                if anonymous_id:
                    try:
                        delete_image_name_mapping(user_id, anonymous_id)
                    except Exception:
                        pass

            try:
                metadata_table_client.delete_entity(partition_key=user_id, row_key=filename)
                cleanup['metadataAction'] = 'deleted'
            except Exception as exc:
                app.logger.warning('Cleanup metadata delete failed for %s: %s', filename, exc)
                cleanup['errors'].append('metadata: delete failed')

    return cleanup


def _row_passes_search_filters(
    row: Dict,
    capture_start: Optional[datetime],
    capture_end: Optional[datetime],
    matched_person_groups: List[List[str]],
    matched_location_terms: List[str],
) -> bool:
    """Hard exclusion checks -- a row failing any of these can never appear
    in results no matter how well it scores, so these must run before (and
    independently of) any lexical/semantic scoring. Kept as its own tier
    (mirroring Apple's Core Spotlight "filtered results" concept -- exact
    metadata matching, separate from semantic ranking) so a future filter
    added here can't accidentally end up gating a scoring signal the way
    two real bugs already did in this function's history: a zero lexical
    score used to veto semantic scoring outright, and a stray location
    filler-word ("the"/"a") used to veto an otherwise-perfect match."""
    if capture_start or capture_end:
        if not _capture_in_range(row, capture_start, capture_end):
            return False
    if matched_person_groups:
        try:
            people_ids = set(str(pid) for pid in json.loads(row.get('peopleIds', '[]') or '[]'))
        except Exception:
            people_ids = set()
        # Every distinct queried person must appear (at least one id from
        # each group) -- "alice and bob" means both, not either.
        if not all(any(pid in people_ids for pid in group) for group in matched_person_groups):
            return False
    if not _metadata_matches_locations(row, matched_location_terms):
        return False
    return True


def _score_search_row(
    tokens: Dict[str, List[str]],
    filename: str,
    row: Dict,
    exif_data: Dict[str, str],
    *,
    query_embedding: List[float],
    vector_scores: Dict[str, float],
    current_embedding_version: str,
    semantic_threshold: float,
    matched_person_groups: List[List[str]],
    matched_location_terms: List[str],
) -> Tuple[float, float, str]:
    """Pure scoring tier: always computes both the lexical and semantic
    signals in full for a row that already passed _row_passes_search_filters,
    then blends them into one combined score. Deliberately has no early
    return/continue of its own -- a row's lexical score being zero (e.g. a
    query word that isn't literally one of its tags) must never prevent its
    semantic score from being computed and considered, and vice versa.
    Returns (combined_score, lexical_score, semantic_text) -- lexical_score
    and semantic_text are returned alongside the combined score because two
    downstream callers (the exact-modifier-match check and the vector-index
    fallback path) need them independently of the blended total."""
    semantic_text = build_semantic_text(filename, row)
    lexical_score = lexical_search_score(tokens, filename, row, exif_data)

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
    return score, lexical_score, semantic_text


def _search_row_belongs_in_fallback_bucket(
    score: float,
    lexical_score: float,
    semantic_text: str,
    tokens: Dict[str, List[str]],
    filename: str,
    row: Dict,
    *,
    has_context_intent: bool,
) -> bool:
    """Ranking/bucketing tier: decides whether an already-scored, already-
    positive-score row belongs in the primary results or the "no exact
    <modifier> <object> found" fallback bucket (only relevant for
    modifier+object queries like "red car"). Separated from scoring itself
    so a bucketing rule can never be mistaken for (or accidentally turned
    into) an exclusion rule -- every row reaching this tier has already
    unconditionally cleared both the filter and scoring tiers above."""
    if not has_context_intent:
        return False
    if tokens.get('modifiers'):
        searchable_text = ' '.join([
            filename,
            row.get('caption', ''),
            semantic_text,
            row.get('ocrText', ''),
            ' '.join(parse_json_list(row.get('objects', '[]'))),
            ' '.join(parse_json_list(row.get('peopleNames', '[]'))),
            row.get('address', ''),
            row.get('locationCity', ''),
            row.get('locationRegion', ''),
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
            return True
    return lexical_score < 12.0


TAG_EMBEDDING_EXPANSION_MIN_SIMILARITY = float(os.getenv('TAG_EMBEDDING_EXPANSION_MIN_SIMILARITY', '0.75'))


def _expand_tokens_with_tag_embeddings(tokens: Dict[str, List[str]], user_id: str) -> None:
    """Mutates tokens['expanded'] in place with real tags from this user's own
    library that are semantically close (via CLIP embeddings) to any query
    word that isn't already one of those tags -- e.g. "puppy" expands to
    "dog" if that's what the library's photos are actually tagged with. Reads
    two precomputed caches only (a static common-word vocabulary table and
    this user's ipworker-built tag-embedding index); never runs live model
    inference, so it's safe to call from the backend role, which never loads
    real CLIP (see docs/ipworker-architecture.md and storage_utils.py's
    "Per-user tag-embedding index" section for the full design)."""
    all_tokens = tokens.get('all') or []
    if not all_tokens:
        return
    tag_index = get_user_tag_embedding_index(user_id, allow_refresh=False)
    if not tag_index or not tag_index.get('tags'):
        return
    known_tags = set(tag_index['tags'])
    expanded = tokens.setdefault('expanded', [])
    seen = set(all_tokens) | set(expanded)
    for token in all_tokens:
        if token in known_tags:
            continue  # already an exact match, no expansion needed
        word_embedding = vision_utils.common_word_embedding(token)
        if not word_embedding:
            continue  # outside the fixed static vocabulary -- no expansion, not an error
        for tag in nearest_tags_for_word(word_embedding, tag_index, top_k=3, min_similarity=TAG_EMBEDDING_EXPANSION_MIN_SIMILARITY):
            if tag not in seen:
                expanded.append(tag)
                seen.add(tag)


def _shared_names_in_batch(names_set: set, user_id: str) -> set:
    """Which of ``names_set`` are content-addressed blobs still referenced by
    another library, so their blobs must NOT be deleted.

    Uses the filename-owners index (PartitionKey=filename) -- one small,
    partition-scoped query per name -- instead of a full list_entities() scan
    of the whole multi-tenant metadata table (every row of every user's every
    photo, unfiltered). That scan was the dominant cost of bulk deletes on a
    large table; see _query_filename_owners in storage_utils.py, which uses
    the same index on the upload path. Falls back to the old full scan only
    if the index table isn't configured."""
    shared: set = set()
    if not names_set:
        return shared
    if filename_owners_table_client is not None:
        def _check(name: str) -> Optional[str]:
            try:
                rows = list(filename_owners_table_client.query_entities(f"PartitionKey eq '{_escape_odata(name)}'"))
            except Exception:
                return None
            if any(str(row.get('RowKey') or '') != user_id for row in rows):
                return name
            return None
        # Each name is an independent point-scoped query -- pure network I/O
        # wait, so running them concurrently rather than one at a time is a
        # large, low-risk throughput win (same shape as DELETE_IO_CONCURRENCY's
        # other call sites in the delete path).
        with ThreadPoolExecutor(max_workers=DELETE_IO_CONCURRENCY) as executor:
            for name in executor.map(_check, names_set):
                if name:
                    shared.add(name)
        return shared
    if metadata_table_client is None:
        return shared
    try:
        # Project only the keys we need so a large multi-tenant table doesn't
        # pull full photo metadata into memory for a delete. list_entities() is
        # the canonical whole-table scan (the sharing check is inherently
        # cross-partition since blobs are content-addressed and dedup'd).
        rows = metadata_table_client.list_entities(select=['PartitionKey', 'RowKey'])
        for row in rows:
            row_key = str(row.get('RowKey') or '')
            if row_key in names_set and str(row.get('PartitionKey') or '') != user_id:
                shared.add(row_key)
    except Exception as exc:
        app.logger.warning('Shared-name batch check failed for %s: %s', user_id, exc)
    return shared


def _batch_remove_faces_for_filenames(user_id: str, names_set: set) -> set:
    """Delete all face rows for ``names_set`` and reconcile affected people in a
    single pass. Returns the set of person_ids that were deleted (emptied),
    so dangling references can be stripped from surviving photos.

    Replaces the per-file ``_remove_faces_for_filename`` (which scanned the whole
    face AND person tables for every file) with one scan of each."""
    deleted_person_ids: set = set()
    if face_table_client is None or not names_set:
        return deleted_person_ids
    try:
        face_rows = list(face_table_client.query_entities(f"PartitionKey eq '{_escape_odata(user_id)}'"))
    except Exception:
        face_rows = []
    matched_face_ids = [
        str(row.get('RowKey') or '')
        for row in face_rows
        if str(row.get('filename') or '') in names_set and row.get('RowKey')
    ]
    removed_face_ids: set = set()
    if matched_face_ids:
        def _delete_face(face_id: str) -> None:
            try:
                face_table_client.delete_entity(partition_key=user_id, row_key=face_id)
            except Exception:
                pass
        # A photo can carry several faces, so a big chunk can mean hundreds of
        # these -- independent point deletes, so run them concurrently rather
        # than one at a time (same reasoning as DELETE_IO_CONCURRENCY above).
        with ThreadPoolExecutor(max_workers=DELETE_IO_CONCURRENCY) as executor:
            list(executor.map(_delete_face, matched_face_ids))
        removed_face_ids.update(matched_face_ids)
    if not removed_face_ids or person_table_client is None:
        return deleted_person_ids
    try:
        people = list(person_table_client.query_entities(f"PartitionKey eq '{_escape_odata(user_id)}'"))
    except Exception:
        people = []
    for person in people:
        person_id = str(person.get('RowKey') or '')
        if not person_id:
            continue
        try:
            face_ids = json.loads(person.get('faceIds', '[]') or '[]')
        except Exception:
            face_ids = []
        next_face_ids = [fid for fid in face_ids if str(fid) not in removed_face_ids]
        if next_face_ids == face_ids:
            continue
        try:
            if next_face_ids:
                person['faceIds'] = json.dumps(next_face_ids)
                person_table_client.upsert_entity(person)
                _update_person_rep_embedding(user_id, person_id)
            else:
                person_table_client.delete_entity(partition_key=user_id, row_key=person_id)
                deleted_person_ids.add(person_id)
        except Exception:
            pass
    return deleted_person_ids


def _extract_job_filename(base: str, user_id: str) -> str:
    """Pull the filename segment out of a structured processing job key such as
    ``processing:{user}:{filename}:...`` or ``{user}:{filename}:...``."""
    if not base:
        return ''
    for prefix in (f'processing:{user_id}:', f'{user_id}:'):
        if base.startswith(prefix):
            return base[len(prefix):].split(':', 1)[0]
    return ''


def _batch_remove_job_rows(user_id: str, names_set: set) -> int:
    """Delete stale job-status rows for any filename in ``names_set`` in a
    single scan of the user's own jobs partition (vs. one scan per file).

    Per-file jobs are userId-partitioned (see _job_partition_key), so this is
    a scoped partition query instead of the fleet-wide 200k+-row scan this
    used to share with _has_active_clustering_job / library-scoped job types
    never correlate to an individual filename anyway."""
    if jobs_table_client is None or not names_set:
        return 0
    try:
        rows = list(jobs_table_client.query_entities(f"PartitionKey eq '{_escape_odata(user_id)}'"))
    except Exception:
        return 0
    removed = 0
    for row in rows:
        row_key = str(row.get('RowKey') or '')
        job_id = str(row.get('jobId') or '')
        correlation_id = str(row.get('correlationId') or '')
        matches = (
            str(row.get('filename') or '') in names_set
            or correlation_id in names_set
            or row_key in names_set
            or any(
                _extract_job_filename(base, user_id) in names_set
                for base in (job_id, row_key, correlation_id)
            )
        )
        if not matches:
            continue
        try:
            jobs_table_client.delete_entity(partition_key=user_id, row_key=row_key)
            removed += 1
        except Exception:
            pass
    return removed


def _batch_remove_filenames_from_albums(user_id: str, names_set: set) -> None:
    """Strip every filename in ``names_set`` from the user's albums, upserting
    each changed album once (vs. re-scanning all albums per file)."""
    if albums_table_client is None or not names_set:
        return
    try:
        rows = list(albums_table_client.query_entities(f"PartitionKey eq '{_escape_odata(user_id)}'"))
    except Exception:
        return
    for row in rows:
        try:
            filenames = json.loads(row.get('filenames', '[]') or '[]')
        except Exception:
            continue
        updated = [item for item in filenames if item not in names_set]
        if len(updated) == len(filenames):
            continue
        row['filenames'] = json.dumps(updated)
        try:
            albums_table_client.upsert_entity(row)
        except Exception:
            pass


def _batch_delete_upload_temp_files(names_set: set) -> set:
    """Remove any staged upload temp files for the batch in a single directory
    listing (vs. one listdir per file). Returns the set of names whose temp
    files were removed."""
    removed_names: set = set()
    if not names_set:
        return removed_names
    temp_dir = os.path.abspath(UPLOAD_TMP_DIR)
    try:
        if not os.path.isdir(temp_dir):
            return removed_names
        entries = os.listdir(temp_dir)
    except OSError:
        return removed_names
    for entry in entries:
        if entry.endswith('.lock'):
            continue
        # Staged files are named "{uploadId}__{filename}".
        suffix_start = entry.find('__')
        name = entry[suffix_start + 2:] if suffix_start != -1 else entry
        if name not in names_set:
            continue
        path = os.path.abspath(os.path.join(temp_dir, entry))
        if not path.startswith(temp_dir + os.sep):
            continue
        try:
            os.remove(path)
            removed_names.add(name)
        except OSError:
            pass
    return removed_names


def _purge_orphaned_photo_data(user_id: str, *, dry_run: bool = True) -> Dict:
    """Cross-reference photometadata and photofaces against actual blobs in the
    images container. Any metadata or face row whose physical blob no longer
    exists is considered orphaned and will be deleted (or reported in dry-run).

    Uses blob storage as the source of truth: if the original image blob is gone,
    all associated metadata, face rows, and person records are stale and removed."""
    result: Dict = {
        'dryRun': dry_run,
        'blobsFound': 0,
        'metadataRowsChecked': 0,
        'orphanedFilenames': [],
        'orphanedMetadataDeleted': 0,
        'orphanedFaceRows': 0,
        'orphanedFacesDeleted': 0,
        'personRecordsDeleted': 0,
        'errors': [],
    }

    if blob_service_client is None or metadata_table_client is None:
        result['errors'].append('Storage not configured')
        return result

    # Step 1: build the set of real blobs (source of truth)
    try:
        container = blob_service_client.get_container_client(BLOB_IMAGE_CONTAINER)
        real_blobs: set = {b.name for b in container.list_blobs()}
        result['blobsFound'] = len(real_blobs)
    except Exception as exc:
        app.logger.exception('Purge: failed to list blobs')
        result['errors'].append('Failed to list blobs')
        return result

    # Step 2: find metadata rows with no backing blob
    try:
        user_meta_rows = list(metadata_table_client.query_entities(
            f"PartitionKey eq '{_escape_odata(user_id)}'",
            select=['PartitionKey', 'RowKey', 'anonymousImageId', 'deleted'],
        ))
    except Exception as exc:
        app.logger.exception('Purge: failed to query metadata')
        result['errors'].append('Failed to query metadata')
        return result

    orphaned_filenames: set = set()
    for row in user_meta_rows:
        result['metadataRowsChecked'] += 1
        if _coerce_bool(row.get('deleted')):
            continue  # already soft-deleted, ignore
        filename = str(row.get('RowKey') or '')
        if not filename:
            continue
        blob_name = _blob_name_from_metadata(row, filename)
        if blob_name not in real_blobs:
            orphaned_filenames.add(filename)
            result['orphanedFilenames'].append(filename)
            if not dry_run:
                try:
                    metadata_table_client.delete_entity(partition_key=user_id, row_key=filename)
                    result['orphanedMetadataDeleted'] += 1
                except Exception as exc:
                    app.logger.warning('Purge: metadata delete failed for %s: %s', filename, exc)
                    result['errors'].append(f'metadata delete failed for {filename}')

    if not orphaned_filenames:
        return result

    # Step 3: count (dry-run) or delete face rows for orphaned photos
    if face_table_client is None:
        if not dry_run and result['orphanedMetadataDeleted'] > 0:
            result['errors'].append(
                'face_table not configured: metadata rows were deleted but face rows and '
                'person records were not cleaned up'
            )
        return result

    if dry_run:
        try:
            face_rows = list(face_table_client.query_entities(
                f"PartitionKey eq '{_escape_odata(user_id)}'",
                select=['RowKey', 'filename'],
            ))
            result['orphanedFaceRows'] = sum(
                1 for f in face_rows if str(f.get('filename') or '') in orphaned_filenames
            )
        except Exception as exc:
            app.logger.exception('Purge: failed to count orphaned face rows')
            result['errors'].append('Failed to count orphaned face rows')
    else:
        try:
            face_rows_before = list(face_table_client.query_entities(
                f"PartitionKey eq '{_escape_odata(user_id)}'",
                select=['RowKey', 'filename'],
            ))
            orphaned_face_count = sum(
                1 for f in face_rows_before if str(f.get('filename') or '') in orphaned_filenames
            )
            deleted_person_ids = _batch_remove_faces_for_filenames(user_id, orphaned_filenames)
            face_rows_after = list(face_table_client.query_entities(
                f"PartitionKey eq '{_escape_odata(user_id)}'",
                select=['RowKey', 'filename'],
            ))
            remaining_orphaned = sum(
                1 for f in face_rows_after if str(f.get('filename') or '') in orphaned_filenames
            )
            # If any remain, they failed to delete — report them
            if remaining_orphaned:
                result['errors'].append(f'{remaining_orphaned} face row(s) could not be deleted')
            result['orphanedFacesDeleted'] = orphaned_face_count - remaining_orphaned
            result['personRecordsDeleted'] = len(deleted_person_ids)
        except Exception as exc:
            app.logger.exception('Purge: face/person cleanup failed')
            result['errors'].append('Face/person cleanup failed')

    return result


def _maybe_enqueue_coalesced_rerun(job_id: Optional[str], user_id: str) -> None:
    """After a clustering job reaches a terminal state, check whether it was
    flagged (via _mark_clustering_job_rerun_requested) while it ran and, if
    so, fire exactly one follow-up job to pick up whatever queued up meanwhile.
    """
    if not job_id:
        return
    row = _get_job_row(user_id, job_id)
    if row is None:
        return
    if not row.get('rerunRequested'):
        return
    if not _clustering_maintenance_due(user_id):
        return
    _enqueue_clustering_job(user_id, job_type='people_cluster', payload={'trigger': 'coalesced_rerun'})


def _handle_clustering_queue_payload(payload: Dict, job_id: str, user_id: str, job_type: str) -> None:
    if not user_id:
        return
    if job_type == PREVIEW_JOB_TYPE:
        filename = _validate_media_filename(str(payload.get('filename') or ''))
        if not filename:
            if job_id:
                _upsert_job_status(job_id, user_id, PREVIEW_JOB_TYPE, 'failed', error='invalid filename')
            return
        metadata = _get_metadata_entity(user_id, filename)
        if metadata is None:
            if job_id:
                _upsert_job_status(job_id, user_id, PREVIEW_JOB_TYPE, 'failed', error='metadata not found', filename=filename)
            return
        if job_id:
            _upsert_job_status(job_id, user_id, PREVIEW_JOB_TYPE, 'running', filename=filename)
        try:
            # Anonymized photos are stored under the anonymous UUID; read from and
            # cache the derived preview under the same physical blob name so the
            # preview cache also stays free of the original filename.
            physical_name = _blob_name_from_metadata(metadata, filename)
            image_bytes = download_media_bytes('image', physical_name)
            preview_bytes = convert_image_to_jpeg(image_bytes, filename)
            if not preview_bytes or not _looks_like_jpeg(preview_bytes):
                raise RuntimeError('preview conversion produced invalid jpeg')
            preview_blob = _preview_cache_blob_name(physical_name)
            upload_media_file('thumbnail', preview_blob, preview_bytes, 'image/jpeg')
            _update_metadata_entity_fields(user_id, filename, {'preview_status': 'done'})
            if job_id:
                _upsert_job_status(
                    job_id,
                    user_id,
                    PREVIEW_JOB_TYPE,
                    'done',
                    filename=filename,
                    result={'previewBlob': preview_blob, 'bytes': len(preview_bytes)},
                )
        except Exception as exc:
            worker_logger.exception('Async preview generation failed for %s', filename)
            _update_metadata_entity_fields(user_id, filename, {'preview_status': 'failed'})
            if job_id:
                _upsert_job_status(job_id, user_id, PREVIEW_JOB_TYPE, 'failed', error='Preview generation failed', filename=filename)
        return

    if job_type == 'library_clean':
        target_library_id = str(payload.get('libraryId') or user_id)
        if job_id:
            _upsert_job_status(job_id, user_id, 'library_clean', 'running', libraryId=target_library_id)

        # Refresh updatedAt periodically while _execute_library_clean is still
        # walking the library's partition -- without this, a large-enough
        # library (many rows x several sequential blob/table deletes each)
        # can legitimately run past CLUSTERING_ACTIVE_JOB_STALE_MINUTES, and
        # /api/jobs/status's staleness sweep force-flips a perfectly healthy,
        # still-running clean to 'failed' ("worker restarted or timed out").
        # Mirrors the same fix already applied to the clustering job branches
        # and _execute_library_download's _live_progress_heartbeat -- see
        # CLUSTERING_JOB_HEARTBEAT_SECONDS's comment.
        stop_heartbeat = threading.Event()

        def _send_heartbeat() -> None:
            while not stop_heartbeat.wait(CLUSTERING_JOB_HEARTBEAT_SECONDS):
                if job_id:
                    try:
                        _upsert_job_status(job_id, user_id, 'library_clean', 'running', libraryId=target_library_id)
                    except Exception:
                        worker_logger.exception('Failed to send library clean job heartbeat')

        heartbeat_thread = threading.Thread(target=_send_heartbeat, daemon=True)
        heartbeat_thread.start()
        try:
            summary = _execute_library_clean(target_library_id)
            if library_store is not None:
                library_store.set_cleanup_completed(
                    target_library_id,
                    int(summary.get('photosDeleted') or 0),
                    int(summary.get('blobsDeleted') or 0),
                )
            _notify_cleanup_completed(target_library_id, summary)
            if job_id:
                _upsert_job_status(job_id, user_id, 'library_clean', 'done', result=summary, libraryId=target_library_id)
        except Exception as exc:
            worker_logger.exception('Library clean failed for %s', target_library_id)
            if library_store is not None:
                library_store.set_cleanup_failed(target_library_id, 'Library clean failed')
            if job_id:
                _upsert_job_status(job_id, user_id, 'library_clean', 'failed', error='Library clean failed', libraryId=target_library_id)
        finally:
            stop_heartbeat.set()
            heartbeat_thread.join(timeout=5)
        return

    if job_type == 'library_download':
        target_library_id = str(payload.get('libraryId') or user_id)
        library_name = str(payload.get('libraryName') or '')
        if job_id:
            _upsert_job_status(job_id, user_id, 'library_download', 'running', libraryId=target_library_id)
        try:
            summary = _execute_library_download(target_library_id, library_name, job_id=job_id, user_id=user_id)
            if job_id:
                _upsert_job_status(job_id, user_id, 'library_download', 'done', result=summary, libraryId=target_library_id)
        except Exception as exc:
            worker_logger.exception('Library download failed for %s', target_library_id)
            if job_id:
                _upsert_job_status(job_id, user_id, 'library_download', 'failed', error='Library download failed', libraryId=target_library_id)
        return

    if job_type == 'people_incremental_assign':
        # Handled here (before the _clustering_job_types() gate below, and
        # its shared per-job-type 'running' status upsert + coalesced-rerun
        # finally block) since these jobs carry no job_id -- see
        # _enqueue_incremental_assign_job for why. Re-fetches metadata and
        # face_ids fresh from storage rather than trusting anything from the
        # enqueue-time request, since this may run long after that request
        # returned.
        if not _people_features_available():
            return
        filename = _validate_media_filename(str(payload.get('filename') or ''))
        if not filename:
            return
        metadata = _get_metadata_entity(user_id, filename)
        if not isinstance(metadata, dict):
            return
        if str(metadata.get('processing_state') or '').strip().lower() == 'deleted':
            return
        if str(metadata.get('face_status') or '').strip().lower() != 'done':
            return
        try:
            face_ids = _face_ids_awaiting_person_assignment(user_id, filename)
            if face_ids:
                _assign_faces_to_people_incrementally(user_id, filename, face_ids)
        except Exception:
            worker_logger.exception('Incremental face-to-person assignment failed for %s/%s', user_id, filename)
        return

    if job_type == 'library_delete_purge':
        library_id = str(payload.get('libraryId') or user_id)
        try:
            _purge_library_data(library_id)
            invalidate_user_vector_index_cache(library_id)
            invalidate_user_lexical_index_cache(library_id)
        except Exception:
            worker_logger.exception('Library purge failed for %s', library_id)
        return

    if job_type == 'people_admin_repair':
        # Backs the Tools/Workbench dry-run+apply repair actions (dedupe,
        # suppress-suspicious, unblock-low-confidence, rebuild-people-index,
        # repair-stale-memberships, purge-orphaned). These used to run
        # synchronously on a backend request thread -- full-account face/
        # photo scans that could take long enough to force backend's own
        # container sizing up to cover a worst-case single request, even
        # though ordinary gallery browsing never hits this code path. Moved
        # here so backend can be sized for browsing traffic instead.
        action = str(payload.get('action') or '')
        dry_run = _coerce_bool(payload.get('dryRun', True))
        handlers = {
            'dedupe_faces': _dedupe_duplicate_faces,
            'suppress_suspicious': _suppress_suspicious_faces,
            'unblock_low_confidence': _unblock_low_confidence_faces,
            'rebuild_people_index': _rebuild_photo_people_index,
            'repair_stale_memberships': _repair_face_memberships,
            'purge_orphaned': _purge_orphaned_photo_data,
        }
        handler = handlers.get(action)
        if job_id:
            _upsert_job_status(job_id, user_id, 'people_admin_repair', 'running', action=action)
        if handler is None:
            if job_id:
                _upsert_job_status(job_id, user_id, 'people_admin_repair', 'failed', error='unknown repair action', action=action)
            return
        try:
            result = handler(user_id, dry_run=dry_run)
            if job_id:
                _upsert_job_status(job_id, user_id, 'people_admin_repair', 'done', result=result, action=action)
        except Exception:
            worker_logger.exception('Admin repair action %s failed for %s', action, user_id)
            if job_id:
                _upsert_job_status(job_id, user_id, 'people_admin_repair', 'failed', error='Repair action failed', action=action)
        return

    if job_type == 'vector_index_rebuild':
        if job_id:
            _upsert_job_status(job_id, user_id, 'vector_index_rebuild', 'running')
        try:
            snapshot = refresh_user_vector_index(user_id)
            if snapshot is None:
                result = {
                    'status': 'empty',
                    'userId': user_id,
                    'rowCount': 0,
                    'message': 'No face embeddings were available to rebuild a vector index.',
                }
            else:
                result = {
                    'status': 'rebuilt',
                    'userId': user_id,
                    'rowCount': len(snapshot.row_keys),
                    'sourceVersion': snapshot.source_version,
                    'embeddingVersion': snapshot.embedding_version,
                    'updatedAt': snapshot.updated_at,
                }
            if job_id:
                _upsert_job_status(job_id, user_id, 'vector_index_rebuild', 'done', result=result)
        except Exception:
            worker_logger.exception('Vector index rebuild failed for %s', user_id)
            if job_id:
                _upsert_job_status(job_id, user_id, 'vector_index_rebuild', 'failed', error='Vector index rebuild failed')
        return

    if not (job_type in _clustering_job_types() and _people_features_available()):
        return
    if job_id:
        _upsert_job_status(job_id, user_id, 'clustering', 'running')

    # Refresh updatedAt periodically while the branches below are still
    # computing -- see CLUSTERING_JOB_HEARTBEAT_SECONDS's comment. Started
    # unconditionally (even without a job_id, where the writes below are
    # cheap no-ops) so every branch is covered uniformly, same as the
    # message-lease-renewal thread this mirrors.
    stop_heartbeat = threading.Event()

    def _send_heartbeat() -> None:
        while not stop_heartbeat.wait(CLUSTERING_JOB_HEARTBEAT_SECONDS):
            if job_id:
                try:
                    _upsert_job_status(job_id, user_id, 'clustering', 'running')
                except Exception:
                    worker_logger.exception('Failed to send clustering job heartbeat')

    heartbeat_thread = threading.Thread(target=_send_heartbeat, daemon=True)
    heartbeat_thread.start()
    try:
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
                    if payload.get('trigger') == 'coalesced_rerun':
                        summary['isIntermediate'] = True
                    _upsert_job_status(job_id, user_id, 'clustering', 'done', result=summary)
        elif job_type == 'people_recluster':
            plan = _build_people_recluster_plan(user_id, allow_reassign_confirmed=bool(payload.get('allowReassignConfirmed', False)))
            if plan.get('error'):
                if job_id:
                    _upsert_job_status(job_id, user_id, 'clustering', 'failed', error=str(plan.get('error')), result=plan)
            else:
                apply_result = {'processed': 0, 'failed': 0}
                if plan.get('assignments') and plan.get('people'):
                    snapshot_id = _create_people_repair_snapshot(
                        user_id,
                        snapshot_prefix='recluster-snapshot',
                        kind='recluster_snapshot',
                    )
                    apply_result = _apply_people_recluster_plan(user_id, plan)
                    apply_result['snapshotId'] = snapshot_id
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
                        'snapshotId': str(apply_result.get('snapshotId') or ''),
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
        elif job_type == 'people_propagate':
            person_id = str(payload.get('personId') or '')
            if not person_id:
                if job_id:
                    _upsert_job_status(job_id, user_id, 'clustering', 'failed', error='missing personId')
                return
            try:
                propagation = _propagate_person_identity(user_id, person_id, apply=True, collect_suggestions=False)
            except Exception as exc:
                worker_logger.exception('Async identity propagation failed for %s', person_id)
                if job_id:
                    _upsert_job_status(job_id, user_id, 'clustering', 'failed', error='Identity propagation failed')
                return
            if job_id:
                _upsert_job_status(
                    job_id,
                    user_id,
                    'clustering',
                    'done',
                    result={'autoAssignedFaces': int(propagation.get('autoAssignedCount') or 0)},
                )
        elif job_type == 'people_propagate_batch':
            person_ids = payload.get('personIds')
            if not isinstance(person_ids, list) or not person_ids:
                if job_id:
                    _upsert_job_status(job_id, user_id, 'clustering', 'failed', error='missing personIds')
                return
            total_assigned = 0
            failed_ids: List[str] = []
            for raw_pid in person_ids:
                pid = str(raw_pid)
                try:
                    propagation = _propagate_person_identity(user_id, pid, apply=True, collect_suggestions=False)
                except Exception:
                    worker_logger.exception('Async batch identity propagation failed for %s', pid)
                    failed_ids.append(pid)
                    continue
                total_assigned += int(propagation.get('autoAssignedCount') or 0)
            if job_id:
                if failed_ids and len(failed_ids) == len(person_ids):
                    _upsert_job_status(job_id, user_id, 'clustering', 'failed', error=f'propagation failed for all {len(failed_ids)} people')
                else:
                    _upsert_job_status(
                        job_id,
                        user_id,
                        'clustering',
                        'done',
                        result={
                            'autoAssignedFaces': total_assigned,
                            'peopleCount': len(person_ids),
                            'failedCount': len(failed_ids),
                        },
                    )
    finally:
        stop_heartbeat.set()
        heartbeat_thread.join(timeout=5)
        _maybe_enqueue_coalesced_rerun(job_id, user_id)


def _poll_clustering_queue_once(queue_client, queue_name: str, max_retries: int) -> bool:
    """Receive and fully process at most one message from ``queue_client``.

    Factored out of run_clustering_worker so the same dequeue-count ceiling +
    lease-renewal + dispatch + delete logic can run against either the
    priority library-ops queue or the general clustering queue. Returns
    whether a message was found at all (whether it completed, errored, or was
    dropped for exceeding max_retries) -- callers use this to distinguish
    "this queue is empty, fall through to the next one" from "this queue had
    work", so the priority queue gets drained before the general one is ever
    touched in a given poll cycle.
    """
    messages = list(queue_client.receive_messages(
        messages_per_page=1,
        max_messages=1,
        visibility_timeout=CLUSTERING_WORKER_VISIBILITY_TIMEOUT_SECONDS,
    ))
    if not messages:
        return False
    message = messages[0]
    payload: Dict = {}
    job_id = ''
    user_id = ''
    job_type = ''

    dequeue_count = int(getattr(message, 'dequeue_count', 0) or 0)
    if dequeue_count > max_retries:
        try:
            payload = json.loads(message.content or '{}')
            if isinstance(payload, dict):
                job_id = str(payload.get('jobId') or payload.get('correlationId') or '').strip()
                user_id = str(payload.get('user_id') or payload.get('userId') or '').strip()
                job_type = str(payload.get('type') or '').strip()
        except Exception:
            pass
        reason = (
            f'Exceeded max retries ({max_retries}); '
            f'redelivered {dequeue_count} times without completing. '
            'Retry manually if this job is still wanted.'
        )
        raw_library_id = str(payload.get('libraryId') or '') if isinstance(payload, dict) else ''
        if job_id and user_id:
            try:
                # libraryId must be passed through for library_clean/
                # library_download so this lands on the authoritative
                # libraryId-partitioned row (see _job_partition_key), not just
                # this user's mirror -- otherwise any other shared-library
                # member's _active_library_cleanup_job check would keep
                # seeing this job as still queued/running forever.
                _upsert_job_status(
                    job_id, user_id, job_type or 'clustering', 'failed', error=reason,
                    libraryId=raw_library_id or None,
                )
            except Exception:
                pass
        if job_type == 'library_clean' and library_store is not None:
            # _upsert_job_status above only writes the jobs-table row; the
            # normal library_clean failure path (_handle_clustering_queue_payload)
            # also stamps the photolibraries row via set_cleanup_failed so
            # uploads unblock immediately instead of waiting on
            # _reconcile_in_progress_from_job_row to notice on the next
            # upload attempt.
            library_id = raw_library_id or user_id
            if library_id:
                try:
                    library_store.set_cleanup_failed(library_id, reason)
                except Exception:
                    pass
        worker_logger.warning(
            'Dropping %s queue message after %s dequeues (max %s), job_id=%s',
            queue_name, dequeue_count, max_retries, job_id,
        )
        try:
            queue_client.delete_message(message)
        except Exception:
            worker_logger.exception('Failed to delete %s queue message exceeding max retries', queue_name)
        return True

    # Keep this message's lease alive for as long as we're actively working
    # it, no matter how long that takes -- see
    # CLUSTERING_WORKER_VISIBILITY_TIMEOUT_SECONDS's comment above.
    # update_message() returns a new message object with a fresh pop receipt
    # each time, which the eventual delete_message must use -- hence the
    # lock-guarded holder rather than reusing the original `message` variable
    # directly.
    message_holder = [message]
    message_lock = threading.Lock()
    stop_renewal = threading.Event()

    def _renew_lease() -> None:
        while not stop_renewal.wait(CLUSTERING_WORKER_LEASE_RENEWAL_SECONDS):
            try:
                with message_lock:
                    current = message_holder[0]
                renewed = queue_client.update_message(
                    current, visibility_timeout=CLUSTERING_WORKER_VISIBILITY_TIMEOUT_SECONDS,
                )
                with message_lock:
                    message_holder[0] = renewed
            except Exception:
                # Transient renewal failures shouldn't abort the job -- if the
                # message is genuinely gone the next attempt just fails
                # harmlessly again until stop_renewal is set below.
                worker_logger.exception('Failed to renew %s queue message lease', queue_name)

    renewal_thread = threading.Thread(target=_renew_lease, daemon=True)
    renewal_thread.start()
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
                _upsert_job_status(job_id, user_id, 'clustering', 'failed', error='Clustering failed')
            except Exception:
                pass
        worker_logger.exception('Failed to process %s queue message', queue_name)
    finally:
        stop_renewal.set()
        renewal_thread.join(timeout=5)
        with message_lock:
            final_message = message_holder[0]
        try:
            queue_client.delete_message(final_message)
        except Exception:
            worker_logger.exception('Failed to delete %s queue message', queue_name)
    return True


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
    # library_ops_client is checked first every poll cycle (see the main loop
    # below) so a user-initiated library_clean/library_download never sits
    # FIFO behind an auto-triggered clustering backlog -- see
    # LIBRARY_OPS_QUEUE_NAME's comment. One extra empty-queue receive_messages
    # call per idle poll cycle is a negligible transaction cost next to that.
    library_ops_client = queue_service_client_local.get_queue_client(LIBRARY_OPS_QUEUE_NAME)
    for ensure_client in (queue_client, library_ops_client):
        try:
            ensure_client.create_queue()
        except Exception:
            pass
    worker_logger.info(
        'Worker polling queue %s (priority) then %s every %ss',
        LIBRARY_OPS_QUEUE_NAME,
        CLUSTERING_QUEUE_NAME,
        poll_seconds,
    )

    # Unlike run_ipworker, this loop had no SIGTERM handler at all -- Python's
    # default disposition for an unhandled SIGTERM is to terminate the process
    # immediately, mid-cluster_user_faces() if one happens to be running. KEDA
    # sends SIGTERM on every scale-down (queueLength=1 recomputes target
    # replica count continuously, not just on deploys), so a live fleet of
    # short-lived replicas will routinely kill a job that was seconds from
    # finishing. The queue message survives (still invisible for the rest of
    # CLUSTERING_WORKER_VISIBILITY_TIMEOUT_SECONDS) so the job isn't lost
    # forever, but the jobs-table row is left stranded at 'running' until
    # either redelivery picks it back up or /jobs/status's stale-cutoff flags
    # it 'failed' -- which is what a user sees as a spurious failure even
    # though the work itself eventually completes. Installing a handler (even
    # one that just sets a flag) stops the instant-kill: Container Apps then
    # waits out its terminationGracePeriodSeconds (default 30s) before
    # SIGKILLing, giving an in-flight message a real chance to finish and
    # delete cleanly instead of being cut off mid-write.
    shutdown_requested = threading.Event()

    def _handle_shutdown_signal(signum, _frame) -> None:
        worker_logger.info('clustering worker received signal %s, finishing in-flight message before exit', signum)
        shutdown_requested.set()

    signal.signal(signal.SIGTERM, _handle_shutdown_signal)
    signal.signal(signal.SIGINT, _handle_shutdown_signal)

    while not shutdown_requested.is_set():
        try:
            # Priority queue first: only fall through to the general
            # clustering queue once library-ops has nothing waiting, so a
            # library_clean/library_download never queues behind backfill
            # traffic. A sustained library-ops backlog can starve clustering
            # entirely under maxReplicas=1 -- accepted deliberately, since
            # these are rare, user-initiated, "someone is staring at a
            # spinner" actions and the whole point is that they win.
            processed_any = _poll_clustering_queue_once(
                library_ops_client, LIBRARY_OPS_QUEUE_NAME, LIBRARY_CLEAN_MAX_RETRIES,
            )
            if not processed_any:
                processed_any = _poll_clustering_queue_once(
                    queue_client, CLUSTERING_QUEUE_NAME, CLUSTERING_WORKER_MAX_RETRIES,
                )
            if not processed_any:
                time.sleep(poll_seconds)
        except Exception:
            worker_logger.exception('Queue polling iteration failed')
            time.sleep(poll_seconds)


# Populated by ipworker model-implementation modules (face detect/embed, OCR,
# vision tagging, geo) as they land -- see the ipworker plan. Each entry maps an
# IPWORK_STEPS name to a callable of (user_id, filename, image_bytes)
# returning a dict shaped like the matching key of the browser's
# `clientProcessing` payload (e.g. {'hasData': True, 'text': ...} for 'ocr'), or
# None/a falsy dict to report no data. A step with no registered processor is
# marked 'failed' with a clear reason instead of crashing the whole job, since
# ipworker's model coverage ships incrementally rather than all at once.
IPWORK_STEP_PROCESSORS: Dict[str, Callable[[str, str, bytes], Optional[Dict]]] = {}


def _register_ipwork_processors() -> None:
    """Import and register ipworker's model-implementation modules.

    Deliberately lazy -- called only from run_ipworker(), never at module
    import time -- because these modules pull in heavy ML deps (onnxruntime,
    opencv, mediapipe, torch, open_clip, tesserocr) that live only in the
    ipworker image's requirements-ipworker.txt. Importing them unconditionally
    at the top of app.py would break the plain backend/worker roles, which
    don't have them installed and don't need them.

    Each import is independently guarded so one missing/broken model doesn't
    take down the others -- ipworker's model coverage ships incrementally,
    not all four at once.
    """
    try:
        import ipwork_preview
        IPWORK_STEP_PROCESSORS['preview'] = ipwork_preview.process_preview
    except Exception:
        worker_logger.exception('ipwork_preview unavailable; preview step will report not_implemented')
    try:
        import ipwork_thumbnail
        IPWORK_STEP_PROCESSORS['thumbnail'] = ipwork_thumbnail.process_thumbnail
    except Exception:
        worker_logger.exception('ipwork_thumbnail unavailable; thumbnail step will report not_implemented')
    try:
        import ipwork_geo
        IPWORK_STEP_PROCESSORS['exif'] = ipwork_geo.process_exif
        IPWORK_STEP_PROCESSORS['map_detection'] = ipwork_geo.process_geo
    except Exception:
        worker_logger.exception('ipwork_geo unavailable; exif/map_detection steps will report not_implemented')
    try:
        import ipwork_ocr
        IPWORK_STEP_PROCESSORS['ocr'] = ipwork_ocr.process_ocr
    except Exception:
        worker_logger.exception('ipwork_ocr unavailable; ocr step will report not_implemented')
    try:
        import ipwork_face
        IPWORK_STEP_PROCESSORS['face'] = ipwork_face.process_face
    except Exception:
        worker_logger.exception('ipwork_face unavailable; face step will report not_implemented')
    try:
        import ipwork_vision
        IPWORK_STEP_PROCESSORS['ai_vision'] = ipwork_vision.process_vision
    except Exception:
        worker_logger.exception('ipwork_vision unavailable; ai_vision step will report not_implemented')


def _run_ipwork_steps(user_id: str, filename: str, steps: List[str]) -> Dict[str, Dict]:
    """Run each requested step's registered processor for one photo.

    Returns a dict shaped like the browser's `clientProcessing` payload so it
    can be handed straight to apply_client_processing_results_for_file.
    """
    client_processing: Dict[str, Dict] = {}
    image_bytes_cache: List[bytes] = []
    download_ms = 0

    def get_image_bytes() -> bytes:
        nonlocal download_ms
        if not image_bytes_cache:
            started = time.monotonic()
            entity = _get_metadata_entity(user_id, filename) or {}
            source_blob = str(entity.get('anonymousImageId') or '').strip() or filename
            image_bytes_cache.append(download_media_bytes('image', source_blob))
            download_ms = round((time.monotonic() - started) * 1000)
        return image_bytes_cache[0]

    def _failure_shape(step: str, error: str) -> Dict:
        # storage_utils's face block only resolves face_status to a terminal
        # state when isinstance(faces, list) is true (even empty) -- without
        # 'faces': [] here, a missing/crashing face processor would leave
        # face_status stuck at 'running' forever instead of a retryable
        # 'failed' (see ipwork_face.process_face's own except blocks for the
        # same fix applied at the per-step level).
        shape: Dict = {'hasData': False, 'error': error}
        if step == 'face':
            shape.update({'faces': [], 'rawFaceCount': 0, 'faceFailureStage': 'unsupported_runtime', 'faceFailureDetail': error})
        return shape

    # Timed separately from each step below (instead of folding it into
    # whichever step happens to trigger the lazy download) so a slow step
    # can't be blamed for I/O that's really the metadata read + blob fetch.
    step_ms: Dict[str, int] = {}
    for step in steps:
        if step not in IPWORK_STEPS:
            continue
        processor = IPWORK_STEP_PROCESSORS.get(step)
        if processor is None:
            client_processing[step] = _failure_shape(step, 'not_implemented')
            continue
        try:
            image_bytes = get_image_bytes()
        except Exception as exc:
            worker_logger.exception('ipworker image download failed for %s/%s', user_id, filename)
            client_processing[step] = _failure_shape(step, 'download_failed')
            continue
        step_started = time.monotonic()
        try:
            result = processor(user_id, filename, image_bytes)
            client_processing[step] = result if isinstance(result, dict) else _failure_shape(step, 'invalid_result_shape')
        except Exception as exc:
            worker_logger.exception('ipworker step %r failed for %s/%s', step, user_id, filename)
            client_processing[step] = _failure_shape(step, 'processing_failed')
        finally:
            step_ms[step] = round((time.monotonic() - step_started) * 1000)
        # 'preview' is meant to run first (see IPWORK_STEPS/callers): once it
        # succeeds, swap the ~2048px shrunk bytes into the shared cache so
        # every later step this call (thumbnail/face/ocr/ai_vision) decodes
        # that instead of re-downloading/re-decoding the full original --
        # this is the whole point of ordering it first (see ipwork_preview.py).
        if step == 'preview' and client_processing[step].get('hasData'):
            try:
                image_bytes_cache[0] = base64.b64decode(str(client_processing[step].get('data') or ''))
            except Exception:
                worker_logger.exception('ipworker failed to swap in shrunk preview bytes for %s/%s', user_id, filename)
    worker_logger.info(
        'ipwork step timings user=%s file=%s download_ms=%s step_ms=%s',
        user_id, filename, download_ms, step_ms,
    )
    return client_processing


def _handle_ipwork_queue_payload(payload: Dict, job_id: str, user_id: str) -> str:
    """Process one ipwork queue message. Returns 'done', 'noop', 'lease_busy',
    or 'not_found'.

    In 'both' mode the browser and ipworker are both trying to process the
    same upload, so before doing any work ipworker first competes for the
    same per-photo processing lease the browser's own tabs already use to
    avoid double-processing each other (claim_processing_lease in
    storage_utils.py -- see /upload/processing/claim). Whichever side claims
    the lease does the work; the other observes it's already held and backs
    off without wasting any inference. This also protects against two
    ipworker replicas (KEDA can scale it beyond 1) picking up the same photo.

    Results are written through the same path the browser uses, tagged with
    origin='ipworker' so provenance and the write-time _step_locked_done
    guard (storage_utils.py) both see who computed this -- a second line of
    defense in case a lease expired mid-flight and got reclaimed.

    A 'lease_busy' return tells run_ipworker's caller to leave the queue
    message undeleted so it gets redelivered and retried later (see
    IPWORK_LEASE_RETRY_LIMIT) -- this is what lets ipworker finish a photo
    whose browser tab claimed the lease and then closed mid-processing,
    instead of that photo only ever getting picked up again if some browser
    tab reopens and polls /upload/processing/pending.

    A 'not_found' return means the photo is gone for good (deleted while
    this message sat in the queue, e.g. uploaded during an ipworker outage
    and deleted before it came back) -- there is no future state in which
    retrying would succeed, so unlike 'lease_busy' this tells the caller to
    delete the message immediately instead of burning IPWORK_LEASE_RETRY_LIMIT
    redeliveries (each costing one IPWORKER_VISIBILITY_TIMEOUT_SECONDS wait)
    on a photo that will never come back.
    """
    filename = str(payload.get('filename') or '').strip()
    steps = [str(s).strip() for s in (payload.get('steps') or []) if str(s).strip() in IPWORK_STEPS]
    if not filename or not user_id or not steps:
        return 'noop'
    message_started = time.monotonic()
    lease_owner = f'ipworker-{job_id}'
    try:
        lease_started = time.monotonic()
        lease = claim_processing_lease(user_id, filename, lease_owner, lease_seconds=IPWORKER_LEASE_SECONDS, steps=steps)
        lease_claim_ms = round((time.monotonic() - lease_started) * 1000)
    except PhotoNotFoundError as exc:
        # Row was deleted (or soft-deleted) out from under this queued
        # message -- nothing to retry, so don't treat it like lease
        # contention (which would redeliver it up to IPWORK_LEASE_RETRY_LIMIT
        # times for no reason).
        _upsert_job_status(job_id, user_id, 'ipwork', 'skipped', reason=str(exc))
        return 'not_found'
    except Exception as exc:
        # Another worker (a browser tab, or another ipworker replica) already
        # holds an active lease on this photo -- they're doing the work.
        _upsert_job_status(job_id, user_id, 'ipwork', 'skipped', reason=str(exc))
        return 'lease_busy'
    # Drop any step someone else already finished while this message was
    # sitting in the queue (e.g. a redelivered retry, or two ipwork messages
    # for the same photo) -- claim_processing_lease just computed fresh
    # statuses, so this is free and avoids redoing completed inference.
    #
    # 'thumbnail' is excluded from that general rule: unlike ocr/face/
    # map_detection (where 'no_data'/'skipped' is a trustworthy "we checked,
    # there's genuinely nothing there"), a browser-reported 'skipped'/
    # 'no_data'/'unsupported' thumbnail only means the browser's lightweight
    # embedded-preview scan gave up -- it says nothing about whether a real
    # thumbnail exists. ipworker's own extraction (exiftool across several
    # embedded-preview tags) is strictly more capable, so it must still get a
    # chance to run; only a genuinely-produced 'done' thumbnail should block
    # it. Without this, kickOffThumbnailForFile's early browser-only report
    # (written before this queue message is even picked up) permanently
    # locked every such RAW photo out of ipworker's better fallback.
    lease_statuses = lease.get('statuses') or {}
    runnable_steps = [
        step for step in steps
        if str(lease_statuses.get(f'{step}Status') or '').strip().lower() not in (
            {'done'} if step == 'thumbnail' else {'done', 'no_data', 'skipped', 'unsupported'}
        )
    ]
    # claim_processing_lease reads the raw face_status field, which doesn't
    # know about embedding-version staleness -- a 'done' status there just
    # means SOME embedding was stored, not that it's the current model. The
    # sweep (_ipwork_sweep_eligible_steps) already re-offers these photos
    # for exactly this reason; without this check here too, every one of
    # them would round-trip through claim_processing_lease, see 'done', and
    # get marked 'skipped' -- silently discarding the whole point of
    # queueing them (this is exactly what happened the first time the sweep
    # ran against a real stale-embedding-version backlog).
    if 'face' in steps and 'face' not in runnable_steps:
        entity = _get_metadata_entity(user_id, filename) or {}
        if _browser_processing_face_version_stale(entity):
            runnable_steps.append('face')
    if not runnable_steps:
        release_processing_lease(user_id, filename, lease_owner)
        _upsert_job_status(job_id, user_id, 'ipwork', 'skipped', reason='already_done')
        return 'noop'
    _upsert_job_status(job_id, user_id, 'ipwork', 'running')
    lease_cleared_by_apply = False
    try:
        steps_started = time.monotonic()
        client_processing = _run_ipwork_steps(user_id, filename, runnable_steps)
        steps_ms = round((time.monotonic() - steps_started) * 1000)
        apply_started = time.monotonic()
        metadata = apply_client_processing_results_for_file(
            user_id,
            filename,
            client_processing=client_processing,
            client_processing_report=None,
            client_asset_id=f'ipworker:{job_id}',
            origin='ipworker',
            claimed_steps=runnable_steps,
        )
        apply_ms = round((time.monotonic() - apply_started) * 1000)
        # apply_client_processing_results_for_file already clears the lease
        # fields unconditionally once it returns -- mark that here so the
        # finally block below doesn't pay a redundant read+write re-releasing
        # a lease that's already cleared (same waste class just fixed on the
        # browser side, see browser AI heartbeat/release redundancy).
        lease_cleared_by_apply = True
        # The browser reaches this same trigger via /upload and
        # /upload/client-processing right after it POSTs its own results
        # (see those routes). ipworker writes results directly through
        # apply_client_processing_results_for_file instead of an HTTP call,
        # so without this it would detect faces that never get clustered
        # into people -- they'd just sit unassigned until someone manually
        # ran the admin recluster-repair flow.
        cluster_started = time.monotonic()
        try:
            _queue_people_clustering_after_face_processing(user_id, filename, metadata)
        except Exception:
            worker_logger.exception('Failed to auto-queue clustering for %s after ipwork', filename)
        cluster_ms = round((time.monotonic() - cluster_started) * 1000)
        _upsert_job_status(job_id, user_id, 'ipwork', 'done')
        # Total-vs-sum-of-parts breakdown for the whole message, not just the
        # per-step split inside _run_ipwork_steps -- lease_claim_ms/apply_ms/
        # cluster_ms cover everything outside that per-step breakdown.
        worker_logger.info(
            'ipwork message timings user=%s file=%s lease_claim_ms=%s steps_ms=%s apply_ms=%s cluster_ms=%s total_ms=%s',
            user_id, filename, lease_claim_ms, steps_ms, apply_ms, cluster_ms,
            round((time.monotonic() - message_started) * 1000),
        )
    finally:
        # Only needed when apply_client_processing_results_for_file never
        # got far enough to clear the lease itself (e.g. an exception from
        # _run_ipwork_steps or the apply call), so a failed attempt doesn't
        # leave the lease held until it naturally expires after
        # IPWORKER_LEASE_SECONDS.
        if not lease_cleared_by_apply:
            release_processing_lease(user_id, filename, lease_owner)
    return 'done'


def _prewarm_ipwork_models() -> None:
    """Synchronously triggers every lazily-created ipwork singleton once,
    before the worker pool starts, so the unlocked check-then-set race in
    each module's lazy getter is never hit concurrently once
    IPWORKER_CONCURRENCY > 1 threads start running steps. Also removes
    first-message cold-start latency. Best-effort and per-module isolated,
    matching _register_ipwork_processors' pattern -- one model failing to
    load here shouldn't block the others; the existing per-step
    'not_implemented'/error-shape fallback already handles a model being
    unavailable at request time."""
    try:
        import ipwork_face
        ipwork_face._get_yolo_session()
        ipwork_face._get_adaface_session()
        ipwork_face._get_face_landmarker()
    except Exception:
        worker_logger.exception('ipwork_face model pre-warm failed')
    try:
        import vision_utils
        vision_utils._load_model()
    except Exception:
        worker_logger.exception('vision_utils CLIP model pre-warm failed')
    try:
        import ipwork_vision
        ipwork_vision._load_vocabulary()
    except Exception:
        worker_logger.exception('ipwork_vision vocabulary pre-warm failed')
    try:
        import maps_utils
        maps_utils._get_geocoder()
        maps_utils.prewarm_offline_geocoder()
    except Exception:
        worker_logger.exception('maps_utils geocoder pre-warm failed')


def _process_ipwork_message(message) -> str:
    """Runs on a worker thread. Parses one queue message and dispatches it
    through _handle_ipwork_queue_payload, returning the outcome string
    ('done'/'noop'/'lease_busy'/'not_found'). Never raises -- any exception here is
    caught and reported via _upsert_job_status, the same as the old
    single-message loop body did inline, so a bug in one worker thread
    can't escape into the main thread's future.result() call."""
    payload = {}
    job_id = ''
    user_id = ''
    outcome = 'done'
    dequeue_count = int(getattr(message, 'dequeue_count', 0) or 0)
    try:
        payload = json.loads(message.content or '{}')
        if isinstance(payload, dict):
            job_id = str(payload.get('jobId') or payload.get('correlationId') or '').strip()
            user_id = str(payload.get('user_id') or payload.get('userId') or '').strip()
            if dequeue_count > IPWORKER_MAX_RETRIES:
                if job_id and user_id:
                    try:
                        _upsert_job_status(
                            job_id, user_id, 'ipwork', 'failed',
                            error=(
                                f'Exceeded max retries ({IPWORKER_MAX_RETRIES}); '
                                f'redelivered {dequeue_count} times without completing. '
                                'Retry manually if this job is still wanted.'
                            ),
                        )
                    except Exception:
                        pass
                worker_logger.warning(
                    'Dropping ipwork queue message after %s dequeues (max %s), job_id=%s',
                    dequeue_count, IPWORKER_MAX_RETRIES, job_id,
                )
                return 'done'  # exceeded retries, not a race -- don't retry-loop it
            outcome = _handle_ipwork_queue_payload(payload, job_id, user_id)
    except Exception as exc:
        if job_id and user_id:
            try:
                _upsert_job_status(job_id, user_id, 'ipwork', 'failed', error='Photo processing failed')
            except Exception:
                pass
        worker_logger.exception('Failed to process ipwork queue message')
        # Previously forced outcome='done' here, which made run_ipworker delete
        # the message on the very first failed attempt -- a genuine processing
        # error (DB write hiccup, transient dependency failure, whatever)
        # silently dropped the photo forever with zero retries, since the
        # dequeue_count>IPWORKER_MAX_RETRIES ceiling above only ever gets a
        # chance to fire on a *redelivered* message. 'error' instead leaves the
        # message in the queue (see the outcome check in run_ipworker) so it's
        # redelivered and retried, bounded by that same ceiling.
        outcome = 'error'
    return outcome


def _log_ipwork_memory_sample(in_flight_after: int) -> None:
    """Logs (peak RSS so far, remaining in-flight count) right after a
    photo finishes, so IPWORKER_CONCURRENCY benchmark runs can correlate
    memory against how many photos were genuinely concurrent -- Azure
    Monitor's WorkingSetBytes is container-aggregate only and can't show
    whether N concurrent photos need ~N x one photo's memory or worse.
    True per-thread RSS isn't a meaningful OS concept (threads share one
    process address space), so this is a process-wide sample, not a
    per-worker one -- correlate the *sequence* of samples against
    IPWORKER_CONCURRENCY across benchmark runs instead."""
    if resource is None:
        return
    try:
        peak_rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    except Exception:
        return
    worker_logger.info('ipwork memory sample: peak_rss_mb=%.1f in_flight=%s', peak_rss_mb, in_flight_after)


def run_ipworker() -> None:
    """Poll the ipwork queue for jobs in a standalone container."""
    logging.basicConfig(
        level=os.getenv('LOG_LEVEL', 'INFO').upper(),
        format='%(asctime)s %(levelname)s %(name)s %(message)s',
    )
    _register_ipwork_processors()
    _prewarm_ipwork_models()
    poll_seconds = float(os.getenv('IPWORKER_POLL_SECONDS', '2'))
    queue_service_client_local = queue_service_client
    if queue_service_client_local is None:
        _init_storage_clients()
        queue_service_client_local = queue_service_client
    if queue_service_client_local is None:
        raise RuntimeError('Queue service client unavailable')
    queue_client = queue_service_client_local.get_queue_client(IPWORKER_QUEUE_NAME)
    try:
        queue_client.create_queue()
    except Exception:
        pass
    worker_logger.info(
        'ipworker polling queue %s every %ss at concurrency=%s',
        IPWORKER_QUEUE_NAME,
        poll_seconds,
        IPWORKER_CONCURRENCY,
    )
    threading.Thread(target=_ipwork_sweep_loop, name='ipwork-sweep', daemon=True).start()

    # Container Apps sends SIGTERM (not just on deploys -- KEDA scaling this
    # replica down mid-backlog-drain does too, since it recomputes target
    # replica count off the shrinking visible-message count constantly) with
    # no grace handling by default, Python's default SIGTERM disposition
    # kills the process immediately. That can strike between a message
    # finishing its work (results already written) and the delete_message
    # call below that removes it from the queue -- orphaning a message that
    # will never be re-processed differently, just endlessly redelivered.
    # This handler stops pulling new work and gives in-flight messages up to
    # IPWORKER_SHUTDOWN_GRACE_SECONDS to finish and be deleted properly.
    shutdown_requested = threading.Event()

    def _handle_shutdown_signal(signum, _frame) -> None:
        worker_logger.info('ipworker received signal %s, draining in-flight work before exit', signum)
        shutdown_requested.set()

    signal.signal(signal.SIGTERM, _handle_shutdown_signal)
    signal.signal(signal.SIGINT, _handle_shutdown_signal)

    executor = ThreadPoolExecutor(max_workers=IPWORKER_CONCURRENCY, thread_name_prefix='ipwork')
    in_flight = {}  # future -> message
    shutdown_deadline: Optional[float] = None
    grace_exhausted = False
    try:
        while True:
            try:
                if shutdown_requested.is_set() and shutdown_deadline is None:
                    shutdown_deadline = time.monotonic() + IPWORKER_SHUTDOWN_GRACE_SECONDS
                    worker_logger.info(
                        'ipworker shutting down: draining %d in-flight message(s), grace=%ss',
                        len(in_flight), IPWORKER_SHUTDOWN_GRACE_SECONDS,
                    )

                # Only fetch as many new messages as there are free worker
                # slots -- keeps the pool saturated by refilling one slot at
                # a time as futures complete, instead of batch-waiting for a
                # full round of IPWORKER_CONCURRENCY messages to finish
                # before fetching more. Azure Queue's GET Messages caps a
                # single call at 32 regardless of IPWORKER_CONCURRENCY.
                # Once shutdown has been requested, stop claiming new work --
                # each claimed message costs a full visibility timeout if it
                # can't finish before the process exits.
                free_slots = 0 if shutdown_requested.is_set() else min(IPWORKER_CONCURRENCY - len(in_flight), 32)
                if free_slots > 0:
                    messages = list(queue_client.receive_messages(
                        messages_per_page=free_slots,
                        max_messages=free_slots,
                        visibility_timeout=IPWORKER_VISIBILITY_TIMEOUT_SECONDS,
                    ))
                    for message in messages:
                        future = executor.submit(_process_ipwork_message, message)
                        in_flight[future] = message

                if not in_flight:
                    if shutdown_requested.is_set():
                        break
                    time.sleep(poll_seconds)
                    continue

                if shutdown_deadline is not None and time.monotonic() >= shutdown_deadline:
                    worker_logger.warning(
                        'ipworker shutdown grace period elapsed with %d message(s) still in flight -- '
                        'exiting now, they will be redelivered after the visibility timeout',
                        len(in_flight),
                    )
                    grace_exhausted = True
                    break

                # Block for up to poll_seconds (or whatever's left of the
                # shutdown grace period, if shorter) waiting for at least one
                # in-flight future to finish (returns early as soon as one
                # does); on timeout, loop back around to check for more
                # free-slot capacity / new messages / the shutdown deadline.
                wait_timeout = poll_seconds
                if shutdown_deadline is not None:
                    wait_timeout = max(0.1, min(poll_seconds, shutdown_deadline - time.monotonic()))
                done, _pending = wait(list(in_flight.keys()), timeout=wait_timeout, return_when=FIRST_COMPLETED)
                for future in done:
                    message = in_flight.pop(future)
                    try:
                        outcome = future.result()
                    except Exception:
                        # Defensive backstop only -- _process_ipwork_message
                        # already catches everything it can attribute to a
                        # job_id internally. Same reasoning as that function's
                        # own except-block: don't force 'done' here either, or
                        # a thread that dies unexpectedly (escaping even that
                        # inner try/except) drops its message with zero retry.
                        worker_logger.exception('ipwork worker task raised unexpectedly')
                        outcome = 'error'
                    _log_ipwork_memory_sample(len(in_flight))
                    # Same lease_busy-vs-delete logic as before, just per
                    # completed future instead of per loop iteration; the
                    # actual delete_message call stays on the main thread
                    # (as does receive_messages above) so there's no
                    # question about QueueClient thread-safety for either.
                    # 'error' (a genuine processing failure, see
                    # _process_ipwork_message) always redelivers rather than
                    # being deleted -- the dequeue_count>IPWORKER_MAX_RETRIES
                    # check at the top of _process_ipwork_message is what
                    # eventually terminates it, not this check.
                    if outcome == 'lease_busy' and int(getattr(message, 'dequeue_count', 0) or 0) < IPWORK_LEASE_RETRY_LIMIT:
                        continue
                    if outcome == 'error':
                        continue
                    try:
                        queue_client.delete_message(message)
                    except Exception:
                        worker_logger.exception('Failed to delete ipwork queue message')
            except Exception:
                worker_logger.exception('ipwork queue polling iteration failed')
                if shutdown_requested.is_set():
                    break
                time.sleep(poll_seconds)
    finally:
        # On a clean exit (no shutdown requested, or shutdown finished
        # draining before the deadline) in_flight is already empty, so a
        # blocking shutdown is instant. On a grace-period timeout there are
        # still-running threads inside the executor -- don't block on them
        # (Azure's own SIGKILL is coming any moment now regardless, so
        # waiting here would just burn the remaining time doing nothing
        # useful) and force-exit immediately after so those stragglers can't
        # hang process termination past what Container Apps allows.
        executor.shutdown(wait=not grace_exhausted, cancel_futures=grace_exhausted)
    if grace_exhausted:
        os._exit(0)


def _remove_file_quietly(path: str) -> None:
    """Best-effort delete of a temp file; never raise from cleanup paths."""
    try:
        os.remove(path)
    except OSError:
        pass


# Guard other optional startup helpers to avoid import-time failures
for _fn in ('create_blob_containers', 'create_metadata_table', 'create_albums_table', 'create_album_token_index_table', 'create_face_table', 'create_person_table', 'create_merge_table', 'create_jobs_table', 'create_workbench_actions_table', 'create_image_names_table', 'create_hash_index_table', 'create_filename_owners_table'):
    if _fn in globals() and callable(globals().get(_fn)):
        try:
            globals().get(_fn)()
        except Exception:
            # Ignore errors during optional startup actions
            pass


# --- Blueprints -------------------------------------------------------
# Route handlers live in backend/routes/<group>.py, grouped by functional
# area (auth, upload, photos, people, albums, public, library, tools,
# admin, system) -- extracted 2026-09-15 from this file's original single
# 16k-line, ~115-endpoint surface as groundwork for eventually splitting
# some of these into their own container apps. Imported here (bottom of the
# file, after every shared helper/cache/table-client global above is fully
# defined) rather than at the top, since each module does `import app` and
# reaches back into this module's globals via app.<name> at request time --
# see routes/__init__.py and any routes/*.py file's module docstring for why
# that access pattern was chosen over `from app import name`.
from routes.auth import auth_bp
from routes.upload import upload_bp
from routes.photos import photos_bp
from routes.people import people_bp
from routes.albums import albums_bp
from routes.public import public_bp
from routes.library import library_bp
from routes.tools import tools_bp
from routes.admin import admin_bp
from routes.system import system_bp

# 2026-09-15: service splits, following the modularity work above. Each
# split group is registered on its own dedicated container app instead of
# here, so it can be sized/scaled independently of the core backend's own
# traffic pattern -- see backend-cpu-optimization-2026-09 memory's coupling
# map for why tools (workbench action-history logging) and upload were the
# two chosen: tools has zero shared-cache dependency, and upload's few
# touches (app._invalidate_metadata_scan_cache) only ever invalidate the
# calling process's OWN in-memory cache regardless of which role runs it --
# see _UserScanCache's docstring for why that per-process staleness bound
# was judged an already-accepted risk, not a new one, before this split.
# entrypoint.sh needs no change for either: any role other than
# 'worker'/'ipworker' already falls through to gunicorn, so a dedicated
# container just runs this same image with a different APP_ROLE.
_app_role = os.getenv('APP_ROLE', 'backend').strip().lower() or 'backend'
if _app_role == 'tools':
    app.register_blueprint(tools_bp)
elif _app_role == 'upload':
    app.register_blueprint(upload_bp)
elif _app_role == 'admin':
    # Isolated on its own container (2026-09-16, same reasoning as
    # tools/upload above): admin's own routes now only ever enqueue to the
    # clustering worker (see _enqueue_admin_repair_job) rather than running
    # full-account scans inline, so there's no shared-cache coupling
    # blocking the split -- and unlike tools/upload, isolating admin also
    # keeps its mutate-everything endpoints (recluster, dedupe, purge,
    # backfill) off the gallery-facing replica's attack surface even if
    # auth were ever bypassed there.
    app.register_blueprint(admin_bp)
elif _app_role == 'extras':
    # 2026-09-17: people/library/public split off the core 'backend' role so
    # backend itself can shrink to a 0.5vCPU/1Gi tier sized for just the
    # everyday gallery loop (auth+photos+albums+system) -- see
    # backend-cpu-optimization-2026-09 memory. These three carry the heavier
    # secondary features (People page's own per-partition face/embedding-
    # index scans, library export/clean orchestration, and public share-link
    # media streaming) that day-to-day browsing doesn't touch. Bundled
    # together on one app rather than three, since none of them are hot
    # enough individually to justify their own bicep/scaling footprint --
    # revisit only if one of them needs independent scaling from the others.
    for _bp in (people_bp, library_bp, public_bp):
        app.register_blueprint(_bp)
else:
    # system_bp stays here rather than moving to 'extras' -- it's negligible
    # weight (a handful of point lookups, including /health) and Container
    # Apps' default TCP probe doesn't need it, but losing a friendly
    # same-origin /health on backend specifically wasn't worth it for zero
    # real memory/CPU savings.
    for _bp in (auth_bp, photos_bp, albums_bp, system_bp):
        app.register_blueprint(_bp)
