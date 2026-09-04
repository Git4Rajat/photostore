import base64
import hashlib
import hmac
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
from azure.storage.blob import BlobSasPermissions, BlobServiceClient, UserDelegationKey, generate_blob_sas
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
    invalidate_user_vector_index_cache,
    delete_user_vector_index_data,
    touch_user_vector_index_state,
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
PEOPLE_TABLE = os.getenv('PEOPLE_TABLE', 'photopeople')
FACE_TABLE = os.getenv('FACE_TABLE', 'photofaces')
MERGE_TABLE = os.getenv('MERGE_TABLE', 'personmerges')
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
# 'sas' hands the browser day-stable read SAS URLs pointing straight at blob
# storage so media bytes never stream through this container; 'proxy' serves
# every byte through the backend. 'sas' silently degrades to proxy URLs when
# minting is impossible (no AAD credential, e.g. Azurite/local dev).
MEDIA_URL_MODE = os.getenv('MEDIA_URL_MODE', 'sas').strip().lower()
BLOB_VECTOR_INDEX_CONTAINER = os.getenv('BLOB_VECTOR_INDEX_CONTAINER', 'vector-index').strip()
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
IPWORK_STEPS = ('thumbnail', 'exif', 'ocr', 'face', 'ai_vision', 'map_detection')
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
    global image_names_table_client
    global hash_index_table_client, filename_owners_table_client
    global config_table_client
    global users_table_client, libraries_table_client, memberships_table_client
    global invites_table_client, audit_table_client, clean_requests_table_client, library_store
    global clustering_queue_client, queue_service_client, ipwork_queue_client

    account_name = STORAGE_ACCOUNT_NAME or os.getenv('AZURE_STORAGE_ACCOUNT_NAME')

    # Prefer local/Azurite connection string when provided.
    if STORAGE_CONNECTION_STRING:
        tbl_svc = TableServiceClient.from_connection_string(STORAGE_CONNECTION_STRING)
        metadata_table_client_local = tbl_svc.get_table_client(METADATA_TABLE)
        albums_table_client_local = tbl_svc.get_table_client(ALBUMS_TABLE)
        face_table_client_local = tbl_svc.get_table_client(FACE_TABLE)
        person_table_client_local = tbl_svc.get_table_client(PEOPLE_TABLE)
        merge_table_client_local = tbl_svc.get_table_client(MERGE_TABLE)
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
        ipwork_queue_client_local = queue_service_client_local.get_queue_client(IPWORKER_QUEUE_NAME)

        # Table clients
        tbl_svc = TableServiceClient(endpoint=f'https://{account_name}.table.core.windows.net', credential=credential)
        metadata_table_client_local = tbl_svc.get_table_client(METADATA_TABLE)
        albums_table_client_local = tbl_svc.get_table_client(ALBUMS_TABLE)
        face_table_client_local = tbl_svc.get_table_client(FACE_TABLE)
        person_table_client_local = tbl_svc.get_table_client(PEOPLE_TABLE)
        merge_table_client_local = tbl_svc.get_table_client(MERGE_TABLE)
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
    # Wrap so every write (from anywhere in app.py or storage_utils.py) auto-invalidates
    # the people/faces scan cache -- see _InvalidatingTableClient.
    face_table_client = _InvalidatingTableClient(face_table_client_local, _invalidate_people_scan_cache)
    person_table_client = _InvalidatingTableClient(person_table_client_local, _invalidate_people_scan_cache)
    merge_table_client = merge_table_client_local
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
    for container_name in (BLOB_IMAGE_CONTAINER, BLOB_THUMBNAIL_CONTAINER, BLOB_VECTOR_INDEX_CONTAINER, BLOB_EXPORTS_CONTAINER):
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
            return None, (jsonify({'error': f'Invalid or expired session: {exc}'}), 401)
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

# _has_active_clustering_job's query_entities("PartitionKey eq 'jobs'") is an
# unfiltered scan of the SAME 'jobs' partition _active_library_cleanup_job
# was found scanning on every upload request (213k+ rows and growing,
# confirmed live to cost 17-33s/call -- see that fix's own comments). This
# one is called far less often (gated behind _clustering_maintenance_due's
# 30-minute cooldown, not every request) but hits the identical partition,
# and that cooldown is a read-then-write race with no etag/CAS (see its own
# docstring) -- every concurrent upload request in flight at the moment the
# cooldown lapses can independently pay the full scan. Cached with a
# constant key (not per-user): the query itself isn't scoped by user_id --
# it fetches the whole partition and filters client-side -- so one scan
# genuinely serves every user's check within the TTL window, the same way
# _face_summary_scan_cache serves every caller of _load_user_face_summary_by_id.
_JOBS_PARTITION_SCAN_CACHE_KEY = '__all_jobs__'
_jobs_partition_scan_cache = _UserScanCache(PEOPLE_SCAN_CACHE_TTL_SECONDS)


def _has_active_clustering_job(user_id: str) -> Optional[str]:
    if metadata_table_client is None:
        return None
    try:
        rows = _jobs_partition_scan_cache.get(
            _JOBS_PARTITION_SCAN_CACHE_KEY,
            lambda: list(metadata_table_client.query_entities("PartitionKey eq 'jobs'")),
        )
    except Exception:
        return None
    stale_before = datetime.now(timezone.utc) - timedelta(minutes=CLUSTERING_ACTIVE_JOB_STALE_MINUTES)
    for row in rows:
        if str(row.get('userId') or '') != user_id:
            continue
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


