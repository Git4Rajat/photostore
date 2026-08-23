import React, { useEffect, useMemo, useState, useCallback, useRef } from 'react';
import { ArrowDownTrayIcon, ArrowPathIcon, ArrowUturnLeftIcon, AdjustmentsHorizontalIcon, CalendarDaysIcon, CheckIcon, ChevronDownIcon, ClockIcon, FunnelIcon, MagnifyingGlassIcon, PhotoIcon, PlusIcon, Squares2X2Icon, TrashIcon, VideoCameraIcon, XMarkIcon } from '@heroicons/react/24/outline';
import { HeartIcon as HeartSolidIcon, StarIcon as StarSolidIcon } from '@heroicons/react/24/solid';
import { Link, useLocation } from 'react-router-dom';
import { get, post } from '../services/apiClient';
import { isApiError } from '../services/apiError';
import { notifyApiError } from '../services/requestFeedback';
import { getActiveLibraryFromToken } from '../services/passwordAuthClient';
import {
    ARCFACE_EMBEDDING_DIMENSIONS,
    ARCFACE_EMBEDDING_VERSION,
    ARCFACE_MODEL_NAME,
    ARCFACE_MODEL_VERSION,
    ARCFACE_RUNTIME,
    computeArcFaceEmbedding,
    detectFaceLandmarks,
    preloadArcFaceEmbeddingModel,
    resetArcFaceEmbeddingModelLoadStateForTests,
} from '../services/arcFaceEmbeddingRuntime';
import { ARC_FACE_5POINT_TEMPLATE, solveSimilarityTransform } from '../services/faceAlignment';
import { loadFaceApiRuntimeBundle } from '../services/faceApiRuntime';
import { preloadYoloFaceModel, detectFacesWithYolo, resetYoloFaceModelStateForTests } from '../services/yoloFaceDetectionRuntime';
import { getFileExtension, isHeicFilename, isRawFilename, isVideoFilename } from '../utils/photoDisplay';
import { plural } from '../utils/format';
import { confirmDialog, promptDialog } from './shared/dialogs';
import { downloadPhotosAsZip } from '../utils/downloadPhotos';
import PhotoTile, { shouldFetchScopedThumbnail } from './shared/PhotoTile';
import { useDragSelect } from '../services/useDragSelect';
import { isAuthEnabled } from '../services/authClient';
import { resolveThumbnailAccessUrls } from '../services/thumbnailAccessCache';
import type { FileSystemFileHandle } from '../services/fileSystemAccess';
import PhotoQuickActions, { workbenchFilenameHref } from './shared/PhotoQuickActions';
import PhotoActionSheet from './shared/PhotoActionSheet';
import PhotoViewer from './shared/PhotoViewer';
import Timeline from './shared/Timeline';
import { EmptyState } from './shared/EmptyState';
import { Loading } from './shared/Loading';
import { ErrorState } from './shared/ErrorState';
import { useTimelineMetadata } from './TimelineMetadataProvider';
import type {
    BrowserAiLoadStage,
    BrowserAiModelCacheStatus,
    BrowserAiModelState as SharedBrowserAiModelState,
    BrowserAiNetworkGate,
    BrowserFaceDetection,
    BrowserFaceFailureStage,
    BrowserFaceDetectionResult,
    ClientProcessingReason,
    ClientProcessingReportItem,
    ClientProcessingResult,
    ClientProcessingSourceKind,
    FilterOptions,
    UploadProfile,
    ClientProcessingStatus,
    ClientProcessingStep,
    UploadProgress,
} from '../types/browserProcessing';
import type { Photo } from '../types/uiTypes';

export const UPLOAD_SESSION_STORAGE_KEY = 'photostore.upload.session.v1';
const UPLOAD_DB_NAME = 'photostore-upload-db';
const UPLOAD_DB_STORE = 'files';
export const PHOTO_CACHE_STORAGE_KEY = 'photostore.photo.cache.v1';
const PHOTO_CACHE_MAX_AGE_MS = 1000 * 60 * 30;
const PHOTO_LIST_REQUEST_TIMEOUT_MS = 15000;

// Discrete pinch-zoom density levels for the gallery grid. Index 0 is the
// default/comfortable tile size (matches the un-zoomed .gallery-grid CSS);
// each entry is a linear tile-size multiplier, not a tile-count multiplier —
// tile count scales with the square of linear size, so level 4's 0.36 works
// out to roughly 8x as many tiles visible per screen as level 0 (before the
// CSS min-tile-size floor kicks in on narrow phones). Deliberately a short,
// discrete list rather than continuous scaling: it keeps the CSS grid
// snapping to a few tuned presets instead of arbitrary in-between sizes, and
// caps how far a user can zoom out -- dense, but never Photos-app-style
// year/month bucketing.
const GALLERY_ZOOM_SCALES = [1, 0.75, 0.6, 0.47, 0.36] as const;
const GALLERY_ZOOM_MAX_LEVEL = GALLERY_ZOOM_SCALES.length - 1;
const GALLERY_ZOOM_STORAGE_KEY = 'photostore.gallery.zoomLevel.v1';
// Pinch distance ratio (relative to where the current level "snapped") that
// triggers stepping to the next/previous zoom level.
const GALLERY_PINCH_STEP_RATIO = 1.3;

const loadGalleryZoomLevel = (): number => {
    const raw = Number(localStorage.getItem(GALLERY_ZOOM_STORAGE_KEY));
    return Number.isInteger(raw) && raw >= 0 && raw <= GALLERY_ZOOM_MAX_LEVEL ? raw : 0;
};

// More visible tiles per screen at denser zoom means the infinite-scroll
// sentinel is reached sooner; scale how many photos we fetch per page so
// zooming out doesn't turn into a rapid string of small pagination requests.
const pageSizeForZoomLevel = (basePageSize: number, zoomLevel: number): number => {
    const scale = GALLERY_ZOOM_SCALES[zoomLevel] ?? 1;
    return Math.round(basePageSize / (scale * scale));
};
// How often to check whether any currently-loaded photo is mid-processing
// (queued/running on any step, or leased by ipworker) and, only then, refresh
// those specific photos' status -- see the processing-status poller effect.
// The gallery has no other push/poll path today for server-side processing
// finishing, so without this the "processing on server" tile icon would only
// ever update on the next unrelated refetch (reload/scroll/filter change).
const PROCESSING_STATUS_POLL_MS = 8000;
const PROCESSING_STEP_KEYS = ['thumbnail', 'exif', 'ocr', 'face', 'aiVision', 'mapDetection'] as const;
const PROCESSING_STATUS_MAX_FILENAMES = 100;

// A scale-to-zero backend can take up to a minute to answer the first request
// after inactivity. Those failures look like timeouts / network errors / 5xx
// gateway errors, so we retry them a few times behind a "waking up" message
// instead of dumping the raw axios error on the user.
const COLD_START_RETRY_PATTERN = /timeout|timed out|network error|econnaborted|err_network|failed to fetch|socket hang up|502|503|504|gateway|unavailable/i;
const COLD_START_MAX_RETRIES = 5;
const coldStartRetryDelayMs = (attempt: number) => Math.min(2000 * attempt, 8000);
const isColdStartError = (err: unknown): boolean => {
    if (typeof navigator !== 'undefined' && navigator.onLine === false) {
        return true;
    }
    // A typed ApiError already tells us the backend was never reached.
    if (isApiError(err)) {
        return err.kind === 'unreachable' || err.kind === 'timeout';
    }
    const message = typeof err === 'string'
        ? err
        : (err instanceof Error ? err.message : String((err as { message?: unknown })?.message ?? err ?? ''));
    return COLD_START_RETRY_PATTERN.test(message);
};

export const formatBytes = (bytes: number) => {
    if (!Number.isFinite(bytes) || bytes <= 0) {
        return '0 B';
    }
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    const exponent = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
    const value = bytes / Math.pow(1024, exponent);
    return `${value >= 10 || exponent === 0 ? value.toFixed(0) : value.toFixed(1)} ${units[exponent]}`;
};

export const formatMegabytesPerSecond = (value: number | undefined) => (
    `${Number(value || 0).toFixed(2)} MB/s`
);

export const UPLOAD_STOPPED_ERROR = 'upload_stopped_by_user';

export const isUploadStoppedError = (err: unknown) => (
    err instanceof Error && err.message === UPLOAD_STOPPED_ERROR
);

export const MB = 1024 * 1024;
export const DEFAULT_UPLOAD_PROFILE: UploadProfile = {
    fileParallelism: 3,
    chunkSizeBytes: 8 * MB,
    reason: 'standard connection',
};
export const MAX_BACKEND_UPLOAD_CHUNK_BYTES = 64 * MB;
export const CLIENT_PROCESSING_SCHEMA_VERSION = 2;
const CLIENT_PROCESSING_FINALIZE_GRACE_MS = 2000;
const CLIENT_MODEL_ACQUISITION_BUDGET_MS = 10000;
const CLIENT_MODEL_WARMUP_BUDGET_MS = 90000;
// Every network fetch() inside acquireBrowserAiModel is individually bounded by
// CLIENT_MODEL_ACQUISITION_BUDGET_MS/CLIENT_MODEL_WARMUP_BUDGET_MS, but the raw
// CacheStorage calls (caches.open/cache.match/cache.put/cache.delete) are not --
// Chrome has a known failure mode where caches.open() never resolves (corrupted
// Cache Storage backend, disk-full, post-crash profile state), which would hang
// this whole function forever with the UI frozen mid-stage and no error surfaced.
// This outer watchdog guarantees it always eventually settles. Sized above the
// worst-case sum of every inner budget (manifest + several asset fetches + face
// model + embedding model, each up to 10s, plus the 90s worker warmup) so it never
// cuts off a legitimately slow-but-progressing cold load.
const BROWSER_AI_MODEL_ACQUISITION_TIMEOUT_MS = 180000;
const CLIENT_BROWSER_STEP_BUDGET_MS = 10000;
const CLIENT_FACE_STEP_BUDGET_MS = 20000;
const FACE_DETECTION_SOFT_BUDGET_MS = Math.max(0, CLIENT_FACE_STEP_BUDGET_MS - 1500);
const CLIENT_AI_INFERENCE_BUDGET_MS = 120000;
const CLIENT_BATCH_AI_ADMISSION_BUDGET_MS = 180000;
const CLIENT_AI_MAX_MEGAPIXELS = 16;
const CLIENT_AI_MAX_STORED_LABELS = 160;
const CLIENT_THUMBNAIL_SIZE = 120;
const CLIENT_RAW_PREVIEW_SCAN_CHUNK_BYTES = 4 * MB;
const CLIENT_RAW_PREVIEW_SCAN_YIELD_BYTES = 16 * MB;
const CLIENT_RAW_PREVIEW_SCAN_BUDGET_MS = 45000;
const CLIENT_RAW_EXIF_SCAN_MAX_BYTES = 16 * MB;
const RAW_PARSER_VERSION = 'raw-preview-exif-scan-v2';
export const MAX_FINALIZE_RETRIES = 8;
const BROWSER_AI_MODEL_MANIFEST_URL = '/models/browser-ai/manifest.json';
const BROWSER_AI_MODEL_CACHE = 'photostore.browser-ai.models.v4';
const BLAZE_FACE_MODEL_URL = '/models/browser-ai/models/blazeface/model.json';
const LEGACY_BLAZE_FACE_MODEL_URL = '/models/blazeface/model.json';
const CANVAS_2D_READBACK_OPTIONS: CanvasRenderingContext2DSettings = { willReadFrequently: true };
let tensorFlowRuntimePromise: Promise<any> | null = null;
let blazeFaceLoadPromise: Promise<any> | null = null;
const configuredTensorFlowCanvasReadbackTargets = new WeakSet<object>();

const getCanvasReadbackContext = (canvas: HTMLCanvasElement): CanvasRenderingContext2D | null => (
    canvas.getContext('2d', CANVAS_2D_READBACK_OPTIONS)
);

const configureTensorFlowCanvasReadback = (runtime: any) => {
    if (!runtime || typeof runtime !== 'object') {
        return;
    }
    let tf: any;
    try {
        tf = runtime && 'tf' in runtime ? runtime.tf : runtime;
    } catch {
        return;
    }
    if (!tf || typeof tf !== 'object' || configuredTensorFlowCanvasReadbackTargets.has(tf)) {
        return;
    }
    const env = typeof tf?.env === 'function' ? tf.env() : null;
    if (!env || typeof env.set !== 'function') {
        return;
    }
    try {
        env.set('CANVAS2D_WILL_READ_FREQUENTLY_FOR_GPU', true);
        configuredTensorFlowCanvasReadbackTargets.add(tf);
    } catch {
        // Some TensorFlow.js builds lock flags after backend initialization.
    }
};

const normalizeBlazeFaceModelUrl = (rawUrl: string | undefined | null): string => {
    const trimmedUrl = String(rawUrl || '').trim();
    const resolvedUrl = trimmedUrl || BLAZE_FACE_MODEL_URL;
    const canonicalUrl = resolvedUrl.endsWith('.json')
        ? resolvedUrl
        : `${resolvedUrl.replace(/\/$/, '')}/model.json`;
    try {
        const parsedUrl = new URL(canonicalUrl, window.location.origin);
        if (parsedUrl.pathname === LEGACY_BLAZE_FACE_MODEL_URL || parsedUrl.pathname.startsWith('/models/blazeface/')) {
            return BLAZE_FACE_MODEL_URL;
        }
    } catch {
        if (canonicalUrl === LEGACY_BLAZE_FACE_MODEL_URL || canonicalUrl.startsWith('/models/blazeface/')) {
            return BLAZE_FACE_MODEL_URL;
        }
    }
    return canonicalUrl;
};

const loadTensorFlowRuntime = async (): Promise<any> => {
    if (!tensorFlowRuntimePromise) {
        tensorFlowRuntimePromise = (async () => {
            const tf = await import('@tensorflow/tfjs-core');
            await import('@tensorflow/tfjs-core/dist/public/chained_ops/register_all_chained_ops');
            configureTensorFlowCanvasReadback(tf);
            try {
                await import('@tensorflow/tfjs-backend-cpu');
                await import('@tensorflow/tfjs-backend-webgl');
                if (typeof tf.setBackend === 'function') {
                    const preferredBackends = ['cpu', 'webgl'];
                    let backendConfigured = false;
                    for (const backend of preferredBackends) {
                        try {
                            await tf.setBackend(backend);
                            if (typeof tf.ready === 'function') {
                                await tf.ready();
                            }
                            backendConfigured = true;
                            break;
                        } catch {
                            // Try the next backend. Some browsers do not support CPU or WebGL.
                        }
                    }
                    if (!backendConfigured) {
                        // Keep going; later model loads surface a concrete failure.
                    }
                }
            } catch {
                // If a backend cannot be registered, later loads will surface a concrete failure.
            }
            return tf;
        })().catch((err) => {
            tensorFlowRuntimePromise = null;
            throw err;
        });
    }
    return await tensorFlowRuntimePromise;
};

const loadBlazeFaceModel = async (): Promise<any> => {
    if (!blazeFaceLoadPromise) {
        blazeFaceLoadPromise = (async () => {
            try {
                // YOLOv8n-face (onnxruntime-web) replaced tfjs BlazeFace, which
                // false-positived on textured backgrounds. The historical
                // blazeFace* names are kept to avoid churn across the pipeline;
                // the returned sentinel just signals "detector ready".
                const runtimeConfig = getRuntimeConfig();
                await preloadYoloFaceModel({ wasmPath: runtimeConfig.arcFaceWasmPath });
                return { detector: 'yolov8n-face' };
            } catch (err) {
                const detail = err instanceof Error ? err.message : String(err || 'module_load_failed');
                const unavailableError = new FaceDetectionUnavailableError('model_load_failed', `yolo_load_failed: ${detail}`);
                (unavailableError as any).faceFailureStage = 'model_load_failed';
                (unavailableError as any).faceFailureDetail = detail;
                throw unavailableError;
            }
        })().catch((err) => {
            blazeFaceLoadPromise = null;
            throw err;
        });
    }
    return await blazeFaceLoadPromise;
};

export const preloadNativeFaceModels = () => {
    if (typeof window === 'undefined') {
        return;
    }
    const runtimeConfig = getRuntimeConfig();
    void loadBlazeFaceModel().catch(() => undefined);
    void preloadArcFaceEmbeddingModel({
        modelUrl: runtimeConfig.arcFaceModelUrl,
        wasmPath: runtimeConfig.arcFaceWasmPath,
    }).catch(() => undefined);
};

export const resetBrowserFaceModelLoadStateForTests = () => {
    blazeFaceLoadPromise = null;
    resetYoloFaceModelStateForTests();
    resetArcFaceEmbeddingModelLoadStateForTests();
};

type AppRuntimeConfig = {
    browserGeocoderUrl?: string;
    browserGeocoderRateMs?: string | number;
    blazeFaceModelUrl?: string;
    arcFaceModelUrl?: string;
    arcFaceWasmPath?: string;
    processingMode?: 'browser' | 'backend' | 'both';
};

interface BrowserVisionSource {
    imageSource: Blob | File | null;
    sourceKind: ClientProcessingSourceKind;
    sourceFormat: string;
    rawParserVersion?: string;
    previewWidth?: number;
    previewHeight?: number;
    originalBytes: number;
    sourceBytes: number;
    skipReason?: ClientProcessingReason;
    isRaw: boolean;
    thumbnailOnly?: boolean;
}

interface ParsedGpsExif {
    exif: Record<string, string>;
    latitude?: string;
    longitude?: string;
    hasExif: boolean;
}

export type BrowserAiModelState = SharedBrowserAiModelState & {
    manifest?: BrowserAiManifest;
};

interface BrowserAiManifestAsset {
    url?: string;
    path?: string;
    bytes?: number;
    size?: number;
    sha256?: string;
}

interface BrowserAiManifest {
    manifestVersion?: string;
    version?: string;
    model?: string;
    faceModel?: string;
    faceTask?: string;
    faceEmbeddingTask?: string;
    faceEmbeddingModel?: string;
    faceEmbeddingModelVersion?: string;
    faceEmbeddingModelTaxonomyVersion?: string;
    faceEmbeddingDescriptorDimensions?: number;
    faceEmbeddingModelUrl?: string;
    modelVersion?: string;
    modelTaxonomyVersion?: string;
    runtime?: string;
    task?: string;
    workerUrl?: string;
    allowLocalModels?: boolean;
    allowRemoteModels?: boolean;
    localModelPath?: string;
    wasmPath?: string;
    topK?: number;
    minStoredLabels?: number;
    maxCandidateLabels?: number;
    scoreThreshold?: number;
    personScoreThreshold?: number;
    faceScoreThreshold?: number;
    assets?: BrowserAiManifestAsset[];
    models?: Array<{
        name?: string;
        version?: string;
        taxonomyVersion?: string;
        assets?: BrowserAiManifestAsset[];
    }>;
}

interface BrowserAiImagePayload {
    data: Uint8Array | Uint8ClampedArray;
    width: number;
    height: number;
    channels: 3 | 4;
}

class FaceDetectionUnavailableError extends Error {
    reason: ClientProcessingReason;

    constructor(reason: ClientProcessingReason, detail: string) {
        super(detail || reason);
        this.name = 'FaceDetectionUnavailableError';
        this.reason = reason;
    }
}

type FaceDetectionDebugStage =
    | 'model_load_started'
    | 'model_load_done'
    | 'detection_started'
    | 'detection_done'
    | 'embedding_model_load_started'
    | 'embedding_model_load_done'
    | 'landmark_detection_started'
    | 'landmark_detection_done'
    | 'crop_started'
    | 'crop_done'
    | 'descriptor_started'
    | 'descriptor_done'
    | 'dedupe_done'
    | 'timeout'
    | 'background_throttled';

const getFaceFailureStage = (
    err: unknown,
    debugStages: FaceDetectionDebugStage[] = [],
): BrowserFaceFailureStage => {
    if (err && typeof err === 'object') {
        const explicitStage = (err as any).faceFailureStage;
        if (typeof explicitStage === 'string' && explicitStage) {
            return explicitStage as BrowserFaceFailureStage;
        }
    }
    const detail = String(err instanceof Error ? err.message : err || '').toLowerCase();
    const stageSet = new Set(debugStages);
    if (detail.includes('background_throttled')) {
        return 'background_throttled';
    }
    if (detail.includes('unsupported_runtime')) {
        return 'unsupported_runtime';
    }
    if (detail.includes('blazeface_load_timeout') || detail.includes('model_budget_exceeded') || detail.includes('timeout')) {
        return 'timeout';
    }
    if (detail.includes('arcface_model_load_failed') || detail.includes('embedding_model_load_failed')) {
        return 'embedding_model_load_failed';
    }
    if (detail.includes('face_model_load_failed') || detail.includes('face_api_load_failed')) {
        return 'face_api_load_failed';
    }
    if (detail.includes('blazeface_load_failed') || detail.includes('module_import_failed')) {
        return 'model_load_failed';
    }
    if (stageSet.has('embedding_model_load_started') && !stageSet.has('embedding_model_load_done')) {
        return 'embedding_model_load_failed';
    }
    if (stageSet.has('descriptor_started') && !stageSet.has('descriptor_done')) {
        return 'descriptor_failed';
    }
    if (stageSet.has('detection_started') && !stageSet.has('detection_done')) {
        return 'detection_failed';
    }
    if (stageSet.has('model_load_started') && !stageSet.has('model_load_done')) {
        return 'model_load_failed';
    }
    return 'unknown';
};

const getFaceFailureDetail = (err: unknown): string => {
    if (err && typeof err === 'object') {
        const explicitDetail = (err as any).faceFailureDetail;
        if (typeof explicitDetail === 'string' && explicitDetail.trim()) {
            return explicitDetail.trim();
        }
    }
    if (err instanceof Error && err.message.trim()) {
        return err.message.trim();
    }
    return String(err || 'face_detection_failed');
};

const isRawFile = (file: File) => isRawFilename(file.name);
const isVideoFile = (file: File) => isVideoFilename(file.name);

const makeClientReport = (
    clientAssetId: string,
    step: ClientProcessingStep,
    status: ClientProcessingStatus,
    reason: ClientProcessingReason,
    startedAt: number,
    extra: Partial<ClientProcessingReportItem> = {},
): ClientProcessingReportItem => ({
    clientAssetId,
    step,
    status,
    reason,
    durationMs: Math.max(0, Math.round(performance.now() - startedAt)),
    ...extra,
});

const getSourceReportFields = (source: Pick<BrowserVisionSource, 'sourceKind' | 'sourceFormat' | 'rawParserVersion' | 'previewWidth' | 'previewHeight' | 'originalBytes' | 'sourceBytes'>): Partial<ClientProcessingReportItem> => ({
    sourceKind: source.sourceKind,
    sourceFormat: source.sourceFormat,
    rawParserVersion: source.rawParserVersion,
    previewWidth: source.previewWidth,
    previewHeight: source.previewHeight,
    originalBytes: source.originalBytes,
    sourceBytes: source.sourceBytes,
});

const blobToDataUrl = (blob: Blob): Promise<string> => new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ''));
    reader.onerror = () => reject(reader.error || new Error('Failed to read blob.'));
    reader.readAsDataURL(blob);
});

// Only surface the "Deleting X/Y…" progress line for larger selections; small
// deletes complete near-instantly and the line would just flash.
const CHUNK_DELETE_FEEDBACK_MIN = 50;
const MAX_FACE_DETECTION_SIDE = 1800;
const FACE_MIN_ACCEPT_CONFIDENCE = 0.28;
const FACE_RELIABLE_CONFIDENCE = 0.38;
const FACE_BLAZEFACE_LOAD_BUDGET_MS = CLIENT_MODEL_ACQUISITION_BUDGET_MS;

const normalizeQuarterTurnRotation = (value: unknown): 0 | 90 | 180 | 270 => {
    const rotation = Number(value || 0);
    if (!Number.isFinite(rotation)) {
        return 0;
    }
    const normalized = ((Math.round(rotation / 90) % 4) + 4) % 4;
    return (normalized * 90) as 0 | 90 | 180 | 270;
};

const rotateCanvasByQuarterTurns = (sourceCanvas: HTMLCanvasElement, rotationDegrees: number): HTMLCanvasElement => {
    const rotation = normalizeQuarterTurnRotation(rotationDegrees);
    if (!rotation) {
        return sourceCanvas;
    }
    const canvas = document.createElement('canvas');
    const sourceWidth = sourceCanvas.width;
    const sourceHeight = sourceCanvas.height;
    canvas.width = rotation === 90 || rotation === 270 ? sourceHeight : sourceWidth;
    canvas.height = rotation === 90 || rotation === 270 ? sourceWidth : sourceHeight;
    const context = getCanvasReadbackContext(canvas);
    if (!context) {
        return sourceCanvas;
    }
    switch (rotation) {
        case 90:
            context.transform(0, 1, -1, 0, sourceHeight, 0);
            break;
        case 180:
            context.transform(-1, 0, 0, -1, sourceWidth, sourceHeight);
            break;
        case 270:
            context.transform(0, -1, 1, 0, 0, sourceWidth);
            break;
        default:
            break;
    }
    context.drawImage(sourceCanvas, 0, 0);
    return canvas;
};

const remapFaceBoundingBoxAfterRotation = (
    bbox: BrowserFaceDetection['bbox'],
    sourceWidth: number,
    sourceHeight: number,
    rotationDegrees: number,
): BrowserFaceDetection['bbox'] => {
    const rotation = normalizeQuarterTurnRotation(rotationDegrees);
    const left = Number(bbox?.left || 0);
    const top = Number(bbox?.top || 0);
    const width = Number(bbox?.width || 0);
    const height = Number(bbox?.height || 0);

    const clampBox = (nextLeft: number, nextTop: number, nextWidth: number, nextHeight: number) => {
        const clampedLeft = Math.max(0, Math.min(nextLeft, sourceWidth));
        const clampedTop = Math.max(0, Math.min(nextTop, sourceHeight));
        return {
            left: clampedLeft,
            top: clampedTop,
            width: Math.max(0, Math.min(nextWidth, sourceWidth - clampedLeft)),
            height: Math.max(0, Math.min(nextHeight, sourceHeight - clampedTop)),
        };
    };

    switch (rotation) {
        case 90:
            return clampBox(
                top,
                sourceHeight - (left + width),
                height,
                width,
            );
        case 180:
            return clampBox(
                sourceWidth - (left + width),
                sourceHeight - (top + height),
                width,
                height,
            );
        case 270:
            return clampBox(
                sourceWidth - (top + height),
                left,
                height,
                width,
            );
        default:
            return clampBox(left, top, width, height);
    }
};

const createHeicDecodeWorker = () => new Worker(new URL('../workers/heicDecodeWorker.ts', import.meta.url), { type: 'module' });

// Chrome/Firefox have no native HEIC decoder, so createImageBitmap throws for
// every HEIC file -- decode it ourselves via a WASM build of libheif, run in a
// worker so a multi-megapixel iPhone photo doesn't block the main thread. This
// is only reached as a fallback (see createOrientedImageCanvas below), so
// browsers with native HEIC support (some Safari builds) never pay this cost.
const decodeHeicToCanvas = (source: Blob): Promise<HTMLCanvasElement> => new Promise((resolve, reject) => {
    let settled = false;
    let timer: number | undefined;
    const worker = createHeicDecodeWorker();
    const requestId = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    const finish = (err?: unknown, canvas?: HTMLCanvasElement) => {
        if (settled) {
            return;
        }
        settled = true;
        if (timer !== undefined) {
            window.clearTimeout(timer);
        }
        worker.terminate();
        if (err) {
            reject(err);
        } else if (canvas) {
            resolve(canvas);
        }
    };
    timer = window.setTimeout(() => finish(new Error('heic_decode_timeout')), CLIENT_BROWSER_STEP_BUDGET_MS);
    worker.onmessage = (event) => {
        const data = event.data || {};
        if (data.type !== 'heic-decode-result' || data.requestId !== requestId) {
            return;
        }
        if (data.ok !== true) {
            finish(new Error(String(data.reason || 'heic_decode_failed')));
            return;
        }
        try {
            const canvas = document.createElement('canvas');
            canvas.width = Math.max(1, data.width);
            canvas.height = Math.max(1, data.height);
            const context = getCanvasReadbackContext(canvas);
            if (!context) {
                finish(new Error('heic_canvas_unavailable'));
                return;
            }
            context.putImageData(new ImageData(new Uint8ClampedArray(data.buffer), data.width, data.height), 0, 0);
            finish(undefined, canvas);
        } catch (err) {
            finish(err);
        }
    };
    worker.onerror = (event) => {
        finish(new Error(event.message || (event as ErrorEvent).error?.message || 'heic_decode_failed'));
    };
    source.arrayBuffer()
        .then((buffer) => {
            worker.postMessage({ type: 'heic-decode', requestId, buffer }, [buffer]);
        })
        .catch((err) => finish(err));
});

const createOrientedImageCanvas = async (source: Blob | File): Promise<HTMLCanvasElement> => {
    // This used to request imageOrientation: 'none' to get raw pixels and then
    // re-apply that correction itself via a canvas transform matrix keyed on
    // the parsed Orientation value — but some browsers silently ignore 'none'
    // and hand back an already-oriented bitmap regardless, which made the
    // manual re-apply double-rotate portrait photos. Fixed at the time by
    // dropping the option entirely and trusting the browser's own default —
    // which was wrong in the other direction: per the WHATWG spec (and
    // confirmed live, 2026-08-01), the actual default is 'none' (raw pixels,
    // EXIF ignored), not 'from-image'. That silently broke every photo whose
    // EXIF Orientation needs real correction (e.g. 8/90°-rotated) while
    // masking the bug for Orientation=1 photos (no correction needed either
    // way) — face detection ran on sideways images and found nothing.
    // Explicitly requesting 'from-image' (rather than omitting the option, or
    // requesting 'none' and re-deriving it ourselves) is the one setting that
    // avoids both failure modes at once.
    // 'from-image' is a valid runtime value (WHATWG spec / all current
    // browsers) but TypeScript's DOM lib only types ImageOrientation as
    // 'none' | 'flipY' (a known lib gap, e.g. microsoft/TypeScript#53053) —
    // the cast below is for the stale type definition, not a real type error.
    try {
        const bitmap = await createImageBitmap(source, { imageOrientation: 'from-image' } as unknown as ImageBitmapOptions);
        try {
            const canvas = document.createElement('canvas');
            canvas.width = Math.max(1, bitmap.width);
            canvas.height = Math.max(1, bitmap.height);
            const context = getCanvasReadbackContext(canvas);
            if (context) {
                context.drawImage(bitmap, 0, 0);
            }
            return canvas;
        } finally {
            bitmap.close?.();
        }
    } catch (err) {
        if (!(source instanceof File) || !isHeicFilename(source.name)) {
            throw err;
        }
        return decodeHeicToCanvas(source);
    }
};

const resizeCanvasToMaxSide = (sourceCanvas: HTMLCanvasElement, maxSide: number): HTMLCanvasElement => {
    const canvas = document.createElement('canvas');
    const scale = Math.min(1, maxSide / Math.max(sourceCanvas.width, sourceCanvas.height, 1));
    canvas.width = Math.max(1, Math.round(sourceCanvas.width * scale));
    canvas.height = Math.max(1, Math.round(sourceCanvas.height * scale));
    const context = getCanvasReadbackContext(canvas);
    if (context) {
        context.drawImage(sourceCanvas, 0, 0, canvas.width, canvas.height);
    }
    return canvas;
};

const normalizeFaceEmbedding = (embedding: unknown): number[] | undefined => {
    if (!embedding || typeof embedding !== 'object' || typeof (embedding as ArrayLike<number>).length !== 'number') {
        return undefined;
    }
    const normalized = Array.from(embedding as ArrayLike<number>)
        .map((value) => Number(value))
        .filter((value) => Number.isFinite(value));
    return normalized.length > 0 ? normalized.slice(0, ARCFACE_EMBEDDING_DIMENSIONS) : undefined;
};

const toArray = <T,>(value: unknown): T[] => (Array.isArray(value) ? value : []);

const firstFiniteNumber = (value: unknown): number | null => {
    if (typeof value === 'number') {
        return Number.isFinite(value) ? value : null;
    }
    if (value && typeof value === 'object' && typeof (value as ArrayLike<unknown>).length === 'number') {
        for (const item of Array.from(value as ArrayLike<unknown>)) {
            const numberValue = firstFiniteNumber(item);
            if (numberValue !== null) {
                return numberValue;
            }
        }
    }
    const coerced = Number(value);
    return Number.isFinite(coerced) ? coerced : null;
};

const clampDetectorConfidence = (value: unknown): number => {
    const confidence = firstFiniteNumber(value);
    return confidence === null ? 0 : Math.max(0, Math.min(confidence, 1));
};

const faceAreaRatio = (face: BrowserFaceDetection) => {
    const imageArea = Math.max(1, face.imageWidth * face.imageHeight);
    return (face.bbox.width * face.bbox.height) / imageArea;
};

const faceMaxSideRatio = (face: BrowserFaceDetection) => (
    Math.max(
        face.bbox.width / Math.max(1, face.imageWidth),
        face.bbox.height / Math.max(1, face.imageHeight),
    )
);

const faceCandidateScore = (face: BrowserFaceDetection) => (
    face.confidence
    - Math.max(0, faceAreaRatio(face) - 0.04) * 2
    - Math.max(0, faceMaxSideRatio(face) - 0.35)
);

const isLikelyFalsePositiveFace = (face: BrowserFaceDetection) => (
    face.confidence < FACE_MIN_ACCEPT_CONFIDENCE
    || (
        face.confidence < FACE_RELIABLE_CONFIDENCE
        && (faceAreaRatio(face) > 0.08 || faceMaxSideRatio(face) > 0.42)
    )
);

const faceBoxIou = (a: BrowserFaceDetection, b: BrowserFaceDetection) => {
    const ax1 = a.bbox.left;
    const ay1 = a.bbox.top;
    const ax2 = ax1 + a.bbox.width;
    const ay2 = ay1 + a.bbox.height;
    const bx1 = b.bbox.left;
    const by1 = b.bbox.top;
    const bx2 = bx1 + b.bbox.width;
    const by2 = by1 + b.bbox.height;
    const interLeft = Math.max(ax1, bx1);
    const interTop = Math.max(ay1, by1);
    const interRight = Math.min(ax2, bx2);
    const interBottom = Math.min(ay2, by2);
    const interArea = Math.max(0, interRight - interLeft) * Math.max(0, interBottom - interTop);
    const areaA = Math.max(0, ax2 - ax1) * Math.max(0, ay2 - ay1);
    const areaB = Math.max(0, bx2 - bx1) * Math.max(0, by2 - by1);
    const union = areaA + areaB - interArea;
    return union > 0 ? interArea / union : 0;
};

const dedupeFaceCandidates = (faces: BrowserFaceDetection[]): BrowserFaceDetection[] => {
    const sorted = [...faces]
        .filter((face) => !isLikelyFalsePositiveFace(face))
        .sort((a, b) => faceCandidateScore(b) - faceCandidateScore(a));
    const kept: BrowserFaceDetection[] = [];
    for (const face of sorted) {
        const duplicateIndex = kept.findIndex((existing) => faceBoxIou(existing, face) >= 0.35);
        if (duplicateIndex === -1) {
            kept.push(face);
            continue;
        }
        if (faceCandidateScore(face) > faceCandidateScore(kept[duplicateIndex])) {
            kept[duplicateIndex] = face;
        }
    }
    return kept.sort((a, b) => faceCandidateScore(b) - faceCandidateScore(a));
};

type FaceDetectionMetrics = {
    detectedFaceCount: number;
    candidateFaceCount: number;
    descriptorMissingCount: number;
    secondaryRejectedCount: number;
};

type FacePoint = { x: number; y: number };

const ARC_FACE_EMBEDDING_CANVAS_SIZE = 112;
// Solved scale = (template eye distance, ~35 units in the 112x112 template
// space) / (source eye distance, in pixels, from a 1.25x-padded FULL FACE
// BBOX crop -- not a tight eye-only crop). For real photos that source eye
// distance typically runs ~150-450px, so genuine scales cluster around
// 0.08-0.25 -- confirmed directly against 6 real production faces (all
// rejected by the previous 0.65-3.5 bounds, which were copied from the old
// 2-point path's *different* target-distance convention without
// re-deriving them for this crop convention; every real 5-point solve was
// being rejected as a false positive). Bounds below are generous around
// that real range, wide enough to still catch genuinely degenerate
// landmarks (e.g. a collapsed/zero eye distance) without rejecting normal
// crops.
const ARC_FACE_MIN_SCALE = 0.03;
// Reverted from a brief 0.9 experiment: raising this let through 5-point
// solves for extreme head-pitch cases, but real embedding testing (real
// ArcFace inference, not just checking the transform "looks plausible")
// showed 5-point alignment actively HURTS those cases -- forcing a severely
// pitched face into a frontal template distorts it more than leaving it
// alone. A confirmed-same-person face scored only 0.19-0.28 via 5-point vs
// 0.51-0.55 via the 2-point fallback below, with respective noise floors of
// -0.03-0.15 and 0.00-0.12 -- 5-point barely clears its own noise floor for
// this case, 2-point clears it with a wide margin. 0.6 is back to correctly
// rejecting these and falling through to 2-point instead.
const ARC_FACE_MAX_SCALE = 0.6;
// A real face's eye-line rarely tilts past this in a normally-held photo; a
// solved transform beyond it more likely reflects bad/mismatched landmarks
// than a genuinely tilted head, so treat it as a failed alignment.
const ARC_FACE_MAX_ROTATION_RADIANS = (60 * Math.PI) / 180;
const ARC_FACE_TARGET_EYE_X_RATIO = 0.50;
const ARC_FACE_TARGET_EYE_Y_RATIO = 0.38;
const ARC_FACE_TARGET_EYE_DISTANCE_RATIO = 0.36;

const clampNumber = (value: number, min: number, max: number) => (
    Math.max(min, Math.min(value, max))
);

type AlignmentMethod = 'landmark-5pt' | 'landmark-2pt' | 'none';

// Guards the 5-point similarity solve against being trusted blindly: noisy or
// mismatched landmarks (e.g. from a hard-angle/occluded face where face-api's
// landmark net still returns *something*) can solve to a technically-finite
// but wildly wrong scale/rotation, producing an aligned crop that's worse
// than an unaligned one. Reject those and let the caller fall back.
const isPlausibleSimilarityTransform = (transform: { a: number; b: number }): boolean => {
    const scale = Math.hypot(transform.a, transform.b);
    if (!Number.isFinite(scale) || scale < ARC_FACE_MIN_SCALE || scale > ARC_FACE_MAX_SCALE) {
        return false;
    }
    const rotation = Math.atan2(transform.b, transform.a);
    return Number.isFinite(rotation) && Math.abs(rotation) <= ARC_FACE_MAX_ROTATION_RADIANS;
};

// Shared padded-bbox-crop math used both to build the temp canvas fed to the
// landmark detector and (when no landmarks are available) as the final plain
// crop+resize fallback in cropFaceCanvas.
const computePaddedCropBounds = (
    bbox: BrowserFaceDetection['bbox'],
    paddingRatio: number,
    canvasWidth: number,
    canvasHeight: number,
): { cropLeft: number; cropTop: number; cropWidth: number; cropHeight: number } | null => {
    const left = Number(bbox?.left || 0);
    const top = Number(bbox?.top || 0);
    const width = Number(bbox?.width || 0);
    const height = Number(bbox?.height || 0);
    if (width <= 0 || height <= 0) {
        return null;
    }
    const padX = width * paddingRatio;
    const padY = height * paddingRatio;
    const cropLeft = Math.max(0, Math.floor(left - padX));
    const cropTop = Math.max(0, Math.floor(top - padY));
    const cropRight = Math.min(canvasWidth, Math.ceil(left + width + padX));
    const cropBottom = Math.min(canvasHeight, Math.ceil(top + height + padY));
    return {
        cropLeft,
        cropTop,
        cropWidth: Math.max(1, cropRight - cropLeft),
        cropHeight: Math.max(1, cropBottom - cropTop),
    };
};

const FIVE_POINT_LANDMARK_PADDING_RATIO = 0.25;
// dlib/face-api 68-point indexing: right eye 36-41, left eye 42-47, nose tip
// ~30, mouth corners 48 (left) and 54 (right).
const RIGHT_EYE_LANDMARK_INDICES = [36, 37, 38, 39, 40, 41];
const LEFT_EYE_LANDMARK_INDICES = [42, 43, 44, 45, 46, 47];

const meanLandmarkPoint = (positions: FacePoint[], indices: number[]): FacePoint => {
    const sum = indices.reduce(
        (acc, index) => ({ x: acc.x + (positions[index]?.x || 0), y: acc.y + (positions[index]?.y || 0) }),
        { x: 0, y: 0 },
    );
    return { x: sum.x / indices.length, y: sum.y / indices.length };
};

type LandmarkDetectionResult = {
    points: FacePoint[];
    // Set only when points is empty, so we can see *why* alignment fell back
    // to 'none' from stored face data instead of needing browser devtools —
    // every real-world run so far has produced alignmentMethod='none' for
    // 100% of faces with no visibility into the cause.
    failureReason?: string;
};

// Runs face-api's landmark net on a padded crop of the face bbox and returns
// the 5 canonical alignment points (rightEye, leftEye, nose, leftMouth,
// rightMouth) in SOURCE-canvas coordinates, or points: [] if detection is
// unavailable/fails — callers should treat that as "no alignment data" and
// fall back to a plain crop, not as an error.
const detectFiveFaceLandmarks = async (
    sourceCanvas: HTMLCanvasElement,
    bbox: BrowserFaceDetection['bbox'],
    embeddingOptions?: { modelUrl?: string; wasmPath?: string },
): Promise<LandmarkDetectionResult> => {
    const bounds = computePaddedCropBounds(bbox, FIVE_POINT_LANDMARK_PADDING_RATIO, sourceCanvas.width, sourceCanvas.height);
    if (!bounds) {
        return { points: [], failureReason: 'invalid_crop_bounds' };
    }
    const { cropLeft, cropTop, cropWidth, cropHeight } = bounds;
    const cropCanvas = document.createElement('canvas');
    cropCanvas.width = cropWidth;
    cropCanvas.height = cropHeight;
    const cropContext = getCanvasReadbackContext(cropCanvas);
    if (!cropContext) {
        return { points: [], failureReason: 'canvas_context_unavailable' };
    }
    cropContext.drawImage(sourceCanvas, cropLeft, cropTop, cropWidth, cropHeight, 0, 0, cropWidth, cropHeight);

    try {
        const positions = await detectFaceLandmarks(cropCanvas, embeddingOptions);
        if (!positions || positions.length < 68) {
            return { points: [], failureReason: 'no_landmarks_returned' };
        }
        const local: FacePoint[] = [
            meanLandmarkPoint(positions, RIGHT_EYE_LANDMARK_INDICES),
            meanLandmarkPoint(positions, LEFT_EYE_LANDMARK_INDICES),
            positions[30],
            positions[48],
            positions[54],
        ];
        return { points: local.map((point) => ({ x: point.x + cropLeft, y: point.y + cropTop })) };
    } catch (err) {
        const detail = err instanceof Error ? err.message : String(err || 'landmark_detection_threw');
        return { points: [], failureReason: detail.slice(0, 200) };
    }
};

const collectBlazeFaceCandidates = async (
    blazeFaceModel: any,
    detectionCanvas: HTMLCanvasElement,
    sourceCanvas: HTMLCanvasElement,
    imageWidth: number,
    imageHeight: number,
    offsetX = 0,
    offsetY = 0,
    scaleX = 1,
    scaleY = 1,
    debugStages?: FaceDetectionDebugStage[],
    metrics?: FaceDetectionMetrics,
    shouldAbort?: () => boolean,
    embeddingOptions?: { modelUrl?: string; wasmPath?: string },
    existingFaces: BrowserFaceDetection[] = [],
): Promise<BrowserFaceDetection[]> => {
    const yoloFaces = await detectFacesWithYolo(detectionCanvas, { wasmPath: embeddingOptions?.wasmPath });
    // Map YOLO boxes (canvas-pixel coords) into the shape the downstream crop
    // math expects. YOLOv8n-face itself has no landmarks; alignment landmarks
    // are detected separately per-face below via face-api's landmark net.
    const normalizedBlazeFaces = yoloFaces.map((face) => ({
        topLeft: [face.left, face.top],
        bottomRight: [face.left + face.width, face.top + face.height],
        probability: face.score,
    }));
    if (metrics) {
        metrics.detectedFaceCount += normalizedBlazeFaces.length;
    }
    debugStages?.push('detection_done');
    if (!normalizedBlazeFaces.length) {
        return [];
    }
    if (shouldAbort?.()) {
        return [];
    }
    debugStages?.push('embedding_model_load_started');
    await preloadArcFaceEmbeddingModel(embeddingOptions);
    debugStages?.push('embedding_model_load_done');
    const candidates: BrowserFaceDetection[] = [];
    let lastError: unknown = null;
    for (const face of normalizedBlazeFaces) {
        if (shouldAbort?.()) {
            break;
        }
        const left = Math.max(0, Number(face?.topLeft?.[0] ?? 0) * scaleX + offsetX);
        const top = Math.max(0, Number(face?.topLeft?.[1] ?? 0) * scaleY + offsetY);
        const right = Math.min(imageWidth, Number(face?.bottomRight?.[0] ?? 0) * scaleX + offsetX);
        const bottom = Math.min(imageHeight, Number(face?.bottomRight?.[1] ?? 0) * scaleY + offsetY);
        const width = Math.max(0, right - left);
        const height = Math.max(0, bottom - top);
        if (width <= 0 || height <= 0) {
            continue;
        }
        // Skip detections that overlap a face already accepted by an earlier pass, so the
        // additive tile search doesn't recrop/re-embed the same face across passes.
        if (existingFaces.length) {
            const candidateBox = { bbox: { left, top, width, height } } as BrowserFaceDetection;
            if (existingFaces.some((existing) => faceBoxIou(existing, candidateBox) >= 0.35)) {
                continue;
            }
        }
        debugStages?.push('landmark_detection_started');
        const landmarkResult = await detectFiveFaceLandmarks(sourceCanvas, { left, top, width, height }, embeddingOptions);
        const landmarks = landmarkResult.points;
        debugStages?.push('landmark_detection_done');
        debugStages?.push('crop_started');
        const cropResult = cropFaceCanvas(sourceCanvas, {
            bbox: { left, top, width, height },
        }, 0.25, landmarks);
        debugStages?.push('crop_done');
        if (!cropResult) {
            continue;
        }
        const { canvas: cropCanvas, alignmentMethod } = cropResult;
        const confidence = clampDetectorConfidence(face?.probability);
        try {
            debugStages?.push('descriptor_started');
            const descriptor = await computeArcFaceEmbedding(cropCanvas, embeddingOptions);
            debugStages?.push('descriptor_done');
            if (!descriptor) {
                if (metrics) {
                    metrics.descriptorMissingCount += 1;
                }
                continue;
            }
            const embedding = normalizeFaceEmbedding(descriptor);
            if (!embedding) {
                if (metrics) {
                    metrics.descriptorMissingCount += 1;
                }
                continue;
            }
            if (metrics) {
                metrics.candidateFaceCount += 1;
            }
            candidates.push({
                bbox: { left, top, width, height },
                confidence,
                imageWidth,
                imageHeight,
                embedding,
                detector: 'yolov8n-face',
                alignmentMethod,
                ...(alignmentMethod === 'none' && landmarkResult.failureReason
                    ? { alignmentFailureReason: landmarkResult.failureReason }
                    : {}),
            });
        } catch (err) {
            if (err && typeof err === 'object' && !(err as any).faceFailureStage) {
                (err as any).faceFailureStage = 'descriptor_failed';
            }
            if (err && typeof err === 'object' && !(err as any).faceFailureDetail) {
                (err as any).faceFailureDetail = getFaceFailureDetail(err);
            }
            lastError = err;
        }
        if (shouldAbort?.()) {
            break;
        }
    }
    if (candidates.length > 0) {
        return candidates;
    }
    if (lastError) {
        throw lastError;
    }
    return candidates;
};

type FaceCropResult = { canvas: HTMLCanvasElement; alignmentMethod: AlignmentMethod };

const cropFaceCanvas = (
    sourceCanvas: HTMLCanvasElement,
    face: { bbox: BrowserFaceDetection['bbox'] },
    paddingRatio = 0.25,
    landmarks: FacePoint[] = [],
): FaceCropResult | null => {
    const bounds = computePaddedCropBounds(face.bbox, paddingRatio, sourceCanvas.width, sourceCanvas.height);
    if (!bounds) {
        return null;
    }
    const { cropLeft, cropTop, cropWidth, cropHeight } = bounds;

    if (landmarks.length >= 5) {
        const transform = solveSimilarityTransform(landmarks.slice(0, 5), ARC_FACE_5POINT_TEMPLATE);
        if (transform && Number.isFinite(transform.tx) && Number.isFinite(transform.ty) && isPlausibleSimilarityTransform(transform)) {
            const canvas = document.createElement('canvas');
            canvas.width = ARC_FACE_EMBEDDING_CANVAS_SIZE;
            canvas.height = ARC_FACE_EMBEDDING_CANVAS_SIZE;
            const context = getCanvasReadbackContext(canvas);
            if (!context) {
                return null;
            }
            context.imageSmoothingEnabled = true;
            try {
                (context as CanvasRenderingContext2D & { imageSmoothingQuality?: ImageSmoothingQuality }).imageSmoothingQuality = 'high';
            } catch {
                // Older canvas implementations may not expose a writable smoothing quality.
            }
            context.fillStyle = '#000';
            context.fillRect(0, 0, canvas.width, canvas.height);
            context.setTransform(transform.a, transform.b, -transform.b, transform.a, transform.tx, transform.ty);
            context.drawImage(sourceCanvas, 0, 0);
            return { canvas, alignmentMethod: 'landmark-5pt' };
        }
    }
    // 2-point eye-only fallback, restored: for extreme head poses where the
    // 5-point solve is rejected, real ArcFace embedding testing (not just
    // "does the transform look plausible") showed this eyes-only alignment
    // produces MUCH better same-person matches than either forcing a
    // 5-point fit (which distorts a pitched face) or a plain unaligned crop
    // -- 0.51-0.55 cosine similarity across 3 confirmed-same-person photos,
    // comfortably clear of a 0.00-0.12 different-person noise floor. This is
    // a genuinely separate quality tier, not a fallback of last resort: the
    // backend clusters landmark-2pt faces in their own DBSCAN pass with a
    // separately-calibrated epsilon (PEOPLE_CLUSTER_EPS_2PT), and never
    // compares a 2pt embedding directly against a 5pt one -- that
    // cross-tier comparison was measured to be unreliable (same person,
    // 0.09-0.56, indistinguishable from noise).
    if (landmarks.length >= 2) {
        const rightEye = landmarks[0];
        const leftEye = landmarks[1];
        const eyeDx = leftEye.x - rightEye.x;
        const eyeDy = leftEye.y - rightEye.y;
        const eyeDistance = Math.hypot(eyeDx, eyeDy);
        if (Number.isFinite(eyeDistance) && eyeDistance > 1) {
            const canvas = document.createElement('canvas');
            canvas.width = ARC_FACE_EMBEDDING_CANVAS_SIZE;
            canvas.height = ARC_FACE_EMBEDDING_CANVAS_SIZE;
            const context = getCanvasReadbackContext(canvas);
            if (!context) {
                return null;
            }
            const angle = Math.atan2(eyeDy, eyeDx);
            const targetEyeX = canvas.width * ARC_FACE_TARGET_EYE_X_RATIO;
            const targetEyeY = canvas.height * ARC_FACE_TARGET_EYE_Y_RATIO;
            const targetEyeDistance = canvas.width * ARC_FACE_TARGET_EYE_DISTANCE_RATIO;
            const scale = clampNumber(targetEyeDistance / eyeDistance, ARC_FACE_MIN_SCALE, 3.5);
            context.imageSmoothingEnabled = true;
            try {
                (context as CanvasRenderingContext2D & { imageSmoothingQuality?: ImageSmoothingQuality }).imageSmoothingQuality = 'high';
            } catch {
                // Older canvas implementations may not expose a writable smoothing quality.
            }
            context.fillStyle = '#000';
            context.fillRect(0, 0, canvas.width, canvas.height);
            if (typeof context.translate === 'function' && typeof context.rotate === 'function' && typeof context.scale === 'function') {
                context.translate(targetEyeX, targetEyeY);
                context.rotate(-angle);
                context.scale(scale, scale);
                context.translate(-(rightEye.x + leftEye.x) / 2, -(rightEye.y + leftEye.y) / 2);
                context.drawImage(sourceCanvas, 0, 0);
                return { canvas, alignmentMethod: 'landmark-2pt' };
            }
        }
    }
    const canvas = document.createElement('canvas');
    canvas.width = ARC_FACE_EMBEDDING_CANVAS_SIZE;
    canvas.height = ARC_FACE_EMBEDDING_CANVAS_SIZE;
    const context = getCanvasReadbackContext(canvas);
    if (!context) {
        return null;
    }
    context.imageSmoothingEnabled = true;
    try {
        (context as CanvasRenderingContext2D & { imageSmoothingQuality?: ImageSmoothingQuality }).imageSmoothingQuality = 'high';
    } catch {
        // Older canvas implementations may not expose a writable smoothing quality.
    }
    context.drawImage(sourceCanvas, cropLeft, cropTop, cropWidth, cropHeight, 0, 0, canvas.width, canvas.height);
    return { canvas, alignmentMethod: 'none' };
};


const detectFacesWithEmbeddings = async (sourceCanvas: HTMLCanvasElement): Promise<BrowserFaceDetectionResult | null> => {
    const imageWidth = sourceCanvas.width;
    const imageHeight = sourceCanvas.height;
    const scaledCanvas = resizeCanvasToMaxSide(sourceCanvas, MAX_FACE_DETECTION_SIDE);
    const faceDetectionStartedAt = performance.now();
    const shouldAbortFaceDetection = () => (
        performance.now() - faceDetectionStartedAt >= FACE_DETECTION_SOFT_BUDGET_MS
    );
    let candidates: BrowserFaceDetection[] = [];
    let lastError: unknown = null;
    const debugStages: FaceDetectionDebugStage[] = ['model_load_started'];
    const metrics: FaceDetectionMetrics = {
        detectedFaceCount: 0,
        candidateFaceCount: 0,
        descriptorMissingCount: 0,
        secondaryRejectedCount: 0,
    };
    const runtimeConfig = getRuntimeConfig();
    const embeddingOptions = {
        modelUrl: runtimeConfig.arcFaceModelUrl,
        wasmPath: runtimeConfig.arcFaceWasmPath,
    };
    const blazeFaceModelPromise = withTimeout(loadBlazeFaceModel(), FACE_BLAZEFACE_LOAD_BUDGET_MS).catch((err) => {
        if (err && typeof err === 'object') {
            (err as any).debugStages = [...debugStages];
            if (!(err as any).faceFailureStage) {
                (err as any).faceFailureStage = getFaceFailureStage(err, debugStages);
            }
            if (!(err as any).faceFailureDetail) {
                (err as any).faceFailureDetail = getFaceFailureDetail(err);
            }
        }
        lastError = err;
        return null;
    });
    debugStages.push('model_load_done', 'detection_started');
    let blazeFaceModel: any = await blazeFaceModelPromise;
    if (!blazeFaceModel && !lastError) {
        const timeoutError = new FaceDetectionUnavailableError('model_load_failed', 'blazeface_load_timeout');
        (timeoutError as any).debugStages = [...debugStages];
        (timeoutError as any).faceFailureStage = 'timeout';
        (timeoutError as any).faceFailureDetail = 'blazeface_load_timeout';
        lastError = timeoutError;
    }
    if (blazeFaceModel) {
        try {
            candidates.push(...await collectBlazeFaceCandidates(
                blazeFaceModel,
                scaledCanvas,
                sourceCanvas,
                imageWidth,
                imageHeight,
                0,
                0,
                imageWidth / Math.max(1, scaledCanvas.width),
                imageHeight / Math.max(1, scaledCanvas.height),
                debugStages,
                metrics,
                shouldAbortFaceDetection,
                embeddingOptions,
            ));
        } catch (err) {
            if (err && typeof err === 'object') {
                (err as any).debugStages = Array.isArray((err as any).debugStages) ? (err as any).debugStages : [...debugStages];
                if (!(err as any).faceFailureStage) {
                    (err as any).faceFailureStage = getFaceFailureStage(err, debugStages);
                }
                if (!(err as any).faceFailureDetail) {
                    (err as any).faceFailureDetail = getFaceFailureDetail(err);
                }
            }
            lastError = err;
        }
    }

    // YOLOv8n-face detects every face on the full frame in one pass (it recovers
    // the small far faces that BlazeFace needed zoomed-in tile passes for), so
    // the additive tiling search is no longer needed.
    const faces = dedupeFaceCandidates(candidates);
    debugStages.push('dedupe_done');
    if (!faces.length && lastError && !shouldAbortFaceDetection()) {
        throw lastError;
    }
    const filteredFaceCount = Math.max(0, metrics.secondaryRejectedCount + candidates.length - faces.length);
    const filteredReason = !faces.length && metrics.detectedFaceCount > 0
        ? (metrics.candidateFaceCount > 0 || metrics.secondaryRejectedCount > 0 ? 'quality_filter_rejected' : 'descriptor_missing')
        : undefined;
    return {
        faces,
        model: `yolov8n-face+${ARCFACE_MODEL_NAME}`,
        modelVersion: ARCFACE_MODEL_VERSION,
        modelTaxonomyVersion: ARCFACE_EMBEDDING_VERSION,
        runtime: `browser-yolov8n-face+${ARCFACE_RUNTIME}`,
        source: 'native_tfjs',
        schemaVersion: 2,
        rawFaceCount: metrics.detectedFaceCount,
        detectedFaceCount: metrics.detectedFaceCount,
        candidateFaceCount: metrics.candidateFaceCount,
        filteredFaceCount,
        ...(filteredReason ? { filteredReason } : {}),
        debugStages,
    };
};