def _mark_clustering_job_rerun_requested(job_id: str) -> None:
    """Flag an in-flight clustering job so the worker fires exactly one
    follow-up job once it finishes, instead of the caller enqueueing its own.

    Used to coalesce a burst of per-photo triggers (e.g. every photo in a big
    upload finishing face detection) into a single clustering pass — and a
    single completion notification — rather than one job per photo.
    """
    if metadata_table_client is None:
        return
    try:
        metadata_table_client.upsert_entity({
            'PartitionKey': 'jobs',
            'RowKey': _job_row_key(job_id),
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
                _mark_clustering_job_rerun_requested(existing_job_id)
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
METADATA_SCAN_CACHE_TTL_SECONDS = float(os.getenv('METADATA_SCAN_CACHE_TTL_SECONDS', '20'))
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


def _invalidate_metadata_scan_cache(user_id: str) -> None:
    _metadata_scan_cache.invalidate(user_id)
    _photo_default_sort_cache.invalidate(user_id)


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
    try:
        rows = list(person_table_client.query_entities(f"PartitionKey eq '{_escape_odata(user_id)}'"))
    except Exception:
        rows = []

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
            if person_id:
                created_person_ids.add(person_id)
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
        return {'error': f'clustering unavailable: {exc}'}

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


def _public_photo_urls(token: str, filename: str, blob_name: Optional[str] = None) -> Dict[str, str]:
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    preview_required = ext in BROWSER_UNVIEWABLE_EXTENSIONS
    preview_url = f'/public/photos/{token}/preview/{filename}' if preview_required else ''
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


@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({
        'status': 'healthy',
        'service': 'photo-store-api',
        'storage_account': account_name,
        'uses_managed_identity': credential is not None,
    })


@app.route('/geocode/reverse', methods=['GET'])
@app.route('/api/geocode/reverse', methods=['GET'])
def geocode_reverse():
    """Server-side reverse geocode, used by the browser AI pipeline's
    map_detection step. Proxying through the backend (instead of the browser
    calling a third-party geocoder directly) avoids depending on that
    service's CORS/availability from an arbitrary browser origin -- the same
    maps_utils.reverse_geocode() call already runs cheaply (1-116ms) from the
    ipworker path."""
    _user_id, error = _require_user_id()
    if error:
        return error
    latitude = (request.args.get('lat') or '').strip()
    longitude = (request.args.get('lon') or '').strip()
    if not latitude or not longitude:
        return jsonify({'error': 'lat and lon are required'}), 400
    import maps_utils
    try:
        result = maps_utils.reverse_geocode(latitude, longitude)
    except Exception:
        worker_logger.exception('geocode_reverse failed')
        result = {}
    return jsonify(result or {})


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
    email_in = library_utils.normalize_email(data.get('email'))
    password = str(data.get('password', '') or '')
    if not email_in or not password:
        return jsonify({'error': 'Email and password are required.'}), 400
    # Brute-force protection is scoped per email so one targeted account can't
    # lock everyone else out of a shared deployment.
    throttle_row = f'login-throttle:{email_in}'
    if not password_auth.login_attempt_allowed(config_table_client, throttle_row):
        return jsonify({'error': 'Too many attempts. Please wait and try again.'}), 429
    account = library_store.get_user_by_email(email_in) if library_store else None
    stored_hash = str((account or {}).get('passwordHash') or '')
    if not account or not stored_hash or not password_auth.verify_password(password, stored_hash):
        password_auth.record_login_failure(config_table_client, row_key=throttle_row)
        return jsonify({'error': 'Incorrect email or password.'}), 401
    password_auth.record_login_success(config_table_client, throttle_row)
    uid = str(account.get('RowKey'))
    email = str(account.get('email') or email_in)
    token = _issue_session_for(uid, email=email, mode='password')
    return jsonify({'token': token, 'email': email, 'expiresIn': SESSION_TTL_SECONDS})


@app.route('/auth/exchange', methods=['POST'])
@app.route('/api/auth/exchange', methods=['POST'])
def auth_exchange():
    """Entra mode: exchange a validated Microsoft access token for a Photostore
    session token that carries the active library + token version. We can't stamp
    those claims into Microsoft's token, so both modes converge on a token we
    sign. Called once by the SPA after MSAL sign-in."""
    if AUTH_MODE != 'entra':
        return jsonify({'error': 'Token exchange is only available in Entra mode.'}), 400
    auth_header = str(request.headers.get('Authorization', '') or '')
    if not auth_header.lower().startswith('bearer '):
        return jsonify({'error': 'Authorization token is required.'}), 401
    ms_token = auth_header.split(' ', 1)[1].strip()
    try:
        payload = validate_entra_bearer_token(
            ms_token, AZURE_AD_TENANT_ID, AZURE_AD_CLIENT_ID, AZURE_AD_API_AUDIENCE,
        )
    except Exception as exc:
        return jsonify({'error': f'Invalid Microsoft token: {exc}'}), 401
    user_id = str(payload.get('oid') or payload.get('sub') or payload.get('preferred_username') or '').strip()
    if not user_id:
        return jsonify({'error': 'Token does not contain a usable user identifier claim.'}), 401
    email = str(payload.get('preferred_username') or payload.get('email') or payload.get('upn') or '').strip()
    _ensure_account_bootstrapped(user_id, email=email)
    token = _issue_session_for(user_id, email=email, mode='entra')
    return jsonify({'token': token, 'email': email, 'expiresIn': SESSION_TTL_SECONDS})


@app.route('/auth/change-password', methods=['POST'])
@app.route('/api/auth/change-password', methods=['POST'])
def auth_change_password():
    guard = _password_mode_guard()
    if guard:
        return guard
    account_id, _library_id, error = _require_library_context(require_auth=True)
    if error:
        return error
    data = request.get_json(silent=True) or {}
    current = str(data.get('currentPassword', '') or '')
    new_password = str(data.get('newPassword', '') or '')
    if len(new_password) < 8:
        return jsonify({'error': 'New password must be at least 8 characters.'}), 400
    account = library_store.get_user(account_id) if library_store else None
    stored_hash = str((account or {}).get('passwordHash') or '')
    if not account or not stored_hash or not password_auth.verify_password(current, stored_hash):
        return jsonify({'error': 'Current password is incorrect.'}), 401
    library_store.set_user_password(account_id, password_auth.hash_password(new_password))
    # Session-kill: invalidate every outstanding token, then hand this session a
    # fresh one so the user who just changed their password stays signed in here.
    library_store.bump_token_version(account_id)
    token = _issue_session_for(account_id, email=str(account.get('email') or ''), mode='password')
    return jsonify({'status': 'ok', 'token': token, 'expiresIn': SESSION_TTL_SECONDS})


@app.route('/auth/forgot', methods=['POST'])
@app.route('/api/auth/forgot', methods=['POST'])
def auth_forgot():
    guard = _password_mode_guard()
    if guard:
        return guard
    # Always return success to avoid revealing whether an account exists for the
    # supplied address (no account enumeration).
    data = request.get_json(silent=True) or {}
    email_in = library_utils.normalize_email(data.get('email'))
    generic = jsonify({'status': 'ok'})
    if not email_utils.is_configured() or not email_in or library_store is None:
        return generic
    account = library_store.get_user_by_email(email_in)
    if not account:
        return generic
    account_id = str(account.get('RowKey'))
    # Throttle the unauthenticated send path (per account) so it can't be used to
    # email-bomb a user or burn ACS email quota. Still return the same 200.
    if not library_store.reset_email_allowed(account_id):
        return generic
    try:
        raw_token = library_store.create_reset_token(account_id, ttl_seconds=3600)
        base = (PUBLIC_APP_BASE_URL or '').rstrip('/')
        reset_url = f'{base}/reset-password?token={raw_token}'
        email_utils.send_password_reset_email(str(account.get('email') or email_in), reset_url)
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
    user_id = library_store.consume_reset_token(token) if library_store else None
    if not user_id:
        return jsonify({'error': 'This reset link is invalid or has expired. Please request a new one.'}), 400
    library_store.set_user_password(user_id, password_auth.hash_password(new_password))
    library_store.bump_token_version(user_id)  # session-kill any existing tokens
    return jsonify({'status': 'ok'})


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


@app.route('/api/library/mine', methods=['GET'])
def library_mine():
    """Libraries the caller belongs to (for the switcher) + the active one."""
    account_id, active_library_id, error = _require_library_context(require_auth=True)
    if error:
        return error
    if library_store is None:
        return jsonify({'activeLibraryId': active_library_id, 'libraries': []})
    return jsonify({
        'activeLibraryId': active_library_id,
        'libraries': library_store.list_user_libraries(account_id),
        'maxMembers': library_utils.MAX_LIBRARY_MEMBERS,
    })


@app.route('/api/library/members', methods=['GET'])
def library_members():
    account_id, library_id, error = _require_library_context(require_auth=True)
    if error:
        return error
    if library_store is None:
        return jsonify({'members': []})
    meta = library_store.get_library(library_id) or {}
    is_owner = library_store.is_owner(account_id, library_id)
    body = {
        'libraryId': library_id,
        'name': str(meta.get('name') or ''),
        'ownerUserId': str(meta.get('ownerUserId') or ''),
        'isOwner': is_owner,
        'members': _member_view(library_id, account_id),
        'maxMembers': library_utils.MAX_LIBRARY_MEMBERS,
    }
    if is_owner:
        # Only the owner (who controls membership) sees outstanding invites.
        body['pendingInvites'] = [
            {
                'inviteId': str(inv.get('RowKey') or ''),
                'email': email_utils.masked_recipient(inv.get('emailNorm')),
                'targetType': str(inv.get('targetType') or ''),
                'expiresAt': int(inv.get('expiresAt', 0) or 0),
            }
            for inv in library_store.pending_invites(library_id)
        ]
    return jsonify(body)


@app.route('/api/library/invite', methods=['POST'])
def library_invite():
    account_id, library_id, error = _require_owner_context()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    email = library_utils.normalize_email(data.get('email'))
    target_type = 'fresh' if str(data.get('targetType') or 'join') == 'fresh' else 'join'
    if not email or '@' not in email:
        return jsonify({'error': 'A valid email address is required.'}), 400
    # Invites are delivered only by email; without it the invitee would never get
    # the link, so refuse up front rather than reserving a seat for a dead invite.
    if not email_utils.is_configured():
        return jsonify({'error': 'Email delivery is not configured, so invitations cannot be sent.'}), 503
    if target_type == 'join' and not library_store.has_capacity(library_id):
        return jsonify({'error': f'This library is full (max {library_utils.MAX_LIBRARY_MEMBERS}).'}), 409
    if target_type == 'join' and library_store.is_member_email(email, library_id):
        return jsonify({'error': 'That person is already a member of this library.'}), 409
    if library_store.find_pending_invite_for_email(library_id, email):
        return jsonify({
            'error': 'An invitation was already sent to that email and is still pending. '
                     'Revoke it in the list below if you want to send a new one.',
        }), 409
    if not library_store.invite_send_allowed(library_id):
        return jsonify({'error': 'Too many invites sent recently. Please wait and try again.'}), 429

    raw = library_store.create_invite(
        library_id=library_id, email=email, target_type=target_type, invited_by=account_id,
    )
    meta = library_store.get_library(library_id) or {}
    inviter_email = str((library_store.get_user(account_id) or {}).get('email') or '')
    base = (PUBLIC_APP_BASE_URL or '').rstrip('/')
    invite_url = f'{base}/accept-invite?token={raw}'
    try:
        email_utils.send_invite_email(
            email, invite_url,
            library_name=str(meta.get('name') or '') if target_type == 'join' else '',
            inviter=inviter_email,
        )
    except Exception as exc:
        app.logger.warning('Invite email failed: %s', exc)
        # The link never went out; free the reserved seat rather than leave a
        # dangling pending invite the owner believes was delivered.
        invite = library_store.find_pending_invite_for_email(library_id, email)
        if invite:
            library_store.revoke_invite(library_id, str(invite.get('RowKey') or ''))
        return jsonify({
            'error': 'The invitation email could not be sent. Please try again.',
            'detail': str(exc),
        }), 502
    library_store.audit(library_id, actor=account_id, action=f'invite:{target_type}', target=email)
    # Uniform response: never reveal whether the email already had an account.
    return jsonify({'status': 'sent'})


@app.route('/api/library/invite/revoke', methods=['POST'])
def library_invite_revoke():
    account_id, library_id, error = _require_owner_context()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    invite_id = str(data.get('inviteId', '') or '').strip()
    if not invite_id:
        return jsonify({'error': 'inviteId is required.'}), 400
    if not library_store.revoke_invite(library_id, invite_id):
        return jsonify({'error': 'That invitation is no longer pending.'}), 404
    library_store.audit(library_id, actor=account_id, action='invite-revoked', target=invite_id)
    return jsonify({'status': 'ok'})


@app.route('/api/library/invite/info', methods=['GET'])
def library_invite_info():
    """Public: minimal, non-sensitive details so the accept page can render.

    Reveals only what the recipient already knows (they hold the emailed link):
    the target library name and whether they must set a password (new account).
    """
    token = str(request.args.get('token', '') or '')
    invite = library_store.get_invite_by_token(token) if library_store else None
    if not invite:
        return jsonify({'valid': False}), 404
    library_id = str(invite.get('PartitionKey') or '')
    target_type = str(invite.get('targetType') or 'join')
    meta = library_store.get_library(library_id) or {}
    email = str(invite.get('emailNorm') or '')
    needs_password = not library_store.user_exists_for_email(email) and AUTH_MODE == 'password'
    return jsonify({
        'valid': True,
        'email': email,
        'targetType': target_type,
        'libraryName': str(meta.get('name') or '') if target_type == 'join' else '',
        'accountExists': library_store.user_exists_for_email(email),
        'needsPassword': needs_password,
    })


@app.route('/api/library/invite/accept', methods=['POST'])
def library_invite_accept():
    data = request.get_json(silent=True) or {}
    token = str(data.get('token', '') or '')
    invite = library_store.get_invite_by_token(token) if library_store else None
    if not invite:
        return jsonify({'error': 'This invitation is invalid or has expired.'}), 400
    email = library_utils.normalize_email(invite.get('emailNorm'))
    target_type = str(invite.get('targetType') or 'join')
    library_id = str(invite.get('PartitionKey') or '')

    existing = library_store.get_user_by_email(email)
    if existing is not None:
        # Existing account: require the caller to be signed in AS that account
        # (email binding + explicit consent click). Works in both auth modes.
        account_id, _active, auth_error = _require_library_context(require_auth=True)
        if auth_error:
            return jsonify({'error': f'Please sign in as {email} to accept this invitation.'}), 401
        if library_utils.normalize_email((library_store.get_user(account_id) or {}).get('email')) != email:
            return jsonify({'error': 'This invitation is for a different account.'}), 403
        new_account = False
    else:
        # New account: only self-service in password mode. In Entra mode the
        # invitee must first sign in with Microsoft (which creates the account),
        # then the existing-account branch above applies.
        if AUTH_MODE != 'password':
            return jsonify({'error': 'Please sign in first, then open this invitation link again.'}), 401
        password = str(data.get('password', '') or '')
        if len(password) < 8:
            return jsonify({'error': 'Please choose a password of at least 8 characters.'}), 400
        account_id = library_utils.new_user_id()
        library_store.create_user(email=email, password_hash=password_auth.hash_password(password), user_id=account_id)
        library_store.ensure_personal_library(account_id, name=email)
        new_account = True

    if target_type == 'join':
        if not library_store.is_member(account_id, library_id):
            # This invitee's own reserved (pending) seat is about to convert into
            # a membership, so gate on accepted members only — using has_capacity
            # here would double-count the pending invite and wrongly reject the
            # member that fills the final slot.
            if library_store.member_count(library_id) >= library_utils.MAX_LIBRARY_MEMBERS:
                return jsonify({'error': f'This library is now full (max {library_utils.MAX_LIBRARY_MEMBERS}).'}), 409
            library_store.add_membership(account_id, library_id, is_owner=False)
    library_store.mark_invite_accepted(invite)
    library_store.audit(library_id, actor=account_id, action='invite-accepted', target=email)

    active_library = library_id if target_type == 'join' else account_id
    token_out = _issue_session_for(account_id, library_id=active_library, email=email, mode=AUTH_MODE)
    return jsonify({
        'status': 'accepted',
        'token': token_out,
        'activeLibraryId': active_library,
        'newAccount': new_account,
        'expiresIn': SESSION_TTL_SECONDS,
    })


@app.route('/api/library/switch', methods=['POST'])
def library_switch():
    account_id, _active, error = _require_library_context(require_auth=True)
    if error:
        return error
    data = request.get_json(silent=True) or {}
    target = str(data.get('libraryId', '') or '').strip()
    if not target or not library_store.is_member(account_id, target):
        return jsonify({'error': 'You are not a member of that library.'}), 403
    email = str((library_store.get_user(account_id) or {}).get('email') or '')
    token = _issue_session_for(account_id, library_id=target, email=email, mode=AUTH_MODE)
    return jsonify({'token': token, 'activeLibraryId': target, 'expiresIn': SESSION_TTL_SECONDS})


@app.route('/api/library/rename', methods=['POST'])
def library_rename():
    account_id, library_id, error = _require_owner_context()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    name = str(data.get('name', '') or '').strip()[:100]
    library_store.rename_library(library_id, name)
    library_store.audit(library_id, actor=account_id, action='rename', target=name)
    return jsonify({'status': 'ok', 'name': name})


@app.route('/api/library/members/remove', methods=['POST'])
def library_remove_member():
    account_id, library_id, error = _require_owner_context()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    target = str(data.get('userId', '') or '').strip()
    if not target:
        return jsonify({'error': 'userId is required.'}), 400
    if target == account_id:
        return jsonify({'error': "You can't remove yourself; delete the library instead."}), 400
    if not library_store.is_member(target, library_id):
        return jsonify({'error': 'That person is not a member of this library.'}), 404
    library_store.remove_membership(target, library_id)
    # No token-version bump needed: the per-request membership check makes the
    # removal take effect immediately for this library, without disturbing the
    # removed user's access to their *own* library.
    library_store.audit(library_id, actor=account_id, action='remove-member', target=target)
    return jsonify({'status': 'ok'})


@app.route('/api/library/leave', methods=['POST'])
def library_leave():
    account_id, _active, error = _require_library_context(require_auth=True)
    if error:
        return error
    data = request.get_json(silent=True) or {}
    target_library = str(data.get('libraryId', '') or '').strip()
    if not target_library:
        return jsonify({'error': 'libraryId is required.'}), 400
    if target_library == account_id or library_store.library_owner_id(target_library) == account_id:
        return jsonify({'error': "You can't leave a library you own."}), 400
    if not library_store.is_member(account_id, target_library):
        return jsonify({'error': 'You are not a member of that library.'}), 404
    library_store.remove_membership(account_id, target_library)
    library_store.audit(target_library, actor=account_id, action='leave', target=account_id)
    # Drop the caller back into their own library.
    email = str((library_store.get_user(account_id) or {}).get('email') or '')
    token = _issue_session_for(account_id, library_id=account_id, email=email, mode=AUTH_MODE)
    return jsonify({'status': 'ok', 'token': token, 'activeLibraryId': account_id})


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


@app.route('/api/library', methods=['DELETE'])
def library_delete():
    account_id, library_id, error = _require_owner_context()
    if error:
        return error
    # The primary owner is the deployment root (recreated from the deploy seed),
    # so it can't self-delete; invited users may delete their own library.
    if library_id == password_auth.OWNER_USER_ID:
        return jsonify({'error': 'The primary owner account cannot be deleted.'}), 400
    others = [m for m in library_store.list_library_members(library_id) if m['userId'] != account_id]
    if others:
        return jsonify({'error': 'Remove all other members before deleting this library.'}), 409

    library_store.audit(library_id, actor=account_id, action='delete-library', target=library_id)
    library_store.delete_all_invites(library_id)
    library_store.delete_all_memberships(library_id)
    library_store.delete_library(library_id)
    # Account deletion. The resolver rejects tokens whose account row is gone
    # (rather than re-creating it), so the caller's active session stops working
    # on its next request.
    library_store.delete_user(account_id)
    _purge_library_data(library_id)
    try:
        invalidate_user_vector_index_cache(library_id)
    except Exception:
        pass
    return jsonify({'status': 'ok'})


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

    Matches both current rows (explicit ``libraryId``) and legacy rows by
    ``jobId`` prefix to support libraries created before cleanup-state tracking
    was added.
    """
    if metadata_table_client is None or not library_id:
        return None
    safe_library_id = str(library_id)
    try:
        rows = list(metadata_table_client.query_entities("PartitionKey eq 'jobs'"))
    except Exception:
        return None
    for row in rows:
        if str(row.get('jobType') or '') != 'library_clean':
            continue
        if str(row.get('status') or '').lower() not in {'queued', 'running'}:
            continue
        row_library_id = str(row.get('libraryId') or '')
        row_job_id = str(row.get('jobId') or '')
        if row_library_id == safe_library_id or row_job_id.startswith(f'libclean:{safe_library_id}:'):
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
    if library_store is None or metadata_table_client is None:
        return False
    job_id = str((meta or {}).get('lastCleanupJobId') or '')
    if not job_id:
        return False
    try:
        row = metadata_table_client.get_entity(partition_key='jobs', row_key=_job_row_key(job_id))
    except Exception:
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
        metadata_rows = list(metadata_table_client.query_entities(f"PartitionKey eq '{pk}'")) if metadata_table_client else []
    except Exception:
        metadata_rows = []

    blobs_deleted = 0
    blob_errors = 0
    for row in metadata_rows:
        filename = str(row.get('RowKey') or '')
        if not filename:
            continue
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
        if _is_filename_shared(filename, library_id):
            # Another library still references this content-addressed blob.
            continue
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
        if errors:
            blob_errors += len(errors)
        else:
            blobs_deleted += 1

    for client in (metadata_table_client, face_table_client, person_table_client,
                   albums_table_client, merge_table_client, image_names_table_client,
                   hash_index_table_client):
        if client is None:
            continue
        try:
            # select=[keys only]: merge_table_client's partition can hold tens of
            # thousands of face_membership_snapshot_chunk rows carrying a ~24KB
            # `payload` blob each (see _create_people_repair_snapshot). Fetching
            # full rows just to read PartitionKey/RowKey materializes all of that
            # into memory at once and OOMs the worker (same bug class fixed for
            # list_merges).
            for row in client.query_entities(f"PartitionKey eq '{pk}'", select=['PartitionKey', 'RowKey']):
                try:
                    client.delete_entity(partition_key=row['PartitionKey'], row_key=row['RowKey'])
                except Exception:
                    pass
        except Exception as exc:
            app.logger.warning('Library clean skipped a table for %s: %s', library_id, exc)

    try:
        invalidate_image_names_cache(library_id)
    except Exception:
        pass
    _delete_cover_blobs_for_library(library_id)
    delete_user_vector_index_data(library_id)
    _invalidate_metadata_scan_cache(library_id)

    return {'photosDeleted': len(metadata_rows), 'blobsDeleted': blobs_deleted, 'blobErrors': blob_errors}


def _enqueue_library_clean_job(library_id: str, actor_user_id: str, request_id: str) -> Dict[str, str]:
    job_id = f"libclean:{library_id}:{uuid.uuid4().hex}"
    if clustering_queue_client is None:
        # No queue configured (e.g. local dev) — run inline rather than silently
        # dropping a destructive action the caller believes is in progress.
        app.logger.warning('Clustering queue client is unavailable; running library clean %s inline', job_id)
        try:
            library_store.set_cleanup_in_progress(library_id, job_id)
            summary = _execute_library_clean(library_id)
            library_store.set_cleanup_completed(library_id, summary.get('photosDeleted', 0), summary.get('blobsDeleted', 0))
            _upsert_job_status(job_id, actor_user_id, 'library_clean', 'done', result=summary, libraryId=library_id)
            _notify_cleanup_completed(library_id, summary)
            return {'status': 'done', 'jobId': job_id}
        except Exception as exc:
            app.logger.exception('Inline library clean failed for %s', library_id)
            library_store.set_cleanup_failed(library_id, str(exc))
            _upsert_job_status(job_id, actor_user_id, 'library_clean', 'failed', error=str(exc), libraryId=library_id)
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
        clustering_queue_client.send_message(json.dumps(message, separators=(',', ':')))
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
    if job_id and metadata_table_client is not None:
        try:
            prior_row = metadata_table_client.get_entity(partition_key='jobs', row_key=_job_row_key(job_id))
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
    parts_result = []
    total_size = 0
    for part in parts_summary:
        idx = part['partIndex']
        blob_name = _library_export_part_blob_name(library_id, idx)
        download_name = (
            f"{library_name or library_id}-export-part-{idx}.zip" if multi_part
            else f"{library_name or library_id}-export.zip"
        )
        download_url, expires_at = _create_stable_read_sas_url(BLOB_EXPORTS_CONTAINER, blob_name, download_filename=download_name)
        total_size += int(part.get('sizeBytes') or 0)
        parts_result.append({
            'partIndex': idx,
            'downloadUrl': download_url,
            'expiresAt': expires_at,
            'photosIncluded': part['photosIncluded'],
            'sizeBytes': part['sizeBytes'],
        })

    return {
        'parts': parts_result,
        'photosIncluded': written_count,
        'photosSkipped': skipped_count,
        'sizeBytes': total_size,
    }


def _has_active_library_download_job(library_id: str) -> Optional[str]:
    """Return the jobId of an in-flight 'library_download' job for this
    library, if any -- de-dupes button-mash/multi-tab clicks. Reuses the
    already-cached whole-'jobs'-partition scan (_jobs_partition_scan_cache)
    rather than issuing a fresh query_entities("PartitionKey eq 'jobs'") --
    see _has_active_clustering_job, whose identical scan was the target of
    three recent fixes (fa03bc9, e1114b7, 4b325c4) for exactly this class of
    unscoped, per-request 'jobs'-partition read."""
    if metadata_table_client is None:
        return None
    try:
        rows = _jobs_partition_scan_cache.get(
            _JOBS_PARTITION_SCAN_CACHE_KEY,
            lambda: list(metadata_table_client.query_entities("PartitionKey eq 'jobs'")),
        )
    except Exception:
        return None
    stale_before = datetime.now(timezone.utc) - timedelta(minutes=CLUSTERING_ACTIVE_JOB_STALE_MINUTES)
    for row in rows:
        if str(row.get('jobType') or '') != 'library_download':
            continue
        if str(row.get('libraryId') or '') != library_id:
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
    if clustering_queue_client is None:
        # No queue configured (e.g. local dev) — run inline rather than
        # silently dropping the request.
        app.logger.warning('Clustering queue client is unavailable; running library download %s inline', job_id)
        try:
            summary = _execute_library_download(library_id, library_name, job_id=job_id, user_id=actor_user_id)
            _upsert_job_status(job_id, actor_user_id, 'library_download', 'done', result=summary, libraryId=library_id)
            return {'status': 'done', 'jobId': job_id}
        except Exception as exc:
            app.logger.exception('Inline library download failed for %s', library_id)
            _upsert_job_status(job_id, actor_user_id, 'library_download', 'failed', error=str(exc), libraryId=library_id)
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
        clustering_queue_client.send_message(json.dumps(message, separators=(',', ':')))
    except Exception:
        app.logger.exception('Failed to enqueue library download job %s', job_id)
        _upsert_job_status(job_id, actor_user_id, 'library_download', 'failed', error='Failed to queue download job', libraryId=library_id)
        return {'status': 'failed', 'jobId': job_id}
    _upsert_job_status(job_id, actor_user_id, 'library_download', 'queued', libraryId=library_id)
    # _has_active_library_download_job reads the same cached partition scan
    # jobs_status() uses; without invalidating here, a second click within the
    # cache TTL (the exact case this de-dupe exists for) would still see the
    # pre-enqueue snapshot and queue a duplicate export job.
    _jobs_partition_scan_cache.invalidate(_JOBS_PARTITION_SCAN_CACHE_KEY)
    return {'status': 'queued', 'jobId': job_id}


@app.route('/api/library/download/request', methods=['POST'])
def library_download_request():
    account_id, library_id, error = _require_owner_context()
    if error:
        return error
    existing_job_id = _has_active_library_download_job(library_id)
    if existing_job_id:
        return jsonify(_clustering_queue_response({'status': 'queued', 'jobId': existing_job_id}, activeLibraryId=library_id))
    meta = library_store.get_library(library_id) or {}
    library_name = str(meta.get('name') or '')
    queued = _enqueue_library_download_job(library_id, account_id, library_name)
    return jsonify(_clustering_queue_response(queued, activeLibraryId=library_id))


@app.route('/api/library/download/status', methods=['GET'])
def library_download_status():
    _account_id, _library_id, error = _require_library_context(require_auth=True)
    if error:
        return error
    job_id = str(request.args.get('jobId', '') or '')
    if not job_id or metadata_table_client is None:
        return jsonify({'status': 'unknown'})
    try:
        row = metadata_table_client.get_entity(partition_key='jobs', row_key=_job_row_key(job_id))
    except Exception:
        return jsonify({'status': 'unknown'})
    result = row.get('result')
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except Exception:
            pass
    return jsonify({'status': str(row.get('status') or 'unknown'), 'result': result, 'error': row.get('error')})


@app.route('/api/library/clean/request', methods=['POST'])
def library_clean_request():
    account_id, library_id, error = _require_owner_context()
    if error:
        return error
    if not email_utils.is_configured():
        return jsonify({'error': 'Email delivery is not configured, so this action is unavailable.'}), 503
    if library_store.get_active_clean_request(library_id):
        return jsonify({'error': 'A cleanup confirmation is already pending for this library.'}), 409

    data = request.get_json(silent=True) or {}
    if AUTH_MODE == 'password':
        password = str(data.get('password', '') or '')
        account = library_store.get_user(account_id) or {}
        stored_hash = str(account.get('passwordHash') or '')
        if not stored_hash or not password_auth.verify_password(password, stored_hash):
            return jsonify({'error': 'Incorrect password.'}), 401

    if not library_store.clean_request_send_allowed(library_id):
        return jsonify({'error': 'Too many attempts recently. Please wait and try again.'}), 429

    other_member_ids = [m['userId'] for m in library_store.list_library_members(library_id) if m['userId'] != account_id]
    required_user_ids = [account_id]
    if other_member_ids:
        required_user_ids.append(random.choice(other_member_ids))

    request_id, tokens_by_user = library_store.create_clean_request(
        library_id=library_id, requested_by=account_id, required_user_ids=required_user_ids,
        ttl_seconds=library_utils.CLEAN_REQUEST_TTL_SECONDS,
    )
    meta = library_store.get_library(library_id) or {}
    requester_email = str((library_store.get_user(account_id) or {}).get('email') or '')
    base = (PUBLIC_APP_BASE_URL or '').rstrip('/')
    sent_to = []
    send_errors = []
    for user_id, raw in tokens_by_user.items():
        recipient_email = str((library_store.get_user(user_id) or {}).get('email') or '')
        if not recipient_email:
            continue
        confirm_url = f'{base}/confirm-library-clean?token={raw}'
        try:
            email_utils.send_library_clean_email(
                recipient_email, confirm_url,
                library_name=str(meta.get('name') or ''),
                requested_by=requester_email,
            )
            sent_to.append(email_utils.masked_recipient(recipient_email))
        except Exception as exc:
            app.logger.warning('Library clean confirmation email failed for %s: %s', user_id, exc)
            send_errors.append(str(exc))

    if not sent_to:
        library_store.cancel_clean_request(library_id, request_id)
        return jsonify({
            'error': 'Could not send confirmation email(s). Please try again.',
            'detail': send_errors[0] if send_errors else '',
        }), 502

    library_store.audit(library_id, actor=account_id, action='clean-requested', target=request_id)
    return jsonify({
        'status': 'pending',
        'requiresAdditionalApproval': len(other_member_ids) > 0,
        'sentTo': sent_to,
        'expiresIn': library_utils.CLEAN_REQUEST_TTL_SECONDS,
    })


@app.route('/api/library/clean/confirm', methods=['POST'])
def library_clean_confirm():
    account_id, library_id, error = _require_library_context(require_auth=True)
    if error:
        return error
    data = request.get_json(silent=True) or {}
    token = str(data.get('token', '') or '')
    status, confirmed_request = library_store.confirm_clean_token(token, account_id=account_id, library_id=library_id)
    if status == 'mismatch':
        return jsonify({'error': 'This confirmation link does not belong to your account.'}), 403
    if status != 'ok' or not confirmed_request:
        return jsonify({'error': 'This confirmation link is invalid or has expired.'}), 400

    request_id = str(confirmed_request.get('RowKey') or '')
    library_store.audit(library_id, actor=account_id, action='clean-confirmed', target=request_id)
    if not library_store.is_clean_request_fully_confirmed(confirmed_request):
        return jsonify({'status': 'awaiting_more_approvals'})

    queued = _enqueue_library_clean_job(library_id, account_id, request_id)
    library_store.cancel_clean_request(library_id, request_id)
    library_store.audit(library_id, actor=account_id, action='clean-executed', target=queued.get('jobId', ''))
    return jsonify(_clustering_queue_response(queued, activeLibraryId=library_id))


@app.route('/api/library/clean/status', methods=['GET'])
def library_clean_status():
    _account_id, _library_id, error = _require_library_context(require_auth=True)
    if error:
        return error
    job_id = str(request.args.get('jobId', '') or '')
    if not job_id or metadata_table_client is None:
        return jsonify({'status': 'unknown'})
    try:
        row = metadata_table_client.get_entity(partition_key='jobs', row_key=_job_row_key(job_id))
    except Exception:
        stale_reason = _reconcile_stale_library_cleanup(_library_id, job_id=job_id)
        if stale_reason:
            return jsonify({'status': 'failed', 'error': stale_reason})
        return jsonify({'status': 'unknown'})
    result = row.get('result')
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except Exception:
            pass
    status = str(row.get('status') or 'unknown')
    stale_reason = None
    if status in {'queued', 'running'}:
        stale_reason = _reconcile_stale_library_cleanup(_library_id, job_id=job_id, job_row=row)
        if stale_reason:
            status = 'failed'
    if library_store is not None and status in {'done', 'failed'}:
        try:
            if status == 'done':
                summary = result if isinstance(result, dict) else {}
                library_store.set_cleanup_completed(
                    _library_id,
                    int(summary.get('photosDeleted') or 0),
                    int(summary.get('blobsDeleted') or 0),
                )
            else:
                library_store.set_cleanup_failed(_library_id, stale_reason or str(row.get('error') or 'cleanup failed'))
        except Exception:
            app.logger.debug('Could not reconcile cleanup status for %s from job %s', _library_id, job_id)
    return jsonify({'status': status, 'result': result, 'error': stale_reason or row.get('error')})


@app.route('/api/library/cleanup-info', methods=['GET'])
@app.route('/api/library/cleanup-info/', methods=['GET'])
def get_library_cleanup_info():
    """Get the cleanup status for the current library."""
    _account_id, library_id, error = _require_library_context(require_auth=True)
    if error:
        return error
    _reconcile_stale_library_cleanup(library_id)
    meta = library_store.get_library(library_id) or {}
    return jsonify({
        'lastCleanupStatus': str(meta.get('lastCleanupStatus') or ''),
        'lastCleanupTime': int(meta.get('lastCleanupTime') or 0),
        'lastCleanupPhotosDeleted': int(meta.get('lastCleanupPhotosDeleted') or 0),
        'lastCleanupBlobsDeleted': int(meta.get('lastCleanupBlobsDeleted') or 0),
        'lastCleanupError': str(meta.get('lastCleanupError') or ''),
    })


@app.route('/api/photos/thumbnail/<path:filename>', methods=['GET'])
def proxy_thumbnail(filename: str):
    """Serve a thumbnail blob or a placeholder when the blob is missing."""
    user_id, error = _require_user_id()
    if error:
        return error
    safe_name = _validate_media_filename(filename)
    if not safe_name:
        return jsonify({'error': 'Invalid filename'}), 400

    metadata_entity = _get_metadata_entity(user_id, safe_name)
    if not metadata_entity:
        return jsonify({'error': 'Not found'}), 404

    # Resolve the blob name: use anonymous ID if available, fallback to original filename
    blob_name_to_serve = _resolve_media_blob_name(user_id, safe_name, metadata_entity)

    try:
        props = get_media_properties('thumbnail', blob_name_to_serve)
        content_type = props.get('content_type') or 'image/jpeg'
        return _stream_media_response(
            'thumbnail',
            blob_name_to_serve,
            content_type=content_type,
            cache_control='private, max-age=3600',
            content_length=props.get('size'),
        )
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
    RAW/CR3 or other backend-preview-required file) -- unlike the
    once-per-upload or once-per-delete call sites of this same 'jobs'
    partition scan, this one is a hot, user-facing read path, so it reuses
    _jobs_partition_scan_cache like _has_active_clustering_job and
    _has_active_library_download_job do instead of re-scanning the whole
    (213k+ row and growing) partition on every call."""
    if metadata_table_client is None:
        return None
    try:
        rows = _jobs_partition_scan_cache.get(
            _JOBS_PARTITION_SCAN_CACHE_KEY,
            lambda: list(metadata_table_client.query_entities("PartitionKey eq 'jobs'")),
        )
    except Exception:
        return None
    for row in rows:
        if str(row.get('userId') or '') != user_id:
            continue
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
    if kind == 'thumbnail':
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
    safe_name = _validate_media_filename(filename)
    if not safe_name:
        return jsonify({'error': 'Invalid filename'}), 400
    metadata = _get_metadata_entity(user_id, safe_name)
    if not metadata:
        return jsonify({'error': 'Not found'}), 404
    if not _is_supported_photo_access_kind(kind):
        return jsonify({'error': 'Invalid media kind'}), 400
    if not blob_service_client or not account_name:
        return jsonify({'error': 'Media access is not configured'}), 503
    if kind == 'preview':
        return jsonify(_access_url_response(_preview_proxy_url(safe_name), '', safe_name, kind))
    if kind == 'thumbnail':
        fallback = _thumbnail_access_response(safe_name, metadata)
        if fallback is not None:
            return jsonify(fallback)
    container = _photo_access_container(kind)
    if container is None:
        return jsonify({'error': 'Invalid media kind'}), 400
    try:
        url, expires_at = _create_stable_read_sas_url(
            container,
            _blob_name_from_metadata(metadata, safe_name),
            download_filename=safe_name if kind == 'image' else None,
        )
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
        safe_name = _validate_media_filename(str(raw_name or ''))
        if not safe_name:
            continue
        metadata = _get_metadata_entity(user_id, safe_name)
        if not metadata:
            continue
        if kind == 'preview':
            urls[safe_name] = _preview_proxy_url(safe_name)
            continue
        container = _photo_access_container(kind)
        if container is None:
            continue
        if kind == 'thumbnail':
            fallback = _thumbnail_access_response(safe_name, metadata)
            if fallback is not None:
                urls[safe_name] = fallback['url']
                continue
        try:
            url, expires_at = _create_stable_read_sas_url(
                container,
                _blob_name_from_metadata(metadata, safe_name),
                download_filename=safe_name if kind == 'image' else None,
            )
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


@app.route('/api/photos/preview/<path:filename>', methods=['GET'])
def proxy_preview(filename: str):
    """Serve a browser-displayable preview for files that cannot be shown directly."""
    user_id, error = _require_user_id()
    if error:
        return error
    safe_name = _validate_media_filename(filename)
    if not safe_name:
        return jsonify({'error': 'Invalid filename'}), 400

    preview_metadata = _get_metadata_entity(user_id, safe_name)
    if not preview_metadata:
        return jsonify({'error': 'Not found'}), 404
    preview_blob_name = _blob_name_from_metadata(preview_metadata, safe_name)

    if _filename_requires_backend_preview(safe_name):
        try:
            cached = _stream_cached_preview(safe_name, cache_control='private, max-age=3600', blob_name=preview_blob_name)
        except Exception:
            app.logger.exception('Failed to stream cached preview for %s', safe_name)
            cached = None
        if cached is not None:
            return cached
        queued = _enqueue_preview_generation_job(user_id, safe_name)
        if queued.get('status') in {'queued', 'already_queued'}:
            return jsonify({
                'error': 'Preview is being prepared',
                'reason': 'preview_queued',
                'detail': 'The server queued a background preview build for this file. Try again shortly.',
                'jobId': queued.get('jobId') or '',
                'canDownloadOriginal': True,
            }), 503
        if queued.get('status') == 'unavailable':
            return jsonify({
                'error': 'Preview worker unavailable',
                'reason': 'preview_worker_unavailable',
                'detail': 'Preview generation worker is unavailable. Please try again later.',
                'canDownloadOriginal': True,
            }), 503
        return jsonify({
            'error': 'Preview queue failed',
            'reason': 'preview_queue_failed',
            'detail': 'Could not queue preview generation. Please try again.',
            'canDownloadOriginal': True,
        }), 503

    try:
        image_bytes = download_media_bytes('image', preview_blob_name)
        preview_bytes = convert_image_to_jpeg(image_bytes, safe_name)
        if not preview_bytes or (_filename_requires_backend_preview(safe_name) and not _looks_like_jpeg(preview_bytes)):
            return jsonify(_preview_failure_payload(safe_name)), 422
        resp = Response(preview_bytes, mimetype='image/jpeg')
        resp.headers['Cache-Control'] = 'private, max-age=3600'
        return resp
    except Exception as exc:
        if _is_missing_media_error(exc):
            return jsonify({
                'error': 'File not found in storage',
                'reason': 'missing',
                'detail': 'The original file could not be found in storage.',
            }), 404
        app.logger.exception('Failed to create preview for %s', safe_name)
        return jsonify({
            'error': 'Failed to create preview',
            'reason': 'server_error',
            'detail': 'The server hit an error while building this preview. Please try again.',
            'canDownloadOriginal': True,
        }), 503


@app.route('/api/photos/image/<path:filename>', methods=['GET'])
def proxy_image(filename: str):
    """Serve full image bytes from storage via backend proxy."""
    user_id, error = _require_user_id()
    if error:
        return error
    safe_name = _validate_media_filename(filename)
    if not safe_name:
        return jsonify({'error': 'Invalid filename'}), 400

    metadata_entity = _get_metadata_entity(user_id, safe_name)
    if not metadata_entity:
        return jsonify({'error': 'Not found'}), 404

    if not blob_service_client:
        return jsonify({'error': 'Image service not configured'}), 503

    # Resolve the blob name: use anonymous ID if available, fallback to original filename
    blob_name_to_serve = _resolve_media_blob_name(user_id, safe_name, metadata_entity)

    try:
        try:
            props = get_media_properties('image', blob_name_to_serve)
            content_type = props.get('content_type') or 'image/jpeg'
        except Exception as e:
            # File doesn't exist or can't be accessed
            if '404' in str(e) or 'ResourceNotFound' in str(e) or 'does not exist' in str(e).lower():
                return jsonify({'error': 'File not found in storage'}), 404
            return jsonify({'error': 'Failed to access image metadata'}), 503

        return _stream_media_response(
            'image',
            blob_name_to_serve,
            content_type=content_type,
            cache_control='private, max-age=3600',
            content_length=props.get('size'),
            download_filename=safe_name,
        )
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
    if not _is_safe_path_segment(leaf):
        return jsonify({'error': 'Invalid cover path'}), 400
    safe_leaf = leaf

    cover_blob = f'{user_hash}/{safe_leaf}'
    try:
        props = get_media_properties('cover', cover_blob)
        content_type = props.get('content_type') or 'image/jpeg'
        return _stream_media_response(
            'cover',
            cover_blob,
            content_type=content_type,
            cache_control='private, max-age=3600',
            content_length=props.get('size'),
        )
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
        names_only = (request.args.get('namesOnly') or '').strip().lower() in ('1', 'true')
        try:
            offset = int(request.args.get('offset', '0'))
            limit = int(request.args.get('limit', '15'))
        except ValueError:
            return jsonify({'error': 'Invalid paging parameters.'}), 400

        rows, face_by_id = _scan_person_and_face_rows(user_id)
        rows_by_id = {str(row.get('RowKey') or ''): row for row in rows}

        # Phase A: one cheap pass over every person using only the bulk face map
        # (no per-face network calls, no SAS minting, no writes) to work out name,
        # named-first order, and total count. The expensive per-person work below
        # (Phase B: full-fidelity face lookups + SAS thumbnail mint) only runs for
        # the requested page slice -- this is what keeps a page load fast
        # regardless of how many clusters/faces the account has.
        entries = []
        unnamed_counter = 1
        for row in rows:
            try:
                person_id = str(row.get('RowKey') or '')
                if not person_id:
                    continue
                try:
                    face_ids = json.loads(row.get('faceIds', '[]') or '[]')
                except Exception:
                    face_ids = []
                active_count = 0
                # See the identical comment in the Phase B loop below for what
                # "indeterminate" protects against. Here, a bulk-map miss is
                # conservatively treated as indeterminate rather than resolved
                # with an individual lookup -- Phase B does that resolution, only
                # for persons that make the page.
                indeterminate = False
                for fid in face_ids:
                    face = face_by_id.get(str(fid))
                    if face is None:
                        indeterminate = True
                        continue
                    if _face_is_rejected(face) or not _face_is_owned_by_person(face, person_id):
                        continue
                    active_count += 1

                # See the matching comment in Phase B: never auto-delete a
                # cluster the user explicitly named, even when it's empty.
                is_named = _person_entity_is_named(row)
                if active_count == 0 and not indeterminate and not is_named:
                    # Eligible for the empty-unnamed auto-delete; Phase B performs
                    # the authoritative check (and the delete) only if this page
                    # is actually requested.
                    continue

                raw_name = str(row.get('name', '') or '').strip()
                name = raw_name
                if not name:
                    name = f'Unnamed {unnamed_counter}'
                    unnamed_counter += 1
                if q and q not in name.lower():
                    continue
                entries.append({'personId': person_id, 'name': name, 'isNamed': is_named, 'faceCount': active_count})
            except Exception:
                continue

        # Stable sort: named clusters first, ties preserve the RowKey order
        # already established above (mirrors the frontend's previous client-side
        # named-first re-sort, now done once here instead).
        entries.sort(key=lambda e: 0 if e['isNamed'] else 1)
        total = len(entries)

        if names_only:
            return jsonify({
                'persons': [
                    {'personId': e['personId'], 'name': e['name'], 'faceCount': e['faceCount']}
                    for e in entries
                ],
                'total': total,
            })

        persons = []
        for entry in entries[offset:offset + limit]:
            try:
                person_id = entry['personId']
                row = rows_by_id.get(person_id)
                if row is None:
                    continue
                try:
                    face_ids = json.loads(row.get('faceIds', '[]') or '[]')
                except Exception:
                    face_ids = []
                active_face_ids = []
                rep_face = None
                rep_face_score = None
                # Track whether any face's status could not be determined (a
                # transient lookup error, as opposed to a face that is definitely
                # rejected/reassigned/deleted). We only auto-remove a cluster when
                # every face was positively determined inactive, so a storage blip
                # can never delete a still-valid person.
                indeterminate = False
                for rep_face_id in face_ids:
                    face = face_by_id.get(str(rep_face_id))
                    if face is None:
                        # Not in the bulk summary: look it up so we can tell a
                        # deleted face (definitively inactive) apart from a
                        # transient error (status unknown -> keep the person).
                        if face_table_client is None:
                            indeterminate = True
                            continue
                        try:
                            face = face_table_client.get_entity(partition_key=user_id, row_key=rep_face_id)
                        except Exception as exc:
                            if not _is_not_found_error(exc):
                                indeterminate = True
                            continue
                    if _face_is_rejected(face) or not _face_is_owned_by_person(face, person_id):
                        continue
                    active_face_ids.append(rep_face_id)
                    score = _face_preview_priority(face)
                    if rep_face is None or rep_face_score is None or score > rep_face_score:
                        rep_face = _face_summary_for_person_list(rep_face_id, face, user_id)
                        rep_face_score = score

                # Auto-remove empty clusters so they stop cluttering the People
                # page — BUT never auto-delete a cluster the user explicitly named.
                # Faces can leave a person temporarily (a merge or identity
                # propagation reassigning them); silently deleting a *named* person
                # on a plain list call is data loss — it's how named clusters
                # "vanished after a refresh", and why re-labelling one then 404s.
                # Keep it, show it empty, and let the user delete it explicitly.
                if not active_face_ids and not indeterminate and not entry['isNamed']:
                    try:
                        person_table_client.delete_entity(partition_key=user_id, row_key=person_id)
                        app.logger.info('Removed empty person cluster %s/%s', user_id, person_id)
                    except Exception:
                        pass
                    continue

                persons.append({
                    'personId': person_id,
                    'name': entry['name'],
                    'faceIds': active_face_ids,
                    'faceCount': len(active_face_ids),
                    'representativeFace': rep_face,
                })
            except Exception:
                continue
        return jsonify({'persons': persons, 'total': total})
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
                'thumbnailUrl': _face_thumbnail_url(str(face.get('filename') or ''), user_id),
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
        # Never show suggestions below the configured hard minimum.
        threshold = max(threshold, MIN_PEOPLE_SUGGEST_THRESHOLD)
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


@app.route('/api/persons/suggestions/decline', methods=['POST'])
def decline_person_suggestion():
    try:
        user_id, error = _require_user_id()
        if error:
            return error
        if not _people_features_available():
            return jsonify({'error': 'People features not configured'}), 503
        data = request.get_json(silent=True) or {}
        source_id = str(data.get('sourcePersonId') or '').strip()
        target_id = str(data.get('targetPersonId') or '').strip()
        if not source_id or not target_id:
            return jsonify({'error': 'sourcePersonId and targetPersonId required'}), 400
        # Store the decline on both persons so the pair stays hidden regardless
        # of which one ends up being the source next time suggestions run.
        ok_source = _add_declined_suggestion(user_id, source_id, target_id)
        ok_target = _add_declined_suggestion(user_id, target_id, source_id)
        if not (ok_source or ok_target):
            return jsonify({'error': 'Not found'}), 404
        return jsonify({'success': True})
    except Exception as exc:
        app.logger.exception('Decline person suggestion endpoint failed')
        return jsonify({'error': 'Decline person suggestion failed', 'detail': str(exc)}), 500


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

    # Naming a cluster is a strong identity signal: use its learned rep to pull
    # this person's faces out of unnamed clusters automatically. Best-effort so a
    # propagation hiccup never fails the label action itself.
    auto_assigned = 0
    if name.strip() and not _is_unnamed_name(name):
        try:
            propagation = _propagate_person_identity(user_id, person_id, apply=True, collect_suggestions=False)
            auto_assigned = int(propagation.get('autoAssignedCount') or 0)
        except Exception:
            app.logger.exception('Identity propagation after label failed for %s', person_id)
    return jsonify({'success': True, 'personId': person_id, 'name': name, 'autoAssignedFaces': auto_assigned})


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
            # face_id is content-addressed (hash of filename+bbox, see
            # _deterministic_face_id) so a persisted cover is immutable for
            # its lifetime — safe to cache long. SAS URLs are day-aligned and
            # valid up to 48h (see _create_stable_read_sas_url), so 1h keeps
            # this well inside that window.
            resp = jsonify({'url': make_media_url(cover_blob, 'cover')})
            resp.headers['Cache-Control'] = 'public, max-age=3600, immutable'
            return resp
    except Exception:
        pass

    # The face's source photo may be stored under an anonymous UUID; resolve the
    # physical blob for both the image read and the thumbnail fallback below.
    source_blob = _resolve_media_blob_name(user_id, filename)

    # Shared fallback: crop from the pre-generated thumbnail instead of the
    # original. Used both when the original can't be downloaded and when it
    # downloads fine but can't be decoded (e.g. a RAW/HEIC file where
    # conversion below also fails) -- either way we still have a usable image.
    def _crop_from_thumbnail():
        thumb_bytes = download_media_bytes('thumbnail', source_blob)
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
            return 'data:image/jpeg;base64,' + base64.b64encode(buf.read()).decode('ascii')

    try:
        image_bytes = download_media_bytes('image', source_blob)
    except Exception:
        try:
            return jsonify({'url': _crop_from_thumbnail()})
        except Exception:
            return jsonify({'error': 'Image not available'}), 404

    # RAW/cinema-RAW/HEIC originals aren't directly decodable the way a plain
    # JPEG is (same check the preview pipeline uses, _filename_requires_backend_preview)
    # -- extract a real preview first so Image.open below doesn't blow up with
    # PIL.UnidentifiedImageError on bytes it can't parse.
    if _filename_requires_backend_preview(filename):
        try:
            converted = convert_image_to_jpeg(image_bytes, filename)
            if converted:
                image_bytes = converted
        except Exception:
            pass

    try:
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
                resp = jsonify({'url': make_media_url(cover_blob, 'cover')})
                resp.headers['Cache-Control'] = 'public, max-age=3600, immutable'
                return resp
            except Exception:
                data_url = 'data:image/jpeg;base64,' + base64.b64encode(cover_bytes).decode('ascii')
    except Exception:
        try:
            return jsonify({'url': _crop_from_thumbnail()})
        except Exception:
            return jsonify({'error': 'Image not available'}), 404

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


@app.route('/api/persons/<person_id>/find-faces', methods=['POST'])
def find_person_faces(person_id: str):
    """Start (or run) identity propagation for this named person.

    Queue-first: long-running full-table scans run on the background worker when
    available. If the queue is unavailable (local/dev/no worker), fall back to
    inline execution so the feature still functions.
    """
    user_id, error = _require_user_id()
    if error:
        return error
    if not _people_features_available():
        return jsonify({'error': 'People features not configured'}), 503
    try:
        person_table_client.get_entity(partition_key=user_id, row_key=person_id)
    except Exception:
        return jsonify({'error': 'person not found'}), 404

    queued = _enqueue_propagate_job(user_id, person_id)
    if queued.get('status') == 'queued':
        return jsonify({
            'success': True,
            'queued': True,
            'status': 'queued',
            'personId': person_id,
            'propagateJobId': queued.get('jobId'),
            'autoAssignedFaces': 0,
            'autoAssigned': [],
            'suggestions': [],
            'candidateFaces': 0,
        })

    try:
        result = _propagate_person_identity(user_id, person_id, apply=True, collect_suggestions=True)
    except Exception as exc:
        app.logger.exception('find_person_faces failed for %s', person_id)
        return jsonify({'error': 'Find faces failed', 'detail': str(exc)}), 500
    return jsonify({
        'success': True,
        'queued': False,
        'status': 'done',
        'personId': person_id,
        'propagateJobId': None,
        'autoAssignedFaces': int(result.get('autoAssignedCount') or 0),
        'autoAssigned': result.get('autoAssigned') or [],
        'suggestions': result.get('suggestions') or [],
        'candidateFaces': int(result.get('candidateFaces') or 0),
        'skipped': result.get('skipped'),
    })


@app.route('/api/persons/<person_id>/suggested-faces/accept', methods=['POST'])
def accept_suggested_faces(person_id: str):
    user_id, error = _require_user_id()
    if error:
        return error
    if not _people_features_available():
        return jsonify({'error': 'People features not configured'}), 503
    data = request.get_json(silent=True) or {}
    face_ids = data.get('faceIds', [])
    if not isinstance(face_ids, list):
        return jsonify({'error': 'faceIds must be a list'}), 400
    try:
        person_table_client.get_entity(partition_key=user_id, row_key=person_id)
    except Exception:
        return jsonify({'error': 'person not found'}), 404

    accepted = []
    affected_files = set()
    for raw_face_id in face_ids:
        face_id = str(raw_face_id or '').strip()
        if not face_id:
            continue
        try:
            face = face_table_client.get_entity(partition_key=user_id, row_key=face_id)
        except Exception:
            continue
        old_person_id = str(face.get('personId') or '')
        if old_person_id and old_person_id != person_id:
            _remove_face_from_person(user_id, old_person_id, face_id)
        _remove_face_from_other_people(user_id, face_id, person_id)
        _add_face_to_person(user_id, person_id, face_id)
        face['personId'] = person_id
        face['confirmedByUser'] = True
        face['reviewStatus'] = 'confirmed'
        face['rejected'] = False
        face.pop('assignedByPropagation', None)
        face.pop('suspiciousReason', None)
        face.pop('rejectedReason', None)
        face.pop('rejectedAt', None)
        face['confidence'] = max(float(face.get('confidence', 0.0) or 0.0), 1.0)
        try:
            face_table_client.upsert_entity(face)
        except Exception:
            continue
        filename = str(face.get('filename') or '')
        if filename:
            affected_files.add(filename)
        accepted.append(face_id)

    if accepted:
        _update_person_rep_embedding(user_id, person_id)
        _rebuild_metadata_faces_for_filenames(user_id, affected_files)
    return jsonify({'success': True, 'personId': person_id, 'acceptedFaces': len(accepted), 'accepted': accepted})


@app.route('/api/persons/<person_id>/suggested-faces/decline', methods=['POST'])
def decline_suggested_faces(person_id: str):
    user_id, error = _require_user_id()
    if error:
        return error
    if not _people_features_available():
        return jsonify({'error': 'People features not configured'}), 503
    data = request.get_json(silent=True) or {}
    face_ids = data.get('faceIds', [])
    if not isinstance(face_ids, list):
        return jsonify({'error': 'faceIds must be a list'}), 400
    declined = _add_declined_face_suggestions(user_id, person_id, [str(fid) for fid in face_ids])
    return jsonify({'success': True, 'personId': person_id, 'declinedFaces': declined})


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

    def _write_merge_record(final_face_map, final_target_name):
        merged_names = [s['name'] for s in merged_snapshots if isinstance(s, dict) and s.get('name')]
        try:
            merge_table_client.upsert_entity({
                'PartitionKey': user_id,
                'RowKey': merge_id,
                'targetPersonId': person_id,
                'mergedIds': json.dumps(merge_ids),
                'targetName': final_target_name or '',
                'mergedNames': json.dumps(merged_names),
                'payload': json.dumps({
                    'base': base_snapshot,
                    'merged': merged_snapshots,
                    'faceMap': final_face_map,
                }),
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

    core = _merge_persons_core(user_id, person_id, merge_ids)
    if core is None:
        return jsonify({'error': 'base person not found'}), 404
    merge_id = core['mergeId']

    # If the merged-into person is named, reuse its strengthened rep to reclaim
    # matching faces still sitting in unnamed clusters. That scans the entire face
    # table (the slowest part of a merge, and a past OOM driver), so hand it to the
    # queue-scaled worker instead of blocking this request; the reclaimed faces
    # surface on the next refresh. If the queue is unavailable, fall back to running
    # it inline so behaviour is unchanged when there is no worker.
    auto_assigned = 0
    propagate_job_id = None
    if _person_is_named(user_id, person_id):
        queued = _enqueue_propagate_job(user_id, person_id)
        if queued.get('status') == 'queued':
            propagate_job_id = queued.get('jobId')
        else:
            try:
                propagation = _propagate_person_identity(user_id, person_id, apply=True, collect_suggestions=False)
                auto_assigned = int(propagation.get('autoAssignedCount') or 0)
            except Exception:
                app.logger.exception('Identity propagation after merge failed for %s', person_id)

    return jsonify({
        'success': True,
        'personId': person_id,
        'mergeId': merge_id,
        'autoAssignedFaces': auto_assigned,
        'propagateJobId': propagate_job_id,
    })


# Sane upper bound on how many merge pairs one bulk-approve request can carry —
# each pair does a full per-user face-table query, so an unbounded batch is a
# single request doing unbounded work.
PEOPLE_MERGE_BATCH_MAX = 50


@app.route('/api/persons/merge/batch', methods=['POST'])
def merge_persons_batch():
    """Bulk-approve several merge-suggestion pairs in one request.

    Each pair's face reassignment still runs sequentially (they're independent
    person ids, so this is safe), but identity propagation — the expensive
    full face-table scan that reclaims a named person's faces from unnamed
    clusters — is coalesced into a single background job covering every named
    target in the batch, instead of one job per pair. Approving suggestions
    one request at a time previously queued one propagate job per approval;
    the worker drains the clustering queue one message at a time, so a big
    batch drained as a slow drip of individual completion toasts over several
    minutes. See [[job-completion-notifications]] / bombardment fix.
    """
    user_id, error = _require_user_id()
    if error:
        return error
    if not _people_features_available():
        return jsonify({'error': 'People features not configured'}), 503
    data = request.get_json(silent=True) or {}
    pairs = data.get('merges', [])
    if not isinstance(pairs, list) or not pairs:
        return jsonify({'error': 'merges must be a non-empty list'}), 400
    if len(pairs) > PEOPLE_MERGE_BATCH_MAX:
        return jsonify({'error': f'too many merges in one batch (max {PEOPLE_MERGE_BATCH_MAX})'}), 400

    results = []
    named_target_ids: List[str] = []
    seen_targets = set()
    for pair in pairs:
        target_id = str((pair or {}).get('targetPersonId') or (pair or {}).get('personId') or '') if isinstance(pair, dict) else ''
        source_ids = pair.get('mergeIds') if isinstance(pair, dict) else None
        if not target_id or not isinstance(source_ids, list) or not source_ids:
            results.append({'targetPersonId': target_id, 'success': False, 'error': 'invalid pair'})
            continue
        core = _merge_persons_core(user_id, target_id, source_ids)
        if core is None:
            results.append({'targetPersonId': target_id, 'success': False, 'error': 'base person not found'})
            continue
        results.append({'targetPersonId': target_id, 'success': True, 'mergeId': core['mergeId']})
        if target_id not in seen_targets and _person_is_named(user_id, target_id):
            seen_targets.add(target_id)
            named_target_ids.append(target_id)

    propagate_job_id = None
    auto_assigned_total = 0
    if named_target_ids:
        queued = _enqueue_propagate_batch_job(user_id, named_target_ids)
        if queued.get('status') == 'queued':
            propagate_job_id = queued.get('jobId')
        else:
            # No worker available (local/dev): fall back to running each pass
            # inline so behaviour is unchanged when there is no queue.
            for target_id in named_target_ids:
                try:
                    propagation = _propagate_person_identity(user_id, target_id, apply=True, collect_suggestions=False)
                    auto_assigned_total += int(propagation.get('autoAssignedCount') or 0)
                except Exception:
                    app.logger.exception('Identity propagation after batch merge failed for %s', target_id)

    return jsonify({
        'success': all(r.get('success') for r in results),
        'results': results,
        'propagateJobId': propagate_job_id,
        'autoAssignedFaces': auto_assigned_total,
        'targetPersonIds': named_target_ids,
    })


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
    # This partition also holds the people-repair snapshots (recluster / dedupe /
    # suspicious / membership), which serialize the entire people+faces+metadata
    # tables into multi-KB `payload` chunk rows (see _create_people_repair_snapshot).
    # Pulling full entities here materialised every one of those payloads into
    # memory just to skip them by `kind` on the next line — enough to OOM-kill the
    # replica on a large library. Project only the small columns the undo list
    # needs (never `payload`) and stream the rows instead of list()-ing them, so
    # the snapshot backups are never transferred or held in memory.
    select_cols = ['RowKey', 'kind', 'targetPersonId', 'mergedIds', 'targetName', 'mergedNames', 'createdAt']
    try:
        rows_iter = merge_table_client.query_entities(
            f"PartitionKey eq '{_escape_odata(user_id)}'",
            select=select_cols,
        )
    except Exception:
        return jsonify({'merges': []})

    merges = []
    try:
        for row in rows_iter:
            try:
                if str(row.get('kind') or '').startswith(('recluster_snapshot', 'face_dedupe_snapshot', 'suspicious_face_snapshot', 'unblock_faces_snapshot', 'face_membership_snapshot')):
                    continue
                try:
                    merged_names = json.loads(row.get('mergedNames', '[]') or '[]')
                except Exception:
                    merged_names = []
                try:
                    merged_ids = json.loads(row.get('mergedIds', '[]') or '[]')
                except Exception:
                    merged_ids = []
                merges.append({
                    'mergeId': row['RowKey'],
                    'targetPersonId': row.get('targetPersonId'),
                    'mergedIds': merged_ids,
                    'targetName': row.get('targetName'),
                    'mergedNames': merged_names,
                    'createdAt': row.get('createdAt'),
                })
            except Exception:
                continue
    except Exception:
        # A mid-stream paging error still returns whatever was collected.
        pass
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
            errors.append({'faceId': face_id, 'error': str(exc)})
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


@app.route('/api/faces', methods=['GET'])
def list_faces():
    """Flat list of the user's active faces across every person.

    Powers the Faces grid, which shows individual face crops (rather than the
    per-person groupings in the Clusters view) so many faces fit in one page.
    """
    try:
        user_id, error = _require_user_id()
        if error:
            return error
        if not _people_features_available():
            return jsonify({'error': 'People features not configured'}), 503
        q = (request.args.get('q') or '').strip().lower()
        try:
            offset = int(request.args.get('offset', '0'))
            limit = int(request.args.get('limit', '50'))
        except ValueError:
            return jsonify({'error': 'Invalid paging parameters.'}), 400

        person_rows, face_by_id = _scan_person_and_face_rows(user_id)

        # Phase A: cheap pass building the ordered (person, face) pairs using
        # only the bulk face map -- no per-face network calls, no SAS minting.
        # The expensive per-face summary (Phase B, below) only runs for the
        # requested page slice.
        entries = []
        seen = set()
        unnamed_counter = 1
        for person in person_rows:
            try:
                person_id = str(person.get('RowKey') or '')
                if not person_id:
                    continue
                name = str(person.get('name', '') or '').strip()
                if not name:
                    name = f'Unnamed {unnamed_counter}'
                    unnamed_counter += 1
                if q and q not in name.lower():
                    continue
                try:
                    face_ids = json.loads(person.get('faceIds', '[]') or '[]')
                except Exception:
                    face_ids = []
                for face_id in face_ids:
                    fid = str(face_id or '')
                    if not fid or fid in seen:
                        continue
                    # A bulk-map miss moments after the scan above is almost
                    # always a genuinely deleted face -- unlike list_persons,
                    # there's no cluster-level "keep it anyway" decision at
                    # stake here, so this cheap pass simply excludes it rather
                    # than paying for an individual lookup.
                    face = face_by_id.get(fid)
                    if face is None:
                        continue
                    if _face_is_rejected(face) or not _face_is_owned_by_person(face, person_id):
                        continue
                    seen.add(fid)
                    entries.append({'personId': person_id, 'personName': name, 'faceId': fid})
            except Exception:
                continue

        total = len(entries)
        faces = []
        for entry in entries[offset:offset + limit]:
            try:
                face = face_by_id.get(entry['faceId'])
                if face is None:
                    continue
                summary = _face_summary_for_person_list(entry['faceId'], face, user_id)
                summary['personId'] = entry['personId']
                summary['personName'] = entry['personName']
                faces.append(summary)
            except Exception:
                continue
        return jsonify({'faces': faces, 'total': total})
    except Exception as exc:
        app.logger.exception('List faces endpoint failed')
        return jsonify({'error': 'List faces failed', 'detail': str(exc)}), 500


@app.route('/api/faces/delete', methods=['POST'])
def delete_faces():
    user_id, error = _require_user_id()
    if error:
        return error
    if not _people_features_available():
        return jsonify({'error': 'People features not configured'}), 503
    data = request.get_json(silent=True) or {}
    face_ids = data.get('faceIds', [])
    if not isinstance(face_ids, list):
        return jsonify({'error': 'faceIds must be a list'}), 400
    result = _delete_faces_bulk(user_id, [str(fid or '') for fid in face_ids])
    status = int(result.pop('status', 200))
    return jsonify({'success': len(result.get('errors') or []) == 0, **result}), status


@app.route('/upload/init', methods=['POST'])
@app.route('/upload/init/', methods=['POST'])
@app.route('/api/upload/init', methods=['POST'])
@app.route('/api/upload/init/', methods=['POST'])
def init_upload():
    user_id, error = _require_user_id()
    if error:
        return error
    blocked = _library_cleanup_block_reason(user_id)
    if blocked:
        return jsonify({'error': blocked, 'code': 'cleanup_in_progress'}), 409
    data = request.get_json(silent=True) or {}
    filename = _validate_media_filename(data.get('filename', ''))
    total_size = int(data.get('totalSize', 0))
    expected_hash = (data.get('sha256') or '').strip()

    if not filename:
        return jsonify({'error': 'Invalid filename'}), 400
    if total_size <= 0:
        return jsonify({'error': 'Invalid totalSize'}), 400
    if total_size > MAX_UPLOAD_FILE_BYTES:
        return jsonify({'error': 'File exceeds upload limit'}), 413

    upload_id = secure_filename(str(data.get('uploadId') or '')) or str(uuid.uuid4())
    direct = bool(data.get('directToBlob'))
    is_fresh_upload = not data.get('uploadId')
    if is_fresh_upload:
        try:
            _cleanup_failed_upload(user_id, filename)
        except Exception:
            pass

    # Reserve the anonymous blob name the browser will upload to. Persisted on the
    # metadata row (durable + replica-safe) and REUSED on a resumed /upload/init so
    # the browser keeps staging blocks to the same blob — a fresh UUID per call
    # would orphan already-staged blocks (InvalidBlockList) and break retry/resume.
    anonymous_blob_name = None
    if is_fresh_upload and direct:
        # Fast path for the common case (new upload, direct-to-blob): one read
        # + one write against the metadata row instead of reset_received_ranges
        # and reserve_pending_anonymous_blob each separately re-reading and
        # re-writing it (was 2 reads + 2 writes here alone).
        try:
            anonymous_blob_name = reset_upload_tracking_and_reserve_blob(
                user_id, filename, total_size, expected_hash or None,
            )
        except Exception:
            app.logger.debug('Failed to reset tracking / reserve anonymous blob name for %s', filename)
    else:
        if is_fresh_upload:
            try:
                reset_received_ranges(user_id, filename, total_size, expected_hash or None)
            except Exception:
                pass
        if direct:
            try:
                anonymous_blob_name = reserve_pending_anonymous_blob(user_id, filename, expected_hash or None)
            except Exception:
                app.logger.debug('Failed to reserve anonymous blob name for %s', filename)

    blob_url = None
    expires_at = None
    if direct:
        try:
            # Use anonymous_blob_name for the SAS URL if available
            blob_url, expires_at = _create_direct_upload_blob_url(anonymous_blob_name or filename)
        except Exception as exc:
            app.logger.exception('Failed to create direct upload SAS for %s', filename)
            return jsonify({'error': 'Direct upload is not configured', 'detail': str(exc)}), 503
    thumbnail_blob_url = None
    thumbnail_sas_expires_at = None
    try:
        # Mint the thumbnail SAS under the same anonymous blob name as the image so
        # the browser's direct thumbnail upload also lands on the anonymized blob.
        thumbnail_blob_url, thumbnail_sas_expires_at = _create_direct_thumbnail_upload_blob_url(anonymous_blob_name or filename)
    except Exception:
        pass
    return jsonify({
        'uploadId': upload_id,
        'uploadUrl': f'/upload/{upload_id}?filename={filename}',
        'blobUrl': blob_url,
        'thumbnailBlobUrl': thumbnail_blob_url,
        'blobName': anonymous_blob_name or filename,
        'originalFilename': filename,
        'sasExpiresAt': expires_at,
        'thumbnailSasExpiresAt': thumbnail_sas_expires_at,
        'totalSize': total_size,
    })


MAX_INIT_BATCH_FILES = 20


@app.route('/upload/init-batch', methods=['POST'])
@app.route('/upload/init-batch/', methods=['POST'])
@app.route('/api/upload/init-batch', methods=['POST'])
@app.route('/api/upload/init-batch/', methods=['POST'])
def init_upload_batch():
    """Batched /upload/init for a chunk of new (non-resumed) direct-to-blob
    uploads: one Azure Table query + one transactional batch write for the
    whole chunk, instead of a read + write PER FILE (see
    reset_upload_tracking_and_reserve_blobs_batch). Meant to be called once
    per ~10-15-file chunk from the frontend, not once per file.

    Scoped to fresh, direct-to-blob uploads only -- deliberately simpler than
    single-file /upload/init: no per-file _cleanup_failed_upload rare-path
    handling (re-uploading a filename that was already fully completed
    before, without deleting the old photo first, is rare enough that the
    frontend falls back to single-file /upload/init for resumes and any
    retried file anyway). Every entry must be a genuinely new upload -- pass
    a client-supplied uploadId through single-file /upload/init instead.
    """
    request_started = time.monotonic()
    phase_ms: Dict[str, int] = {}
    phase_started = request_started
    def _mark(phase: str) -> None:
        nonlocal phase_started
        now = time.monotonic()
        phase_ms[phase] = round((now - phase_started) * 1000)
        phase_started = now

    user_id, error = _require_user_id()
    if error:
        return error
    blocked = _library_cleanup_block_reason(user_id)
    if blocked:
        return jsonify({'error': blocked, 'code': 'cleanup_in_progress'}), 409
    _mark('auth_ms')
    data = request.get_json(silent=True) or {}
    files = data.get('files')
    if not isinstance(files, list) or not files:
        return jsonify({'error': 'files must be a non-empty list'}), 400
    if len(files) > MAX_INIT_BATCH_FILES:
        return jsonify({'error': f'Batch too large (max {MAX_INIT_BATCH_FILES} files)'}), 400

    parsed = []
    for idx, item in enumerate(files):
        if not isinstance(item, dict):
            parsed.append({'index': idx, 'error': 'Invalid file entry'})
            continue
        filename = _validate_media_filename(item.get('filename', ''))
        total_size = int(item.get('totalSize', 0) or 0)
        expected_hash = str(item.get('sha256') or '').strip()
        if not filename:
            parsed.append({'index': idx, 'error': 'Invalid filename'})
            continue
        if total_size <= 0 or total_size > MAX_UPLOAD_FILE_BYTES:
            parsed.append({'index': idx, 'filename': filename, 'error': 'Invalid totalSize'})
            continue
        parsed.append({
            'index': idx,
            'filename': filename,
            'total_size': total_size,
            'expected_hash': expected_hash,
            'upload_id': str(uuid.uuid4()),
        })
    _mark('validate_ms')

    valid = [p for p in parsed if 'error' not in p]
    anonymous_blob_names: Dict[str, str] = {}
    if valid:
        try:
            anonymous_blob_names = reset_upload_tracking_and_reserve_blobs_batch(
                user_id,
                [
                    {
                        'filename': p['filename'],
                        'total_size': p['total_size'],
                        'expected_hash': p['expected_hash'] or None,
                        'is_fresh': True,
                    }
                    for p in valid
                ],
            )
        except Exception:
            app.logger.exception('Batch upload-tracking reservation failed for %s files', len(valid))
    _mark('batch_reserve_ms')

    results = []
    for p in parsed:
        if 'error' in p:
            results.append({'index': p['index'], 'filename': p.get('filename'), 'error': p['error']})
            continue
        filename = p['filename']
        anonymous_blob_name = anonymous_blob_names.get(filename)
        try:
            blob_url, expires_at = _create_direct_upload_blob_url(anonymous_blob_name or filename)
        except Exception as exc:
            results.append({'index': p['index'], 'filename': filename, 'error': 'Direct upload is not configured', 'detail': str(exc)})
            continue
        thumbnail_blob_url = None
        thumbnail_sas_expires_at = None
        try:
            thumbnail_blob_url, thumbnail_sas_expires_at = _create_direct_thumbnail_upload_blob_url(anonymous_blob_name or filename)
        except Exception:
            pass
        results.append({
            'index': p['index'],
            'filename': filename,
            'uploadId': p['upload_id'],
            'uploadUrl': f"/upload/{p['upload_id']}?filename={filename}",
            'blobUrl': blob_url,
            'thumbnailBlobUrl': thumbnail_blob_url,
            'blobName': anonymous_blob_name or filename,
            'originalFilename': filename,
            'sasExpiresAt': expires_at,
            'thumbnailSasExpiresAt': thumbnail_sas_expires_at,
            'totalSize': p['total_size'],
        })
    _mark('sas_mint_ms')
    app.logger.info(
        'init-batch timings user=%s files=%s phase_ms=%s total_ms=%s',
        user_id, len(files), phase_ms, round((time.monotonic() - request_started) * 1000),
    )
    return jsonify({'results': results})


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


@app.route('/upload/known-hashes', methods=['GET'])
@app.route('/upload/known-hashes/', methods=['GET'])
@app.route('/api/upload/known-hashes', methods=['GET'])
@app.route('/api/upload/known-hashes/', methods=['GET'])
def get_known_upload_hashes():
    """One bulk fetch for the whole library's dedup index -- meant to be called
    once per upload batch (see list_known_file_hashes), not per file, so the
    frontend can skip re-uploading a known duplicate before spending any
    transfer bandwidth on it."""
    _, user_id, error = _require_library_context()
    if error:
        return error
    return jsonify({'hashes': list_known_file_hashes(user_id)})


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


@app.route('/upload/finalize', methods=['POST'])
@app.route('/upload/finalize/', methods=['POST'])
@app.route('/api/upload/finalize', methods=['POST'])
@app.route('/api/upload/finalize/', methods=['POST'])
def finalize_direct_upload():
    account_id, user_id, error = _require_library_context()
    if error:
        return error
    blocked = _library_cleanup_block_reason(user_id)
    if blocked:
        return jsonify({'error': blocked, 'code': 'cleanup_in_progress'}), 409
    data = request.get_json(silent=True) or {}
    filename = _validate_media_filename(data.get('filename', ''))
    total_size = int(data.get('totalSize', 0) or 0)
    content_type = str(data.get('contentType') or 'application/octet-stream')
    if not filename:
        return jsonify({'error': 'Invalid filename'}), 400
    if total_size <= 0 or total_size > MAX_UPLOAD_FILE_BYTES:
        return jsonify({'error': 'Invalid totalSize'}), 400

    # Prefer the blob name the browser itself got back from /upload/init and
    # actually staged its blocks against, over re-deriving it from the shared
    # (user, filename) metadata row: when several files share an original
    # filename (e.g. many photos named "Ip_image.jpeg"), every one of their
    # /upload/init calls reserves its OWN blob but writes it onto that same
    # row -- renaming apart into distinct rows only happens later, inside
    # finalize_uploaded_file below. Re-deriving from the row here would pick
    # up whichever file's init call happened to run last, not necessarily
    # this one, causing spurious "Uploaded blob not found"/"size mismatch"
    # for files that lose that race. Falls back to the row lookup for
    # sessions that predate this field (durable + replica-safe, so it still
    # works even when finalize lands on a different replica than init).
    anonymous_blob_name = _validate_client_blob_name(data.get('blobName')) or read_pending_anonymous_blob(user_id, filename)
    blob_to_check = anonymous_blob_name or filename

    try:
        props = blob_service_client.get_blob_client(container=BLOB_IMAGE_CONTAINER, blob=blob_to_check).get_blob_properties()
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
            client_sha256=str(data.get('sha256') or ''),
            anonymous_blob_name=anonymous_blob_name,
        )
    except Exception as exc:
        app.logger.exception('Direct upload finalization failed for %s', filename)
        return jsonify({'error': 'Upload finalization failed', 'detail': str(exc)}), 500
    # finalize_uploaded_file writes metadata via storage_utils (bypassing
    # _update_metadata_entity_fields), so drop the scan cache explicitly: the
    # gallery refetches right after an upload and must see the new photo.
    _invalidate_metadata_scan_cache(user_id)
    # Persist the blob size (validated above) and the adding account in one write:
    # the size lets the gallery listing skip a per-photo blob HEAD, and uploadedBy
    # attributes the photo to the library member who added it.
    try:
        finalize_updates: Dict[str, object] = {'size': total_size}
        if account_id:
            finalize_updates['uploadedBy'] = account_id
        client_last_modified_iso = epoch_millis_to_iso(data.get('clientLastModified'))
        if client_last_modified_iso:
            finalize_updates['clientLastModified'] = client_last_modified_iso
        _update_metadata_entity_fields(user_id, final_name, finalize_updates)
    except Exception:
        app.logger.debug('Could not stamp finalize metadata for %s', final_name)
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
        _mark_fresh_upload_activity(user_id)
    except Exception:
        app.logger.exception('Failed to record fresh upload activity for %s', user_id)
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


@app.route('/upload/finalize-batch', methods=['POST'])
@app.route('/upload/finalize-batch/', methods=['POST'])
@app.route('/api/upload/finalize-batch', methods=['POST'])
@app.route('/api/upload/finalize-batch/', methods=['POST'])
def finalize_upload_batch():
    """Batched /upload/finalize for a chunk of just-committed direct-to-blob
    uploads -- same reasoning as /upload/init-batch
    (reset_upload_tracking_and_reserve_blobs_batch), applied to finalize.

    Each individual finalize_uploaded_file call is inherently slow (dedup
    check, metadata write, queue enqueue -- tens of seconds under load,
    see docs/ipworker-architecture.md-adjacent upload-speed investigation),
    and one HTTP request per file meant one gunicorn thread held for that
    whole duration per file. Under concurrent load this both exhausted the
    fleet's thread pool (requests queueing behind each other) and put
    multiple finalize calls in real Python-level GIL contention with each
    other (each concurrently executing real work, not just waiting on I/O).
    Looping sequentially through a chunk on ONE thread inside ONE request
    fixes both: one thread instead of N, and no concurrent execution within
    this chunk to contend over. finalize_uploaded_file itself is untouched --
    this only changes how many HTTP requests/threads are spent invoking it,
    not what it does per file.

    Every entry must be a genuinely new (non-resumed) direct-to-blob upload,
    same restriction as /upload/init-batch -- a resumed upload still uses
    single-file /upload/finalize.
    """
    request_started = time.monotonic()
    account_id, user_id, error = _require_library_context()
    if error:
        return error
    blocked = _library_cleanup_block_reason(user_id)
    if blocked:
        return jsonify({'error': blocked, 'code': 'cleanup_in_progress'}), 409
    auth_ms = round((time.monotonic() - request_started) * 1000)
    data = request.get_json(silent=True) or {}
    files = data.get('files')
    if not isinstance(files, list) or not files:
        return jsonify({'error': 'files must be a non-empty list'}), 400
    if len(files) > MAX_INIT_BATCH_FILES:
        return jsonify({'error': f'Batch too large (max {MAX_INIT_BATCH_FILES} files)'}), 400

    try:
        _mark_fresh_upload_activity(user_id)
    except Exception:
        app.logger.exception('Failed to record fresh upload activity for %s', user_id)

    # Summed across every file in the batch, then logged once at the end
    # (instead of once per file) so a large batch under concurrent load
    # doesn't multiply log volume at exactly the concurrency level this is
    # meant to help measure.
    phase_totals_ms = {
        'blob_check': 0, 'finalize_write': 0, 'metadata_stamp': 0,
        'metadata_read': 0, 'client_processing': 0, 'queue': 0, 'clustering_queue': 0,
    }

    def _accum(key: str, start: float) -> float:
        now = time.monotonic()
        phase_totals_ms[key] += round((now - start) * 1000)
        return now

    results = []
    for idx, item in enumerate(files):
        if not isinstance(item, dict):
            results.append({'index': idx, 'error': 'Invalid file entry'})
            continue
        filename = _validate_media_filename(item.get('filename', ''))
        total_size = int(item.get('totalSize', 0) or 0)
        content_type = str(item.get('contentType') or 'application/octet-stream')
        if not filename:
            results.append({'index': idx, 'error': 'Invalid filename'})
            continue
        if total_size <= 0 or total_size > MAX_UPLOAD_FILE_BYTES:
            results.append({'index': idx, 'filename': filename, 'error': 'Invalid totalSize'})
            continue

        t = time.monotonic()
        # See the matching comment in finalize_direct_upload above -- same
        # same-filename-collision race, same fix.
        anonymous_blob_name = _validate_client_blob_name(item.get('blobName')) or read_pending_anonymous_blob(user_id, filename)
        blob_to_check = anonymous_blob_name or filename
        try:
            props = blob_service_client.get_blob_client(container=BLOB_IMAGE_CONTAINER, blob=blob_to_check).get_blob_properties()
            if int(getattr(props, 'size', 0) or 0) != total_size:
                t = _accum('blob_check', t)
                results.append({'index': idx, 'filename': filename, 'error': 'Uploaded blob size mismatch'})
                continue
        except Exception as exc:
            t = _accum('blob_check', t)
            results.append({'index': idx, 'filename': filename, 'error': 'Uploaded blob not found', 'detail': str(exc)})
            continue
        t = _accum('blob_check', t)

        try:
            duplicates, final_name = finalize_uploaded_file(
                user_id,
                filename,
                content_type,
                client_processing=item.get('clientProcessing'),
                client_processing_report=item.get('clientProcessingReport'),
                client_asset_id=str(item.get('clientAssetId') or item.get('uploadId') or ''),
                client_sha256=str(item.get('sha256') or ''),
                anonymous_blob_name=anonymous_blob_name,
            )
        except Exception as exc:
            t = _accum('finalize_write', t)
            app.logger.exception('Batch finalize failed for %s', filename)
            results.append({'index': idx, 'filename': filename, 'error': 'Upload finalization failed', 'detail': str(exc)})
            continue
        t = _accum('finalize_write', t)

        # Same per-file follow-up as single-file finalize above, just inline
        # in this loop instead of a separate request.
        _invalidate_metadata_scan_cache(user_id)
        try:
            finalize_updates: Dict[str, object] = {'size': total_size}
            if account_id:
                finalize_updates['uploadedBy'] = account_id
            client_last_modified_iso = epoch_millis_to_iso(item.get('clientLastModified'))
            if client_last_modified_iso:
                finalize_updates['clientLastModified'] = client_last_modified_iso
            _update_metadata_entity_fields(user_id, final_name, finalize_updates)
        except Exception:
            app.logger.debug('Could not stamp finalize metadata for %s', final_name)
        t = _accum('metadata_stamp', t)

        metadata = None
        hash_mismatch = False
        try:
            metadata = metadata_table_client.get_entity(partition_key=user_id, row_key=final_name)
            if metadata.get('upload_sha256_expected') and metadata.get('upload_sha256_match') is False:
                hash_mismatch = True
        except Exception:
            pass
        t = _accum('metadata_read', t)
        if hash_mismatch:
            results.append({
                'index': idx,
                'filename': final_name,
                'error': 'Upload hash mismatch',
                'uploadSha256Match': metadata.get('upload_sha256_match') if metadata else False,
            })
            continue

        if item.get('clientProcessing') or item.get('clientProcessingReport'):
            try:
                metadata = apply_client_processing_results_for_file(
                    user_id,
                    final_name,
                    client_processing=item.get('clientProcessing'),
                    client_processing_report=item.get('clientProcessingReport'),
                    client_asset_id=str(item.get('clientAssetId') or item.get('uploadId') or ''),
                )
            except Exception:
                app.logger.exception('Inline client processing update failed for %s', final_name)
        t = _accum('client_processing', t)
        try:
            metadata = metadata or metadata_table_client.get_entity(partition_key=user_id, row_key=final_name)
        except Exception:
            pass
        t = _accum('metadata_read', t)
        try:
            _queue_upload_processing(user_id, final_name)
        except Exception:
            app.logger.exception('Failed to queue post-finalize processing for %s', final_name)
        t = _accum('queue', t)
        try:
            _queue_people_clustering_after_face_processing(user_id, final_name, metadata)
        except Exception:
            app.logger.exception('Failed to auto-queue clustering for %s', final_name)
        t = _accum('clustering_queue', t)

        results.append({
            'index': idx,
            'uploadId': item.get('uploadId') or '',
            'filename': final_name,
            'bytesReceived': total_size,
            'totalSize': total_size,
            'complete': True,
            'duplicates': duplicates,
            'clientProcessingLateResultWaitSeconds': 0,
        })

    app.logger.info(
        'finalize-batch timings user=%s files=%s auth_ms=%s phase_totals_ms=%s total_ms=%s',
        user_id, len(files), auth_ms, phase_totals_ms, round((time.monotonic() - request_started) * 1000),
    )
    return jsonify({'results': results})


@app.route('/upload/client-processing', methods=['POST'])
@app.route('/upload/client-processing/', methods=['POST'])
@app.route('/api/upload/client-processing', methods=['POST'])
@app.route('/api/upload/client-processing/', methods=['POST'])
def upload_client_processing_results():
    request_started = time.monotonic()
    user_id, error = _require_user_id()
    if error:
        return error
    blocked = _library_cleanup_block_reason(user_id)
    if blocked:
        return jsonify({'error': blocked, 'code': 'cleanup_in_progress'}), 409
    auth_ms = round((time.monotonic() - request_started) * 1000)
    data = request.get_json(silent=True) or {}
    filename = _validate_media_filename(data.get('filename', ''))
    if not filename:
        return jsonify({'error': 'Invalid filename'}), 400
    t = time.monotonic()
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
    apply_ms = round((time.monotonic() - t) * 1000)

    # apply_client_processing_results_for_file writes via storage_utils,
    # bypassing _update_metadata_entity_fields.
    _invalidate_metadata_scan_cache(user_id)

    t = time.monotonic()
    try:
        _queue_people_clustering_after_face_processing(user_id, filename, metadata)
    except Exception:
        app.logger.exception('Failed to auto-queue clustering after browser processing update for %s', filename)
    clustering_queue_ms = round((time.monotonic() - t) * 1000)

    app.logger.info(
        'client-processing timings user=%s file=%s auth_ms=%s apply_ms=%s clustering_queue_ms=%s total_ms=%s',
        user_id, filename, auth_ms, apply_ms, clustering_queue_ms,
        round((time.monotonic() - request_started) * 1000),
    )
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
        if status in BROWSER_PROCESSING_TERMINAL_STATUSES and not retryable_no_data and not face_version_retry:
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
        except Exception:
            worker_logger.exception('ipwork sweep iteration failed')
        time.sleep(IPWORK_SWEEP_INTERVAL_SECONDS)


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
        # Raised from 25: the frontend's pending-drain now fetches a real batch
        # (PENDING_PROCESSING_BATCH_SIZE=40, see AppServicesProvider.tsx) up
        # front for its lanes to work through, decoupled from lane count --
        # capping this below that silently truncated the batch every time.
        limit = max(1, min(int(request.args.get('limit', '1') or 1), 60))
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
        # Mint SAS against the physical blob (anonymous UUID for anonymized photos),
        # then drop the internal marker so it isn't exposed to the browser.
        physical_name = item.pop('_blobName', None) or item['filename']
        try:
            url, expires_at = _create_scoped_blob_url(BLOB_IMAGE_CONTAINER, physical_name, minutes=10)
            item['sourceUrl'] = url
            item['sourceExpiresAt'] = expires_at
        except Exception:
            app.logger.warning('Failed to mint browser processing source URL for %s', item.get('filename'), exc_info=True)
        try:
            thumbnail_url, thumbnail_expires_at = _create_direct_thumbnail_upload_blob_url(physical_name)
            item['thumbnailUploadUrl'] = thumbnail_url
            item['thumbnailUploadExpiresAt'] = thumbnail_expires_at
        except Exception:
            app.logger.warning('Failed to mint browser thumbnail upload URL for %s', item.get('filename'), exc_info=True)
    return jsonify({'pending': bounded, 'totalPending': len(pending)})


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
        if 'already held by another client' in message.lower() or 'lease is already held' in message.lower():
            return {'claimed': False, 'reason': 'lease_active', 'detail': message}, 200
        return {'claimed': False, 'reason': 'lease_active', 'detail': message}, 409
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


@app.route('/upload/processing/claim', methods=['POST'])
@app.route('/upload/processing/claim/', methods=['POST'])
@app.route('/api/upload/processing/claim', methods=['POST'])
@app.route('/api/upload/processing/claim/', methods=['POST'])
def upload_processing_claim():
    user_id, error = _require_user_id()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    filename = _validate_media_filename(str(data.get('filename') or '')) or ''
    if not filename:
        return jsonify({'error': 'Missing filename'}), 400
    lease_owner = str(data.get('leaseId') or data.get('ownerId') or f'browser-{uuid.uuid4()}').strip()
    requested_steps = data.get('steps')
    steps = [str(step or '').strip() for step in requested_steps] if isinstance(requested_steps, list) else None
    response, status = _claim_processing_lease_response(user_id, filename, lease_owner, steps, data.get('blobName'))
    return jsonify(response), status


MAX_CLAIM_BATCH_ITEMS = 60


@app.route('/upload/processing/claim-batch', methods=['POST'])
@app.route('/upload/processing/claim-batch/', methods=['POST'])
@app.route('/api/upload/processing/claim-batch', methods=['POST'])
@app.route('/api/upload/processing/claim-batch/', methods=['POST'])
def upload_processing_claim_batch():
    """Claim leases for several photos in one round trip.

    Scoped deliberately to "claim what you're about to start on right now"
    (the frontend only ever batches this to its current lane count, not its
    whole fetched pending batch) -- claiming far more than that upfront would
    hold leases on photos no lane has reached yet, and could let them expire
    before anyone actually processes them. Each item's claim is still fully
    independent (own read/write, own success/failure), same as calling
    /upload/processing/claim in a loop; this only collapses the HTTP round
    trips, not the lease semantics.
    """
    user_id, error = _require_user_id()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    items = data.get('items')
    if not isinstance(items, list) or not items:
        return jsonify({'error': 'items must be a non-empty list'}), 400
    if len(items) > MAX_CLAIM_BATCH_ITEMS:
        return jsonify({'error': f'Too many items (max {MAX_CLAIM_BATCH_ITEMS})'}), 400

    results = []
    for raw_item in items:
        if not isinstance(raw_item, dict):
            results.append({'claimed': False, 'reason': 'invalid_item'})
            continue
        filename = _validate_media_filename(str(raw_item.get('filename') or '')) or ''
        if not filename:
            results.append({'claimed': False, 'reason': 'invalid_filename'})
            continue
        lease_owner = str(raw_item.get('leaseId') or raw_item.get('ownerId') or f'browser-{uuid.uuid4()}').strip()
        requested_steps = raw_item.get('steps')
        steps = [str(step or '').strip() for step in requested_steps] if isinstance(requested_steps, list) else None
        response, _status = _claim_processing_lease_response(user_id, filename, lease_owner, steps, raw_item.get('blobName'))
        results.append({'filename': filename, **response})

    return jsonify({'results': results})


@app.route('/upload/processing/heartbeat', methods=['POST'])
@app.route('/upload/processing/heartbeat/', methods=['POST'])
@app.route('/api/upload/processing/heartbeat', methods=['POST'])
@app.route('/api/upload/processing/heartbeat/', methods=['POST'])
def upload_processing_heartbeat():
    user_id, error = _require_user_id()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    filename = _validate_media_filename(str(data.get('filename') or '')) or ''
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
    filename = _validate_media_filename(str(data.get('filename') or '')) or ''
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
        return str(exc)
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
                cleanup['errors'].append(f'metadata: {str(exc)}')
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
        safe_name = _validate_media_filename(original_name)
        if not safe_name:
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
        # Default to capture-date order so a request without an explicit sort opens
        # on the most recently taken photos (matches the gallery's default view).
        sort = request.args.get('sort', 'capture')
        offset = int(request.args.get('offset', '0'))
        limit = int(request.args.get('limit', '24'))
    except ValueError:
        return jsonify({'error': 'Invalid paging parameters.'}), 400

    capture_start, capture_end = _parse_capture_range_args()

    user_id, error = _require_user_id()
    if error:
        return error
    try:
        metadata_rows = _cached_metadata_rows_for_user(user_id, purpose='photos.list')
        entries = [row['RowKey'] for row in metadata_rows if row.get('RowKey')]
        metadata_map = {row['RowKey']: row for row in metadata_rows if row.get('RowKey')}
    except Exception as exc:
        return jsonify({'error': 'Unable to read photo metadata.', 'details': str(exc)}), 503

    # Backfill: rows uploaded before finalize persisted uploadDate sort via the
    # volatile last_processing_update fallback. Stamp the derived value as their
    # permanent uploadDate (best-effort, capped per request) so their position
    # can never shift again — e.g. when a legacy photo gets reprocessed.
    backfilled = 0
    for name in entries:
        if backfilled >= UPLOAD_DATE_BACKFILL_MAX_PER_REQUEST:
            break
        row = metadata_map.get(name) or {}
        if row.get('uploadDate'):
            continue
        derived = str(row.get('upload_started_at') or row.get('last_processing_update') or '')
        if not derived:
            continue
        try:
            _update_metadata_entity_fields(user_id, name, {'uploadDate': derived})
            row['uploadDate'] = derived
            backfilled += 1
        except Exception:
            break  # storage hiccup: stop backfilling, listing still works

    # Deterministic ordering with a filename tie-break so the gallery returns an
    # identical sequence on every load (see ordering_utils.order_photo_entries).
    entries = order_photo_entries(entries, metadata_map, sort)

    if capture_start or capture_end:
        entries = [name for name in entries if _capture_in_range(metadata_map.get(name, {}), capture_start, capture_end)]

    selected = entries[offset:offset + limit]

    # Persist blob size for legacy rows that predate finalize-time stamping, so the
    # gallery stops doing a blob HEAD per tile. Capped per request (converges over
    # a few page views); after that _build_photo_summary reads size from metadata
    # with head_missing=False and never HEADs.
    props_backfilled = 0
    for name in selected:
        if props_backfilled >= PHOTO_PROPS_BACKFILL_MAX_PER_REQUEST:
            break
        row = metadata_map.get(name) or {}
        if row.get('size'):
            continue
        try:
            props = get_media_properties('image', _blob_name_from_metadata(row, name))
        except Exception:
            break  # storage hiccup: stop backfilling, listing still works
        size_val = int(props.get('size') or 0)
        if not size_val:
            continue
        updates: Dict[str, object] = {'size': size_val}
        lm = props.get('last_modified')
        if lm is not None:
            updates['lastModified'] = lm.isoformat()
        try:
            _update_metadata_entity_fields(user_id, name, updates)
            row.update(updates)
            props_backfilled += 1
        except Exception:
            break

    pid_to_name, _ = _load_people_name_index(user_id)
    photos = _build_photo_summaries_page(
        user_id,
        [(filename, metadata_map.get(filename, {})) for filename in selected],
        pid_to_name,
    )

    return jsonify({'photos': photos, 'total': len(entries)})


@app.route('/photos/processing-status', methods=['GET'])
@app.route('/photos/processing-status/', methods=['GET'])
@app.route('/api/photos/processing-status', methods=['GET'])
@app.route('/api/photos/processing-status/', methods=['GET'])
def photos_processing_status():
    """Point-lookup refresh for the gallery's processing-status poller
    (PhotoGallery.tsx) -- lets the "processing on server" tile icon update
    without re-fetching/relisting the whole page. Bounded to a small explicit
    filename list, not a scan."""
    user_id, error = _require_user_id()
    if error:
        return error
    raw_filenames = request.args.get('filenames', '')
    filenames = [
        f for f in (_validate_media_filename(name.strip()) for name in raw_filenames.split(',') if name.strip()) if f
    ][:100]
    statuses: Dict[str, Dict] = {}
    for filename in filenames:
        entity = _get_metadata_entity(user_id, filename)
        if entity is None:
            continue
        statuses[filename] = {
            'thumbnail': entity.get('thumbnail_status'),
            'exif': entity.get('exif_status'),
            'ocr': entity.get('ocr_status'),
            'face': entity.get('face_status'),
            'aiVision': entity.get('ai_vision_status'),
            'mapDetection': entity.get('map_detection_status'),
            'activeWorker': _active_processing_worker(entity),
        }
    return jsonify({'statuses': statuses})


@app.route('/photos/lookup/<path:filename>', methods=['GET'])
@app.route('/photos/lookup/<path:filename>/', methods=['GET'])
@app.route('/api/photos/lookup/<path:filename>', methods=['GET'])
@app.route('/api/photos/lookup/<path:filename>/', methods=['GET'])
def lookup_photo(filename: str):
    # Exact-filename point lookup for deep links (e.g. "view in library" from a
    # page that doesn't otherwise share the gallery's paginated listing), so the
    # caller doesn't need to guess a page offset or rely on fuzzy search ranking.
    user_id, error = _require_user_id()
    if error:
        return error
    safe_name = _validate_media_filename(filename)
    if not safe_name:
        return jsonify({'error': 'Invalid filename'}), 400
    metadata = _get_metadata_entity(user_id, safe_name)
    if not metadata:
        return jsonify({'error': 'Not found'}), 404
    pid_to_name, _ = _load_people_name_index(user_id)
    return jsonify({'photo': _build_photo_summary(user_id, safe_name, metadata, include_props=False, pid_to_name=pid_to_name)})


@app.route('/photos/timeline', methods=['GET'])
@app.route('/photos/timeline/', methods=['GET'])
@app.route('/api/photos/timeline', methods=['GET'])
@app.route('/api/photos/timeline/', methods=['GET'])
def photos_timeline():
    # Single compact year/month/day summary for the client-side zoomable
    # timeline (see timeline_metadata.build_timeline_summary). Reuses the same
    # cached full-partition scan as /photos, so a newly-uploaded photo appears
    # here within the same staleness window (METADATA_SCAN_CACHE_TTL_SECONDS)
    # it appears in the gallery, with no separate cache/invalidation to manage.
    user_id, error = _require_user_id()
    if error:
        return error
    try:
        metadata_rows = _cached_metadata_rows_for_user(user_id, purpose='photos.timeline')
    except Exception as exc:
        return jsonify({'error': 'Unable to read photo metadata.', 'details': str(exc)}), 503
    return jsonify(build_timeline_summary(metadata_rows))


@app.route('/uploads/corrupted', methods=['GET'])
@app.route('/uploads/corrupted/', methods=['GET'])
@app.route('/api/uploads/corrupted', methods=['GET'])
@app.route('/api/uploads/corrupted/', methods=['GET'])
def list_corrupted_uploads():
    user_id, error = _require_user_id()
    if error:
        return error
    try:
        rows = _cached_metadata_rows_for_user(user_id, purpose='uploads.corrupted')
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

        media_urls = _private_photo_media_urls(filename, row)
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
    safe_name = _validate_media_filename(filename)
    if not safe_name:
        return jsonify({'error': 'Invalid filename'}), 400

    metadata = _get_metadata_entity(user_id, safe_name)
    if metadata is None:
        return jsonify({'error': 'Not found'}), 404

    try:
        download_media_bytes('image', _blob_name_from_metadata(metadata, safe_name))
    except Exception:
        return jsonify({'error': 'Image file not found'}), 404

    metadata['corrupted'] = False
    metadata.pop('verification_error', None)
    metadata.pop('corrupted_at', None)
    if metadata.get('verification_status') == 'failed':
        metadata['verification_status'] = 'pending'
    metadata_table_client.upsert_entity(metadata)
    _invalidate_metadata_scan_cache(user_id)

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

    capture_start, capture_end = _parse_capture_range_args()

    user_id, error = _require_user_id()
    if error:
        return error

    try:
        rows = _cached_metadata_rows_for_user(user_id, purpose='photos.search')
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

    photos = _build_photo_summaries_page(
        user_id,
        [(filename, metadata) for _, filename, metadata in selected],
        pid_to_name,
    )

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
        safe_name = _validate_media_filename(filename)
        if not safe_name:
            metadata[filename] = {'error': 'Invalid filename'}
            continue

        row = _get_metadata_entity(user_id, safe_name)
        if not row:
            metadata[filename] = {'error': 'Not found'}
            continue

        try:
            props = get_media_properties('image', _blob_name_from_metadata(row, safe_name))
            metadata[filename] = {
                'size': props.get('size'),
                'lastModified': props.get('last_modified').isoformat() if props.get('last_modified') else None,
            }
        except Exception:
            metadata[filename] = {'error': 'Not found'}

    return jsonify(metadata)


def _shared_names_in_batch(names_set: set, user_id: str) -> set:
    """Which of ``names_set`` are content-addressed blobs still referenced by
    another library, so their blobs must NOT be deleted.

    The per-file ``_is_filename_shared`` runs a full cross-partition scan of the
    metadata table; doing that once for the whole batch (recording only rows
    whose RowKey is in the batch, so memory stays bounded by the batch size)
    replaces up to N such scans with one."""
    shared: set = set()
    if metadata_table_client is None or not names_set:
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
    removed_face_ids: set = set()
    for row in face_rows:
        if str(row.get('filename') or '') not in names_set:
            continue
        face_id = str(row.get('RowKey') or '')
        if not face_id:
            continue
        try:
            face_table_client.delete_entity(partition_key=user_id, row_key=face_id)
        except Exception:
            pass
        removed_face_ids.add(face_id)
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
    """Delete stale job-status rows for any filename in ``names_set`` in a single
    scan of the ``jobs`` partition (vs. one scan per file)."""
    if metadata_table_client is None or not names_set:
        return 0
    try:
        rows = list(metadata_table_client.query_entities("PartitionKey eq 'jobs'"))
    except Exception:
        return 0
    removed = 0
    for row in rows:
        row_user_id = str(row.get('userId') or '')
        if row_user_id and row_user_id != user_id:
            continue
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
            metadata_table_client.delete_entity(partition_key='jobs', row_key=row_key)
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

    # Validate up front and resolve each requested name to its safe form once.
    valid_names = []
    seen = set()
    for filename in filenames:
        safe_name = _validate_media_filename(filename)
        if not safe_name:
            errors.append(f'{filename}: Invalid filename')
            continue
        if safe_name in seen:
            continue
        seen.add(safe_name)
        valid_names.append(safe_name)

    if not valid_names:
        return jsonify({'deleted': deleted, 'errors': errors, 'success': False})

    names_set = set(valid_names)

    # --- Batch the expensive table/partition scans ONCE for the whole request.
    # The previous implementation ran several full scans PER file, which made
    # large deletions (hundreds of photos) take minutes. ---
    try:
        own_rows = list(metadata_table_client.query_entities(f"PartitionKey eq '{_escape_odata(user_id)}'")) if metadata_table_client else []
    except Exception:
        own_rows = []
    own_rows_by_name = {str(row.get('RowKey') or ''): row for row in own_rows}

    shared_names = _shared_names_in_batch(names_set, user_id)
    temp_removed_names = _batch_delete_upload_temp_files(names_set)

    # Per-file point operations only (no scans): blob + metadata-row deletes.
    for safe_name in valid_names:
        metadata = own_rows_by_name.get(safe_name)
        shared_with_other_user = safe_name in shared_names
        file_errors = []
        removed_any = safe_name in temp_removed_names

        # Anonymized photos store their blobs under the anonymous UUID; resolve it
        # from the metadata row we already loaded so the delete targets the real blob.
        anonymous_id = str((metadata or {}).get('anonymousImageId') or '').strip()

        if not shared_with_other_user:
            physical_name = anonymous_id or safe_name
            # Clear the original-filename blobs too when anonymized, in case a
            # pre-anonymization copy ever lingered under the real name.
            extra = [safe_name] if anonymous_id else None
            blob_errors = _delete_photo_blobs_if_present(physical_name, extra)
            removed_any = True
            file_errors.extend(blob_errors)
            # Drop the name-mapping row so no anonymous_id -> original_filename
            # record survives the photo (and the mapping table doesn't accrete
            # dead rows). Skipped for shared content still referenced elsewhere.
            if anonymous_id:
                try:
                    delete_image_name_mapping(user_id, anonymous_id)
                except Exception:
                    app.logger.debug('Failed to delete image-name mapping for %s', safe_name)

        if metadata is not None:
            try:
                metadata_table_client.delete_entity(partition_key=user_id, row_key=safe_name)
                removed_any = True
            except Exception as exc:
                file_errors.append(f'metadata: {str(exc)}')
            # Best-effort: drop this library's dedup/collision index rows too,
            # so a deleted photo's hash/filename doesn't linger and confuse a
            # later upload (detect_duplicates self-heals a stale hit anyway).
            file_hash = str(metadata.get('fileHash') or '')
            if file_hash:
                delete_hash_index_entry(user_id, file_hash)
            delete_filename_owner_entry(user_id, safe_name)
        elif not removed_any:
            errors.append(f'{safe_name}: Not found')
            continue

        if file_errors:
            errors.append(f'{safe_name}: {"; ".join(file_errors)}')
        elif removed_any:
            deleted.append(safe_name)
        else:
            errors.append(f'{safe_name}: Not found')

    # Faces / people, jobs, and albums reconciliation — one scan each.
    deleted_names_set = set(deleted)
    try:
        deleted_person_ids = _batch_remove_faces_for_filenames(user_id, deleted_names_set)
    except Exception as exc:
        app.logger.warning('Batch face removal failed for %s: %s', user_id, exc)
        deleted_person_ids = set()
    try:
        removed_jobs = _batch_remove_job_rows(user_id, deleted_names_set)
        if removed_jobs:
            app.logger.info('Removed %s stale job row(s) for %s during batch delete', removed_jobs, user_id)
    except Exception as exc:
        app.logger.warning('Batch job cleanup failed for %s: %s', user_id, exc)
    try:
        _batch_remove_filenames_from_albums(user_id, deleted_names_set)
    except Exception as exc:
        app.logger.warning('Batch album cleanup failed for %s: %s', user_id, exc)

    # Strip references to any person that was emptied by this delete from the
    # photos that survive (their peopleIds may still name a now-deleted person).
    if deleted_person_ids and metadata_table_client is not None:
        for name, row in own_rows_by_name.items():
            if name in deleted_names_set:
                continue
            try:
                pids = json.loads(row.get('peopleIds', '[]') or '[]')
            except Exception:
                continue
            next_pids = [pid for pid in pids if pid not in deleted_person_ids]
            if len(next_pids) == len(pids):
                continue
            row['peopleIds'] = json.dumps(next_pids)
            try:
                metadata_table_client.upsert_entity(row)
            except Exception:
                pass

    if deleted:
        # Deletions bypass _update_metadata_entity_fields; drop the scan cache so
        # the next gallery load doesn't resurrect deleted photos, and mark the
        # vector index dirty since the library's photo set changed.
        _invalidate_metadata_scan_cache(user_id)
        try:
            touch_user_vector_index_state(user_id)
        except Exception:
            pass

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
        safe = _validate_media_filename(str(filename))
        if not safe:
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
        metadata_rows = _cached_metadata_rows_for_user(user_id, purpose='albums.smart_create')
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


@app.route('/api/people/diagnostic', methods=['GET'])
def people_diagnostic():
    """Diagnostic endpoint: why aren't faces clustering into people?"""
    try:
        user_id, error = _require_user_id()
        if error:
            return error
        if not _people_features_available():
            return jsonify({'error': 'People features not configured'}), 503

        # Fetch all faces and people
        try:
            faces = list(face_table_client.query_entities(f"PartitionKey eq '{_escape_odata(user_id)}'")) if face_table_client else []
        except Exception:
            faces = []
        try:
            people = list(person_table_client.query_entities(f"PartitionKey eq '{_escape_odata(user_id)}'")) if person_table_client else []
        except Exception:
            people = []

        # Analyze face clustering eligibility
        total_faces = len(faces)
        accepted_faces = 0
        rejected_faces = 0
        suspicious_faces = 0
        low_confidence_faces = 0
        stale_embedding_version_faces = 0
        no_embedding_faces = 0
        unassigned_faces = 0
        confirmed_faces = 0

        allowed_versions = _face_embedding_allowed_versions()

        for face in faces:
            if _face_is_rejected(face):
                rejected_faces += 1
                continue
            if _face_is_suspicious(face):
                suspicious_faces += 1
                continue
            if not _face_embedding_allowed_for_clustering(face):
                stale_embedding_version_faces += 1
                continue
            emb = _face_embedding_from_entity(face)
            if not emb:
                no_embedding_faces += 1
                continue
            if _coerce_bool(face.get('confirmedByUser', False)):
                confirmed_faces += 1
            if not str(face.get('personId') or '').strip():
                unassigned_faces += 1
            accepted_faces += 1

        # Check for active clustering job
        active_job = _has_active_clustering_job(user_id)

        # Check configuration
        clustering_available = clustering_queue_client is not None
        browser_only = BROWSER_ONLY_PROCESSING

        return jsonify({
            'totalFaces': total_faces,
            'acceptedForClustering': accepted_faces,
            'rejectedFaces': rejected_faces,
            'suspiciousFaces': suspicious_faces,
            'lowConfidenceFaces': low_confidence_faces,
            'staleEmbeddingVersionFaces': stale_embedding_version_faces,
            'noEmbeddingFaces': no_embedding_faces,
            'unassignedFaces': unassigned_faces,
            'confirmedFaces': confirmed_faces,
            'totalPeople': len(people),
            'clusteringConfiguration': {
                'browserOnlyProcessing': browser_only,
                'clusteringQueueAvailable': clustering_available,
                'activeClusteringJob': active_job,
                'allowedEmbeddingVersions': list(allowed_versions),
                'clusteringEps': PEOPLE_CLUSTER_EPS,
                'clusteringPreset': PEOPLE_CLUSTER_PRESET,
            },
            'recommendation': (
                'No faces detected.' if total_faces == 0
                else 'All faces rejected or below confidence threshold.' if accepted_faces == 0
                else 'Clustering queue not available; check BROWSER_ONLY_PROCESSING.' if not clustering_available
                else f'Trigger clustering with POST /api/people/assign-unclustered or POST /api/people/recluster (with repair confirmation).' if unassigned_faces > 0 or len(people) == 0
                else f'All {accepted_faces} faces already assigned to people.'
            ),
        })
    except Exception as exc:
        app.logger.exception('People diagnostic failed')
        return jsonify({'error': 'Diagnostic failed', 'detail': str(exc)}), 500


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


@app.route('/api/jobs/status', methods=['GET'])
@app.route('/jobs/status', methods=['GET'])
def jobs_status():
    """Return the current user's recent background jobs so the in-app notifier
    can surface completions (reclustering, find-more-faces, library cleanup,
    preview generation, ...). Includes anything still queued/running plus jobs
    that finished within the recent window; the client dedupes what it has
    already shown so a completed job is only ever announced once.
    """
    try:
        user_id, error = _require_user_id()
        if error:
            return error
        if metadata_table_client is None:
            return jsonify({'jobs': []})
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=JOB_STATUS_WINDOW_MINUTES)).isoformat()
        # A job of ANY type (clustering, ipwork, library_clean, preview, ...)
        # this old and still queued/running is dead, not in-flight — the
        # worker/ipworker crashed mid-job (e.g. OOM) and never wrote a
        # terminal status. This used to only cover job_type == 'clustering'
        # (mirroring _has_active_clustering_job's de-dupe cutoff), but any job
        # type can be orphaned the same way — an old stuck 'ipwork' row was
        # found stuck 15 days "running", keeping the server-processing
        # indicator on forever with nothing left to actually process. Without
        # this cutoff a dead row shows as perpetually "in flight" and its
        # activity indicator never clears.
        stale_cutoff = (datetime.now(timezone.utc) - timedelta(minutes=CLUSTERING_ACTIVE_JOB_STALE_MINUTES)).isoformat()
        try:
            # Same 'jobs' partition _has_active_clustering_job scans (219k+
            # rows and growing) -- userId isn't a key property, so Table
            # Storage has no secondary index for it and "...and userId eq X"
            # still costs a full partition scan server-side, paid on every
            # poll of this endpoint. Reuse the same constant-key cache
            # instead of re-scanning: one fetch of the whole partition genuinely
            # serves every user's poll within the TTL window.
            all_rows = _jobs_partition_scan_cache.get(
                _JOBS_PARTITION_SCAN_CACHE_KEY,
                lambda: list(metadata_table_client.query_entities("PartitionKey eq 'jobs'")),
            )
            rows = [row for row in all_rows if str(row.get('userId') or '') == user_id]
        except Exception:
            app.logger.exception('Failed to query job status rows for %s', user_id)
            return jsonify({'jobs': []})
        jobs = []
        flushed_any_stale = False
        for row in rows:
            status = str(row.get('status') or '').lower()
            updated_at = str(row.get('updatedAt') or '')
            job_type = str(row.get('jobType') or '')
            if status in {'queued', 'running'} and updated_at and updated_at < stale_cutoff:
                _upsert_job_status(str(row.get('jobId') or ''), user_id, job_type, 'failed', error='Job did not finish (worker restarted or timed out)')
                flushed_any_stale = True
                continue
            # Keep in-flight jobs, plus terminal ones that finished recently.
            # updatedAt is a UTC isoformat string, so lexicographic comparison
            # against the cutoff is a valid recency test.
            if status in {'queued', 'running'} or updated_at >= cutoff:
                jobs.append(_humanize_job(row))
        if flushed_any_stale:
            # The write(s) above just happened in this same request/process --
            # don't make the caller (or the next poller, in-process) wait out
            # the full cache TTL to see its own just-written result.
            _jobs_partition_scan_cache.invalidate(_JOBS_PARTITION_SCAN_CACHE_KEY)
        jobs.sort(key=lambda job: job.get('updatedAt') or '', reverse=True)
        return jsonify({'jobs': jobs[:50]})
    except Exception as exc:
        app.logger.exception('Job status route failed')
        return jsonify({'jobs': [], 'error': str(exc)}), 500


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


@app.route('/api/admin/people/unblock-low-confidence-faces', methods=['POST'])
@app.route('/admin/people/unblock-low-confidence-faces', methods=['POST'])
def admin_unblock_low_confidence_faces():
    """Un-reject faces that were auto-suppressed as low-confidence but now meet
    the current (lowered) threshold. Run this after lowering
    FACE_LOW_CONFIDENCE_REJECT_BELOW to bring previously-rejected faces back
    into clustering."""
    user_id, error = _require_user_id()
    if error:
        return error
    if not _people_features_available():
        return jsonify({'error': 'People features not configured'}), 503
    data = request.get_json(silent=True) or {}
    if data.get('repair') is not True or data.get('confirm') != 'UNBLOCK_LOW_CONFIDENCE_FACES':
        return jsonify({'error': 'repair confirmation required', 'code': 'protected_repair_required'}), 403
    dry_run = _coerce_bool(data.get('dryRun', True))
    return jsonify(_unblock_low_confidence_faces(user_id, dry_run=dry_run))


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
        if not _people_features_available():
            return jsonify({'error': 'People features not configured'}), 503
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


@app.route('/api/admin/backfill/photos', methods=['POST'])
@app.route('/admin/backfill/photos', methods=['POST'])
def admin_backfill_photos():
    """Re-queue existing photos through the processing pipeline.

    Marks processing steps as 'queued' (force=True) for every non-deleted,
    non-video photo in the user's library. The browser's background scheduler
    picks them up via /upload/processing/pending and re-runs them — identical
    to what happens for a freshly uploaded photo. When PROCESSING_MODE is
    'backend' or 'both', each photo is also enqueued to ipworker
    (_queue_ipwork_processing) -- this is the way to bulk-reprocess an
    existing library server-side without any browser tab needing to be open.

    By default all steps are re-queued (thumbnails, EXIF, OCR, AI vision, map
    tagging, and face detection). Pass a 'steps' list in the body to scope the
    re-queue to a subset, e.g. {"steps": ["ocr"]} to re-run OCR only, across
    every photo in the library.
    """
    user_id, error = _require_user_id()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    if data.get('repair') is not True or data.get('confirm') != 'BACKFILL_ALL_PHOTOS':
        return jsonify({'error': 'repair confirmation required', 'code': 'protected_repair_required'}), 403

    all_steps = ['thumbnail', 'exif', 'ocr', 'ai_vision', 'map_detection', 'face']
    requested_steps = data.get('steps')
    if requested_steps is None:
        steps_to_run = all_steps
    else:
        if not isinstance(requested_steps, list) or not requested_steps:
            return jsonify({'error': 'steps must be a non-empty list', 'code': 'invalid_steps'}), 400
        invalid_steps = [step for step in requested_steps if step not in all_steps]
        if invalid_steps:
            return jsonify({'error': f'invalid steps: {invalid_steps}', 'code': 'invalid_steps'}), 400
        steps_to_run = requested_steps

    try:
        metadata_rows = _cached_metadata_rows_for_user(user_id, purpose='admin.backfill')
    except Exception as exc:
        app.logger.exception('Backfill: failed to load metadata for %s', user_id)
        return jsonify({'error': 'Failed to load photo metadata', 'detail': str(exc)}), 503

    queued = 0
    skipped = 0
    for row in metadata_rows:
        filename = str(row.get('RowKey') or '').strip()
        if not filename:
            continue
        if str(row.get('processing_state') or '').strip().lower() == 'deleted':
            skipped += 1
            continue
        if is_video_file(filename):
            skipped += 1
            continue
        try:
            _enqueue_processing_steps(user_id, filename, steps_to_run, force=True)
            # Bulk/background reprocessing of an already-uploaded library is
            # one of the two reasons ipworker exists (see the ipworker plan) --
            # without this, backend/both mode would only ever reach ipworker
            # for brand-new uploads (_queue_upload_processing), and this
            # admin action would silently do nothing beyond flipping table
            # status columns nothing consumes.
            _queue_ipwork_processing(user_id, filename, steps=steps_to_run)
            queued += 1
        except Exception:
            app.logger.exception('Backfill: failed to enqueue steps for %s/%s', user_id, filename)
            skipped += 1

    _invalidate_metadata_scan_cache(user_id)
    app.logger.info('Backfill queued %d photos (steps=%s), skipped %d for user %s', queued, steps_to_run, skipped, user_id)
    return jsonify({
        'queued': queued,
        'skipped': skipped,
        'total': queued + skipped,
        'steps': steps_to_run,
    })


@app.route('/api/admin/ipwork/enqueue', methods=['POST'])
@app.route('/admin/ipwork/enqueue', methods=['POST'])
def admin_enqueue_ipwork():
    """Queue specific steps for a caller-supplied set of photos, server-side.

    The Tools page's per-step buttons (Thumbnails/EXIF/OCR/AI vision/Map
    tagging/Faces) used to only call startBrowserProcessing() -- a purely
    client-side pipeline. Under PROCESSING_MODE=backend that pipeline
    self-gates every step to a no-op (see runBrowserProcessing in
    PhotoGallery.tsx), so those buttons silently did nothing. This is the
    selection-scoped counterpart to admin_backfill_photos() above (which
    already does this, but only for the *entire* library): it lets a
    specific set of filenames be (re)enqueued to ipworker without touching
    the rest of the account. No confirm gate is required here, unlike the
    library-wide backfill -- the blast radius is bounded to whatever the
    caller explicitly selected.
    """
    user_id, error = _require_user_id()
    if error:
        return error
    data = request.get_json(silent=True) or {}

    all_steps = ['thumbnail', 'exif', 'ocr', 'ai_vision', 'map_detection', 'face']
    requested_steps = data.get('steps')
    if not isinstance(requested_steps, list) or not requested_steps:
        return jsonify({'error': 'steps must be a non-empty list', 'code': 'invalid_steps'}), 400
    invalid_steps = [step for step in requested_steps if step not in all_steps]
    if invalid_steps:
        return jsonify({'error': f'invalid steps: {invalid_steps}', 'code': 'invalid_steps'}), 400
    steps_to_run = requested_steps

    raw_filenames = data.get('filenames')
    if not isinstance(raw_filenames, list) or not raw_filenames:
        return jsonify({'error': 'filenames must be a non-empty list', 'code': 'invalid_filenames'}), 400
    if len(raw_filenames) > 2000:
        return jsonify({'error': 'Too many filenames', 'code': 'too_many_filenames'}), 400
    force = bool(data.get('force'))

    queued = 0
    skipped = 0
    for raw_name in raw_filenames:
        filename = _validate_media_filename(str(raw_name or ''))
        entity = _get_metadata_entity(user_id, filename) if filename else None
        if not entity or str(entity.get('processing_state') or '').strip().lower() == 'deleted' or is_video_file(filename):
            skipped += 1
            continue
        try:
            _enqueue_processing_steps(user_id, filename, steps_to_run, force=force)
            # Same reasoning as admin_backfill_photos: without this, backend/both
            # mode would never reach ipworker for an already-uploaded photo the
            # user re-runs from the Tools page.
            _queue_ipwork_processing(user_id, filename, steps=steps_to_run)
            queued += 1
        except Exception:
            app.logger.exception('ipwork enqueue: failed to enqueue steps for %s/%s', user_id, filename)
            skipped += 1

    app.logger.info('ipwork enqueue: queued %d photos (steps=%s), skipped %d for user %s', queued, steps_to_run, skipped, user_id)
    return jsonify({
        'queued': queued,
        'skipped': skipped,
        'total': queued + skipped,
        'steps': steps_to_run,
    })


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
        result['errors'].append(f'Failed to list blobs: {exc}')
        return result

    # Step 2: find metadata rows with no backing blob
    try:
        user_meta_rows = list(metadata_table_client.query_entities(
            f"PartitionKey eq '{_escape_odata(user_id)}'",
            select=['PartitionKey', 'RowKey', 'anonymousImageId', 'deleted'],
        ))
    except Exception as exc:
        result['errors'].append(f'Failed to query metadata: {exc}')
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
                    result['errors'].append(f'metadata delete failed for {filename}: {exc}')

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
            result['errors'].append(f'Failed to count orphaned face rows: {exc}')
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
            result['errors'].append(f'Face/person cleanup failed: {exc}')

    return result


@app.route('/api/admin/photos/purge-orphaned-data', methods=['POST'])
@app.route('/admin/photos/purge-orphaned-data', methods=['POST'])
def admin_purge_orphaned_photo_data():
    """Purge metadata, face rows, and person records for photos whose image blob
    no longer exists. Uses the images blob container as the source of truth.

    Requires ``repair: true, confirm: 'PURGE_ORPHANED_PHOTO_DATA'`` in the body
    to execute; omit ``confirm`` (or pass ``dryRun: true``) to preview only."""
    user_id, error = _require_user_id()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    dry_run = _coerce_bool(data.get('dryRun', True))
    if not dry_run and (
        data.get('repair') is not True
        or data.get('confirm') != 'PURGE_ORPHANED_PHOTO_DATA'
    ):
        return jsonify({'error': 'repair confirmation required', 'code': 'protected_repair_required'}), 403
    result = _purge_orphaned_photo_data(user_id, dry_run=dry_run)
    return jsonify(result)


def _maybe_enqueue_coalesced_rerun(job_id: Optional[str], user_id: str) -> None:
    """After a clustering job reaches a terminal state, check whether it was
    flagged (via _mark_clustering_job_rerun_requested) while it ran and, if
    so, fire exactly one follow-up job to pick up whatever queued up meanwhile.
    """
    if not job_id or metadata_table_client is None:
        return
    try:
        row = metadata_table_client.get_entity(partition_key='jobs', row_key=_job_row_key(job_id))
    except Exception:
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
                _upsert_job_status(job_id, user_id, PREVIEW_JOB_TYPE, 'failed', error=str(exc), filename=filename)
        return

    if job_type == 'library_clean':
        target_library_id = str(payload.get('libraryId') or user_id)
        if job_id:
            _upsert_job_status(job_id, user_id, 'library_clean', 'running', libraryId=target_library_id)
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
                library_store.set_cleanup_failed(target_library_id, str(exc))
            if job_id:
                _upsert_job_status(job_id, user_id, 'library_clean', 'failed', error=str(exc), libraryId=target_library_id)
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
                _upsert_job_status(job_id, user_id, 'library_download', 'failed', error=str(exc), libraryId=target_library_id)
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
                    _upsert_job_status(job_id, user_id, 'clustering', 'failed', error=str(exc))
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
        processed_any = False
        try:
            messages = list(queue_client.receive_messages(
                messages_per_page=1,
                max_messages=1,
                visibility_timeout=CLUSTERING_WORKER_VISIBILITY_TIMEOUT_SECONDS,
            ))
            for message in messages:
                processed_any = True
                payload = {}
                job_id = ''
                user_id = ''
                job_type = ''

                dequeue_count = int(getattr(message, 'dequeue_count', 0) or 0)
                if dequeue_count > CLUSTERING_WORKER_MAX_RETRIES:
                    try:
                        payload = json.loads(message.content or '{}')
                        if isinstance(payload, dict):
                            job_id = str(payload.get('jobId') or payload.get('correlationId') or '').strip()
                            user_id = str(payload.get('user_id') or payload.get('userId') or '').strip()
                            job_type = str(payload.get('type') or '').strip()
                    except Exception:
                        pass
                    if job_id and user_id:
                        try:
                            _upsert_job_status(
                                job_id, user_id, job_type or 'clustering', 'failed',
                                error=(
                                    f'Exceeded max retries ({CLUSTERING_WORKER_MAX_RETRIES}); '
                                    f'redelivered {dequeue_count} times without completing. '
                                    'Retry manually if this job is still wanted.'
                                ),
                            )
                        except Exception:
                            pass
                    worker_logger.warning(
                        'Dropping clustering queue message after %s dequeues (max %s), job_id=%s',
                        dequeue_count, CLUSTERING_WORKER_MAX_RETRIES, job_id,
                    )
                    try:
                        queue_client.delete_message(message)
                    except Exception:
                        worker_logger.exception('Failed to delete clustering queue message exceeding max retries')
                    continue

                # Keep this message's lease alive for as long as we're actively
                # working it, no matter how long that takes -- see
                # CLUSTERING_WORKER_VISIBILITY_TIMEOUT_SECONDS's comment above.
                # update_message() returns a new message object with a fresh
                # pop receipt each time, which the eventual delete_message
                # must use -- hence the lock-guarded holder rather than
                # reusing the original `message` variable directly.
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
                            # Transient renewal failures shouldn't abort the job --
                            # if the message is genuinely gone the next attempt just
                            # fails harmlessly again until stop_renewal is set below.
                            worker_logger.exception('Failed to renew clustering queue message lease')

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
                            _upsert_job_status(job_id, user_id, 'clustering', 'failed', error=str(exc))
                        except Exception:
                            pass
                    worker_logger.exception('Failed to process clustering queue message')
                finally:
                    stop_renewal.set()
                    renewal_thread.join(timeout=5)
                    with message_lock:
                        final_message = message_holder[0]
                    try:
                        queue_client.delete_message(final_message)
                    except Exception:
                        worker_logger.exception('Failed to delete clustering queue message')
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
            client_processing[step] = _failure_shape(step, str(exc))
            continue
        step_started = time.monotonic()
        try:
            result = processor(user_id, filename, image_bytes)
            client_processing[step] = result if isinstance(result, dict) else _failure_shape(step, 'invalid_result_shape')
        except Exception as exc:
            worker_logger.exception('ipworker step %r failed for %s/%s', step, user_id, filename)
            client_processing[step] = _failure_shape(step, str(exc))
        finally:
            step_ms[step] = round((time.monotonic() - step_started) * 1000)
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
    lease_statuses = lease.get('statuses') or {}
    runnable_steps = [
        step for step in steps
        if str(lease_statuses.get(f'{step}Status') or '').strip().lower() not in {'done', 'no_data', 'skipped', 'unsupported'}
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
                _upsert_job_status(job_id, user_id, 'ipwork', 'failed', error=str(exc))
            except Exception:
                pass
        worker_logger.exception('Failed to process ipwork queue message')
        outcome = 'done'  # a real processing error, not a race -- don't retry-loop it
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
                        # job_id internally.
                        worker_logger.exception('ipwork worker task raised unexpectedly')
                        outcome = 'done'
                    _log_ipwork_memory_sample(len(in_flight))
                    # Same lease_busy-vs-delete logic as before, just per
                    # completed future instead of per loop iteration; the
                    # actual delete_message call stays on the main thread
                    # (as does receive_messages above) so there's no
                    # question about QueueClient thread-safety for either.
                    if outcome == 'lease_busy' and int(getattr(message, 'dequeue_count', 0) or 0) < IPWORK_LEASE_RETRY_LIMIT:
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
        metadata = _get_metadata_entity(owner_id, name) if owner_id else {}
        urls = _public_photo_urls(token, name, blob_name=_blob_name_from_metadata(metadata, name))
        photos.append({
            'filename': name,
            'url': urls['url'],
            'thumbnailUrl': urls['thumbnailUrl'],
            'previewUrl': urls.get('previewUrl') or '',
            'rotation': _normalize_rotation((metadata or {}).get('rotation', 0)),
            'thumbnailRotation': _thumbnail_rotation_from_metadata(metadata),
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
    safe_name = _validate_media_filename(filename)
    if not safe_name:
        return jsonify({'error': 'Invalid filename'}), 400
    if safe_name not in _album_filenames(entity):
        return jsonify({'error': 'Not found'}), 404

    if not blob_service_client:
        return jsonify({'error': 'Thumbnail service not configured'}), 503

    # Resolve the physical blob (anonymous UUID for anonymized photos) for the
    # album owner. The thumbnail blob shares the image's anonymous id.
    owner_id = str(entity.get('PartitionKey') or '')
    blob_name_to_serve = resolve_physical_blob_name(owner_id, safe_name, 'image') if owner_id else safe_name

    try:
        props = get_media_properties('thumbnail', blob_name_to_serve)
        content_type = props.get('content_type') or 'image/jpeg'
        return _stream_media_response(
            'thumbnail',
            blob_name_to_serve,
            content_type=content_type,
            cache_control='public, max-age=3600',
            content_length=props.get('size'),
        )
    except Exception as exc:
        if _is_missing_media_error(exc):
            resp = Response(placeholder_bytes, mimetype='image/jpeg')
            resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
            return resp
        print(f"Unexpected error serving public thumbnail for {safe_name}: {str(exc)}", flush=True)
        return jsonify({'error': 'Thumbnail not found'}), 404


@app.route('/public/photos/<token>/image/<path:filename>', methods=['GET'])
def public_image(token: str, filename: str):
    entity = _find_public_album_by_token(token)
    if not entity or not _coerce_bool(entity.get('isPublic', False)) or _album_is_expired(entity):
        return jsonify({'error': 'Not found'}), 404
    if not _album_grant_valid(entity, token):
        return jsonify({'error': 'Not found'}), 404
    safe_name = _validate_media_filename(filename)
    if not safe_name:
        return jsonify({'error': 'Invalid filename'}), 400
    if safe_name not in _album_filenames(entity):
        return jsonify({'error': 'Not found'}), 404

    owner_id = str(entity.get('PartitionKey') or '')
    blob_name_to_serve = resolve_physical_blob_name(owner_id, safe_name, 'image') if owner_id else safe_name

    try:
        try:
            props = get_media_properties('image', blob_name_to_serve)
            content_type = props.get('content_type') or 'image/jpeg'
            content_length = props.get('size')
        except Exception:
            content_type = 'image/jpeg'
            content_length = None
        return _stream_media_response(
            'image',
            blob_name_to_serve,
            content_type=content_type,
            cache_control='public, max-age=3600',
            content_length=content_length,
            download_filename=safe_name,
        )
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
    safe_name = _validate_media_filename(filename)
    if not safe_name:
        return jsonify({'error': 'Invalid filename'}), 400
    if safe_name not in _album_filenames(entity):
        return jsonify({'error': 'Not found'}), 404

    if _filename_requires_backend_preview(safe_name):
        owner_id = str(entity.get('PartitionKey') or '')
        cached_blob_name = resolve_physical_blob_name(owner_id, safe_name, 'image') if owner_id else safe_name
        try:
            cached = _stream_cached_preview(safe_name, cache_control='public, max-age=3600', blob_name=cached_blob_name)
        except Exception:
            app.logger.exception('Failed to stream cached public preview for %s', safe_name)
            cached = None
        if cached is not None:
            return cached
        queued = _enqueue_preview_generation_job(owner_id, safe_name) if owner_id else {'status': 'failed'}
        if queued.get('status') in {'queued', 'already_queued'}:
            return jsonify({
                'error': 'Preview is being prepared',
                'reason': 'preview_queued',
                'detail': 'The server queued a background preview build for this file. Try again shortly.',
            }), 503
        return jsonify({
            'error': 'Preview not available yet',
            'reason': 'preview_unavailable',
            'detail': 'Preview generation is unavailable right now. Please try again later.',
        }), 503

    try:
        owner_id = str(entity.get('PartitionKey') or '')
        blob_name_to_read = resolve_physical_blob_name(owner_id, safe_name, 'image') if owner_id else safe_name
        image_bytes = download_media_bytes('image', blob_name_to_read)
        preview_bytes = convert_image_to_jpeg(image_bytes, safe_name)
        if not preview_bytes or (_filename_requires_backend_preview(safe_name) and not _looks_like_jpeg(preview_bytes)):
            return jsonify(_preview_failure_payload(safe_name)), 422
        resp = Response(preview_bytes, mimetype='image/jpeg')
        resp.headers['Cache-Control'] = 'public, max-age=3600'
        return resp
    except Exception as exc:
        if _is_missing_media_error(exc):
            return jsonify({'error': 'File not found in storage', 'reason': 'missing'}), 404
        app.logger.exception('Failed to create public preview for %s', safe_name)
        return jsonify({
            'error': 'Failed to create preview',
            'reason': 'server_error',
            'detail': 'The server hit an error while building this preview.',
        }), 503


def _remove_file_quietly(path: str) -> None:
    """Best-effort delete of a temp file; never raise from cleanup paths."""
    try:
        os.remove(path)
    except OSError:
        pass


@app.route('/public/albums/<token>/download-check', methods=['GET'])
def public_album_download_check(token: str):
    """Cheap reachability/validity probe the frontend calls before submitting
    the real download form. The form POST (below) can't be driven through
    httpClient — a browser form submission gives no programmatic success/
    failure signal — so this lets a dead backend or an expired/removed album
    be caught and shown to the user instead of silently opening a blank tab.
    Deliberately does not re-validate the access code (the album page load
    already did, and the real download POST re-checks it via the grant
    cookie): this only needs to answer "is there something to download",
    which doesn't require credentialed cross-origin CORS.
    """
    entity = _find_public_album_by_token(token)
    if not entity or not _coerce_bool(entity.get('isPublic', False)) or _album_is_expired(entity):
        return jsonify({'error': 'Not found'}), 404
    return jsonify({'ok': True})


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

    # Build the archive on a temp file on disk rather than in a BytesIO. A whole
    # album buffered in RAM (and previously copied a second time for the
    # Response body) could exceed the container's memory limit and OOM-kill the
    # replica. Spooling to disk keeps peak memory to roughly one photo at a time
    # (the bytes returned by download_media_bytes), and the response is streamed
    # straight off disk, then the temp file is removed once the stream drains.
    tmp = tempfile.NamedTemporaryFile(prefix='album-', suffix='.zip', delete=False)
    tmp_path = tmp.name
    written_count = 0
    try:
        owner_id = str(entity.get('PartitionKey') or '')
        with zipfile.ZipFile(tmp, 'w', compression=zipfile.ZIP_DEFLATED) as zip_file:
            for name in selected:
                try:
                    # Read from the physical (anonymous) blob, but keep the original
                    # filename as the entry name so users get familiar names.
                    blob_name = resolve_physical_blob_name(owner_id, name, 'image') if owner_id else name
                    data_bytes = download_media_bytes('image', blob_name)
                    zip_file.writestr(name, data_bytes)
                    written_count += 1
                except Exception as exc:
                    print(f"Skipping {name} while creating public album download: {str(exc)}", flush=True)
        tmp.close()
    except Exception:
        try:
            tmp.close()
        finally:
            _remove_file_quietly(tmp_path)
        raise

    if written_count == 0:
        _remove_file_quietly(tmp_path)
        return jsonify({'error': 'No files could be downloaded'}), 404

    zip_size = os.path.getsize(tmp_path)

    def _stream_and_cleanup():
        try:
            with open(tmp_path, 'rb') as fh:
                while True:
                    chunk = fh.read(256 * 1024)
                    if not chunk:
                        break
                    yield chunk
        finally:
            _remove_file_quietly(tmp_path)

    resp = Response(stream_with_context(_stream_and_cleanup()), mimetype='application/zip')
    resp.headers['Content-Length'] = str(zip_size)
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
        safe_name = _validate_media_filename(filename)
        if not safe_name:
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
        safe_name = _validate_media_filename(filename)
        if not safe_name:
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
            # len(liked_by) is the post-toggle count; metadata['likes'] held the
            # pre-update value (and raised KeyError → 500 on rows without it).
            'likes': len(liked_by),
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
        safe_name = _validate_media_filename(filename)
        if not safe_name:
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
        safe_name = _validate_media_filename(filename)
        if not safe_name:
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
                image_bytes = download_media_bytes('image', _blob_name_from_metadata(metadata, safe_name))
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

    capture_start, capture_end = _parse_capture_range_args()

    try:
        # Already sorted (rating/likes -> recency -> filename, stable across
        # loads) -- see _cached_sorted_metadata_rows_for_user. Filtering below
        # preserves that order, so no per-request re-sort is needed.
        all_photos = _cached_sorted_metadata_rows_for_user(user_id, purpose='photos.filter')
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

        selected = filtered[offset:offset + limit]
        pid_to_name, _ = _load_people_name_index(user_id)
        photos = _build_photo_summaries_page(
            user_id,
            [(photo['RowKey'], photo) for photo in selected],
            pid_to_name,
        )

        return jsonify({'photos': photos, 'total': len(filtered), 'offset': offset, 'limit': limit})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# Guard other optional startup helpers to avoid import-time failures
for _fn in ('create_blob_containers', 'create_metadata_table', 'create_albums_table', 'create_face_table', 'create_person_table', 'create_merge_table', 'create_image_names_table', 'create_hash_index_table', 'create_filename_owners_table'):
    if _fn in globals() and callable(globals().get(_fn)):
        try:
            globals().get(_fn)()
        except Exception:
            # Ignore errors during optional startup actions
            pass