export const dataUrlToBlob = async (dataUrl: string): Promise<Blob> => {
    // Decoded locally rather than via fetch(dataUrl): fetching a data: URI is
    // treated as a connect-src-governed request, which the app's CSP blocks.
    const match = dataUrl.match(/^data:([^;,]*)(;base64)?,([\s\S]*)$/);
    if (!match) {
        throw new Error('Failed to convert thumbnail data URL to blob.');
    }
    const [, mimeType, isBase64, data] = match;
    const contentType = mimeType || 'application/octet-stream';
    if (isBase64) {
        const byteString = atob(data);
        const bytes = new Uint8Array(byteString.length);
        for (let i = 0; i < byteString.length; i += 1) {
            bytes[i] = byteString.charCodeAt(i);
        }
        return new Blob([bytes], { type: contentType });
    }
    return new Blob([decodeURIComponent(data)], { type: contentType });
};

export const readBlobArrayBuffer = (blob: Blob): Promise<ArrayBuffer> => {
    if (typeof blob.arrayBuffer === 'function') {
        return blob.arrayBuffer();
    }
    return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => {
            if (reader.result instanceof ArrayBuffer) {
                resolve(reader.result);
            } else {
                reject(new Error('Blob did not produce an ArrayBuffer.'));
            }
        };
        reader.onerror = () => reject(reader.error || new Error('Failed to read blob.'));
        reader.readAsArrayBuffer(blob);
    });
};

const withTimeout = async <T,>(promise: Promise<T>, timeoutMs: number): Promise<T | null> => {
    let timer: number | undefined;
    try {
        return await Promise.race([
            promise,
            new Promise<null>((resolve) => {
                timer = window.setTimeout(() => resolve(null), timeoutMs);
            }),
        ]);
    } finally {
        if (timer !== undefined) {
            window.clearTimeout(timer);
        }
    }
};

const withTimeoutOutcome = async <T,>(promise: Promise<T>, timeoutMs: number): Promise<
    { timedOut: true; value: null } | { timedOut: false; value: T }
> => {
    let timer: number | undefined;
    try {
        return await Promise.race([
            promise.then((value) => ({ timedOut: false, value } as const)),
            new Promise<{ timedOut: true; value: null }>((resolve) => {
                timer = window.setTimeout(() => resolve({ timedOut: true, value: null }), timeoutMs);
            }),
        ]);
    } finally {
        if (timer !== undefined) {
            window.clearTimeout(timer);
        }
    }
};

const yieldToBrowser = () => new Promise<void>((resolve) => window.setTimeout(resolve, 0));

export const sha256ArrayBuffer = async (buffer: ArrayBuffer): Promise<string> => {
    const hashBuffer = await crypto.subtle.digest('SHA-256', buffer);
    const hashArray = Array.from(new Uint8Array(hashBuffer));
    return hashArray.map((b) => b.toString(16).padStart(2, '0')).join('');
};

const blobSha256 = async (blob: Blob): Promise<string> => sha256ArrayBuffer(await readBlobArrayBuffer(blob));

const getRuntimeConfig = (): AppRuntimeConfig => {
    if (typeof window === 'undefined') {
        return {};
    }
    return (window as Window & { __APP_CONFIG__?: AppRuntimeConfig }).__APP_CONFIG__ || {};
};

let lastGeocodeAt = 0;
const geocodeWithThrottle = async (latitude: string, longitude: string): Promise<Record<string, string> | null> => {
    const config = getRuntimeConfig();
    const rateMs = Math.max(0, Number(config.browserGeocoderRateMs || 1100));
    const now = Date.now();
    const waitMs = Math.max(0, lastGeocodeAt + rateMs - now);
    if (waitMs > 0) {
        await new Promise((resolve) => window.setTimeout(resolve, waitMs));
    }
    lastGeocodeAt = Date.now();
    const baseUrl = String(config.browserGeocoderUrl || 'https://photon.komoot.io/reverse').replace(/\/$/, '');
    const url = `${baseUrl}?lat=${encodeURIComponent(latitude)}&lon=${encodeURIComponent(longitude)}&limit=1`;
    const response = await fetch(url, { credentials: 'omit' });
    if (!response.ok) {
        return null;
    }
    const data = await response.json() as any;
    const feature = Array.isArray(data?.features) ? data.features[0] : null;
    const props = feature?.properties || {};
    const coords = feature?.geometry?.coordinates;
    return {
        address: [props.name, props.street, props.housenumber].filter(Boolean).join(' ').trim(),
        city: props.city || props.town || props.village || '',
        country: props.country || '',
        latitude: String(coords?.[1] || latitude),
        longitude: String(coords?.[0] || longitude),
    };
};

// Total budget for the whole function (setup + both possible recognize()
// attempts combined), kept safely under the caller's CLIENT_BROWSER_STEP_BUDGET_MS
// (10s, wrapping the entire runBrowserOcr(...) call). A single fixed per-call
// timeout applied to each of the up-to-two sequential recognize() calls would
// let their worst case (2x that timeout) exceed the outer budget -- and once
// the *outer* withTimeout abandons a still-running runBrowserOcr mid-retry,
// its `finally` (and the worker.terminate() inside it) no longer fires on any
// predictable schedule the caller can rely on. Racing every recognize() call
// against a shared deadline instead guarantees runBrowserOcr always settles
// (and terminates its worker) before the outer budget gives up.
const OCR_TOTAL_BUDGET_MS = 8500;
// Skip the preprocessed retry if less than this much of the shared budget is
// left -- not enough time for a real attempt, so it would just dispatch a
// second worker job that immediately loses its own race.
const OCR_MIN_RETRY_BUDGET_MS = 1500;

const runBrowserOcrInner = async (source: Blob | File): Promise<string> => {
    // tesseract.js's loadImage() reads `.name` off any Blob/File source (to special-case
    // .pbm files); a plain Blob (e.g. from canvas.toBlob or File.slice, as used by the
    // RAW/HEIC preview paths) has no `.name` and throws. Always give it a named File.
    const toOcrSource = (blob: Blob | File): File => (
        blob instanceof File ? blob : new File([blob], 'ocr-input.jpg', { type: blob.type || 'image/jpeg' })
    );

    const preprocessBlob = async (blob: Blob): Promise<Blob | null> => {
        if (typeof document === 'undefined' || typeof createImageBitmap !== 'function') return null;
        let imgBitmap: ImageBitmap | null = null;
        try {
            imgBitmap = await createImageBitmap(blob);
            const canvas = document.createElement('canvas');
            canvas.width = imgBitmap.width;
            canvas.height = imgBitmap.height;
            const ctx = canvas.getContext('2d');
            if (!ctx) return null;
            ctx.drawImage(imgBitmap, 0, 0);
            // Simple contrast/brightness boost and convert to grayscale
            const imageData = ctx.getImageData(0, 0, canvas.width, canvas.height);
            const data = imageData.data;
            // Parameters tuned to improve screenshot readability
            const contrast = 1.2; // >1 increases contrast
            const brightness = 10; // add brightness
            for (let i = 0; i < data.length; i += 4) {
                // convert to grayscale
                const r = data[i];
                const g = data[i + 1];
                const b = data[i + 2];
                let gray = 0.299 * r + 0.587 * g + 0.114 * b;
                gray = (gray - 128) * contrast + 128 + brightness;
                gray = Math.max(0, Math.min(255, gray));
                data[i] = data[i + 1] = data[i + 2] = gray;
            }
            ctx.putImageData(imageData, 0, 0);
            return await new Promise<Blob | null>((resolve) => canvas.toBlob((b) => resolve(b), 'image/png', 0.9));
        } catch {
            return null;
        } finally {
            // finally, not inline after putImageData -- the `if (!ctx) return null`
            // path above skipped the close entirely before this change.
            imgBitmap?.close?.();
        }
    };

    // Measured from function entry, not after setup, so a slow cold-start
    // (worker spin-up, language-data fetch) counts against the same budget as
    // the recognize() calls -- otherwise setup time is "free" and the total
    // wall-clock time the outer caller sees can still exceed its own budget.
    const deadline = performance.now() + OCR_TOTAL_BUDGET_MS;
    const remainingBudgetMs = () => Math.max(0, deadline - performance.now());

    try {
        const tesseract = await import('../vendor/tesseract');
        const { createWorker } = tesseract as any;
        const worker = await createWorker();
        try {
            if (typeof worker.load === 'function') {
                await worker.load();
            }
            if (typeof worker.loadLanguage === 'function') {
                try { await worker.loadLanguage('eng'); } catch { /* ignore */ }
            }
            if (typeof worker.initialize === 'function') {
                try { await worker.initialize('eng'); } catch { /* ignore */ }
            }
            if (typeof worker.setParameters === 'function') {
                // PSM 11 (SPARSE_TEXT): look for scattered text anywhere in the image
                // instead of assuming a structured document layout. Real photos (signs,
                // screenshots, labels) routinely trip the default AUTO mode's layout
                // analysis into over-segmenting background texture into a huge tree of
                // spurious blocks/words/symbols, which can grow large enough that the
                // worker's postMessage back to the main thread throws a DataCloneError
                // (structured-clone OOM) instead of ever resolving.
                try { await worker.setParameters({ tessedit_pageseg_mode: '11' }); } catch { /* ignore */ }
            }

            // Try original image first
            let result: { data?: { text?: string } } | null = null;
            try {
                result = await withTimeout<{ data?: { text?: string } }>(worker.recognize(toOcrSource(source)), remainingBudgetMs());
            } catch {
                // fall through with result still null -- treated the same as a timeout below
            }
            let text = String(result?.data?.text || '').trim().slice(0, 2048);
            if (text) return text;
            if (!result || remainingBudgetMs() < OCR_MIN_RETRY_BUDGET_MS) {
                // Timed out / errored, or not enough of the shared budget left for a
                // real second attempt -- skip the retry and fall through to `finally`
                // to terminate the worker instead of leaking it.
                return '';
            }

            // If empty, attempt a preprocessed retry
            const blobSource = source instanceof Blob ? source : new Blob([await (source as File).arrayBuffer()], { type: (source as File).type || 'image/*' });
            const pre = await preprocessBlob(blobSource);
            if (pre && remainingBudgetMs() >= OCR_MIN_RETRY_BUDGET_MS) {
                try {
                    result = await withTimeout<{ data?: { text?: string } }>(worker.recognize(toOcrSource(pre)), remainingBudgetMs());
                    text = String(result?.data?.text || '').trim().slice(0, 2048);
                    if (text) return text;
                } catch {
                    // ignore retry errors
                }
            }

            return '';
        } finally {
            await worker.terminate?.();
        }
    } catch {
        return '';
    }
};

// Real-world crash (reproduced in a real headless Chrome, not theorized): firing
// a whole photo backlog's worth of OCR calls at once -- exactly what happens
// right after turning browser AI on -- spins up one dedicated Tesseract WASM
// worker per photo concurrently, and beyond a handful of simultaneous workers
// this reliably crashes with "Cannot read properties of null (reading
// 'postMessage')" inside tesseract.js's own internals (and, at higher
// concurrency, crashes the whole tab from memory pressure). A cap of 2 was
// clean across repeated bursts in headless Chrome, but a live Edge session
// under real load (concurrent failed fetches/retries competing for the same
// resources) still hit the same crash at that cap -- lowered to 1 for a wider
// safety margin, since real user hardware/browser/load conditions vary more
// than a synthetic benchmark can capture. Excess calls are skipped (return
// '') immediately rather than queued -- a queue would make late callers wait
// an unbounded time, which would itself blow past the outer per-step timeout
// budget (CLIENT_BROWSER_STEP_BUDGET_MS) during a real backlog burst. Skipped
// photos aren't lost: the existing pending-work mechanism retries incomplete
// steps later once capacity frees up.
const OCR_MAX_CONCURRENT_WORKERS = 1;
let ocrActiveWorkerCount = 0;

const runBrowserOcr = async (source: Blob | File): Promise<string> => {
    if (ocrActiveWorkerCount >= OCR_MAX_CONCURRENT_WORKERS) {
        return '';
    }
    ocrActiveWorkerCount += 1;
    try {
        return await runBrowserOcrInner(source);
    } finally {
        ocrActiveWorkerCount -= 1;
    }
};

// Safety net for the same underlying tesseract.js v2.1.5 architecture issue:
// its worker wrapper tracks in-flight jobs keyed only by action name ('recognize'),
// not per-call, and a stray, already-abandoned job's rejection can arrive *after*
// our own worker.terminate() already nulled its Worker reference -- surfacing as
// a genuinely uncaught "Cannot read properties of null (reading 'postMessage')"
// that isn't reachable from any try/catch we hold (nothing is still listening to
// that specific stray promise by the time it settles). The concurrency cap above
// minimizes how often this fires; this listener stops the residual case from
// showing up as an alarming console crash -- our own awaited OCR call has already
// resolved via its own timeout/catch handling well before this stray rejection
// arrives, so suppressing it changes nothing about the actual OCR outcome.
if (typeof window !== 'undefined') {
    window.addEventListener('unhandledrejection', (event) => {
        const reason = event.reason;
        const message = reason instanceof Error ? reason.message : String(reason || '');
        const stack = reason instanceof Error ? String(reason.stack || '') : '';
        if (message.includes("Cannot read properties of null (reading 'postMessage')") && stack.includes('recognize')) {
            event.preventDefault();
            console.debug('[ocr] suppressed a known stray tesseract.js worker rejection (harmless -- see runBrowserOcr comments)', message);
        }
    });
}

const resolveManifestUrl = (path: string) => new URL(path, window.location.origin).toString();

const resolveManifestAssetUrl = (manifestUrl: string, path: string) => new URL(path, manifestUrl).toString();

const makeModelReportFields = (modelState?: BrowserAiModelState): Partial<ClientProcessingReportItem> => ({
    model: modelState?.model || '',
    modelVersion: modelState?.modelVersion || '',
    modelTaxonomyVersion: modelState?.modelTaxonomyVersion || '',
    runtime: modelState?.runtime || 'browser-no-model-configured',
    modelAvailability: modelState?.modelAvailability || 'unavailable',
    modelCacheStatus: modelState?.modelCacheStatus || 'miss',
    modelManifestVersion: modelState?.modelManifestVersion,
    modelAcquisitionMs: modelState?.modelAcquisitionMs,
});

const isLocalVisionFallbackResult = (aiResult: Record<string, any>) => (
    String(aiResult?.fallbackReason || '').trim() === 'classifier_unavailable'
    || String(aiResult?.model || '').trim() === 'photostore-local-vision-fallback'
    || String(aiResult?.runtime || '').trim() === 'browser-worker/local-vision-heuristics'
);

const normalizeBrowserAiError = (err: unknown): { reason: ClientProcessingReason; detail: string } => {
    const detail = err instanceof Error ? err.message : String(err || 'model_load_failed');
    if (detail === 'inference_timeout' || detail === 'model_download_timeout' || detail === 'model_budget_exceeded') {
        return { reason: detail, detail };
    }
    if (detail.startsWith('model_budget_exceeded:')) {
        const phase = detail.split(':').slice(1).join(':') || 'unknown';
        return {
            reason: 'model_budget_exceeded',
            detail: `Model warm-up exceeded budget during ${phase}`,
        };
    }
    if (detail.includes('Unexpected token') || detail.includes('<!DOCTYPE')) {
        return {
            reason: 'model_unavailable',
            detail: 'Model files unavailable or returned HTML instead of model JSON',
        };
    }
    return { reason: 'model_load_failed', detail };
};

export const normalizeNativeFaceDetectionError = (err: unknown) => {
    if (err instanceof FaceDetectionUnavailableError) {
        if (err && typeof err === 'object' && !(err as any).faceFailureStage) {
            (err as any).faceFailureStage = getFaceFailureStage(err, Array.isArray((err as any).debugStages) ? (err as any).debugStages : []);
        }
        if (err && typeof err === 'object' && !(err as any).faceFailureDetail) {
            (err as any).faceFailureDetail = getFaceFailureDetail(err);
        }
        return err;
    }
    const detail = err instanceof Error ? err.message : String(err || 'face_detection_failed');
    const debugStages = Array.isArray((err as any)?.debugStages) ? (err as any).debugStages : [];
    const faceFailureStage = getFaceFailureStage(err, debugStages);
    const reason: ClientProcessingReason = faceFailureStage === 'model_load_failed' || faceFailureStage === 'embedding_model_load_failed'
        ? 'model_load_failed'
        : 'model_unavailable';
    const wrapped = new FaceDetectionUnavailableError(reason, `face_detection_failed: ${detail}`);
    if (err && typeof err === 'object' && Array.isArray((err as any).debugStages)) {
        (wrapped as any).debugStages = (err as any).debugStages;
    }
    (wrapped as any).faceFailureStage = faceFailureStage;
    (wrapped as any).faceFailureDetail = getFaceFailureDetail(err);
    return wrapped;
};


export const getBrowserAiNetworkGate = (): BrowserAiNetworkGate => {
    if (typeof navigator === 'undefined') {
        return {
            allowed: false,
            reason: 'network_info_unavailable',
            detail: 'Network information is unavailable',
            hasNetworkInfo: false,
        };
    }
    if (!navigator.onLine) {
        return {
            allowed: false,
            reason: 'offline',
            detail: 'Browser is offline',
            hasNetworkInfo: false,
        };
    }
    const nav = navigator as Navigator & {
        connection?: {
            effectiveType?: string;
            saveData?: boolean;
            downlink?: number;
            rtt?: number;
        };
    };
    const connection = nav.connection;
    const hasNetworkInfo = Boolean(connection);
    if (connection?.saveData) {
        return {
            allowed: false,
            reason: 'save_data_enabled',
            detail: 'Data saver is enabled',
            hasNetworkInfo,
        };
    }
    const effectiveType = String(connection?.effectiveType || '').toLowerCase();
    if (effectiveType === 'slow-2g' || effectiveType === '2g') {
        return {
            allowed: false,
            reason: 'poor_network',
            detail: `${effectiveType} connection`,
            hasNetworkInfo,
        };
    }
    const downlink = Number(connection?.downlink || 0);
    const rtt = Number(connection?.rtt || 0);
    if (!connection) {
        return {
            allowed: false,
            reason: 'network_info_unavailable',
            detail: 'Network Information API is unavailable',
            hasNetworkInfo: false,
        };
    }
    // Chrome's `downlink` sample is coarse and can under-report even on a good
    // connection (little recent transfer history to base it on, esp. desktop/Wi-Fi).
    // Only trust it as a blocking signal when effectiveType doesn't already say
    // the connection behaves like 4g -- that classification is the more holistic
    // signal, and a 4g/low-downlink combo means the downlink sample is the outlier.
    if (downlink > 0 && downlink < 1.5 && effectiveType !== '4g') {
        return {
            allowed: false,
            reason: 'poor_network',
            detail: `Downlink ${downlink} Mbps is below 1.5 Mbps`,
            hasNetworkInfo,
        };
    }
    if (rtt >= 400) {
        return {
            allowed: false,
            reason: 'poor_network',
            detail: `RTT ${rtt}ms is 400ms or higher`,
            hasNetworkInfo,
        };
    }
    return {
        allowed: true,
        reason: null,
        detail: 'Network is suitable for browser AI auto-load',
        hasNetworkInfo,
    };
};

const getPoorNetworkReason = (): ClientProcessingReason | null => {
    const gate = getBrowserAiNetworkGate();
    return gate.reason === 'network_info_unavailable' ? null : gate.reason;
};

export const isBrowserAiAutoLoadAllowed = () => getBrowserAiNetworkGate().allowed;

export const isBrowserAiNetworkRetryReason = (reason?: ClientProcessingReason) => (
    reason === 'offline' || reason === 'poor_network' || reason === 'save_data_enabled'
);

export const browserAiIdleState = (detail = 'Browser AI model is not loaded'): BrowserAiModelState => ({
    status: 'idle',
    detail,
    modelAvailability: 'skipped',
    modelCacheStatus: 'miss',
    runtime: 'browser-ai-worker',
});

export const browserAiLoadingState = (current?: BrowserAiModelState): BrowserAiModelState => ({
    status: 'loading',
    detail: 'Loading browser AI',
    modelAvailability: 'skipped',
    modelCacheStatus: current?.modelCacheStatus || 'miss',
    runtime: current?.runtime || 'browser-ai-worker',
});

export const browserAiUnsupportedState = (detail: string): BrowserAiModelState => ({
    status: 'unsupported',
    reason: 'unsupported_runtime',
    detail,
    modelAvailability: 'unavailable',
    modelCacheStatus: 'failed',
    runtime: 'browser-ai-worker',
});

const isConservativeBrowserMode = () => {
    if (typeof navigator === 'undefined' || typeof window === 'undefined') {
        return true;
    }
    const nav = navigator as Navigator & {
        deviceMemory?: number;
        hardwareConcurrency?: number;
        connection?: unknown;
    };
    const ua = navigator.userAgent || '';
    const isSafari = /^((?!chrome|android).)*safari/i.test(ua);
    const isMobile = window.matchMedia?.('(max-width: 760px)').matches || window.matchMedia?.('(pointer: coarse)').matches;
    const lowMemory = typeof nav.deviceMemory === 'number' && nav.deviceMemory <= 4;
    const lowCores = typeof nav.hardwareConcurrency === 'number' && nav.hardwareConcurrency <= 4;
    return Boolean(isSafari || isMobile || lowMemory || lowCores || !nav.connection);
};

export const getBrowserAiUnsupportedReason = (): string | null => {
    if (typeof window === 'undefined' || typeof navigator === 'undefined') {
        return 'Browser runtime unavailable';
    }
    if (typeof Worker === 'undefined') {
        return 'Web Workers are unavailable';
    }
    if (typeof fetch === 'undefined') {
        return 'Fetch API is unavailable';
    }
    if (!('caches' in window)) {
        return 'Cache Storage is unavailable';
    }
    if (!crypto?.subtle) {
        return 'Web Crypto is unavailable';
    }
    return null;
};

export const formatBrowserAiReason = (state: BrowserAiModelState) => (
    state.detail || state.reason || 'model_unavailable'
);

const createBrowserAiWorker = () => new Worker(new URL('../workers/browserAiWorker.ts', import.meta.url), { type: 'module' });

type BrowserAiWarmupResult = {
    fallback?: boolean;
    reason?: string;
    model?: string;
    modelVersion?: string;
    modelTaxonomyVersion?: string;
    runtime?: string;
};

// Ordered stages a cold (uncached) load walks through, in the order acquireBrowserAiModel
// and the vision worker actually reach them. Used only to give the UI a determinate
// "step X of N" progress readout instead of an indeterminate spinner during the (up to
// ~90s) first-ever download+warm-up.
const BROWSER_AI_LOAD_STAGES: Array<{ key: string; label: string }> = [
    { key: 'manifest', label: 'Checking model manifest' },
    { key: 'face_model', label: 'Downloading face detector' },
    { key: 'embedding_model', label: 'Downloading face embedding model' },
    { key: 'worker_received', label: 'Starting vision worker' },
    { key: 'vocabulary_loading', label: 'Loading vocabulary' },
    { key: 'vocabulary_loaded', label: 'Vocabulary ready' },
    { key: 'clip_loading', label: 'Loading scene classifier' },
    { key: 'clip_loaded', label: 'Scene classifier ready' },
    { key: 'warmup_inference', label: 'Running warm-up inference' },
];
const BROWSER_AI_LOAD_STAGE_INDEX = new Map(BROWSER_AI_LOAD_STAGES.map((stage, i) => [stage.key, i]));

const emitBrowserAiLoadStage = (onProgress: ((stage: BrowserAiLoadStage) => void) | undefined, key: string) => {
    const index = BROWSER_AI_LOAD_STAGE_INDEX.get(key);
    if (!onProgress || index === undefined) {
        return;
    }
    onProgress({ key, label: BROWSER_AI_LOAD_STAGES[index].label, index: index + 1, total: BROWSER_AI_LOAD_STAGES.length });
};

const warmBrowserAiWorker = (
    manifest: BrowserAiManifest,
    timeoutMs: number,
    onProgress?: (stage: BrowserAiLoadStage) => void,
): Promise<BrowserAiWarmupResult> => new Promise((resolve, reject) => {
    let settled = false;
    let timer: number | undefined;
    let lastPhase = 'worker_created';
    const worker = createBrowserAiWorker();
    const finish = (err?: unknown, result?: BrowserAiWarmupResult) => {
        if (settled) {
            return;
        }
        settled = true;
        if (timer !== undefined) {
            window.clearTimeout(timer);
        }
        worker.terminate();
        if (err) {
            reject(err);
        } else {
            resolve(result || {});
        }
    };
    timer = window.setTimeout(() => finish(new Error(`model_budget_exceeded:${lastPhase}`)), timeoutMs);
    worker.onmessage = (event) => {
        const data = event.data || {};
        if (data.type === 'browser-ai-warmup-progress') {
            lastPhase = String(data.phase || lastPhase);
            emitBrowserAiLoadStage(onProgress, lastPhase);
            return;
        }
        if (data.type === 'browser-ai-warmup-result' && data.ok === true) {
            finish(undefined, {
                fallback: Boolean(data.fallback),
                reason: data.reason ? String(data.reason) : undefined,
                model: data.model ? String(data.model) : undefined,
                modelVersion: data.modelVersion ? String(data.modelVersion) : undefined,
                modelTaxonomyVersion: data.modelTaxonomyVersion ? String(data.modelTaxonomyVersion) : undefined,
                runtime: data.runtime ? String(data.runtime) : undefined,
            });
        } else if (data.type === 'browser-ai-warmup-result') {
            finish(new Error(String(data.reason || 'model_load_failed')));
        }
    };
    worker.onerror = (event) => {
        finish(new Error(event.message || (event as ErrorEvent).error?.message || 'model_load_failed'));
    };
    worker.postMessage({ type: 'browser-ai-warmup', manifest, timeoutMs });
});

// A single analyze() call against a Worker that stays alive across many
// photos, so the CLIP model + tokenized vocabulary (browserAiWorker.ts loads
// and caches both at module scope, meant to be "encoded once per worker
// session and reused for every photo") actually gets reused across a batch
// instead of being rebuilt from scratch on every single photo -- see
// createPersistentBrowserAiWorker for the batch-drain lifecycle this exists
// for. Multiple in-flight analyze() calls are matched back to their
// resolvers by requestId in case a caller ever pipelines more than one.
export type BrowserAiWorkerHandle = {
    analyze: (imageSource: Blob | File, modelState: BrowserAiModelState, timeoutMs: number) => Promise<Record<string, any>>;
    dispose: () => void;
};

export const createPersistentBrowserAiWorker = (): BrowserAiWorkerHandle => {
    const worker = createBrowserAiWorker();
    const pending = new Map<string, { resolve: (result: Record<string, any>) => void; reject: (err: unknown) => void; timer?: number }>();

    const settle = (requestId: string, err?: unknown, result?: Record<string, any>) => {
        const entry = pending.get(requestId);
        if (!entry) {
            return;
        }
        pending.delete(requestId);
        if (entry.timer !== undefined) {
            window.clearTimeout(entry.timer);
        }
        if (err) {
            entry.reject(err);
        } else {
            entry.resolve(result || {});
        }
    };

    worker.onmessage = (event) => {
        const data = event.data || {};
        if (data.type !== 'browser-ai-analyze-result') {
            return;
        }
        if (data.ok === true) {
            settle(String(data.requestId || ''), undefined, data.result || {});
        } else {
            settle(String(data.requestId || ''), new Error(String(data.reason || 'model_load_failed')));
        }
    };
    worker.onerror = (event) => {
        const err = new Error(event.message || (event as ErrorEvent).error?.message || 'model_load_failed');
        for (const requestId of Array.from(pending.keys())) {
            settle(requestId, err);
        }
    };

    const analyze = (imageSource: Blob | File, modelState: BrowserAiModelState, timeoutMs: number): Promise<Record<string, any>> => (
        new Promise((resolve, reject) => {
            if (!modelState.manifest) {
                reject(new Error('model_unavailable'));
                return;
            }
            const requestId = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
            const timer = window.setTimeout(() => settle(requestId, new Error('inference_timeout')), timeoutMs);
            pending.set(requestId, { resolve, reject, timer });
            createBrowserAiImagePayload(imageSource)
                .then((image) => {
                    if (!image) {
                        settle(requestId, new Error('unsupported_runtime'));
                        return;
                    }
                    worker.postMessage({
                        type: 'browser-ai-analyze',
                        requestId,
                        manifest: modelState.manifest,
                        image,
                        timeoutMs,
                    }, [image.data.buffer]);
                })
                .catch((err) => settle(requestId, err));
        })
    );

    const dispose = () => {
        worker.onmessage = null;
        worker.onerror = null;
        const disposedError = new Error('worker_disposed');
        for (const requestId of Array.from(pending.keys())) {
            settle(requestId, disposedError);
        }
        worker.terminate();
    };

    return { analyze, dispose };
};

const runBrowserAiVisionInWorker = (
    imageSource: Blob | File,
    modelState: BrowserAiModelState,
    timeoutMs: number,
    workerHandle?: BrowserAiWorkerHandle,
): Promise<Record<string, any>> => {
    if (workerHandle) {
        return workerHandle.analyze(imageSource, modelState, timeoutMs);
    }
    return new Promise((resolve, reject) => {
        if (!modelState.manifest) {
            reject(new Error('model_unavailable'));
            return;
        }
        let settled = false;
        let timer: number | undefined;
        const worker = createBrowserAiWorker();
        const requestId = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
        const finish = (err?: unknown, result?: Record<string, any>) => {
            if (settled) {
                return;
            }
            settled = true;
            if (timer !== undefined) {
                window.clearTimeout(timer);
            }
            worker.terminate();
            if (err) {
                reject(err);
            } else {
                resolve(result || {});
            }
        };
        timer = window.setTimeout(() => finish(new Error('inference_timeout')), timeoutMs);
        worker.onmessage = (event) => {
            const data = event.data || {};
            if (data.type !== 'browser-ai-analyze-result' || data.requestId !== requestId) {
                return;
            }
            if (data.ok === true) {
                finish(undefined, data.result || {});
            } else {
                finish(new Error(String(data.reason || 'model_load_failed')));
            }
        };
        worker.onerror = (event) => {
            finish(new Error(event.message || (event as ErrorEvent).error?.message || 'model_load_failed'));
        };
        createBrowserAiImagePayload(imageSource)
            .then((image) => {
                if (!image) {
                    finish(new Error('unsupported_runtime'));
                    return;
                }
                worker.postMessage({
                    type: 'browser-ai-analyze',
                    requestId,
                    manifest: modelState.manifest,
                    image,
                    timeoutMs,
                }, [image.data.buffer]);
            })
            .catch((err) => finish(err));
    });
};

// withTimeoutOutcome (used at the runNativeFaceDetection call site) only stops
// *waiting* on a slow detection -- it can't cancel the underlying ONNX
// session.run() once started. On an already-overloaded/throttling mobile tab,
// each timed-out photo leaves its detection running in the background while
// the drain loop moves straight on to the next photo's detection call, so a
// device that's slow enough to time out once starts piling up concurrent
// inference passes on the SAME shared singleton session -- more heavy WASM
// work and memory held per photo, which makes the device slower still, which
// times out more photos. That runaway feedback loop (not any single leaked
// object) is what matches "tab crashes after a certain count of files": each
// timeout adds one more zombie inference pass instead of freeing anything.
// Concurrent session.run() calls on one onnxruntime-web session are also not
// something to rely on being safe. Mirrors the OCR concurrency cap fix below:
// while a detection call (including an orphaned one) is still in flight, skip
// starting a new one rather than queue it -- the existing pending-work/deferred
// mechanism retries the skipped step on a later pass once capacity frees up.
const FACE_DETECTION_MAX_CONCURRENT = 1;
let faceDetectionActiveCount = 0;

const runNativeFaceDetection = async (imageSource: Blob | File, rotationDegrees = 0): Promise<BrowserFaceDetectionResult | null> => {
    faceDetectionActiveCount += 1;
    try {
        return await runNativeFaceDetectionInner(imageSource, rotationDegrees);
    } finally {
        faceDetectionActiveCount -= 1;
    }
};

const runNativeFaceDetectionInner = async (imageSource: Blob | File, rotationDegrees = 0): Promise<BrowserFaceDetectionResult | null> => {
    if (typeof window === 'undefined' || typeof createImageBitmap !== 'function') {
        return null;
    }
    const sourceCanvas = await createOrientedImageCanvas(imageSource);
    if (!sourceCanvas.width || !sourceCanvas.height) {
        return null;
    }
    // Rotate the pixels for detection, then map boxes back to the source orientation.
    const rotation = normalizeQuarterTurnRotation(rotationDegrees);
    const canvas = rotateCanvasByQuarterTurns(sourceCanvas, rotation);
    const effectiveRotation = canvas === sourceCanvas ? 0 : rotation;
    const sourceWidth = sourceCanvas.width;
    const sourceHeight = sourceCanvas.height;
    try {
        const faceEmbeddingResult = await detectFacesWithEmbeddings(canvas);
        if (faceEmbeddingResult) {
            if (!effectiveRotation) {
                return faceEmbeddingResult;
            }
            return {
                ...faceEmbeddingResult,
                faces: faceEmbeddingResult.faces.map((face) => ({
                    ...face,
                    bbox: remapFaceBoundingBoxAfterRotation(face.bbox, sourceWidth, sourceHeight, effectiveRotation),
                    imageWidth: sourceWidth,
                    imageHeight: sourceHeight,
                })),
            };
        }
    } catch (err) {
        throw normalizeNativeFaceDetectionError(err);
    }
    return null;
};

export const acquireBrowserAiModel = async (
    onProgress?: (stage: BrowserAiLoadStage) => void,
): Promise<BrowserAiModelState> => {
    const startedAt = performance.now();
    const result = await withTimeout(acquireBrowserAiModelInner(onProgress), BROWSER_AI_MODEL_ACQUISITION_TIMEOUT_MS);
    if (result) {
        return result;
    }
    return {
        status: 'unavailable',
        reason: 'model_download_timeout',
        detail: 'Model load timed out -- the browser cache or network appears unresponsive. Try reloading the page.',
        modelAvailability: 'unavailable',
        modelCacheStatus: 'failed',
        runtime: 'browser-ai-worker',
        modelAcquisitionMs: Math.max(0, Math.round(performance.now() - startedAt)),
    };
};

const acquireBrowserAiModelInner = async (
    onProgress?: (stage: BrowserAiLoadStage) => void,
): Promise<BrowserAiModelState> => {
    const startedAt = performance.now();
    const finish = (state: Omit<BrowserAiModelState, 'modelAcquisitionMs'>): BrowserAiModelState => ({
        ...state,
        modelAcquisitionMs: Math.max(0, Math.round(performance.now() - startedAt)),
    });
    const unsupportedDetail = getBrowserAiUnsupportedReason();
    if (unsupportedDetail) {
        return finish({
            status: 'unsupported',
            reason: 'unsupported_runtime',
            detail: unsupportedDetail,
            modelAvailability: 'unavailable',
            modelCacheStatus: 'failed',
            runtime: 'browser-ai-worker',
        });
    }
    emitBrowserAiLoadStage(onProgress, 'manifest');

    const cache = await caches.open(BROWSER_AI_MODEL_CACHE);
    const manifestUrl = resolveManifestUrl(BROWSER_AI_MODEL_MANIFEST_URL);
    let manifestResponse = await cache.match(manifestUrl);
    let modelCacheStatus: BrowserAiModelCacheStatus = manifestResponse ? 'hit' : 'miss';
    const networkReason = getPoorNetworkReason();
    const cachedManifestContentType = String(manifestResponse?.headers.get('content-type') || '').toLowerCase();
    if (manifestResponse && cachedManifestContentType && !cachedManifestContentType.includes('json')) {
        await cache.delete(manifestUrl);
        manifestResponse = undefined;
        modelCacheStatus = 'miss';
    }
    if (!manifestResponse) {
        if (networkReason) {
            return finish({
                status: 'unavailable',
                reason: networkReason,
                detail: networkReason,
                modelAvailability: 'unavailable',
                modelCacheStatus: 'miss',
                runtime: 'browser-ai-worker',
            });
        }
        const fetched = await withTimeout(fetch(manifestUrl), CLIENT_MODEL_ACQUISITION_BUDGET_MS);
        if (!fetched || !fetched.ok) {
            return finish({
                status: 'unavailable',
                reason: 'model_unavailable',
                detail: `Manifest unavailable at ${BROWSER_AI_MODEL_MANIFEST_URL}`,
                modelAvailability: 'unavailable',
                modelCacheStatus: 'failed',
                runtime: 'browser-ai-worker',
            });
        }
        const manifestContentType = String(fetched.headers.get('content-type') || '').toLowerCase();
        if (manifestContentType && !manifestContentType.includes('json')) {
            return finish({
                status: 'unavailable',
                reason: 'model_unavailable',
                detail: `Manifest unavailable at ${BROWSER_AI_MODEL_MANIFEST_URL}`,
                modelAvailability: 'unavailable',
                modelCacheStatus: 'failed',
                runtime: 'browser-ai-worker',
            });
        }
        await cache.put(manifestUrl, fetched.clone());
        manifestResponse = fetched;
        modelCacheStatus = 'downloaded';
    }

    const parseManifestResponse = async (response: Response): Promise<BrowserAiManifest> => {
        const manifestText = await response.clone().text();
        const trimmedManifest = manifestText.trim();
        if (!trimmedManifest.startsWith('{')) {
            throw new Error('manifest_not_json');
        }
        return JSON.parse(trimmedManifest);
    };

    let manifest: BrowserAiManifest | null = null;
    try {
        manifest = await parseManifestResponse(manifestResponse);
    } catch {
        await cache.delete(manifestUrl);
        if (modelCacheStatus === 'hit' && !networkReason) {
            const refetched = await withTimeout(fetch(manifestUrl), CLIENT_MODEL_ACQUISITION_BUDGET_MS);
            if (refetched?.ok) {
                try {
                    manifest = await parseManifestResponse(refetched);
                    await cache.put(manifestUrl, refetched.clone());
                    modelCacheStatus = 'downloaded';
                } catch {
                    // Fall through to the unavailable result below.
                }
            }
        }
    }
    if (!manifest) {
        return finish({
            status: 'unavailable',
            reason: 'model_unavailable',
            detail: `Manifest unavailable at ${BROWSER_AI_MODEL_MANIFEST_URL}`,
            modelAvailability: 'unavailable',
            modelCacheStatus: 'failed',
            runtime: 'browser-ai-worker',
        });
    }
    if (modelCacheStatus === 'hit' && !networkReason) {
        try {
            const refetched = await withTimeout(fetch(manifestUrl, { cache: 'no-store' }), CLIENT_MODEL_ACQUISITION_BUDGET_MS);
            if (refetched?.ok) {
                const refreshedManifest = await parseManifestResponse(refetched);
                await cache.put(manifestUrl, refetched.clone());
                manifest = refreshedManifest;
                modelCacheStatus = 'downloaded';
            }
        } catch {
            // Keep the cached manifest and let warm-up decide availability.
        }
    }

    const manifestVersion = String(manifest.manifestVersion || manifest.version || '');
    const manifestModels = toArray<NonNullable<BrowserAiManifest['models']>[number]>(manifest.models);
    const firstModel = manifestModels[0];
    const firstModelAssets = toArray<BrowserAiManifestAsset>(firstModel?.assets);
    const assetCandidates = [
        ...toArray<BrowserAiManifestAsset>(manifest.assets),
        ...firstModelAssets,
    ];
    const requiresBundledAssets = !String(manifest.faceModel || '').trim();
    if (requiresBundledAssets && assetCandidates.length === 0) {
        return finish({
            status: 'unavailable',
            reason: 'model_unavailable',
            detail: 'Model manifest does not list any model assets',
            modelAvailability: 'unavailable',
            modelCacheStatus: modelCacheStatus === 'hit' ? 'hit' : 'failed',
            modelManifestVersion: manifestVersion,
            model: manifest.model || firstModel?.name || '',
            modelVersion: manifest.modelVersion || firstModel?.version || '',
            modelTaxonomyVersion: manifest.modelTaxonomyVersion || firstModel?.taxonomyVersion || '',
            runtime: manifest.runtime || 'browser-ai-worker',
        });
    }

    if (assetCandidates.length > 0) {
        for (const asset of assetCandidates) {
            const assetPath = asset.url || asset.path || '';
            if (!assetPath) {
                return finish({
                    status: 'unavailable',
                    reason: 'model_load_failed',
                    detail: 'Model manifest contains an asset without a URL',
                    modelAvailability: 'unavailable',
                    modelCacheStatus: 'failed',
                    modelManifestVersion: manifestVersion,
                    runtime: manifest.runtime || 'browser-ai-worker',
                });
            }
            const assetUrl = resolveManifestAssetUrl(manifestUrl, assetPath);
            let assetResponse = await cache.match(assetUrl);
            const assetWasCached = Boolean(assetResponse);
            if (!assetResponse) {
                if (networkReason) {
                    return finish({
                        status: 'unavailable',
                        reason: networkReason,
                        detail: networkReason,
                        modelAvailability: 'unavailable',
                        modelCacheStatus,
                        modelManifestVersion: manifestVersion,
                        runtime: manifest.runtime || 'browser-ai-worker',
                    });
                }
                const fetchedAsset = await withTimeout(fetch(assetUrl), CLIENT_MODEL_ACQUISITION_BUDGET_MS);
                if (!fetchedAsset || !fetchedAsset.ok) {
                    return finish({
                        status: 'unavailable',
                        reason: 'model_unavailable',
                        detail: `Model asset unavailable: ${assetPath}`,
                        modelAvailability: 'unavailable',
                        modelCacheStatus: 'failed',
                        modelManifestVersion: manifestVersion,
                        runtime: manifest.runtime || 'browser-ai-worker',
                    });
                }
                await cache.put(assetUrl, fetchedAsset.clone());
                assetResponse = fetchedAsset;
                modelCacheStatus = 'downloaded';
            }

            let assetBlob = await assetResponse.clone().blob();
            const expectedBytes = Number(asset.bytes || asset.size || 0);
            const describeAssetMismatch = async (): Promise<string | null> => {
                if (expectedBytes > 0 && assetBlob.size !== expectedBytes) {
                    return `Model asset size mismatch: ${assetPath}`;
                }
                if (asset.sha256) {
                    const actualHash = await blobSha256(assetBlob);
                    if (actualHash.toLowerCase() !== String(asset.sha256).toLowerCase()) {
                        return `Model asset checksum mismatch: ${assetPath}`;
                    }
                }
                return null;
            };

            let assetMismatch = await describeAssetMismatch();
            if (assetMismatch && assetWasCached && !networkReason) {
                // A previously cached asset can go stale when a deploy ships a new
                // model file at the same URL (CacheStorage has no revalidation of
                // its own). Evict and retry once from the network before failing,
                // mirroring the manifest's own self-heal above.
                await cache.delete(assetUrl);
                const refetchedAsset = await withTimeout(
                    fetch(assetUrl, { cache: 'no-store' }),
                    CLIENT_MODEL_ACQUISITION_BUDGET_MS,
                );
                if (refetchedAsset && refetchedAsset.ok) {
                    await cache.put(assetUrl, refetchedAsset.clone());
                    assetBlob = await refetchedAsset.clone().blob();
                    modelCacheStatus = 'downloaded';
                    assetMismatch = await describeAssetMismatch();
                }
            }
            if (assetMismatch) {
                return finish({
                    status: 'unavailable',
                    reason: 'model_load_failed',
                    detail: assetMismatch,
                    modelAvailability: 'unavailable',
                    modelCacheStatus: 'failed',
                    modelManifestVersion: manifestVersion,
                    runtime: manifest.runtime || 'browser-ai-worker',
                });
            }
        }
    }

    const runtimeConfig = getRuntimeConfig();
    emitBrowserAiLoadStage(onProgress, 'face_model');
    try {
        const blazeFaceModel = await withTimeout(loadBlazeFaceModel(), CLIENT_MODEL_ACQUISITION_BUDGET_MS);
        if (!blazeFaceModel) {
            throw new Error('blazeface_load_timeout');
        }
    } catch (err) {
        const normalizedError = normalizeBrowserAiError(err);
        return finish({
            status: 'unavailable',
            reason: normalizedError.reason === 'inference_timeout' ? 'model_load_failed' : normalizedError.reason,
            detail: normalizedError.detail || 'face_model_unavailable',
            modelAvailability: 'unavailable',
            modelCacheStatus: 'failed',
            modelManifestVersion: manifestVersion,
            model: manifest.model || firstModel?.name || '',
            modelVersion: manifest.modelVersion || firstModel?.version || '',
            modelTaxonomyVersion: manifest.modelTaxonomyVersion || firstModel?.taxonomyVersion || '',
            runtime: manifest.runtime || 'browser-ai-worker',
            manifest,
        });
    }

    emitBrowserAiLoadStage(onProgress, 'embedding_model');
    try {
        await withTimeout(preloadArcFaceEmbeddingModel({
            modelUrl: runtimeConfig.arcFaceModelUrl || manifest.faceEmbeddingModelUrl,
            wasmPath: runtimeConfig.arcFaceWasmPath || manifest.wasmPath,
        }), CLIENT_MODEL_ACQUISITION_BUDGET_MS);
    } catch (err) {
        const normalizedError = normalizeBrowserAiError(err);
        return finish({
            status: 'unavailable',
            reason: normalizedError.reason === 'inference_timeout' ? 'model_load_failed' : normalizedError.reason,
            detail: normalizedError.detail || 'arcface_model_unavailable',
            modelAvailability: 'unavailable',
            modelCacheStatus: 'failed',
            modelManifestVersion: manifestVersion,
            model: ARCFACE_MODEL_NAME,
            modelVersion: ARCFACE_MODEL_VERSION,
            modelTaxonomyVersion: ARCFACE_EMBEDDING_VERSION,
            runtime: ARCFACE_RUNTIME,
            manifest,
        });
    }

    let warmupResult: BrowserAiWarmupResult = {};
    try {
        warmupResult = await warmBrowserAiWorker(manifest, CLIENT_MODEL_WARMUP_BUDGET_MS, onProgress);
    } catch (err) {
        const normalizedError = normalizeBrowserAiError(err);
        const warmupDetail = normalizedError.detail || 'browser_ai_worker_warmup_failed';
        return finish({
            status: 'unavailable',
            reason: normalizedError.reason === 'inference_timeout' ? 'model_load_failed' : normalizedError.reason,
            detail: warmupDetail,
            modelAvailability: 'unavailable',
            modelCacheStatus: 'failed',
            modelManifestVersion: manifestVersion,
            model: manifest.model || firstModel?.name || '',
            modelVersion: manifest.modelVersion || firstModel?.version || '',
            modelTaxonomyVersion: manifest.modelTaxonomyVersion || firstModel?.taxonomyVersion || '',
            runtime: manifest.runtime || 'browser-ai-worker',
            manifest,
        });
    }

    // warmBrowserAiWorker only ever warms the CLIP scene classifier + tag
    // vocabulary (browserAiWorker.ts's `warmup()`/`classify()`) -- face
    // detection and the AdaFace embedding model are separate, main-thread
    // pipelines already validated by the 'face_model'/'embedding_model'
    // stages above, with their own hard-fail returns. And `warmup()` itself
    // never throws (as long as the manifest's enableLocalVisionFallback
    // isn't explicitly false, which it isn't here): any classify() failure
    // -- network-blocked CDN, a bad vocabulary fetch, an unsupported model
    // type, anything -- is already caught there and reported as a graceful
    // `fallback: true`. So reaching here with `usingLocalFallback` true
    // always means "classifier degraded, everything else already works";
    // there's no reason to hard-fail the whole feature over it.
    const usingLocalFallback = Boolean(warmupResult.fallback);
    return finish({
        status: 'available',
        reason: 'done',
        detail: usingLocalFallback
            ? `Browser AI ready; image classifier disabled (${warmupResult.reason})`
            : 'Browser AI ready',
        modelAvailability: modelCacheStatus === 'downloaded' ? 'downloaded' : 'cached',
        modelCacheStatus,
        modelManifestVersion: manifestVersion,
        model: usingLocalFallback ? manifest.model || firstModel?.name || '' : warmupResult.model || manifest.model || firstModel?.name || '',
        modelVersion: usingLocalFallback ? manifest.modelVersion || firstModel?.version || '' : warmupResult.modelVersion || manifest.modelVersion || firstModel?.version || '',
        modelTaxonomyVersion: usingLocalFallback ? manifest.modelTaxonomyVersion || firstModel?.taxonomyVersion || '' : warmupResult.modelTaxonomyVersion || manifest.modelTaxonomyVersion || firstModel?.taxonomyVersion || '',
        runtime: usingLocalFallback ? manifest.runtime || 'browser-ai-worker' : warmupResult.runtime || manifest.runtime || 'browser-ai-worker',
        manifest,
    });
};


const validateImageBlob = async (blob: Blob): Promise<{ width: number; height: number } | null> => {
    if (typeof createImageBitmap !== 'function') {
        return null;
    }
    const bitmap = await createImageBitmap(blob);
    try {
        if (!bitmap.width || !bitmap.height) {
            return null;
        }
        return { width: bitmap.width, height: bitmap.height };
    } finally {
        bitmap.close?.();
    }
};

const findLargestEmbeddedJpegRange = async (file: File): Promise<{ start: number; end: number; timedOut: boolean } | null> => {
    const deadline = performance.now() + CLIENT_RAW_PREVIEW_SCAN_BUDGET_MS;
    let previousByte = -1;
    let activeStart = -1;
    // Whether the JPEG currently being scanned has a browser-decodable
    // Start-of-Frame. DNG (and some other RAWs) embed their raw sensor data as a
    // *lossless* JPEG (SOF3) which also runs 0xFFD8…0xFFD9 and is far larger than
    // the real preview; without this guard the scanner picks that undecodable
    // stream, so the tile falls back to a "RAW preview unavailable" placeholder.
    let activeHasDecodableSof = false;
    let bestStart = -1;
    let bestEnd = -1;
    let bestLength = 0;
    let bytesSinceYield = 0;
    let timedOut = false;

    for (let offset = 0; offset < file.size; offset += CLIENT_RAW_PREVIEW_SCAN_CHUNK_BYTES) {
        if (performance.now() > deadline) {
            timedOut = true;
            break;
        }
        const end = Math.min(file.size, offset + CLIENT_RAW_PREVIEW_SCAN_CHUNK_BYTES);
        const bytes = new Uint8Array(await readBlobArrayBuffer(file.slice(offset, end)));
        for (let index = 0; index < bytes.length; index += 1) {
            const value = bytes[index];
            if (previousByte === 0xff) {
                const markerStart = offset + index - 1;
                if (value === 0xd8 && activeStart < 0) {
                    activeStart = markerStart;
                    activeHasDecodableSof = false;
                } else if (value === 0xd9 && activeStart >= 0) {
                    const candidateEnd = offset + index + 1;
                    const length = candidateEnd - activeStart;
                    // Only accept ranges that carry a decodable SOF (see above).
                    if (activeHasDecodableSof && length > bestLength && length > 1024) {
                        bestStart = activeStart;
                        bestEnd = candidateEnd;
                        bestLength = length;
                    }
                    activeStart = -1;
                    activeHasDecodableSof = false;
                } else if (activeStart >= 0 && (value === 0xc0 || value === 0xc1 || value === 0xc2)) {
                    // SOF0 baseline / SOF1 extended-sequential / SOF2 progressive —
                    // the frame types a browser can actually render (unlike SOF3
                    // lossless). 0xC4 (DHT), 0xC8 (JPG), 0xCC (DAC) are not SOFs.
                    activeHasDecodableSof = true;
                }
            }
            previousByte = value;
        }

        bytesSinceYield += bytes.length;
        if (bytesSinceYield >= CLIENT_RAW_PREVIEW_SCAN_YIELD_BYTES) {
            bytesSinceYield = 0;
            await yieldToBrowser();
        }
    }

    if (bestStart < 0 || bestEnd <= bestStart) {
        return timedOut ? { start: -1, end: -1, timedOut } : null;
    }
    return { start: bestStart, end: bestEnd, timedOut };
};

const createRawFallbackPreviewBlob = async (file: File): Promise<{ blob: Blob; width: number; height: number } | null> => {
    if (typeof document === 'undefined') {
        return null;
    }
    const canvas = document.createElement('canvas');
    const width = 320;
    const height = 240;
    canvas.width = width;
    canvas.height = height;
    const ctx = canvas.getContext('2d');
    if (!ctx || typeof canvas.toBlob !== 'function') {
        return null;
    }

    const ext = (getFileExtension(file.name) || 'raw').toUpperCase();
    ctx.fillStyle = '#111827';
    ctx.fillRect(0, 0, width, height);
    ctx.fillStyle = '#1f2937';
    ctx.fillRect(16, 16, width - 32, height - 32);
    ctx.strokeStyle = '#94a3b8';
    ctx.lineWidth = 2;
    ctx.strokeRect(16, 16, width - 32, height - 32);
    ctx.fillStyle = '#f8fafc';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.font = '700 54px system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif';
    ctx.fillText(ext, width / 2, 102);
    ctx.font = '600 20px system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif';
    ctx.fillText('RAW preview unavailable', width / 2, 154);

    const blob = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, 'image/jpeg', 0.78));
    return blob ? { blob, width, height } : null;
};

const createRawFallbackVisionSource = async (
    file: File,
    base: Omit<BrowserVisionSource, 'imageSource' | 'sourceKind' | 'skipReason' | 'sourceBytes'>,
    reason: ClientProcessingReason,
): Promise<BrowserVisionSource> => {
    const fallback = await createRawFallbackPreviewBlob(file);
    if (!fallback) {
        return { ...base, imageSource: null, sourceKind: 'unsupported', sourceBytes: 0, skipReason: reason };
    }
    return {
        ...base,
        imageSource: fallback.blob,
        sourceKind: 'unsupported',
        sourceBytes: fallback.blob.size,
        previewWidth: fallback.width,
        previewHeight: fallback.height,
        skipReason: reason,
        thumbnailOnly: true,
    };
};

const createRawConvertedVisionSource = async (
    convertedPreview: Blob | File | undefined,
    base: Omit<BrowserVisionSource, 'imageSource' | 'sourceKind' | 'skipReason' | 'sourceBytes'>,
): Promise<BrowserVisionSource | null> => {
    if (!convertedPreview || convertedPreview.size <= 0) {
        return null;
    }
    let dimensions: { width: number; height: number } | null = null;
    try {
        dimensions = await validateImageBlob(convertedPreview);
    } catch {
        dimensions = null;
    }
    if (!dimensions) {
        return null;
    }
    return {
        ...base,
        imageSource: convertedPreview,
        sourceKind: 'raw_converted_jpeg',
        previewWidth: dimensions.width,
        previewHeight: dimensions.height,
        sourceBytes: convertedPreview.size,
    };
};

const extractEmbeddedJpegPreview = async (file: File): Promise<BrowserVisionSource> => {
    const sourceFormat = getFileExtension(file.name) || 'raw';
    const base = {
        sourceFormat,
        rawParserVersion: RAW_PARSER_VERSION,
        originalBytes: file.size,
        sourceBytes: 0,
        isRaw: true,
    };
    const range = await findLargestEmbeddedJpegRange(file);
    if (!range || range.start < 0 || range.end <= range.start) {
        return createRawFallbackVisionSource(file, base, range?.timedOut ? 'raw_container_unsupported' : 'raw_preview_missing');
    }
    const previewBlob = file.slice(range.start, range.end, 'image/jpeg');
    let dimensions: { width: number; height: number } | null = null;
    try {
        dimensions = await validateImageBlob(previewBlob);
    } catch {
        dimensions = null;
    }
    if (!dimensions) {
        return createRawFallbackVisionSource(file, base, 'raw_preview_invalid');
    }
    return {
        ...base,
        imageSource: previewBlob,
        sourceKind: 'raw_embedded_jpeg',
        previewWidth: dimensions.width,
        previewHeight: dimensions.height,
        sourceBytes: previewBlob.size,
    };
};

// Chrome/Edge have no native HEIC decoder (see createOrientedImageCanvas above), so handing
// OCR/face-detection the raw undecoded file as the 'original' fallback below is a guaranteed
// failure for every one of those steps, not a graceful degradation -- worse, tesseract.js's
// vendored blueimp-load-image throws an uncaught "toBlob is not a function" when its internal
// <img> fails to load HEIC bytes, which leaves its promise permanently unresolved and burns the
// entire OCR_TOTAL_BUDGET_MS on a call that could never have succeeded. Reuse the same
// libheif-backed decode already proven for thumbnails so this fallback only fires when the
// backend-converted preview is truly unavailable (e.g. a transient 503), not on every HEIC photo.
const createHeicDecodedVisionSource = async (
    file: File,
    base: Omit<BrowserVisionSource, 'imageSource' | 'sourceKind' | 'skipReason' | 'sourceBytes'>,
): Promise<BrowserVisionSource | null> => {
    try {
        const canvas = await withTimeout(createOrientedImageCanvas(file), CLIENT_BROWSER_STEP_BUDGET_MS);
        if (!canvas) {
            return null;
        }
        const blob = await new Promise<Blob | null>((resolve) => canvas.toBlob((b) => resolve(b), 'image/jpeg', 0.9));
        if (!blob) {
            return null;
        }
        return {
            ...base,
            imageSource: blob,
            sourceKind: 'raw_converted_jpeg',
            previewWidth: canvas.width,
            previewHeight: canvas.height,
            sourceBytes: blob.size,
        };
    } catch {
        return null;
    }
};

const resolveBrowserVisionSource = async (file: File, convertedPreview?: Blob | File): Promise<BrowserVisionSource> => {
    const sourceFormat = getFileExtension(file.name) || file.type || 'unknown';
    if (!isRawFile(file)) {
        const converted = await createRawConvertedVisionSource(convertedPreview, {
            sourceFormat,
            originalBytes: file.size,
            isRaw: false,
        });
        if (converted) {
            return {
                ...converted,
                sourceKind: 'backend_converted_jpeg',
            };
        }
        if (isHeicFilename(file.name)) {
            const decoded = await createHeicDecodedVisionSource(file, {
                sourceFormat,
                originalBytes: file.size,
                isRaw: false,
            });
            if (decoded) {
                return decoded;
            }
        }
        return {
            imageSource: file,
            sourceKind: 'original',
            sourceFormat,
            originalBytes: file.size,
            sourceBytes: file.size,
            isRaw: false,
        };
    }
    const base = {
        sourceFormat,
        rawParserVersion: RAW_PARSER_VERSION,
        originalBytes: file.size,
        sourceBytes: 0,
        isRaw: true,
    };
    const converted = await createRawConvertedVisionSource(convertedPreview, base);
    if (converted) {
        return converted;
    }
    try {
        return await extractEmbeddedJpegPreview(file);
    } catch {
        return createRawFallbackVisionSource(file, base, 'raw_container_unsupported');
    }
};

// withTimeout (used at the call site below) only stops *waiting* on a slow
// video decode -- it can't cancel the underlying <video> load/seek once
// started. Same orphaned-work pileup as the face-detection bug this mirrors
// (see FACE_DETECTION_MAX_CONCURRENT above): a timed-out video keeps
// decoding in the background while the drain loop moves on, so a slow/
// mobile device can accumulate several concurrent decodes, each pinning its
// own hardware video-decoder handle -- mobile browsers cap those in the
// single digits, making this a likely contributor to tab crashes on long
// sessions with many videos.
const VIDEO_THUMBNAIL_MAX_CONCURRENT = 1;
let videoThumbnailActiveCount = 0;

const createVideoBrowserThumbnail = async (file: File): Promise<{ dataUrl: string; width: number; height: number; rotationDegrees: number } | null> => {
    if (typeof document === 'undefined' || videoThumbnailActiveCount >= VIDEO_THUMBNAIL_MAX_CONCURRENT) {
        return null;
    }
    videoThumbnailActiveCount += 1;
    const objectUrl = URL.createObjectURL(file);
    const video = document.createElement('video');
    try {
        video.muted = true;
        video.playsInline = true;
        video.preload = 'auto';
        video.src = objectUrl;
        await new Promise<void>((resolve, reject) => {
            video.onloadeddata = () => resolve();
            video.onerror = () => reject(new Error('Video decode failed.'));
        });
        const seekTarget = Math.min(1, Math.max(0, (video.duration || 0) * 0.1));
        if (seekTarget > 0 && Number.isFinite(seekTarget)) {
            await new Promise<void>((resolve) => {
                video.onseeked = () => resolve();
                video.onerror = () => resolve();
                video.currentTime = seekTarget;
            });
        }
        if (!video.videoWidth || !video.videoHeight) {
            return null;
        }
        const scale = Math.min(CLIENT_THUMBNAIL_SIZE / video.videoWidth, CLIENT_THUMBNAIL_SIZE / video.videoHeight, 1);
        const width = Math.max(1, Math.round(video.videoWidth * scale));
        const height = Math.max(1, Math.round(video.videoHeight * scale));
        const canvas = document.createElement('canvas');
        canvas.width = width;
        canvas.height = height;
        const ctx = canvas.getContext('2d');
        if (!ctx) {
            return null;
        }
        ctx.drawImage(video, 0, 0, width, height);
        const blob = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, 'image/jpeg', 0.65));
        if (!blob) {
            return null;
        }
        return { dataUrl: await blobToDataUrl(blob), width, height, rotationDegrees: 0 };
    } finally {
        // Explicit teardown, not just letting `video` fall out of scope --
        // browsers keep decoder buffers / hardware video-decoder handles
        // pinned to a <video> element until the src is cleared and load()
        // is called; GC of the element wrapper alone doesn't reliably
        // release them promptly.
        video.pause();
        video.removeAttribute('src');
        video.load();
        URL.revokeObjectURL(objectUrl);
        videoThumbnailActiveCount -= 1;
    }
};

export const createBrowserThumbnail = async (source: Blob | File, rotationDegrees = 0): Promise<{ dataUrl: string; width: number; height: number; rotationDegrees: number } | null> => {
    if (typeof createImageBitmap !== 'function') {
        return null;
    }
    const orientedCanvas = await createOrientedImageCanvas(source);
    const sourceCanvas = rotateCanvasByQuarterTurns(orientedCanvas, rotationDegrees);
    const appliedRotation = normalizeQuarterTurnRotation(rotationDegrees);
    const scale = Math.min(CLIENT_THUMBNAIL_SIZE / sourceCanvas.width, CLIENT_THUMBNAIL_SIZE / sourceCanvas.height, 1);
    const width = Math.max(1, Math.round(sourceCanvas.width * scale));
    const height = Math.max(1, Math.round(sourceCanvas.height * scale));
    const canvas = document.createElement('canvas');
    canvas.width = width;
    canvas.height = height;
    const ctx = canvas.getContext('2d');
    if (!ctx) {
        return null;
    }
    ctx.drawImage(sourceCanvas, 0, 0, width, height);
    const blob = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, 'image/jpeg', 0.65));
    if (!blob) {
        return null;
    }
    return { dataUrl: await blobToDataUrl(blob), width, height, rotationDegrees: appliedRotation };
};

const createBrowserAiImagePayload = async (source: Blob | File): Promise<BrowserAiImagePayload | null> => {
    if (typeof createImageBitmap !== 'function') {
        return null;
    }
    const sourceCanvas = await createOrientedImageCanvas(source);
    const maxSide = Math.max(1, Math.sqrt(CLIENT_AI_MAX_MEGAPIXELS * 1000000));
    const scale = Math.min(maxSide / sourceCanvas.width, maxSide / sourceCanvas.height, 1);
    const width = Math.max(1, Math.round(sourceCanvas.width * scale));
    const height = Math.max(1, Math.round(sourceCanvas.height * scale));
    const canvas = document.createElement('canvas');
    canvas.width = width;
    canvas.height = height;
    const ctx = getCanvasReadbackContext(canvas);
    if (!ctx) {
        return null;
    }
    ctx.drawImage(sourceCanvas, 0, 0, width, height);
    const imageData = ctx.getImageData(0, 0, width, height);
    const rgbData = new Uint8Array(width * height * 3);
    for (let index = 0, sourceIndex = 0; index < rgbData.length; index += 3, sourceIndex += 4) {
        rgbData[index] = imageData.data[sourceIndex];
        rgbData[index + 1] = imageData.data[sourceIndex + 1];
        rgbData[index + 2] = imageData.data[sourceIndex + 2];
    }
    return {
        data: rgbData,
        width,
        height,
        channels: 3,
    };
};

const readAscii = (view: DataView, offset: number, length: number) => {
    let value = '';
    for (let i = 0; i < length; i += 1) {
        value += String.fromCharCode(view.getUint8(offset + i));
    }
    return value;
};

const gpsRational = (view: DataView, offset: number, little: boolean) => {
    const numerator = view.getUint32(offset, little);
    const denominator = view.getUint32(offset + 4, little);
    return denominator === 0 ? 0 : numerator / denominator;
};

const TIFF_TYPE_BYTES: Record<number, number> = {
    1: 1,
    2: 1,
    3: 2,
    4: 4,
    5: 8,
    7: 1,
    9: 4,
    10: 8,
};

const TIFF_IFD0_TAGS: Record<number, string> = {
    0x010f: 'Make',
    0x0110: 'Model',
    0x0112: 'Orientation',
    0x0132: 'DateTime',
    0x8769: 'ExifIFDPointer',
};

const TIFF_EXIF_TAGS: Record<number, string> = {
    0x829a: 'ExposureTime',
    0x829d: 'FNumber',
    0x8827: 'ISOSpeedRatings',
    0x9003: 'DateTimeOriginal',
    0x920a: 'FocalLength',
    0xa002: 'ExifImageWidth',
    0xa003: 'ExifImageHeight',
    0xa405: 'FocalLengthIn35mmFilm',
    0xa434: 'LensModel',
};

const GPS_TAGS: Record<number, string> = {
    1: 'GPSLatitudeRef',
    2: 'GPSLatitude',
    3: 'GPSLongitudeRef',
    4: 'GPSLongitude',
    5: 'GPSAltitudeRef',
    6: 'GPSAltitude',
    7: 'GPSTimeStamp',
    29: 'GPSDateStamp',
};

const tiffValueOffset = (view: DataView, tiff: number, entry: number, type: number, count: number, little: boolean) => {
    const typeBytes = TIFF_TYPE_BYTES[type] || 0;
    if (!typeBytes || count < 0) {
        return -1;
    }
    const byteCount = typeBytes * count;
    if (byteCount <= 4) {
        return entry + 8;
    }
    return tiff + view.getUint32(entry + 8, little);
};

const tiffAscii = (view: DataView, offset: number, count: number) => {
    if (offset < 0 || count <= 0 || offset + count > view.byteLength) {
        return '';
    }
    return readAscii(view, offset, count).replace(/\0+$/, '').trim();
};

const tiffValueString = (view: DataView, offset: number, type: number, count: number, little: boolean) => {
    if (offset < 0 || offset >= view.byteLength) {
        return '';
    }
    try {
        if (type === 2) {
            return tiffAscii(view, offset, count);
        }
        if (type === 3 && offset + 2 <= view.byteLength) {
            const values = Array.from({ length: Math.min(count, 4) }, (_, idx) => (
                offset + idx * 2 + 2 <= view.byteLength ? String(view.getUint16(offset + idx * 2, little)) : ''
            )).filter(Boolean);
            return values.join(', ');
        }
        if (type === 4 && offset + 4 <= view.byteLength) {
            const values = Array.from({ length: Math.min(count, 4) }, (_, idx) => (
                offset + idx * 4 + 4 <= view.byteLength ? String(view.getUint32(offset + idx * 4, little)) : ''
            )).filter(Boolean);
            return values.join(', ');
        }
        if (type === 5 && offset + 8 <= view.byteLength) {
            const values = Array.from({ length: Math.min(count, 4) }, (_, idx) => {
                const valueOffset = offset + idx * 8;
                if (valueOffset + 8 > view.byteLength) {
                    return '';
                }
                const numerator = view.getUint32(valueOffset, little);
                const denominator = view.getUint32(valueOffset + 4, little);
                return denominator ? `${numerator}/${denominator}` : String(numerator);
            }).filter(Boolean);
            return values.join(', ');
        }
    } catch {
        return '';
    }
    return '';
};

const parseTiffGpsExif = (view: DataView, tiff: number): ParsedGpsExif | null => {
    if (tiff + 8 > view.byteLength) {
        return null;
    }
    const endian = readAscii(view, tiff, 2);
    const little = endian === 'II';
    if (!little && endian !== 'MM') {
        return null;
    }
    if (view.getUint16(tiff + 2, little) !== 42) {
        return null;
    }
    const ifd0 = tiff + view.getUint32(tiff + 4, little);
    if (ifd0 + 2 > view.byteLength) {
        return null;
    }
    const exif: Record<string, string> = {};
    const entries = view.getUint16(ifd0, little);
    let gpsIfd = 0;
    let exifIfd = 0;
    for (let i = 0; i < entries; i += 1) {
        const entry = ifd0 + 2 + i * 12;
        if (entry + 12 > view.byteLength) {
            break;
        }
        const tag = view.getUint16(entry, little);
        const type = view.getUint16(entry + 2, little);
        const count = view.getUint32(entry + 4, little);
        const valueOffset = tiffValueOffset(view, tiff, entry, type, count, little);
        const tagName = TIFF_IFD0_TAGS[tag];
        if (tagName && tagName !== 'ExifIFDPointer') {
            const value = tiffValueString(view, valueOffset, type, count, little);
            if (value) {
                exif[tagName] = value;
            }
        }
        if (tag === 0x8825) {
            gpsIfd = tiff + view.getUint32(entry + 8, little);
        } else if (tag === 0x8769) {
            exifIfd = tiff + view.getUint32(entry + 8, little);
        }
    }
    if (exifIfd && exifIfd + 2 <= view.byteLength) {
        const exifEntries = view.getUint16(exifIfd, little);
        for (let i = 0; i < exifEntries; i += 1) {
            const entry = exifIfd + 2 + i * 12;
            if (entry + 12 > view.byteLength) {
                break;
            }
            const tag = view.getUint16(entry, little);
            const tagName = TIFF_EXIF_TAGS[tag];
            if (!tagName) {
                continue;
            }
            const type = view.getUint16(entry + 2, little);
            const count = view.getUint32(entry + 4, little);
            const valueOffset = tiffValueOffset(view, tiff, entry, type, count, little);
            const value = tiffValueString(view, valueOffset, type, count, little);
            if (value) {
                exif[tagName] = value;
            }
        }
    }
    if (!gpsIfd || gpsIfd + 2 > view.byteLength) {
        return { exif, hasExif: true };
    }
    const gpsEntries = view.getUint16(gpsIfd, little);
    let latRef = 'N';
    let lonRef = 'E';
    let latValues: number[] | null = null;
    let lonValues: number[] | null = null;
    for (let i = 0; i < gpsEntries; i += 1) {
        const entry = gpsIfd + 2 + i * 12;
        if (entry + 12 > view.byteLength) {
            break;
        }
        const tag = view.getUint16(entry, little);
        const type = view.getUint16(entry + 2, little);
        const count = view.getUint32(entry + 4, little);
        const valueOffset = tiff + view.getUint32(entry + 8, little);
        const gpsName = GPS_TAGS[tag];
        if (gpsName) {
            const gpsValueOffset = tiffValueOffset(view, tiff, entry, type, count, little);
            const value = tiffValueString(view, gpsValueOffset, type, count, little);
            if (value) {
                exif[`GPS.${gpsName}`] = value;
            }
        }
        if ((tag === 1 || tag === 3) && type === 2) {
            const ref = String.fromCharCode(view.getUint8(entry + 8));
            if (tag === 1) latRef = ref;
            if (tag === 3) lonRef = ref;
        }
        if ((tag === 2 || tag === 4) && type === 5 && count >= 3 && valueOffset + 24 <= view.byteLength) {
            const values = [0, 1, 2].map((idx) => gpsRational(view, valueOffset + idx * 8, little));
            if (tag === 2) latValues = values;
            if (tag === 4) lonValues = values;
        }
    }
    const toDecimal = (values: number[], ref: string) => {
        const decimal = values[0] + values[1] / 60 + values[2] / 3600;
        return (ref === 'S' || ref === 'W' ? -decimal : decimal).toFixed(7).replace(/\.?0+$/, '');
    };
    if (latValues && lonValues) {
        const latitude = toDecimal(latValues, latRef);
        const longitude = toDecimal(lonValues, lonRef);
        exif['GPS.GPSLatitudeRef'] = latRef;
        exif['GPS.GPSLongitudeRef'] = lonRef;
        exif['GPS.LatitudeDecimal'] = latitude;
        exif['GPS.LongitudeDecimal'] = longitude;
        return { exif, latitude, longitude, hasExif: true };
    }
    return { exif, hasExif: true };
};

const parseJpegGpsExif = async (source: Blob | File, sourceName = ''): Promise<ParsedGpsExif | null> => {
    const contentType = source.type || '';
    if (!/^image\/jpe?g$/i.test(contentType) && !/\.(jpe?g)$/i.test(sourceName)) {
        return null;
    }
    const buffer = await readBlobArrayBuffer(source.slice(0, Math.min(source.size, 512 * 1024)));
    const view = new DataView(buffer);
    if (view.byteLength < 4 || view.getUint16(0) !== 0xffd8) {
        return null;
    }
    let offset = 2;
    while (offset + 4 < view.byteLength) {
        if (view.getUint8(offset) !== 0xff) {
            break;
        }
        const marker = view.getUint8(offset + 1);
        const size = view.getUint16(offset + 2);
        if (marker === 0xe1 && size > 8 && readAscii(view, offset + 4, 6) === 'Exif\0\0') {
            return parseTiffGpsExif(view, offset + 10) || { exif: {}, hasExif: true };
        }
        offset += 2 + size;
    }
    return { exif: {}, hasExif: false };
};

const bytesMatchAscii = (bytes: Uint8Array, offset: number, text: string) => {
    if (offset < 0 || offset + text.length > bytes.length) {
        return false;
    }
    for (let i = 0; i < text.length; i += 1) {
        if (bytes[offset + i] !== text.charCodeAt(i)) {
            return false;
        }
    }
    return true;
};

const isTiffHeaderAt = (bytes: Uint8Array, offset: number) => (
    offset >= 0
    && offset + 4 <= bytes.length
    && (
        (bytes[offset] === 0x49 && bytes[offset + 1] === 0x49 && bytes[offset + 2] === 0x2a && bytes[offset + 3] === 0x00)
        || (bytes[offset] === 0x4d && bytes[offset + 1] === 0x4d && bytes[offset + 2] === 0x00 && bytes[offset + 3] === 0x2a)
    )
);

const mergeParsedExif = (existing: ParsedGpsExif | null, next: ParsedGpsExif | null): ParsedGpsExif | null => {
    if (!next) {
        return existing;
    }
    if (!existing) {
        return next;
    }
    return {
        exif: { ...existing.exif, ...next.exif },
        latitude: existing.latitude || next.latitude,
        longitude: existing.longitude || next.longitude,
        hasExif: existing.hasExif || next.hasExif,
    };
};

const parseTiffCandidates = (view: DataView, bytes: Uint8Array, start = 0, end = bytes.length): ParsedGpsExif | null => {
    let best: ParsedGpsExif | null = null;
    const safeStart = Math.max(0, start);
    const safeEnd = Math.min(bytes.length - 4, end);
    for (let offset = safeStart; offset <= safeEnd; offset += 1) {
        if (!isTiffHeaderAt(bytes, offset)) {
            continue;
        }
        const parsed = parseTiffGpsExif(view, offset);
        if (!parsed) {
            continue;
        }
        best = mergeParsedExif(best, parsed);
        if (parsed.latitude && parsed.longitude) {
            return best;
        }
    }
    return best;
};

const parseExifSignatureCandidates = (view: DataView, bytes: Uint8Array): ParsedGpsExif | null => {
    let best: ParsedGpsExif | null = null;
    for (let offset = 0; offset <= bytes.length - 10; offset += 1) {
        if (!bytesMatchAscii(bytes, offset, 'Exif\0\0')) {
            continue;
        }
        const tiffOffset = offset + 6;
        if (!isTiffHeaderAt(bytes, tiffOffset)) {
            continue;
        }
        const parsed = parseTiffGpsExif(view, tiffOffset);
        if (!parsed) {
            continue;
        }
        best = mergeParsedExif(best, parsed);
        if (parsed.latitude && parsed.longitude) {
            return best;
        }
    }
    return best;
};

const parseIsoBmffExifCandidates = (view: DataView, bytes: Uint8Array, start = 0, end = bytes.length, depth = 0): ParsedGpsExif | null => {
    if (depth > 4) {
        return null;
    }
    let best: ParsedGpsExif | null = null;
    let offset = Math.max(0, start);
    const safeEnd = Math.min(bytes.length, end);
    const containerBoxes = new Set(['moov', 'trak', 'mdia', 'minf', 'stbl', 'meta', 'iprp', 'ipco', 'iinf']);
    while (offset + 8 <= safeEnd) {
        let size = view.getUint32(offset);
        const type = readAscii(view, offset + 4, 4);
        let header = 8;
        if (size === 1 && offset + 16 <= safeEnd) {
            const high = view.getUint32(offset + 8);
            const low = view.getUint32(offset + 12);
            if (high > 0 || low <= 16) {
                break;
            }
            size = low;
            header = 16;
        } else if (size === 0) {
            size = safeEnd - offset;
        }
        if (size < header || offset + size > safeEnd) {
            break;
        }
        const payloadStart = offset + header + (type === 'uuid' ? 16 : 0) + (type === 'meta' ? 4 : 0);
        const payloadEnd = offset + size;
        if (payloadStart < payloadEnd) {
            if (type.toLowerCase().includes('exif') || type === 'uuid') {
                best = mergeParsedExif(best, parseExifSignatureCandidates(view, bytes));
                best = mergeParsedExif(best, parseTiffCandidates(view, bytes, payloadStart, payloadEnd));
                if (best?.latitude && best.longitude) {
                    return best;
                }
            }
            if (containerBoxes.has(type)) {
                best = mergeParsedExif(best, parseIsoBmffExifCandidates(view, bytes, payloadStart, payloadEnd, depth + 1));
                if (best?.latitude && best.longitude) {
                    return best;
                }
            }
        }
        offset += size;
    }
    return best;
};

const parseRawGpsExif = async (file: File): Promise<ParsedGpsExif | null> => {
    if (!isRawFile(file)) {
        return null;
    }
    const buffer = await readBlobArrayBuffer(file.slice(0, Math.min(file.size, CLIENT_RAW_EXIF_SCAN_MAX_BYTES)));
    const view = new DataView(buffer);
    const bytes = new Uint8Array(buffer);
    const directTiff = parseTiffGpsExif(view, 0);
    if (directTiff?.latitude && directTiff.longitude) {
        return directTiff;
    }
    const exifMarker = parseExifSignatureCandidates(view, bytes);
    if (exifMarker?.latitude && exifMarker.longitude) {
        return exifMarker;
    }
    const isoExif = parseIsoBmffExifCandidates(view, bytes);
    if (isoExif?.latitude && isoExif.longitude) {
        return isoExif;
    }
    return mergeParsedExif(mergeParsedExif(directTiff, exifMarker), isoExif) || parseTiffCandidates(view, bytes);
};

export const runBrowserProcessing = async (
    file: File,
    clientAssetId: string,
    batchStartedAt: number,
    browserAiModelState?: BrowserAiModelState,
    partialResult?: ClientProcessingResult,
    processingOptions: {
        faceRotationDegrees?: number;
        thumbnailRotationDegrees?: number;
        convertedPreview?: Blob | File;
        aiWorkerHandle?: BrowserAiWorkerHandle;
    } = {},
): Promise<ClientProcessingResult> => {
    const clientProcessing: Record<string, any> = partialResult?.clientProcessing || {};
    const clientProcessingReport: ClientProcessingReportItem[] = partialResult?.clientProcessingReport || [];
    // On-device AI (OCR, vision, face) only runs once the user has loaded browser AI,
    // AND only when the deploy-time processing mode allows client-side AI at all --
    // 'backend' mode means the browser never attempts these regardless of model
    // state, leaving them to ipworker exclusively. Until ready, these steps are
    // left untouched so they stay pending and get backfilled once eligible.
    // thumbnail and exif are not AI-gated (no model dependency) but are still
    // skipped outright in 'backend' mode -- ipwork_thumbnail.py/ipwork_geo.py
    // own them there so bulk reprocessing doesn't need a live browser tab
    // (geocode is gated separately below, transitively via exif).
    const processingMode = getRuntimeConfig().processingMode || 'browser';
    const browserAiReady = processingMode !== 'backend' && browserAiModelState?.status === 'available';

    if (isVideoFile(file)) {
        // Videos only get a poster-frame thumbnail in the browser; EXIF metadata
        // (date/location) is extracted server-side and image AI steps do not apply.
        const videoSourceFields: Partial<ClientProcessingReportItem> = {
            sourceKind: 'original',
            sourceFormat: getFileExtension(file.name) || file.type || 'video',
            originalBytes: file.size,
            sourceBytes: file.size,
        };
        const videoStartedAt = performance.now();
        if (processingMode === 'backend') {
            clientProcessingReport.push(makeClientReport(clientAssetId, 'thumbnail', 'skipped', 'backend_processing_mode', videoStartedAt, {
                runtime: 'canvas-video',
                ...videoSourceFields,
            }));
        } else try {
            const thumbnail = await withTimeout(createVideoBrowserThumbnail(file), CLIENT_BROWSER_STEP_BUDGET_MS);
            if (thumbnail) {
                clientProcessing.thumbnail = {
                    hasData: true,
                    contentType: 'image/jpeg',
                    data: thumbnail.dataUrl,
                    width: thumbnail.width,
                    height: thumbnail.height,
                    rotationDegrees: thumbnail.rotationDegrees,
                    source: 'browser',
                    ...videoSourceFields,
                };
                clientProcessingReport.push(makeClientReport(clientAssetId, 'thumbnail', 'done', 'done', videoStartedAt, {
                    runtime: 'canvas-video',
                    ...videoSourceFields,
                }));
            } else {
                clientProcessingReport.push(makeClientReport(clientAssetId, 'thumbnail', 'skipped', 'video_unsupported', videoStartedAt, {
                    runtime: 'canvas-video',
                    ...videoSourceFields,
                }));
            }
        } catch {
            clientProcessingReport.push(makeClientReport(clientAssetId, 'thumbnail', 'failed', 'video_unsupported', videoStartedAt, {
                runtime: 'canvas-video',
                ...videoSourceFields,
            }));
        }
        const videoSkippedSteps: ClientProcessingStep[] = ['exif', 'ocr', 'ai_vision', 'map_detection', 'face'];
        videoSkippedSteps.forEach((step) => {
            clientProcessingReport.push(makeClientReport(clientAssetId, step, 'skipped', 'video_unsupported', videoStartedAt, {
                runtime: 'browser-video',
                ...videoSourceFields,
            }));
        });
        return { clientProcessing, clientProcessingReport };
    }

    const conservative = isConservativeBrowserMode();
    const networkReason = getPoorNetworkReason();
    const admissionExpired = performance.now() - batchStartedAt > CLIENT_BATCH_AI_ADMISSION_BUDGET_MS;
    const rawFallback = (source: BrowserVisionSource, startedAt: number) => {
        const reason = source.skipReason || 'raw_container_unsupported';
        const sourceFields = getSourceReportFields(source);
        clientProcessingReport.push(makeClientReport(clientAssetId, 'thumbnail', 'skipped', reason, startedAt, {
            runtime: 'browser-raw-preview',
            ...sourceFields,
        }));
        clientProcessingReport.push(makeClientReport(clientAssetId, 'ocr', 'skipped', reason, startedAt, {
            runtime: 'browser-raw-preview',
            ...sourceFields,
        }));
        clientProcessingReport.push(makeClientReport(clientAssetId, 'ai_vision', 'skipped', reason, startedAt, {
            ...makeModelReportFields(browserAiModelState),
            runtime: 'browser-raw-preview',
            ...sourceFields,
        }));
    };

    const sourceStartedAt = performance.now();
    const fileIsRaw = isRawFile(file);
    const resolvedVisionSource = fileIsRaw
        ? await resolveBrowserVisionSource(file, processingOptions.convertedPreview)
        : await withTimeout(resolveBrowserVisionSource(file, processingOptions.convertedPreview), CLIENT_BROWSER_STEP_BUDGET_MS);
    const visionSource: BrowserVisionSource = resolvedVisionSource || {
        imageSource: null,
        sourceKind: fileIsRaw ? 'unsupported' : 'original',
        sourceFormat: getFileExtension(file.name) || file.type || 'unknown',
        rawParserVersion: fileIsRaw ? RAW_PARSER_VERSION : undefined,
        originalBytes: file.size,
        sourceBytes: 0,
        skipReason: fileIsRaw ? 'raw_container_unsupported' : 'inference_timeout',
        isRaw: fileIsRaw,
    };
    const sourceFields = getSourceReportFields(visionSource);

    let startedAt = performance.now();
    if (processingMode === 'backend') {
        clientProcessingReport.push(makeClientReport(clientAssetId, 'thumbnail', 'skipped', 'backend_processing_mode', startedAt, {
            runtime: 'canvas',
            ...sourceFields,
        }));
    } else if (visionSource.imageSource) {
        try {
            const thumbnail = await withTimeout(createBrowserThumbnail(visionSource.imageSource, processingOptions.thumbnailRotationDegrees || 0), CLIENT_BROWSER_STEP_BUDGET_MS);
            if (thumbnail) {
                clientProcessing.thumbnail = {
                    hasData: true,
                    contentType: 'image/jpeg',
                    data: thumbnail.dataUrl,
                    width: thumbnail.width,
                    height: thumbnail.height,
                    rotationDegrees: thumbnail.rotationDegrees,
                    source: 'browser',
                    ...sourceFields,
                };
                clientProcessingReport.push(makeClientReport(clientAssetId, 'thumbnail', 'done', 'done', startedAt, {
                    runtime: 'canvas',
                    ...sourceFields,
                }));
            } else {
                // null from createBrowserThumbnail means a transient canvas API
                // failure (memory pressure, toBlob returning null, etc.) — NOT that
                // the format is inherently unsupported. Report 'failed'/'unknown_error'
                // so the status stays retryable instead of being permanently terminal.
                clientProcessingReport.push(makeClientReport(clientAssetId, 'thumbnail', 'failed', 'unknown_error', startedAt, {
                    runtime: 'canvas',
                    ...sourceFields,
                }));
            }
        } catch {
            const reason = visionSource.isRaw ? 'raw_preview_invalid' : 'unknown_error';
            clientProcessingReport.push(makeClientReport(clientAssetId, 'thumbnail', 'failed', reason, startedAt, {
                runtime: 'canvas',
                ...sourceFields,
            }));
        }
    } else if (visionSource.isRaw) {
        rawFallback(visionSource, sourceStartedAt);
    } else {
        clientProcessingReport.push(makeClientReport(clientAssetId, 'thumbnail', 'timeout', 'inference_timeout', sourceStartedAt, {
            runtime: 'browser-source-resolver',
            ...sourceFields,
        }));
    }

    startedAt = performance.now();
    let parsedGpsExif: ParsedGpsExif | null = null;
    // In 'backend' mode ipworker owns exif (and, by extension below, geocode --
    // it depends on parsedGpsExif) entirely; leaving parsedGpsExif null here
    // naturally skips the geocode block too without a second gate there.
    if (processingMode === 'backend') {
        clientProcessingReport.push(makeClientReport(clientAssetId, 'exif', 'skipped', 'backend_processing_mode', startedAt, {
            runtime: 'browser-dataview',
            ...sourceFields,
        }));
    } else try {
        const exif = await withTimeout(
            visionSource.isRaw ? parseRawGpsExif(file) : parseJpegGpsExif(file, file.name),
            CLIENT_BROWSER_STEP_BUDGET_MS,
        );
        parsedGpsExif = exif;
        if (exif && exif.hasExif) {
            const hasGps = Boolean(exif.latitude && exif.longitude);
            clientProcessing.exif = {
                hasData: hasGps || Object.keys(exif.exif).length > 0,
                data: exif.exif,
                latitude: exif.latitude,
                longitude: exif.longitude,
                source: 'browser',
                ...(visionSource.isRaw ? { ...sourceFields, sourceKind: 'raw_exif_only' } : sourceFields),
            };
            clientProcessingReport.push(makeClientReport(clientAssetId, 'exif', 'done', 'done', startedAt, {
                runtime: 'browser-dataview',
                ...(visionSource.isRaw ? { ...sourceFields, sourceKind: 'raw_exif_only' as const } : sourceFields),
            }));
        } else if (exif) {
            clientProcessing.exif = {
                hasData: false,
                data: {},
                source: 'browser',
                ...(visionSource.isRaw ? { ...sourceFields, sourceKind: 'raw_exif_only' } : sourceFields),
            };
            clientProcessingReport.push(makeClientReport(clientAssetId, 'exif', 'done', 'done', startedAt, {
                runtime: 'browser-dataview',
                ...(visionSource.isRaw ? { ...sourceFields, sourceKind: 'raw_exif_only' as const } : sourceFields),
            }));
        } else {
            clientProcessingReport.push(makeClientReport(clientAssetId, 'exif', 'unsupported', visionSource.isRaw ? 'raw_container_unsupported' : 'unsupported_runtime', startedAt, {
                runtime: 'browser-dataview',
                ...(visionSource.isRaw ? { ...sourceFields, sourceKind: 'unsupported' as const } : sourceFields),
            }));
        }
    } catch {
        clientProcessingReport.push(makeClientReport(clientAssetId, 'exif', 'failed', visionSource.isRaw ? 'raw_container_unsupported' : 'unknown_error', startedAt, {
            runtime: 'browser-dataview',
            ...(visionSource.isRaw ? { ...sourceFields, sourceKind: 'unsupported' as const } : sourceFields),
        }));
    }

    if (visionSource.isRaw && !visionSource.imageSource) {
        return { clientProcessing, clientProcessingReport };
    }

    startedAt = performance.now();
    if (visionSource.imageSource && !browserAiReady) {
        clientProcessingReport.push(makeClientReport(clientAssetId, 'ocr', 'skipped', 'model_unavailable', startedAt, {
            runtime: 'tesseract.js',
            detail: 'browser_ai_not_loaded',
            ...sourceFields,
        }));
    } else if (visionSource.imageSource) {
        try {
            const ocrText = await withTimeout(runBrowserOcr(visionSource.imageSource), CLIENT_BROWSER_STEP_BUDGET_MS);
            const normalizedOcr = String(ocrText || '').trim().slice(0, 2048);
            clientProcessing.ocr = {
                hasData: Boolean(normalizedOcr),
                text: normalizedOcr,
                source: 'browser',
                ...sourceFields,
            };
            clientProcessingReport.push(makeClientReport(clientAssetId, 'ocr', normalizedOcr ? 'done' : 'skipped', normalizedOcr ? 'done' : 'upstream_incomplete', startedAt, {
                runtime: 'tesseract.js',
                ...sourceFields,
            }));
        } catch {
            clientProcessing.ocr = {
                hasData: false,
                text: '',
                source: 'browser',
                ...sourceFields,
            };
            clientProcessingReport.push(makeClientReport(clientAssetId, 'ocr', 'failed', 'unknown_error', startedAt, {
                runtime: 'tesseract.js',
                ...sourceFields,
            }));
        }
    } else if (visionSource.isRaw) {
        clientProcessing.ocr = {
            hasData: false,
            text: '',
            source: 'browser',
            ...sourceFields,
        };
        clientProcessingReport.push(makeClientReport(clientAssetId, 'ocr', 'skipped', visionSource.skipReason || 'raw_preview_missing', startedAt, {
            runtime: 'browser-raw-preview',
            ...sourceFields,
        }));
    }

    const exifGps = visionSource.imageSource ? parsedGpsExif : null;
    if (exifGps?.latitude && exifGps.longitude) {
        startedAt = performance.now();
        try {
            const location = await withTimeout(geocodeWithThrottle(exifGps.latitude, exifGps.longitude), CLIENT_BROWSER_STEP_BUDGET_MS);
            clientProcessing.map_detection = {
                hasData: true,
                latitude: exifGps.latitude,
                longitude: exifGps.longitude,
                address: location?.address || '',
                city: location?.city || '',
                country: location?.country || '',
                source: 'browser',
                ...sourceFields,
            };
            clientProcessingReport.push(makeClientReport(clientAssetId, 'map_detection', 'done', 'done', startedAt, {
                runtime: 'browser-geocoder',
                ...sourceFields,
            }));
        } catch {
            clientProcessing.map_detection = {
                hasData: true,
                latitude: exifGps.latitude,
                longitude: exifGps.longitude,
                source: 'browser',
                ...sourceFields,
            };
            clientProcessingReport.push(makeClientReport(clientAssetId, 'map_detection', 'skipped', 'unknown_error', startedAt, {
                runtime: 'browser-geocoder',
                ...sourceFields,
            }));
        }
    } else {
        clientProcessingReport.push(makeClientReport(clientAssetId, 'map_detection', 'skipped', 'upstream_incomplete', performance.now(), {
            runtime: 'browser-geocoder',
            ...sourceFields,
        }));
    }

    startedAt = performance.now();
    try {
        const faceSource = visionSource.imageSource;
        if (!faceSource) {
            const isBackgroundThrottled = typeof document !== 'undefined' && document.visibilityState !== 'visible';
            const faceSkipReason = visionSource.skipReason || 'raw_preview_missing';
            const faceFailureStage: BrowserFaceFailureStage = isBackgroundThrottled
                ? 'background_throttled'
                : (faceSkipReason === 'raw_preview_missing' ? 'source_unavailable' : 'unsupported_runtime');
            clientProcessing.face = {
                hasData: false,
                faces: [],
                source: 'browser',
                embeddingsReady: false,
                faceModelReady: false,
                deferredReason: isBackgroundThrottled ? 'background_throttled' : faceSkipReason,
                faceFailureStage,
                faceFailureDetail: isBackgroundThrottled ? 'browser_background_throttled' : faceSkipReason,
                ...sourceFields,
            };
            clientProcessingReport.push(makeClientReport(clientAssetId, 'face', 'skipped', faceSkipReason, startedAt, {
                runtime: visionSource.isRaw ? 'browser-raw-preview' : 'browser-face-detector',
                reason: isBackgroundThrottled ? 'background_throttled' : faceSkipReason,
                detail: isBackgroundThrottled ? 'browser_background_throttled' : faceSkipReason,
                faceFailureStage,
                faceFailureDetail: isBackgroundThrottled ? 'browser_background_throttled' : faceSkipReason,
                ...sourceFields,
            }));
        } else if (!browserAiReady) {
            // Browser AI isn't loaded yet: don't spin up BlazeFace/ArcFace. Leave the
            // face step untouched so it stays pending and is backfilled once loaded.
            clientProcessing.face = {
                hasData: false,
                faces: [],
                source: 'browser',
                embeddingsReady: false,
                faceModelReady: false,
                deferredReason: 'browser_ai_not_loaded',
                faceFailureStage: 'unsupported_runtime',
                faceFailureDetail: 'browser_ai_not_loaded',
                ...sourceFields,
            };
            clientProcessingReport.push(makeClientReport(clientAssetId, 'face', 'skipped', 'model_unavailable', startedAt, {
                runtime: 'browser-face-detector',
                reason: 'model_unavailable',
                detail: 'browser_ai_not_loaded',
                faceFailureStage: 'unsupported_runtime',
                faceFailureDetail: 'browser_ai_not_loaded',
                ...sourceFields,
            }));
        } else {
            // A prior photo's detection call may still be running in the background
            // (its own step timed out, but the underlying inference wasn't
            // cancellable) -- don't start a second concurrent pass on the same
            // shared session. Treat it exactly like a timeout: deferred, not a
            // "zero faces found" result, so it gets retried once capacity frees up.
            const faceDetectorBusy = faceDetectionActiveCount >= FACE_DETECTION_MAX_CONCURRENT;
            const faceAttempt = faceDetectorBusy
                ? ({ timedOut: true, value: null } as const)
                : await withTimeoutOutcome(
                    runNativeFaceDetection(faceSource, processingOptions.faceRotationDegrees || 0),
                    CLIENT_FACE_STEP_BUDGET_MS,
                );
            const faceResult = faceAttempt.value;
            if (faceAttempt.timedOut) {
                const isBackgroundThrottled = typeof document !== 'undefined' && document.visibilityState !== 'visible';
                const deferredReason = isBackgroundThrottled ? 'background_throttled' : 'inference_timeout';
                const faceFailureStage = isBackgroundThrottled ? 'background_throttled' : 'timeout';
                const faceFailureDetail = faceDetectorBusy ? 'browser_face_detector_busy' : (isBackgroundThrottled ? 'browser_background_throttled' : 'blazeface_load_timeout');
                clientProcessing.face = {
                    hasData: false,
                    faces: [],
                    source: 'browser',
                    embeddingsReady: false,
                    faceModelReady: false,
                    deferredReason,
                    faceFailureStage,
                    faceFailureDetail,
                    debugStages: faceDetectorBusy ? ['model_load_started', 'busy'] : (isBackgroundThrottled ? ['model_load_started', 'background_throttled'] : ['model_load_started', 'timeout']),
                    ...sourceFields,
                };
                clientProcessingReport.push(makeClientReport(clientAssetId, 'face', 'timeout', deferredReason, startedAt, {
                    runtime: 'browser-face-detector',
                    reason: deferredReason,
                    detail: faceFailureDetail,
                    faceFailureStage,
                    faceFailureDetail,
                    ...sourceFields,
                }));
            } else {
                const faces = toArray<BrowserFaceDetection>(faceResult?.faces);
                const faceCountFields = {
                    rawFaceCount: Math.max(0, Number(faceResult?.rawFaceCount ?? faceResult?.detectedFaceCount ?? faces.length) || 0),
                    detectedFaceCount: Math.max(0, Number(faceResult?.detectedFaceCount ?? faceResult?.rawFaceCount ?? faces.length) || 0),
                    candidateFaceCount: Math.max(0, Number(faceResult?.candidateFaceCount ?? faces.length) || 0),
                    filteredFaceCount: Math.max(0, Number(faceResult?.filteredFaceCount ?? 0) || 0),
                    ...(typeof faceResult?.filteredReason === 'string' && faceResult.filteredReason
                        ? { filteredReason: faceResult.filteredReason }
                        : {}),
                };
                // A face can reach here already carrying a valid embedding (it only
                // exists in `faces` because it passed the SAME normalizeFaceEmbedding
                // check once already, inside collectBlazeFaceCandidates) yet fail
                // this second, redundant check anyway -- an observed failure mode
                // (2026-08-01, ~4/24 photos in one batch, faces visibly present in
                // the source photo) with no reproduction found via static analysis
                // or native-runtime testing of the embedding model itself. Capture
                // what the embedding actually looked like at the drop point so the
                // next occurrence is diagnosable from stored data.
                let secondStageEmbeddingDropDetail: string | undefined;
                const normalizedFaces = faces
                    .map((face: any) => {
                        const bbox = face?.bbox || {};
                        const rawEmbedding = face?.embedding;
                        const embedding = normalizeFaceEmbedding(rawEmbedding) || [];
                        // Widened (2026-08-01) to also fire when rawEmbedding itself is
                        // missing/undefined, not just when it's present-but-unnormalizable:
                        // a face can only ever reach this array already carrying an
                        // embedding (collectBlazeFaceCandidates only pushes bbox+embedding
                        // together), so bbox-present-but-embedding-absent here is *always*
                        // a bug, never a legitimate "no data" state. The previous, narrower
                        // condition (rawEmbedding truthy) missed exactly that case, letting
                        // it fall through to a silent, non-retryable-looking 'no_data'
                        // instead of a diagnosable 'descriptor_failed'.
                        if (!embedding.length && !secondStageEmbeddingDropDetail) {
                            const isArr = Array.isArray(rawEmbedding);
                            const rawLength = (rawEmbedding as ArrayLike<number> | undefined)?.length;
                            const sample = isArr ? (rawEmbedding as number[]).slice(0, 4) : undefined;
                            const faceKeys = face && typeof face === 'object' ? Object.keys(face).sort().join('|') : 'not_object';
                            secondStageEmbeddingDropDetail = `second_stage_embedding_drop: type=${typeof rawEmbedding}_isArray=${isArr}_length=${rawLength}_sample=${JSON.stringify(sample)}_faceKeys=${faceKeys}_confidence=${face?.confidence}_alignmentMethod=${face?.alignmentMethod}`;
                        }
                        return {
                            bbox: {
                                left: Math.max(0, Number(bbox.left ?? 0)),
                                top: Math.max(0, Number(bbox.top ?? 0)),
                                width: Math.max(0, Number(bbox.width ?? 0)),
                                height: Math.max(0, Number(bbox.height ?? 0)),
                            },
                            confidence: clampDetectorConfidence(face?.confidence),
                            imageWidth: Math.max(0, Number(face?.imageWidth ?? 0)),
                            imageHeight: Math.max(0, Number(face?.imageHeight ?? 0)),
                            ...(typeof face?.detector === 'string' && face.detector ? { detector: face.detector } : {}),
                            ...(typeof face?.alignmentMethod === 'string' && face.alignmentMethod ? { alignmentMethod: face.alignmentMethod } : {}),
                            ...(typeof face?.alignmentFailureReason === 'string' && face.alignmentFailureReason
                                ? { alignmentFailureReason: face.alignmentFailureReason }
                                : {}),
                            ...(embedding.length ? { embedding: embedding.slice(0, ARCFACE_EMBEDDING_DIMENSIONS) } : {}),
                        };
                    })
                    .filter((face: any) => face.bbox.width > 0 && face.bbox.height > 0 && face.imageWidth > 0 && face.imageHeight > 0);
                const facesWithEmbeddings = normalizedFaces.filter((face: any) => Array.isArray(face.embedding) && face.embedding.length > 0);

                if (faceResult && normalizedFaces.length > 0 && facesWithEmbeddings.length > 0) {
                    clientProcessing.face = {
                        hasData: true,
                        faces: facesWithEmbeddings,
                        source: 'browser',
                        embeddingsReady: true,
                        faceModelReady: true,
                        debugStages: Array.isArray(faceResult?.debugStages) ? faceResult.debugStages : undefined,
                        ...faceCountFields,
                        ...sourceFields,
                        model: faceResult.model || `blazeface+${ARCFACE_MODEL_NAME}`,
                        modelVersion: faceResult.modelVersion || ARCFACE_MODEL_VERSION,
                        modelTaxonomyVersion: faceResult.modelTaxonomyVersion || ARCFACE_EMBEDDING_VERSION,
                        runtime: faceResult.runtime || `browser-blazeface+${ARCFACE_RUNTIME}`,
                        schemaVersion: faceResult.schemaVersion || 2,
                    };
                    clientProcessingReport.push(makeClientReport(clientAssetId, 'face', 'done', 'done', startedAt, {
                        runtime: faceResult.runtime || `browser-blazeface+${ARCFACE_RUNTIME}`,
                        model: faceResult.model || `blazeface+${ARCFACE_MODEL_NAME}`,
                        modelVersion: faceResult.modelVersion || ARCFACE_MODEL_VERSION,
                        modelTaxonomyVersion: faceResult.modelTaxonomyVersion || ARCFACE_EMBEDDING_VERSION,
                        ...faceCountFields,
                        ...sourceFields,
                    }));
                } else if (faceResult) {
                    const noAcceptableFaces = normalizedFaces.length === 0;
                    const isBackgroundThrottled = typeof document !== 'undefined' && document.visibilityState !== 'visible';
                    const sawDetectorFaces = faceCountFields.rawFaceCount > 0 || faceCountFields.detectedFaceCount > 0 || faceCountFields.candidateFaceCount > 0;
                    const faceFailureStage = isBackgroundThrottled
                        ? 'background_throttled'
                        : (secondStageEmbeddingDropDetail || (sawDetectorFaces && faceCountFields.candidateFaceCount === 0)
                            ? 'descriptor_failed'
                            : undefined);
                    const faceFailureDetail = faceFailureStage
                        ? (secondStageEmbeddingDropDetail || faceCountFields.filteredReason || getFaceFailureDetail(faceResult))
                        : undefined;
                    const faceReportStatus = faceFailureStage ? 'failed' : (noAcceptableFaces ? 'done' : 'skipped');
                    const faceReportReason = faceFailureStage ? 'model_unavailable' : (noAcceptableFaces ? 'done' : 'model_unavailable');
                    const faceReportDetail = faceFailureDetail || (noAcceptableFaces
                        ? (faceCountFields.filteredReason || 'no_acceptable_faces')
                        : isBackgroundThrottled
                            ? 'browser_background_throttled'
                            : (normalizedFaces.length > 0 ? 'face_detected_but_embeddings_missing' : 'face_model_loaded_but_embeddings_missing'));
                    clientProcessing.face = {
                        hasData: false,
                        faces: [],
                        source: 'browser',
                        embeddingsReady: noAcceptableFaces && !sawDetectorFaces,
                        faceModelReady: true,
                        embeddingMissing: sawDetectorFaces,
                        ...(faceFailureStage ? { faceFailureStage } : {}),
                        ...(faceFailureDetail ? { faceFailureDetail } : {}),
                        debugStages: Array.isArray(faceResult?.debugStages) ? faceResult.debugStages : undefined,
                        ...(isBackgroundThrottled
                            ? { deferredReason: 'background_throttled' }
                            : {}),
                        ...faceCountFields,
                        ...(noAcceptableFaces ? { filteredReason: faceCountFields.filteredReason || 'no_acceptable_faces' } : {}),
                        ...sourceFields,
                    };
                    clientProcessingReport.push(makeClientReport(clientAssetId, 'face', faceReportStatus, faceReportReason, startedAt, {
                        runtime: faceResult.runtime || `browser-blazeface+${ARCFACE_RUNTIME}`,
                        reason: faceReportReason,
                        detail: faceReportDetail,
                        ...faceCountFields,
                        ...(faceFailureStage ? { faceFailureStage } : {}),
                        ...(faceFailureDetail ? { faceFailureDetail } : {}),
                        ...sourceFields,
                    }));
                } else {
                    const isBackgroundThrottled = typeof document !== 'undefined' && document.visibilityState !== 'visible';
                    clientProcessing.face = {
                        hasData: false,
                        faces: [],
                        source: 'browser',
                        embeddingsReady: false,
                        faceModelReady: false,
                        deferredReason: isBackgroundThrottled ? 'background_throttled' : 'unsupported_runtime',
                        faceFailureStage: isBackgroundThrottled ? 'background_throttled' : 'unsupported_runtime',
                        faceFailureDetail: isBackgroundThrottled ? 'browser_background_throttled' : 'browser_face_detector_unavailable',
                        debugStages: ['model_load_started'],
                        ...sourceFields,
                    };
                    clientProcessingReport.push(makeClientReport(clientAssetId, 'face', 'skipped', 'unsupported_runtime', startedAt, {
                        runtime: 'browser-face-detector',
                        reason: isBackgroundThrottled ? 'background_throttled' : 'unsupported_runtime',
                        detail: isBackgroundThrottled ? 'browser_background_throttled' : 'browser_face_detector_unavailable',
                        faceFailureStage: isBackgroundThrottled ? 'background_throttled' : 'unsupported_runtime',
                        faceFailureDetail: isBackgroundThrottled ? 'browser_background_throttled' : 'browser_face_detector_unavailable',
                        ...sourceFields,
                    }));
                }
            }
        }
    } catch (err) {
        const normalizedError = err instanceof FaceDetectionUnavailableError
            ? { reason: err.reason, detail: err.message }
            : normalizeBrowserAiError(err);
        const isBackgroundThrottled = normalizedError.reason === 'unsupported_runtime'
            && typeof document !== 'undefined'
            && document.visibilityState !== 'visible';
        const faceReportStatus = normalizedError.reason === 'inference_timeout'
            ? 'timeout'
            : normalizedError.reason === 'unsupported_runtime'
                ? 'skipped'
                : 'failed';
        const faceFailureStage = isBackgroundThrottled
            ? 'background_throttled'
            : getFaceFailureStage(err, Array.isArray((err as any)?.debugStages) ? (err as any).debugStages : []);
        clientProcessing.face = {
            hasData: false,
            faces: [],
            source: 'browser',
            embeddingsReady: false,
            faceModelReady: false,
            deferredReason: isBackgroundThrottled ? 'background_throttled' : normalizedError.reason,
            faceFailureStage,
            faceFailureDetail: getFaceFailureDetail(err),
            debugStages: Array.isArray((err as any)?.debugStages) ? (err as any).debugStages : ['model_load_started'],
            ...sourceFields,
        };
        clientProcessingReport.push(makeClientReport(clientAssetId, 'face', faceReportStatus, normalizedError.reason, startedAt, {
            runtime: normalizedError.reason === 'model_load_failed' ? 'browser-face-model-loader' : 'browser-face-detector',
            reason: isBackgroundThrottled ? 'background_throttled' : normalizedError.reason,
            detail: isBackgroundThrottled ? 'browser_background_throttled' : normalizedError.detail,
            faceModelReady: false,
            embeddingsReady: false,
            faceFailureStage,
            faceFailureDetail: getFaceFailureDetail(err),
            ...sourceFields,
        }));
    }

    const modelReportFields = makeModelReportFields(browserAiModelState);
    const modelSkipReason: ClientProcessingReason = browserAiModelState?.status === 'unavailable' || browserAiModelState?.status === 'unsupported'
        ? (browserAiModelState.reason || 'model_unavailable')
        : 'model_unavailable';
    let aiVisionEvaluated = false;
    const aiVisionSource = visionSource.imageSource;
    const hasUsableAiVisionSource = Boolean(aiVisionSource) && !visionSource.thumbnailOnly;
    const aiSkipReason: ClientProcessingReason | null = networkReason || (
        admissionExpired
            ? 'model_budget_exceeded'
            : browserAiReady
                ? null
                : modelSkipReason
    );
    if (!aiSkipReason && aiVisionSource && hasUsableAiVisionSource && browserAiReady) {
        const aiStartedAt = performance.now();
        aiVisionEvaluated = true;
        try {
            const aiResult = await runBrowserAiVisionInWorker(
                aiVisionSource,
                browserAiModelState,
                CLIENT_AI_INFERENCE_BUDGET_MS,
                processingOptions.aiWorkerHandle,
            );
            const localFallback = isLocalVisionFallbackResult(aiResult);
            const tags = (localFallback ? [] : toArray<unknown>(aiResult.tags))
                .map((tag: unknown) => String(tag || '').trim().toLowerCase())
                .filter(Boolean)
                .slice(0, CLIENT_AI_MAX_STORED_LABELS);
            const objects = (localFallback ? [] : toArray<unknown>(aiResult.objects))
                .map((tag: unknown) => String(tag || '').trim().toLowerCase())
                .filter(Boolean)
                .slice(0, CLIENT_AI_MAX_STORED_LABELS);
            const predictions = (localFallback ? [] : toArray<any>(aiResult.predictions))
                .slice(0, CLIENT_AI_MAX_STORED_LABELS)
                .map((item: any) => ({
                    label: String(item?.label || '').trim().toLowerCase(),
                    score: Math.max(0, Math.min(Number(item?.score || 0), 1)),
                }))
                .filter((item: { label: string; score: number }) => item.label);
            const caption = localFallback ? '' : String(aiResult.caption || '').trim().slice(0, 512);
            const ocrText = localFallback ? '' : String(aiResult.ocrText || '').trim().slice(0, 2048);
            const aiPersonLabel = localFallback ? '' : String(aiResult.aiPersonLabel || '').trim().toLowerCase().slice(0, 80);
            const aiPersonScore = localFallback ? 0 : Math.max(0, Math.min(Number(aiResult.aiPersonScore || 0), 1));
            const aiPersonCandidate = localFallback ? false : Boolean(aiResult.aiPersonCandidate);
            const imageEmbedding = (localFallback ? [] : toArray<unknown>(aiResult.imageEmbedding))
                .map((value: unknown) => Number(value))
                .filter((value: number) => Number.isFinite(value));
            const hasData = Boolean(tags.length || objects.length || caption || ocrText || aiPersonLabel || imageEmbedding.length);
            const resultModelReportFields = {
                ...modelReportFields,
                ...(aiResult.model ? { model: String(aiResult.model).slice(0, 100) } : {}),
                ...(aiResult.modelVersion ? { modelVersion: String(aiResult.modelVersion).slice(0, 100) } : {}),
                ...(aiResult.modelTaxonomyVersion ? { modelTaxonomyVersion: String(aiResult.modelTaxonomyVersion).slice(0, 100) } : {}),
                ...(aiResult.runtime ? { runtime: String(aiResult.runtime).slice(0, 100) } : {}),
            };
            clientProcessing.ai_vision = {
                hasData,
                source: 'browser',
                tags,
                objects,
                caption,
                ocrText,
                predictions,
                aiPersonCandidate,
                aiPersonLabel,
                aiPersonScore,
                imageEmbedding,
                ...sourceFields,
                ...resultModelReportFields,
            };
            clientProcessingReport.push(makeClientReport(clientAssetId, 'ai_vision', 'done', 'done', aiStartedAt, {
                ...sourceFields,
                ...resultModelReportFields,
                ...(aiResult.fallbackReason ? { detail: String(aiResult.fallbackReason).slice(0, 500) } : {}),
            }));
        } catch (err) {
            const normalizedError = normalizeBrowserAiError(err);
            const reason = normalizedError.reason === 'model_download_timeout' ? 'model_load_failed' : normalizedError.reason;
            clientProcessingReport.push(makeClientReport(clientAssetId, 'ai_vision', reason === 'inference_timeout' ? 'timeout' : 'failed', reason, aiStartedAt, {
                ...sourceFields,
                ...modelReportFields,
                detail: normalizedError.detail,
            }));
            aiVisionEvaluated = true;
        }
    }

    if (!aiVisionEvaluated) {
        const finalAiSkipReason = visionSource.thumbnailOnly
            ? (visionSource.skipReason || 'raw_preview_missing')
            : (aiSkipReason || 'model_unavailable');
        clientProcessingReport.push(makeClientReport(clientAssetId, 'ai_vision', 'skipped', finalAiSkipReason, performance.now(), {
            ...modelReportFields,
            runtime: visionSource.thumbnailOnly
                ? 'browser-raw-fallback-thumbnail'
                : (conservative ? 'conservative-browser-mode' : 'browser-no-model-configured'),
            detail: browserAiModelState?.detail || finalAiSkipReason,
            ...sourceFields,
        }));
        // The ocr block above (the visionSource.imageSource / .isRaw branches)
        // already pushes exactly one 'ocr' report for every photo except the one
        // gap it doesn't cover: imageSource absent AND isRaw false. Pushing here
        // unconditionally used to silently overwrite whatever the earlier block
        // already correctly reported (e.g. 'model_unavailable', matching what
        // 'face' reports in the same scenario) with a misleading
        // upstream_incomplete/browser-ocr-pending status -- making OCR's reported
        // status untrustworthy for telling "ran and found nothing" apart from
        // "never ran." Only fill the actual gap.
        if (!visionSource.imageSource && !visionSource.isRaw) {
            clientProcessingReport.push(makeClientReport(clientAssetId, 'ocr', 'skipped', 'upstream_incomplete', performance.now(), {
                runtime: 'browser-ocr-pending',
                ...sourceFields,
            }));
        }
    }

    return { clientProcessing, clientProcessingReport };
};

export const withFinalizeGrace = async (
    promise: Promise<ClientProcessingResult>,
    clientAssetId: string,
    partialResult?: ClientProcessingResult,
): Promise<ClientProcessingResult> => {
    const result = await withTimeout(promise, CLIENT_PROCESSING_FINALIZE_GRACE_MS);
    if (result) {
        return result;
    }
    const now = performance.now();
    const steps: ClientProcessingStep[] = ['thumbnail', 'exif', 'ocr', 'ai_vision', 'map_detection', 'face'];
    const reportedSteps = new Set((partialResult?.clientProcessingReport || []).map((item) => item.step));
    return {
        clientProcessing: { ...(partialResult?.clientProcessing || {}) },
        clientProcessingReport: [
            ...(partialResult?.clientProcessingReport || []),
            ...steps
                .filter((step) => !reportedSteps.has(step))
                .map((step) => makeClientReport(clientAssetId, step, 'timeout', 'finalize_grace_expired', now)),
        ],
        lateResultPending: true,
    };
};


interface PersistedPhotoCache {
    timestamp: number;
    photos: Photo[];
    totalAvailable: number;
    offset: number;
    hasMore: boolean;
    sortBy: string;
    searchQuery: string;
    filters: FilterOptions;
    captureStartDate: string;
    captureEndDate: string;
}

// Scope the boot cache to the active library so switching libraries never
// replays the previous library's photos. Falls back to the base key when there
// is no session (local dev / unauthenticated).
const photoCacheKey = (): string => {
    const lib = getActiveLibraryFromToken();
    return lib ? `${PHOTO_CACHE_STORAGE_KEY}.${lib}` : PHOTO_CACHE_STORAGE_KEY;
};

const loadPhotoCache = (): PersistedPhotoCache | null => {
    try {
        const raw = localStorage.getItem(photoCacheKey());
        if (!raw) {
            return null;
        }
        const parsed = JSON.parse(raw) as PersistedPhotoCache;
        if (!parsed || !Array.isArray(parsed.photos) || typeof parsed.timestamp !== 'number') {
            return null;
        }
        if (Date.now() - parsed.timestamp > PHOTO_CACHE_MAX_AGE_MS) {
            return null;
        }
        return parsed;
    } catch {
        return null;
    }
};

const writePhotoCache = (cache: PersistedPhotoCache) => {
    try {
        const key = photoCacheKey();
        // Purge the pre-scoping global cache entry (written before caches were
        // scoped per library) so it can never be replayed under another library.
        if (key !== PHOTO_CACHE_STORAGE_KEY) {
            localStorage.removeItem(PHOTO_CACHE_STORAGE_KEY);
        }
        localStorage.setItem(key, JSON.stringify(cache));
    } catch {
        // Ignore storage quota or serialization errors.
    }
};

const openUploadDb = (): Promise<IDBDatabase> => new Promise((resolve, reject) => {
    const request = indexedDB.open(UPLOAD_DB_NAME, 1);
    request.onupgradeneeded = () => {
        const db = request.result;
        if (!db.objectStoreNames.contains(UPLOAD_DB_STORE)) {
            db.createObjectStore(UPLOAD_DB_STORE);
        }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error('Failed to open upload database.'));
});

// Also accepts a FileSystemFileHandle (Chrome/Edge desktop's resume path --
// see fileSystemAccess.ts): IndexedDB's structured clone algorithm supports
// storing handles directly, and they're far cheaper to persist than the
// file's actual bytes.
export const idbPut = async (key: string, value: Blob | FileSystemFileHandle): Promise<void> => {
    const db = await openUploadDb();
    await new Promise<void>((resolve, reject) => {
        const tx = db.transaction(UPLOAD_DB_STORE, 'readwrite');
        tx.objectStore(UPLOAD_DB_STORE).put(value, key);
        tx.oncomplete = () => resolve();
        tx.onerror = () => reject(tx.error || new Error('Failed to persist upload blob.'));
    });
    db.close();
};

export const idbGet = async (key: string): Promise<Blob | FileSystemFileHandle | null> => {
    const db = await openUploadDb();
    const result = await new Promise<Blob | FileSystemFileHandle | null>((resolve, reject) => {
        const tx = db.transaction(UPLOAD_DB_STORE, 'readonly');
        const req = tx.objectStore(UPLOAD_DB_STORE).get(key);
        req.onsuccess = () => resolve((req.result as Blob | FileSystemFileHandle | undefined) || null);
        req.onerror = () => reject(req.error || new Error('Failed to load upload blob.'));
    });
    db.close();
    return result;
};

export const idbDelete = async (key: string): Promise<void> => {
    const db = await openUploadDb();
    await new Promise<void>((resolve, reject) => {
        const tx = db.transaction(UPLOAD_DB_STORE, 'readwrite');
        tx.objectStore(UPLOAD_DB_STORE).delete(key);
        tx.oncomplete = () => resolve();
        tx.onerror = () => reject(tx.error || new Error('Failed to delete upload blob.'));
    });
    db.close();
};


interface PhotoGalleryProps {
    addNotification?: (title: string, details: string, progress?: UploadProgress) => string;
    registerUploadCompletionHandler?: (handler: () => void | Promise<void>) => () => void;
    registerUploadErrorHandler?: (handler: (message: string | null) => void) => () => void;
}

interface AlbumSummary {
    id: string;
    name: string;
    photoCount: number;
}

const noopAddNotification = () => '';

const PhotoGallery: React.FC<PhotoGalleryProps> = ({
    addNotification = noopAddNotification,
    registerUploadCompletionHandler,
    registerUploadErrorHandler,
}) => {
    const location = useLocation();
    const cachedBoot = loadPhotoCache();
    const [photos, setPhotos] = useState<Photo[]>(cachedBoot?.photos || []);
    // filename -> batch-resolved access URL (see thumbnailAccessCache). '' means
    // "resolved, but the thumbnail isn't generated yet" (render placeholder);
    // an absent key means resolution for that page hasn't finished yet.
    const [thumbAccessUrls, setThumbAccessUrls] = useState<Map<string, string>>(new Map());
    const [galleryZoomLevel, setGalleryZoomLevel] = useState<number>(loadGalleryZoomLevel);
    const galleryGridRef = useRef<HTMLDivElement | null>(null);
    const pinchPointersRef = useRef<Map<number, { x: number; y: number }>>(new Map());
    // Distance between the two pointers the last time the zoom level stepped —
    // reset on every step so each subsequent step needs the same relative
    // pinch motion again, giving discrete "notches" rather than a runaway.
    const pinchStepDistanceRef = useRef<number>(0);
    // Mirrors `photos` for the processing-status poller below so that effect can
    // read the latest list on each tick without re-registering its interval
    // every time `photos` changes (which happens on far more than just
    // processing updates -- scrolling, liking, rating, filtering, ...).
    const photosRef = useRef<Photo[]>(photos);
    useEffect(() => { photosRef.current = photos; }, [photos]);

    // Bounded liveness poll for server-side (ipworker) processing: only ever
    // fires a request when at least one currently-loaded photo looks
    // mid-processing, and only refreshes those specific photos' `processing`
    // field in place (no full relist, no scroll/selection disruption).
    useEffect(() => {
        const timer = window.setInterval(async () => {
            const pendingFilenames = photosRef.current
                .filter((photo) => {
                    const proc = photo.processing;
                    if (!proc) {
                        return false;
                    }
                    if (proc.activeWorker === 'ipworker') {
                        return true;
                    }
                    return PROCESSING_STEP_KEYS.some((key) => {
                        const value = proc[key];
                        return value === 'queued' || value === 'running';
                    });
                })
                .map((photo) => photo.filename)
                .slice(0, PROCESSING_STATUS_MAX_FILENAMES);
            if (pendingFilenames.length === 0) {
                return;
            }
            try {
                const response = await get(`/photos/processing-status?filenames=${encodeURIComponent(pendingFilenames.join(','))}`);
                const statuses: Record<string, Photo['processing']> = (response && response.statuses) || {};
                if (Object.keys(statuses).length === 0) {
                    return;
                }
                setPhotos((prev) => prev.map((photo) => (
                    statuses[photo.filename]
                        ? { ...photo, processing: { ...photo.processing, ...statuses[photo.filename] } }
                        : photo
                )));
            } catch {
                // Best effort -- the next tick retries.
            }
        }, PROCESSING_STATUS_POLL_MS);
        return () => window.clearInterval(timer);
    }, []);
    const [totalAvailable, setTotalAvailable] = useState<number>(cachedBoot?.totalAvailable || 0);
    const [serverTotalLoaded, setServerTotalLoaded] = useState<boolean>(false);
    const [loading, setLoading] = useState<boolean>(false);
    const [loadingMore, setLoadingMore] = useState<boolean>(false);
    const [error, setError] = useState<string | null>(null);
    const [warmingUp, setWarmingUp] = useState<boolean>(false);
    const [searchNotice, setSearchNotice] = useState<string | null>(null);
    // Default to capture-date order ("Captured") so the gallery opens on the most
    // recently *taken* photos. Upload date ("Recent") is a poor proxy for recency
    // in a bulk-imported library — every photo finalizes at roughly the same
    // instant, so that sort collapses to its filename tie-break and reads oldest
    // first. A returning session still restores whatever sort the user last chose.
    const [sortBy, setSortBy] = useState<string>(cachedBoot?.sortBy || 'capture');
    const [offset, setOffset] = useState<number>(cachedBoot?.offset || 0);
    const [hasMore, setHasMore] = useState<boolean>(cachedBoot?.hasMore ?? true);
    const [searchInput, setSearchInput] = useState<string>(cachedBoot?.searchQuery || '');
    const [searchQuery, setSearchQuery] = useState<string>(cachedBoot?.searchQuery || '');
    const [selectedPhotos, setSelectedPhotos] = useState<Set<string>>(new Set());
    const [deleting, setDeleting] = useState<boolean>(false);
    const [deleteProgress, setDeleteProgress] = useState<{ done: number; total: number } | null>(null);
    const [filters, setFilters] = useState<FilterOptions>(cachedBoot?.filters || { minRating: 0, minLikes: 0 });
    const [mediaFilter, setMediaFilter] = useState<'all' | 'photos' | 'videos'>('all');
    const [showFilters, setShowFilters] = useState<boolean>(false);
    const [showSortMenu, setShowSortMenu] = useState<boolean>(false);
    const [showAlbumMenu, setShowAlbumMenu] = useState<boolean>(false);
    const [albumMenuOptions, setAlbumMenuOptions] = useState<AlbumSummary[] | null>(null);
    const [albumMenuLoading, setAlbumMenuLoading] = useState<boolean>(false);
    const [addingToAlbumId, setAddingToAlbumId] = useState<string | null>(null);
    const [searchOpen, setSearchOpen] = useState<boolean>(false);
    const searchRef = useRef<HTMLDivElement | null>(null);
    const sortMenuRef = useRef<HTMLDivElement | null>(null);
    const filterMenuRef = useRef<HTMLDivElement | null>(null);
    const albumMenuRef = useRef<HTMLDivElement | null>(null);
    const [captureStartDate, setCaptureStartDate] = useState<string>(cachedBoot?.captureStartDate || '');
    const [captureEndDate, setCaptureEndDate] = useState<string>(cachedBoot?.captureEndDate || '');
    const { summary: timelineSummary, status: timelineStatus } = useTimelineMetadata();
    const [downloading, setDownloading] = useState<boolean>(false);
    const [downloadProgress, setDownloadProgress] = useState<{ completed: number; total: number } | null>(null);
    const [lightboxIndex, setLightboxIndex] = useState<number | null>(null);
    const [focusedFilename, setFocusedFilename] = useState<string | null>(null);
    const [focusedPhoto, setFocusedPhoto] = useState<Photo | null>(null);
    const [focusLoading, setFocusLoading] = useState<boolean>(false);
    const [focusError, setFocusError] = useState<string | null>(null);
    const [focusLightboxOpen, setFocusLightboxOpen] = useState<boolean>(false);
    const [actionSheetTarget, setActionSheetTarget] = useState<{ filenames: string[]; people?: Photo['people'] } | null>(null);

    const observerRef = useRef<IntersectionObserver | null>(null);
    const loadMoreRef = useRef<HTMLDivElement | null>(null);
    const photoListRequestSeqRef = useRef<number>(0);
    const hasBootstrappedFiltersRef = useRef<boolean>(false);
    const didInitialRevalidateRef = useRef<boolean>(false);
    const PAGE_SIZE = pageSizeForZoomLevel(24, galleryZoomLevel);

    useEffect(() => {
        localStorage.setItem(GALLERY_ZOOM_STORAGE_KEY, String(galleryZoomLevel));
    }, [galleryZoomLevel]);

    const getUserFacingFetchError = (err: unknown): string => {
        if (isApiError(err)) {
            if (err.status === 401 || err.status === 403) {
                return 'Please sign in to view photos.';
            }
            if (isColdStartError(err)) {
                return 'The server is taking longer than usual to respond — it may be waking up after a period of inactivity. Please try again in a moment.';
            }
            return err.kind === 'server' ? 'Unable to load photos — something went wrong on our end. Please try again.' : err.message;
        }
        if (typeof err === 'string') {
            const normalized = err.toLowerCase();
            if (normalized.includes('401') || normalized.includes('unauthorized')) {
                return 'Please sign in to view photos.';
            }
            if (isColdStartError(err)) {
                return 'The server is taking longer than usual to respond — it may be waking up after a period of inactivity. Please try again in a moment.';
            }
            return err;
        }
        if (isColdStartError(err)) {
            return 'The server is taking longer than usual to respond — it may be waking up after a period of inactivity. Please try again in a moment.';
        }
        return 'Unable to load photos.';
    };

    const buildCaptureQuery = useCallback((): string => {
        const params = new URLSearchParams();
        if (captureStartDate) {
            params.set('captureStart', captureStartDate);
        }
        if (captureEndDate) {
            params.set('captureEnd', captureEndDate);
        }
        const query = params.toString();
        return query ? `&${query}` : '';
    }, [captureStartDate, captureEndDate]);

    const getDisplayName = useCallback((filename: string): string => {
        return filename;
    }, []);

    // Optimistic writes: apply the change to local state immediately so the UI
    // responds on click, send the request in the background, and roll back (with
    // an error surfaced) only if the server rejects it. Waiting on the backend —
    // which may be cold-starting or busy — made every tap feel unresponsive.
    const patchPhoto = useCallback((filename: string, patch: Partial<Photo>) => {
        setPhotos(prev => prev.map(p => (p.filename === filename ? { ...p, ...patch } : p)));
    }, []);

    const handleRatePhoto = async (filename: string, rating: number) => {
        const previous = photos.find(p => p.filename === filename);
        patchPhoto(filename, { rating });
        addNotification('Rating updated', `${getDisplayName(filename)} rated ${rating}/5.`);
        try {
            await post(`/photos/${filename}/rating`, { rating });
        } catch (err) {
            patchPhoto(filename, { rating: previous?.rating ?? 0 });
            notifyApiError(err, { context: 'Couldn’t save rating.', retry: () => handleRatePhoto(filename, rating) });
        }
    };

    const handleSaveRotation = async (filename: string, rotation: number) => {
        const previous = photos.find(p => p.filename === filename);
        patchPhoto(filename, { rotation });
        addNotification('Rotation saved', `${getDisplayName(filename)} rotated ${rotation}°.`);
        try {
            await post(`/photos/${encodeURIComponent(filename)}/rotation`, { rotation });
        } catch (err) {
            patchPhoto(filename, { rotation: previous?.rotation ?? 0 });
            notifyApiError(err, { context: 'Couldn’t save rotation.', retry: () => handleSaveRotation(filename, rotation) });
        }
    };

    const handleToggleLike = async (filename: string) => {
        const previous = photos.find(p => p.filename === filename);
        const optimisticLiked = !(previous?.liked);
        const optimisticLikes = Math.max(0, (previous?.likes ?? 0) + (optimisticLiked ? 1 : -1));
        patchPhoto(filename, { liked: optimisticLiked, likes: optimisticLikes });
        addNotification(
            optimisticLiked ? 'Photo liked' : 'Like removed',
            `${getDisplayName(filename)} now has ${plural(optimisticLikes, 'like')}.`
        );
        try {
            const response = await post(`/photos/${filename}/like`, {});
            // Reconcile with the authoritative count (another member of a shared
            // library may have liked the same photo).
            patchPhoto(filename, { likes: response.likes, liked: response.liked });
        } catch (err) {
            patchPhoto(filename, { liked: previous?.liked ?? false, likes: previous?.likes ?? 0 });
            notifyApiError(err, { context: 'Couldn’t update like.', retry: () => handleToggleLike(filename) });
        }
    };

    const fetchPhotos = useCallback(async (sort: string = sortBy, nextOffset = 0, append = false, queryText: string = searchQuery) => {
        const requestSeq = photoListRequestSeqRef.current + 1;
        photoListRequestSeqRef.current = requestSeq;
        const isInitialLoad = nextOffset === 0 && !append;
        if (isInitialLoad) {
            setLoading(true);
            setError(null);
            setHasMore(true);
            setServerTotalLoaded(false);
            setWarmingUp(false);
        } else {
            setLoadingMore(true);
        }

        const trimmedQuery = queryText.trim();
        const captureQuery = buildCaptureQuery();
        const requestPhotoPage = () => {
            if (trimmedQuery) {
                return get(
                    `/photos/search?q=${encodeURIComponent(trimmedQuery)}&offset=${nextOffset}&limit=${PAGE_SIZE}${captureQuery}`,
                    { timeout: PHOTO_LIST_REQUEST_TIMEOUT_MS },
                );
            }
            if (filters.minRating > 0 || filters.minLikes > 0) {
                return get(
                    `/photos/filter?minRating=${filters.minRating}&minLikes=${filters.minLikes}&offset=${nextOffset}&limit=${PAGE_SIZE}${captureQuery}`,
                    { timeout: PHOTO_LIST_REQUEST_TIMEOUT_MS },
                );
            }
            return get(
                `/photos?sort=${sort}&offset=${nextOffset}&limit=${PAGE_SIZE}${captureQuery}`,
                { timeout: PHOTO_LIST_REQUEST_TIMEOUT_MS },
            );
        };

        try {
            let response;
            // Retry cold-start failures (the backend waking from scale-to-zero)
            // behind a "waking up" message rather than surfacing the raw timeout.
            for (let attempt = 0; ; attempt += 1) {
                try {
                    response = await requestPhotoPage();
                    break;
                } catch (err) {
                    if (requestSeq !== photoListRequestSeqRef.current) {
                        return;
                    }
                    if (!isInitialLoad || attempt >= COLD_START_MAX_RETRIES || !isColdStartError(err)) {
                        throw err;
                    }
                    setWarmingUp(true);
                    await new Promise((resolve) => window.setTimeout(resolve, coldStartRetryDelayMs(attempt + 1)));
                    if (requestSeq !== photoListRequestSeqRef.current) {
                        return;
                    }
                }
            }
            setWarmingUp(false);

            if (requestSeq !== photoListRequestSeqRef.current) {
                return;
            }
            const list: Photo[] = Array.isArray(response.photos) ? response.photos : [];
            setPhotos(prevPhotos => append ? [...prevPhotos, ...list] : list);
            // One batched access-token request per fetched page, not one per
            // tile — otherwise a denser zoomed-in page (up to PAGE_SIZE tiles)
            // would fire that many individual requests on mount. See
            // thumbnailAccessCache.ts for why this matters.
            if (isAuthEnabled()) {
                const needsAccess = list
                    .filter(p => shouldFetchScopedThumbnail(p.filename, p.thumbnailUrl))
                    .map(p => p.filename);
                if (needsAccess.length > 0) {
                    resolveThumbnailAccessUrls(needsAccess).then((resolved) => {
                        if (requestSeq !== photoListRequestSeqRef.current) {
                            return;
                        }
                        setThumbAccessUrls(prev => {
                            const next = new Map(prev);
                            resolved.forEach((url, filename) => next.set(filename, url));
                            return next;
                        });
                    });
                }
            }
            setTotalAvailable(typeof response.total === 'number' ? response.total : list.length);
            setServerTotalLoaded(true);
            setOffset(nextOffset + list.length);
            setHasMore(list.length === PAGE_SIZE && nextOffset + list.length < response.total);
            if (trimmedQuery) {
                setSearchNotice(typeof response.searchNotice === 'string' ? response.searchNotice : null);
            } else {
                setSearchNotice(null);
            }
        } catch (err) {
            if (requestSeq !== photoListRequestSeqRef.current) {
                return;
            }
            setWarmingUp(false);
            setError(getUserFacingFetchError(err));
            // Prevent infinite-scroll from hammering the API when requests are failing.
            setHasMore(false);
        } finally {
            if (requestSeq !== photoListRequestSeqRef.current) {
                return;
            }
            if (isInitialLoad) {
                setLoading(false);
            } else {
                setLoadingMore(false);
            }
        }
    }, [sortBy, filters, searchQuery, buildCaptureQuery, galleryZoomLevel]);

    // Deep link from another page ("view in library" on a photo tile) — an exact
    // point lookup rather than reusing the fuzzy/semantic search endpoint, so it
    // reliably resolves regardless of how well the filename tokenizes.
    useEffect(() => {
        const params = new URLSearchParams(location.search);
        const target = params.get('focus');
        if (!target) {
            setFocusedFilename(null);
            setFocusedPhoto(null);
            setFocusError(null);
            setFocusLoading(false);
            return undefined;
        }
        setFocusedFilename(target);
        setFocusedPhoto(null);
        setFocusError(null);
        setFocusLoading(true);
        let cancelled = false;
        (async () => {
            try {
                const response = await get(`/photos/lookup/${encodeURIComponent(target)}`);
                if (cancelled) {
                    return;
                }
                if (response?.photo) {
                    setFocusedPhoto(response.photo as Photo);
                } else {
                    setFocusError("Couldn't find that photo.");
                }
            } catch {
                if (!cancelled) {
                    setFocusError("Couldn't find that photo.");
                }
            } finally {
                if (!cancelled) {
                    setFocusLoading(false);
                }
            }
        })();
        return () => {
            cancelled = true;
        };
    }, [location.search]);

    useEffect(() => {
        if (!registerUploadCompletionHandler) {
            return undefined;
        }
        return registerUploadCompletionHandler(() => fetchPhotos(sortBy, 0, false, searchQuery));
    }, [fetchPhotos, registerUploadCompletionHandler, searchQuery, sortBy]);

    useEffect(() => {
        if (!registerUploadErrorHandler) {
            return undefined;
        }
        return registerUploadErrorHandler(setError);
    }, [registerUploadErrorHandler]);

    const handleSortChange = (newSort: string) => {
        setSortBy(newSort);
        setOffset(0);
        setHasMore(true);
        fetchPhotos(newSort, 0, false, searchQuery);
    };

    const handlePhotoSelect = (filename: string, multi: boolean = false) => {
        setSelectedPhotos(prev => {
            const updated = new Set(prev);
            if (updated.has(filename)) {
                updated.delete(filename);
            } else if (multi) {
                updated.add(filename);
            } else {
                updated.clear();
                updated.add(filename);
            }
            return updated;
        });
    };

    const dragSelectHandlers = useDragSelect({
        isSelected: (filename) => selectedPhotos.has(filename),
        setSelected: (filename, selected) => {
            setSelectedPhotos(prev => {
                if (selected === prev.has(filename)) {
                    return prev;
                }
                const updated = new Set(prev);
                if (selected) {
                    updated.add(filename);
                } else {
                    updated.delete(filename);
                }
                return updated;
            });
        },
    });

    const handleTileLongPress = (photo: Photo) => {
        if (selectedPhotos.size > 1 && selectedPhotos.has(photo.filename)) {
            setActionSheetTarget({ filenames: Array.from(selectedPhotos) });
        } else {
            setActionSheetTarget({ filenames: [photo.filename], people: photo.people });
        }
    };

    const handleSelectAll = () => {
        if (selectedPhotos.size === filteredPhotos.length && filteredPhotos.length > 0) {
            setSelectedPhotos(new Set());
        } else {
            setSelectedPhotos(new Set(filteredPhotos.map(p => p.filename)));
        }
    };

    const handleDeletePhotos = async () => {
        if (selectedPhotos.size === 0) return;

        const deleteCount = selectedPhotos.size;
        const confirmDelete = await confirmDialog({
            title: 'Delete photos',
            message: `Delete ${plural(deleteCount, 'photo')}? This will permanently remove them from the gallery and any albums.`,
            confirmLabel: 'Delete',
            danger: true,
        });
        if (!confirmDelete) return;

        const toDelete = Array.from(selectedPhotos);
        const toDeleteSet = new Set(toDelete);
        // Snapshot the current list so we can restore any photos that fail to
        // delete, preserving their original order.
        const snapshot = photos;

        // Optimistically clear the selection and drop the tiles immediately so
        // the gallery reflects the deletion right away instead of appearing
        // frozen while the (potentially large) request is in flight.
        setSelectedPhotos(new Set());
        setPhotos(prev => prev.filter(p => !toDeleteSet.has(p.filename)));
        setError(null);
        setDeleting(true);
        setDeleteProgress({ done: 0, total: toDelete.length });

        // Delete in bounded chunks: each request stays well under any gateway
        // timeout and lets us report incremental progress for big selections.
        const CHUNK_SIZE = 100;
        const succeeded = new Set<string>();
        const failureMessages: string[] = [];
        try {
            for (let start = 0; start < toDelete.length; start += CHUNK_SIZE) {
                const chunk = toDelete.slice(start, start + CHUNK_SIZE);
                try {
                    const response = await post('/photos/delete', { filenames: chunk });
                    const deletedList: string[] = Array.isArray(response?.deleted)
                        ? response.deleted
                        : (response?.success ? chunk : []);
                    deletedList.forEach((name: string) => succeeded.add(name));
                    if (Array.isArray(response?.errors) && response.errors.length > 0) {
                        failureMessages.push(...response.errors);
                    }
                } catch (err) {
                    failureMessages.push(
                        typeof err === 'string' ? err : (err instanceof Error ? err.message : 'Request failed'),
                    );
                }
                setDeleteProgress({ done: Math.min(start + CHUNK_SIZE, toDelete.length), total: toDelete.length });
            }

            // Reconcile the optimistic update: restore any photo that was not
            // confirmed deleted, keeping the original ordering.
            if (succeeded.size < toDelete.length) {
                setPhotos(snapshot.filter(p => !(toDeleteSet.has(p.filename) && succeeded.has(p.filename))));
            }

            if (succeeded.size > 0) {
                addNotification('Photos deleted', `Deleted ${plural(succeeded.size, 'photo')} from your gallery.`);
            }
            if (failureMessages.length > 0) {
                setError(`Failed to delete some photos: ${failureMessages.slice(0, 5).join(', ')}`);
                addNotification('Delete had issues', `${plural(toDelete.length - succeeded.size, 'photo')} could not be deleted.`);
            }
        } finally {
            setDeleting(false);
            setDeleteProgress(null);
        }
    };

    const handleToggleAlbumMenu = async () => {
        setShowSortMenu(false);
        setShowFilters(false);
        setShowAlbumMenu((prev) => !prev);
        if (albumMenuOptions !== null) {
            return;
        }
        setAlbumMenuLoading(true);
        try {
            const response = await get('/albums');
            setAlbumMenuOptions(Array.isArray(response?.albums) ? response.albums : []);
        } catch {
            setAlbumMenuOptions([]);
            setError("Couldn't load albums.");
        } finally {
            setAlbumMenuLoading(false);
        }
    };

    const handleAddSelectedToAlbum = async (album: AlbumSummary) => {
        if (selectedPhotos.size === 0) {
            return;
        }
        const filenames = Array.from(selectedPhotos);
        setAddingToAlbumId(album.id);
        try {
            await post(`/albums/${album.id}/photos/add`, { filenames });
            addNotification('Added to album', `Added ${plural(filenames.length, 'photo')} to "${album.name}".`);
            setAlbumMenuOptions((prev) => prev
                ? prev.map((a) => (a.id === album.id ? { ...a, photoCount: a.photoCount + filenames.length } : a))
                : prev);
            setSelectedPhotos(new Set());
            setShowAlbumMenu(false);
        } catch (err) {
            setError(typeof err === 'string' ? err : 'Failed to add photos to album.');
        } finally {
            setAddingToAlbumId(null);
        }
    };

    const handleCreateAlbumFromSelected = async () => {
        if (selectedPhotos.size === 0) {
            return;
        }
        setShowAlbumMenu(false);

        const suggestedName = `Album ${new Date().toLocaleDateString()}`;
        const input = await promptDialog({
            title: 'Create album',
            label: 'Album name',
            defaultValue: suggestedName,
            confirmLabel: 'Create',
        });
        if (input === null) {
            return;
        }

        const albumName = input.trim();
        if (!albumName) {
            setError('Album name is required.');
            return;
        }

        const filenames = Array.from(selectedPhotos);
        try {
            const createResponse = await post('/albums', { name: albumName });
            const newAlbumId = String(createResponse?.album?.id || '');
            if (!newAlbumId) {
                throw new Error('Album was created but no album id was returned.');
            }

            await post(`/albums/${newAlbumId}/photos/add`, { filenames });
            addNotification('Album created', `Created "${albumName}" with ${plural(filenames.length, 'photo')}.`);
            setSelectedPhotos(new Set());
            setAlbumMenuOptions(null);
        } catch (err) {
            setError(typeof err === 'string' ? err : 'Failed to create album from selection.');
        }
    };

    const handleDownloadSelected = async () => {
        if (selectedPhotos.size === 0) return;
        const files = photos.filter((photo) => selectedPhotos.has(photo.filename));
        if (files.length === 0) return;

        setDownloading(true);
        setError(null);
        setDownloadProgress({ completed: 0, total: files.length });
        try {
            await downloadPhotosAsZip(
                files.map((photo) => ({ filename: photo.filename, url: photo.url })),
                `keepsake-gallery-${new Date().toISOString().slice(0, 10)}.zip`,
                setDownloadProgress
            );
            addNotification('Download ready', `Downloaded ${plural(files.length, 'photo')}.`);
        } catch (err) {
            setError(typeof err === 'string' ? err : 'Failed to download selected photos.');
        } finally {
            setDownloading(false);
            setDownloadProgress(null);
        }
    };

    const handleResetFilters = () => {
        // Reset every field in the filter panel, not just rating/likes — leaving
        // the capture-date range set made "Reset" look like it did nothing when a
        // date filter was active. Clearing the state triggers a refetch via the
        // capture-date / empty-photos effects (with the cleared values), so the
        // gallery reloads unfiltered. Note fetchPhotos reads these values from its
        // closure, so we deliberately let the effects re-run it rather than call
        // it here with stale filter values.
        setFilters({ minRating: 0, minLikes: 0 });
        setCaptureStartDate('');
        setCaptureEndDate('');
        setOffset(0);
        setPhotos([]);
        setHasMore(true);
    };

    useEffect(() => {
        observerRef.current = new IntersectionObserver(entries => {
            if (entries[0].isIntersecting && hasMore && !loadingMore && !loading && !error) {
                fetchPhotos(sortBy, offset, true, searchQuery);
            }
        }, { threshold: 0.1 });

        if (loadMoreRef.current) {
            observerRef.current.observe(loadMoreRef.current);
        }

        return () => {
            if (observerRef.current) {
                observerRef.current.disconnect();
            }
        };
    }, [hasMore, loadingMore, loading, error, fetchPhotos, sortBy, offset, searchQuery]);

    useEffect(() => {
        if (!didInitialRevalidateRef.current) {
            didInitialRevalidateRef.current = true;
            // Always revalidate on mount, even when a cache is showing. Otherwise a
            // returning user keeps seeing the cached photo order (persisted from a
            // previous load — potentially before an ordering change on the server)
            // and never sees the current newest-first order until the cache
            // expires. The cached grid stays visible while this refetches.
            fetchPhotos(sortBy, 0, false, searchQuery);
            return;
        }
        if (photos.length === 0) {
            fetchPhotos(sortBy, 0, false, searchQuery);
        }
    }, [fetchPhotos, photos.length, sortBy, searchQuery]);

    useEffect(() => {
    }, []);

    useEffect(() => {
        if (!hasBootstrappedFiltersRef.current) {
            hasBootstrappedFiltersRef.current = true;
            return;
        }

        // Debounced so dragging the rating/likes sliders (which fire onChange on
        // every tick, not just on release) doesn't hammer the API — the gallery
        // still updates live, just ~350ms after the user settles on a value.
        const timeoutId = window.setTimeout(() => {
            setOffset(0);
            setHasMore(true);
            fetchPhotos(sortBy, 0, false, searchQuery);
        }, 350);
        return () => window.clearTimeout(timeoutId);
    }, [captureStartDate, captureEndDate, filters.minRating, filters.minLikes, sortBy, searchQuery, fetchPhotos]);

    const submitSearch = useCallback((override?: string) => {
        const nextQuery = (override ?? searchInput).trim();
        setSearchQuery(nextQuery);
        setOffset(0);
        setHasMore(true);
        fetchPhotos(sortBy, 0, false, nextQuery);
    }, [fetchPhotos, searchInput, sortBy]);

    // Leaving the box empty (never submitted, or cleared back out after a prior
    // search) should drop back to the unfiltered gallery rather than leaving a
    // stale query applied — covers click-away, tab-away (blur), and Escape.
    const closeSearch = useCallback(() => {
        if (!searchInput.trim() && searchQuery) {
            setSearchQuery('');
            setOffset(0);
            setHasMore(true);
            fetchPhotos(sortBy, 0, false, '');
        }
        setSearchOpen(false);
    }, [searchInput, searchQuery, fetchPhotos, sortBy]);

    const clearSearch = useCallback(() => {
        setSearchInput('');
        if (searchQuery) {
            setSearchQuery('');
            setOffset(0);
            setHasMore(true);
            fetchPhotos(sortBy, 0, false, '');
        }
        setSearchOpen(false);
    }, [searchQuery, fetchPhotos, sortBy]);

    useEffect(() => {
        if (!showSortMenu) {
            return;
        }
        const handlePointerDown = (event: PointerEvent) => {
            if (sortMenuRef.current && !sortMenuRef.current.contains(event.target as Node)) {
                setShowSortMenu(false);
            }
        };
        const handleKeyDown = (event: KeyboardEvent) => {
            if (event.key === 'Escape') {
                setShowSortMenu(false);
            }
        };
        document.addEventListener('pointerdown', handlePointerDown);
        document.addEventListener('keydown', handleKeyDown);
        return () => {
            document.removeEventListener('pointerdown', handlePointerDown);
            document.removeEventListener('keydown', handleKeyDown);
        };
    }, [showSortMenu]);

    useEffect(() => {
        if (!showFilters) {
            return;
        }
        const handlePointerDown = (event: PointerEvent) => {
            if (filterMenuRef.current && !filterMenuRef.current.contains(event.target as Node)) {
                setShowFilters(false);
            }
        };
        const handleKeyDown = (event: KeyboardEvent) => {
            if (event.key === 'Escape') {
                setShowFilters(false);
            }
        };
        document.addEventListener('pointerdown', handlePointerDown);
        document.addEventListener('keydown', handleKeyDown);
        return () => {
            document.removeEventListener('pointerdown', handlePointerDown);
            document.removeEventListener('keydown', handleKeyDown);
        };
    }, [showFilters]);

    useEffect(() => {
        if (!showAlbumMenu) {
            return;
        }
        const handlePointerDown = (event: PointerEvent) => {
            if (albumMenuRef.current && !albumMenuRef.current.contains(event.target as Node)) {
                setShowAlbumMenu(false);
            }
        };
        const handleKeyDown = (event: KeyboardEvent) => {
            if (event.key === 'Escape') {
                setShowAlbumMenu(false);
            }
        };
        document.addEventListener('pointerdown', handlePointerDown);
        document.addEventListener('keydown', handleKeyDown);
        return () => {
            document.removeEventListener('pointerdown', handlePointerDown);
            document.removeEventListener('keydown', handleKeyDown);
        };
    }, [showAlbumMenu]);

    useEffect(() => {
        if (selectedPhotos.size === 0) {
            setShowAlbumMenu(false);
        }
    }, [selectedPhotos]);

    useEffect(() => {
        if (!serverTotalLoaded) {
            return;
        }
        writePhotoCache({
            timestamp: Date.now(),
            photos,
            totalAvailable,
            offset,
            hasMore,
            sortBy,
            searchQuery,
            filters,
            captureStartDate,
            captureEndDate,
        });
    }, [photos, totalAvailable, offset, hasMore, sortBy, searchQuery, filters, captureStartDate, captureEndDate, serverTotalLoaded]);

    const filteredPhotos = useMemo(() => {
        if (mediaFilter === 'photos') {
            return photos.filter((photo) => !isVideoFilename(photo.filename));
        }
        if (mediaFilter === 'videos') {
            return photos.filter((photo) => isVideoFilename(photo.filename));
        }
        return photos;
    }, [mediaFilter, photos]);
    const totalPhotos = totalAvailable;
    const showingPhotos = filteredPhotos.length;
    const selectedCount = selectedPhotos.size;
    const hasCaptureFilter = captureStartDate.length > 0 || captureEndDate.length > 0;
    const closeLightbox = useCallback(() => {
        setLightboxIndex(null);
    }, []);

    const openLightboxAt = useCallback((index: number) => {
        if (index < 0 || index >= filteredPhotos.length) {
            return;
        }
        setLightboxIndex(index);
    }, [filteredPhotos.length]);

    // Two-finger pinch on the grid steps the density level (see
    // GALLERY_ZOOM_SCALES). Modeled on Timeline.tsx's pinch-to-zoom-level
    // handling: track active pointers in a ref, and step by whole levels
    // once the pinch has moved far enough, rather than scaling continuously
    // -- the grid only has a handful of tuned presets, not arbitrary sizes.
    const stepGalleryZoom = useCallback((direction: 1 | -1) => {
        setGalleryZoomLevel((level) => Math.max(0, Math.min(GALLERY_ZOOM_MAX_LEVEL, level + direction)));
    }, []);

    const handleGalleryPointerDown = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
        if (e.pointerType !== 'touch') {
            return;
        }
        pinchPointersRef.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
        if (pinchPointersRef.current.size === 2) {
            try {
                e.currentTarget.setPointerCapture(e.pointerId);
            } catch {
                // Best-effort: a failed capture just means a finger that
                // wanders off the grid element mid-pinch may stop delivering
                // move events. The baseline below still needs to be set
                // either way, or the gesture would eat its first step just
                // re-establishing it.
            }
            const [a, b] = Array.from(pinchPointersRef.current.values());
            pinchStepDistanceRef.current = Math.hypot(a.x - b.x, a.y - b.y);
        }
    }, []);

    const handleGalleryPointerMove = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
        if (pinchPointersRef.current.size !== 2 || !pinchPointersRef.current.has(e.pointerId)) {
            return;
        }
        e.preventDefault();
        pinchPointersRef.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
        const [a, b] = Array.from(pinchPointersRef.current.values());
        const distance = Math.hypot(a.x - b.x, a.y - b.y);
        const baseline = pinchStepDistanceRef.current;
        if (baseline <= 0 || distance <= 0) {
            pinchStepDistanceRef.current = distance;
            return;
        }
        if (distance / baseline >= GALLERY_PINCH_STEP_RATIO) {
            // Fingers spreading apart -- step toward the default, less-dense view.
            stepGalleryZoom(-1);
            pinchStepDistanceRef.current = distance;
        } else if (baseline / distance >= GALLERY_PINCH_STEP_RATIO) {
            // Fingers pinching together -- step toward a denser grid.
            stepGalleryZoom(1);
            pinchStepDistanceRef.current = distance;
        }
    }, [stepGalleryZoom]);

    const handleGalleryPointerEnd = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
        pinchPointersRef.current.delete(e.pointerId);
        pinchStepDistanceRef.current = 0;
    }, []);

    const hideDiscovery = location.pathname && location.pathname.startsWith('/people');

    const sectionClass = hideDiscovery ? '' : 'gallery-wrap card-glass reveal-up delay-1 gallery-studio';

    return (
        <section className={sectionClass}>
            {!hideDiscovery && lightboxIndex === null && (
            <>
            <div className="gallery-controls-surface">
                <div className="gallery-toolbar">
                    <p className="gallery-meta-line" aria-live="polite">
                        <span className="gallery-meta-count">{loading && !serverTotalLoaded ? '…' : totalPhotos}</span>
                        <span className="gallery-meta-sep"> {serverTotalLoaded ? 'photos' : 'cached'}</span>
                        <span className="gallery-meta-dim"> · {showingPhotos} shown</span>
                        {selectedCount > 0 && <span className="gallery-meta-dim"> · {selectedCount} selected</span>}
                    </p>

                    <div className="gallery-tool-cluster">
                        {searchOpen ? (
                            <div className="gallery-search-open" ref={searchRef}>
                                <input
                                    type="text"
                                    placeholder="Search by meaning…"
                                    value={searchInput}
                                    autoFocus
                                    onChange={(e) => setSearchInput(e.target.value)}
                                    onKeyDown={(e) => {
                                        if (e.key === 'Enter') {
                                            submitSearch();
                                        } else if (e.key === 'Escape') {
                                            closeSearch();
                                        }
                                    }}
                                    onBlur={closeSearch}
                                    enterKeyHint="search"
                                    className="field gallery-search-field"
                                    aria-label="Search photos"
                                />
                                {searchInput && (
                                    <button
                                        type="button"
                                        className="gallery-search-clear"
                                        onMouseDown={(e) => e.preventDefault()}
                                        onClick={clearSearch}
                                        aria-label="Clear search"
                                    >
                                        <XMarkIcon className="toolbar-icon" />
                                    </button>
                                )}
                            </div>
                        ) : (
                            <button
                                type="button"
                                onClick={() => setSearchOpen(true)}
                                className={`btn icon-btn ${searchQuery ? 'btn-primary' : 'btn-soft'}`}
                                aria-label="Search"
                                title={searchQuery ? `Searching: ${searchQuery}` : 'Search'}
                            >
                                <MagnifyingGlassIcon className="toolbar-icon" />
                                <span className="sr-only">Search</span>
                            </button>
                        )}

                        <div className="gallery-menu-anchor" ref={sortMenuRef}>
                            <button
                                type="button"
                                onClick={() => {
                                    setShowFilters(false);
                                    setShowSortMenu((prev) => !prev);
                                }}
                                className={`btn icon-btn ${showSortMenu ? 'btn-primary' : 'btn-soft'}`}
                                aria-label="Sort and view options"
                                aria-expanded={showSortMenu}
                                title="Sort & view"
                            >
                                <AdjustmentsHorizontalIcon className="toolbar-icon" />
                                <span className="sr-only">Sort and view options</span>
                            </button>
                            {showSortMenu && (
                                <div className="gallery-menu" role="menu" aria-label="Sort and view">
                                    <p className="gallery-menu-label">Sort by</p>
                                    <div className="gallery-menu-row">
                                        <button type="button" onClick={() => handleSortChange('date')} className={`btn btn-soft gallery-menu-btn ${sortBy === 'date' ? 'active' : ''}`}>
                                            <ClockIcon className="toolbar-icon" /> Recent
                                        </button>
                                        <button type="button" onClick={() => handleSortChange('capture')} className={`btn btn-soft gallery-menu-btn ${sortBy === 'capture' ? 'active' : ''}`}>
                                            <CalendarDaysIcon className="toolbar-icon" /> Captured
                                        </button>
                                    </div>
                                    <p className="gallery-menu-label">Show</p>
                                    <div className="gallery-menu-row">
                                        <button type="button" onClick={() => setMediaFilter('all')} className={`btn btn-soft gallery-menu-btn ${mediaFilter === 'all' ? 'active' : ''}`}>
                                            <Squares2X2Icon className="toolbar-icon" /> All
                                        </button>
                                        <button type="button" onClick={() => setMediaFilter('photos')} className={`btn btn-soft gallery-menu-btn ${mediaFilter === 'photos' ? 'active' : ''}`}>
                                            <PhotoIcon className="toolbar-icon" /> Photos
                                        </button>
                                        <button type="button" onClick={() => setMediaFilter('videos')} className={`btn btn-soft gallery-menu-btn ${mediaFilter === 'videos' ? 'active' : ''}`}>
                                            <VideoCameraIcon className="toolbar-icon" /> Videos
                                        </button>
                                    </div>
                                </div>
                            )}
                        </div>

                        <div className="gallery-menu-anchor" ref={filterMenuRef}>
                            <button
                                type="button"
                                onClick={() => {
                                    setShowSortMenu(false);
                                    setShowFilters((prev) => !prev);
                                }}
                                className={`btn icon-btn ${showFilters || filters.minRating > 0 || filters.minLikes > 0 || hasCaptureFilter ? 'btn-primary' : 'btn-soft'}`}
                                aria-label="Filters"
                                aria-expanded={showFilters}
                                title="Filters"
                            >
                                <FunnelIcon className="toolbar-icon" />
                                <span className="sr-only">Filters</span>
                            </button>
                            {showFilters && (
                                <div className="gallery-menu gallery-menu-filters" role="menu" aria-label="Filters">
                                    <div className="gallery-filter-field">
                                        <label className="gallery-menu-label" htmlFor="flt-rating">Minimum rating: {filters.minRating}</label>
                                        <input
                                            id="flt-rating"
                                            type="range"
                                            min="0"
                                            max="5"
                                            value={filters.minRating}
                                            onChange={(e) => setFilters({ ...filters, minRating: parseInt(e.target.value) })}
                                        />
                                    </div>
                                    <div className="gallery-filter-field">
                                        <label className="gallery-menu-label" htmlFor="flt-likes">Minimum likes: {filters.minLikes}</label>
                                        <input
                                            id="flt-likes"
                                            type="range"
                                            min="0"
                                            max="100"
                                            value={filters.minLikes}
                                            onChange={(e) => setFilters({ ...filters, minLikes: parseInt(e.target.value) })}
                                        />
                                    </div>
                                    <div className="gallery-filter-field">
                                        <label className="gallery-menu-label" htmlFor="capture-start">Captured from</label>
                                        <input
                                            id="capture-start"
                                            type="date"
                                            className="field field-date"
                                            value={captureStartDate}
                                            onChange={(e) => setCaptureStartDate(e.target.value)}
                                        />
                                    </div>
                                    <div className="gallery-filter-field">
                                        <label className="gallery-menu-label" htmlFor="capture-end">Captured to</label>
                                        <input
                                            id="capture-end"
                                            type="date"
                                            className="field field-date"
                                            value={captureEndDate}
                                            onChange={(e) => setCaptureEndDate(e.target.value)}
                                        />
                                    </div>
                                    <div className="gallery-menu-row">
                                        <button
                                            type="button"
                                            onClick={handleResetFilters}
                                            className="btn btn-soft gallery-menu-btn"
                                        >
                                            <ArrowUturnLeftIcon className="toolbar-icon" /> Reset
                                        </button>
                                    </div>
                                </div>
                            )}
                        </div>

                        <button
                            type="button"
                            onClick={() => fetchPhotos(sortBy, 0, false, searchQuery)}
                            className="btn btn-soft icon-btn"
                            aria-label="Refresh"
                            title="Refresh"
                        >
                            <ArrowPathIcon className="toolbar-icon" />
                            <span className="sr-only">Refresh</span>
                        </button>

                        {selectedCount > 0 && (
                            <>
                                <div className="gallery-menu-anchor" ref={albumMenuRef}>
                                    <button
                                        type="button"
                                        onClick={() => void handleToggleAlbumMenu()}
                                        className={`btn icon-btn ${showAlbumMenu ? 'btn-primary' : 'btn-soft'}`}
                                        aria-label={`Add ${selectedCount} to album`}
                                        aria-expanded={showAlbumMenu}
                                        title={`Add ${selectedCount} to album`}
                                    >
                                        <PlusIcon className="toolbar-icon" />
                                        <span className="sr-only">Add {selectedCount} to album</span>
                                    </button>
                                    {showAlbumMenu && (
                                        <div className="gallery-menu" role="menu" aria-label="Add to album">
                                            <button
                                                type="button"
                                                className="btn btn-soft gallery-menu-action"
                                                onClick={() => void handleCreateAlbumFromSelected()}
                                            >
                                                <PlusIcon className="toolbar-icon" aria-hidden="true" />
                                                <span>Create new album</span>
                                            </button>
                                            <div className="gallery-menu-divider" />
                                            <p className="gallery-menu-label">Add to existing album</p>
                                            <div className="gallery-menu-album-list">
                                                {albumMenuLoading && <p className="gallery-menu-empty">Loading…</p>}
                                                {!albumMenuLoading && albumMenuOptions && albumMenuOptions.length === 0 && (
                                                    <p className="gallery-menu-empty">No albums yet.</p>
                                                )}
                                                {!albumMenuLoading && albumMenuOptions && albumMenuOptions.map((album) => (
                                                    <button
                                                        key={album.id}
                                                        type="button"
                                                        className="btn btn-soft gallery-menu-action"
                                                        disabled={addingToAlbumId !== null}
                                                        onClick={() => void handleAddSelectedToAlbum(album)}
                                                    >
                                                        <span>{album.name}</span>
                                                        <span className="gallery-menu-action-meta">{album.photoCount}</span>
                                                    </button>
                                                ))}
                                            </div>
                                        </div>
                                    )}
                                </div>
                                <button
                                    type="button"
                                    onClick={handleDownloadSelected}
                                    disabled={downloading}
                                    className="btn btn-soft icon-btn"
                                    aria-label={`Download selected (${selectedCount})`}
                                    title={`Download selected (${selectedCount})`}
                                >
                                    <ArrowDownTrayIcon className="toolbar-icon" />
                                    <span className="sr-only">Download selected ({selectedCount})</span>
                                </button>
                                <button
                                    type="button"
                                    onClick={handleDeletePhotos}
                                    disabled={deleting}
                                    className="btn btn-danger icon-btn"
                                    aria-label={`Delete selected (${selectedCount})`}
                                    title={`Delete selected (${selectedCount})`}
                                >
                                    <TrashIcon className="toolbar-icon" />
                                    <span className="sr-only">Delete selected ({selectedCount})</span>
                                </button>
                            </>
                        )}
                    </div>
                </div>
            </div>

            {timelineStatus === 'ready' && timelineSummary && (
                <Timeline
                    summary={timelineSummary}
                    currentStartISO={captureStartDate}
                    currentEndISO={captureEndDate}
                    onRangeSettled={(startISO, endISO) => {
                        setCaptureStartDate(startISO);
                        setCaptureEndDate(endISO);
                    }}
                />
            )}
            </>
            )}

            {focusedFilename && (
                <div className="gallery-focus-panel card-glass">
                    <div className="gallery-focus-header">
                        <p className="gallery-focus-title">Viewing 1 photo · {focusedFilename}</p>
                        <Link to="/" className="btn btn-soft" aria-label="Show full library">
                            Show full library
                        </Link>
                    </div>
                    {focusLoading && <Loading label="Loading photo…" fullPage={false} />}
                    {!focusLoading && focusError && <p className="empty">{focusError}</p>}
                    {!focusLoading && focusedPhoto && (
                        <div className="gallery-grid gallery-focus-grid">
                            <PhotoTile
                                photo={focusedPhoto}
                                title={focusedPhoto.filename}
                                showBody={false}
                                onMediaClick={(e) => {
                                    e.stopPropagation();
                                    setFocusLightboxOpen(true);
                                }}
                            />
                        </div>
                    )}
                    {focusLightboxOpen && focusedPhoto && (
                        <PhotoViewer
                            photos={[focusedPhoto]}
                            index={0}
                            onClose={() => setFocusLightboxOpen(false)}
                            onIndexChange={() => {}}
                            useProtectedMedia={true}
                        />
                    )}
                </div>
            )}

            {downloading && downloadProgress && (
                <p className="status">Downloading {downloadProgress.completed}/{downloadProgress.total}…</p>
            )}

            {deleting && deleteProgress && deleteProgress.total > CHUNK_DELETE_FEEDBACK_MIN && (
                <p className="status">Deleting {deleteProgress.done}/{deleteProgress.total}…</p>
            )}

            {loading && filteredPhotos.length === 0 && (
                <Loading
                    label={warmingUp
                        ? 'Waking up the server… this can take up to a minute after inactivity.'
                        : 'Loading photos…'}
                    fullPage={false}
                />
            )}
            {error && (
                <ErrorState
                    title="Couldn't load your photos"
                    message={error}
                    onRetry={() => fetchPhotos(sortBy, 0, false, searchQuery)}
                />
            )}
            {!loading && !error && searchNotice && <p className="status">{searchNotice}</p>}
            {!loading && !error && filteredPhotos.length === 0 && photos.length > 0 && mediaFilter !== 'all' && (
                <p className="empty">{mediaFilter === 'videos' ? 'No videos in the loaded results.' : 'No photos in the loaded results.'}</p>
            )}
            {!loading && !error && filteredPhotos.length === 0 && photos.length === 0 && searchQuery && (
                <EmptyState
                    icon={<MagnifyingGlassIcon />}
                    title="No matches"
                    message="No photos match your search. Try different or broader terms."
                />
            )}
            {!loading && !error && filteredPhotos.length === 0 && photos.length === 0 && !searchQuery && hasCaptureFilter && <p className="empty">No photos found in the selected capture date range.</p>}
            {!loading && !error && filteredPhotos.length === 0 && photos.length === 0 && !searchQuery && !hasCaptureFilter && (
                <EmptyState
                    icon={<PhotoIcon />}
                    title="Your library is empty"
                    message="Upload your first photos and videos to start building your Keepsake — thumbnails, search, and people appear automatically."
                />
            )}

            {selectedCount > 0 && lightboxIndex === null && (
                <div className="selection-bar">
                    <div className="selection-bar-actions">
                        <span className="selection-count">{selectedCount} selected</span>
                        <button
                            type="button"
                            className="btn btn-soft selection-select-all"
                            onClick={handleSelectAll}
                        >
                            {selectedPhotos.size === filteredPhotos.length
                                ? `Deselect all (${filteredPhotos.length})`
                                : `Select all (${filteredPhotos.length})`}
                        </button>
                    </div>
                </div>
            )}

            {lightboxIndex === null ? (
                <div
                    ref={galleryGridRef}
                    className="gallery-grid gallery-grid--zoomable"
                    style={{ '--gallery-zoom-scale': GALLERY_ZOOM_SCALES[galleryZoomLevel] } as React.CSSProperties}
                    onPointerDown={handleGalleryPointerDown}
                    onPointerMove={handleGalleryPointerMove}
                    onPointerUp={handleGalleryPointerEnd}
                    onPointerCancel={handleGalleryPointerEnd}
                >
                    {filteredPhotos.map((photo, index) => {
                        const isSelected = selectedPhotos.has(photo.filename);
                        const rating = Math.max(0, Math.min(5, Math.round(photo.rating || 0)));
                        return (
                            <PhotoTile
                                key={photo.filename}
                                photo={photo}
                                selected={isSelected}
                                animationDelayMs={(index % 8) * 36}
                                title={photo.filename}
                                showBody={false}
                                useBatchedAccess
                                resolvedAccessUrl={thumbAccessUrls.get(photo.filename)}
                                onMediaClick={(e) => {
                                    e.stopPropagation();
                                    openLightboxAt(index);
                                }}
                                onLongPress={() => handleTileLongPress(photo)}
                                mediaOverlay={(
                                    <>
                                        <label
                                            className={`tile-select ${isSelected ? 'is-on' : ''}`}
                                            onClick={(e) => e.stopPropagation()}
                                            title={isSelected ? 'Selected' : 'Select photo'}
                                            {...dragSelectHandlers}
                                        >
                                            <input
                                                type="checkbox"
                                                className="tile-select-input"
                                                checked={isSelected}
                                                onChange={() => handlePhotoSelect(photo.filename, true)}
                                                aria-label={`Select ${photo.filename}`}
                                            />
                                            <CheckIcon className="tile-select-icon" aria-hidden="true" />
                                        </label>
                                        {(rating > 0 || photo.liked || photo.processing?.activeWorker === 'ipworker') && (
                                            <div className="tile-badges" aria-hidden="false">
                                                {photo.processing?.activeWorker === 'ipworker' && (
                                                    <span
                                                        className="tile-processing"
                                                        title="Processing on server"
                                                        aria-label="Processing on server"
                                                    >
                                                        <ArrowPathIcon className="tile-processing-icon spin-icon" />
                                                    </span>
                                                )}
                                                {rating > 0 && (
                                                    <span
                                                        className="tile-star"
                                                        title={`Rated ${rating}/5`}
                                                        aria-label={`Rated ${rating} out of 5`}
                                                    >
                                                        <StarSolidIcon className="tile-star-track" />
                                                        <span className="tile-star-fill" style={{ width: `${(rating / 5) * 100}%` }}>
                                                            <StarSolidIcon className="tile-star-front" />
                                                        </span>
                                                    </span>
                                                )}
                                                {photo.liked && (
                                                    <span
                                                        className="tile-like"
                                                        title={`${photo.likes || 0} ${photo.likes === 1 ? 'like' : 'likes'}`}
                                                        aria-label={`Liked, ${photo.likes || 0} likes`}
                                                    >
                                                        <HeartSolidIcon className="tile-like-icon" />
                                                    </span>
                                                )}
                                            </div>
                                        )}
                                        <PhotoQuickActions
                                            workbenchHref={workbenchFilenameHref(photo.filename)}
                                            people={photo.people}
                                        />
                                    </>
                                )}
                            />
                        );
                    })}
                </div>
            ) : (
                <PhotoViewer
                    photos={filteredPhotos}
                    index={lightboxIndex}
                    onClose={closeLightbox}
                    onIndexChange={setLightboxIndex}
                    useProtectedMedia={true}
                    onRotationSave={handleSaveRotation}
                    onRate={handleRatePhoto}
                    onToggleLike={handleToggleLike}
                />
            )}

            <div ref={loadMoreRef} className="load-more">
                {hasMore && !loading && (
                    <button
                        type="button"
                        onClick={() => fetchPhotos(sortBy, offset, true, searchQuery)}
                        className="btn btn-primary icon-btn"
                        aria-label="Load more"
                    >
                        <ChevronDownIcon className="toolbar-icon" />
                        <span className="sr-only">Load more</span>
                    </button>
                )}
                {loadingMore && <p className="status">Loading more photos…</p>}
            </div>

            <PhotoActionSheet
                open={!!actionSheetTarget}
                onClose={() => setActionSheetTarget(null)}
                filenames={actionSheetTarget?.filenames || []}
                people={actionSheetTarget?.people}
                showLibraryLink={false}
                onDownload={actionSheetTarget && actionSheetTarget.filenames.length > 1 ? handleDownloadSelected : undefined}
                onDelete={actionSheetTarget && actionSheetTarget.filenames.length > 1 ? handleDeletePhotos : undefined}
            />
        </section>
    );
};

export default PhotoGallery;
