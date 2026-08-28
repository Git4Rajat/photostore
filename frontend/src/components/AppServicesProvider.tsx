import React, { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { BellIcon, ServerIcon, TrashIcon, UserGroupIcon, XMarkIcon } from '@heroicons/react/24/outline';
import { get, getUpload, post, postUpload, resolveApiUrl } from '../services/apiClient';
import { getAccessToken, isAuthEnabled } from '../services/authClient';
import { getRuntimeConfig } from '../config/appConfig';
import type {
    AppNotification,
    BrowserAiLoadStage,
    BrowserAiModelState as SharedBrowserAiModelState,
    UploadProgress,
    UploadProfile,
    PersistedUploadFile,
    PersistedUploadSession,
} from '../types/browserProcessing';
import {
    CLIENT_PROCESSING_SCHEMA_VERSION,
    DEFAULT_UPLOAD_PROFILE,
    MAX_BACKEND_UPLOAD_CHUNK_BYTES,
    MAX_FINALIZE_RETRIES,
    MB,
    PHOTO_CACHE_STORAGE_KEY,
    UPLOAD_SESSION_STORAGE_KEY,
    UPLOAD_STOPPED_ERROR,
    browserAiIdleState,
    browserAiLoadingState,
    browserAiUnsupportedState,
    formatBrowserAiReason,
    formatBytes,
    formatMegabytesPerSecond,
    getAdaptiveUploadProfile,
    getBrowserAiUnsupportedReason,
    isBrowserAiNetworkRetryReason,
    isConstrainedUploadDevice,
    isUploadStoppedError,
} from './browserAiShared';
import { FILE_ACCEPT_FILTER, requiresBackendPreview } from '../utils/photoDisplay';
import { plural } from '../utils/format';
import { shouldSuppressLeaseWarning } from '../utils/processingLease';
import { BackgroundKeepAlive } from '../services/backgroundKeepAlive';
import { getUnattendedConcurrencyBonus, reportBrowserProcessingOutcome } from '../services/unattendedProcessingScaler';
import { showToast } from '../services/toast';
import {
    isFileSystemAccessSupported,
    isFileSystemFileHandle,
    pickUploadFilesViaFileSystemAccess,
    resolveFileFromHandle,
    type FileSystemFileHandle,
} from '../services/fileSystemAccess';
import {
    fetchJobStatuses,
    isLikelyAuthenticated,
    loadSeenJobIds,
    persistSeenJobIds,
    onJobPollRequested,
    requestJobPoll,
    clusteringActivityLabel,
    ipworkActivityLabel,
    JOB_NOTIFY_WINDOW_MS,
    TERMINAL_JOB_STATUSES,
    TOASTABLE_JOB_KINDS,
    CLUSTERING_JOB_KINDS,
    type JobStatusRecord,
} from '../services/jobNotifications';

// While a background job is in flight the notifier polls this often; once no
// jobs are active it stops until a trigger site nudges it or the tab refocuses.
const JOB_POLL_INTERVAL_MS = 6000;
// Safety cap so a job that never reports a terminal status can't poll forever.
const JOB_POLL_MAX_MS = 20 * 60 * 1000;
// Clustering-kind completions (people_cluster after every upload, find-faces
// propagation after every merge) can finish many at once — a big upload fires
// one background job per photo, and a bulk "approve all suggestions" fires one
// per item. Both the bell and the toast batch these into a single entry.
// Originally flushed as soon as a poll observed no clustering job still
// queued/running, on the assumption a batch's jobs overlap in time (a fast
// upload burst does). That assumption breaks for a *slow* trickle — e.g. an
// embedding-version migration re-processing the whole library one photo every
// ~20-30s, each on its own non-overlapping clustering job — where "nothing in
// flight right now" is true between every single completion, producing one
// toast per photo instead of one for the whole sweep. Flushing is now debounced
// on a real quiet period (see scheduleClusterFlush/CLUSTER_FLUSH_QUIET_MS)
// instead: each new completion pushes the flush back out, so it only fires
// once the whole batch — fast burst or slow trickle alike — has genuinely
// gone quiet.
const CLUSTER_FLUSH_QUIET_MS = 40000;

// A freshly-opened tab can fail the browser AI model load once even though
// nothing is actually broken -- WASM/manifest fetches racing the rest of page
// load, a momentary network blip. Retry a couple of times behind a visible
// "retrying" message rather than surfacing "unavailable" after one bad attempt.
const BROWSER_AI_LOAD_MAX_RETRIES = 2;
const browserAiLoadRetryDelayMs = (attempt: number) => Math.min(1500 * attempt, 4000);

type PhotoGalleryRuntime = typeof import('./PhotoGallery');
type BlockBlobClientCtor = typeof import('@azure/storage-blob')['BlockBlobClient'];

let photoGalleryRuntimePromise: Promise<typeof import('./PhotoGallery')> | null = null;
let blockBlobClientCtorPromise: Promise<BlockBlobClientCtor> | null = null;
const loadPhotoGalleryRuntime = () => {
    if (!photoGalleryRuntimePromise) {
        photoGalleryRuntimePromise = import('./PhotoGallery');
    }
    return photoGalleryRuntimePromise;
};

const loadBlockBlobClient = () => {
    if (!blockBlobClientCtorPromise) {
        blockBlobClientCtorPromise = import('@azure/storage-blob').then((module) => module.BlockBlobClient);
    }
    return blockBlobClientCtorPromise;
};

const withPhotoGalleryRuntime = async <T,>(select: (runtime: PhotoGalleryRuntime) => Promise<T> | T): Promise<T> => (
    select(await loadPhotoGalleryRuntime())
);

const dataUrlToBlob = (dataUrl: string) => withPhotoGalleryRuntime((runtime) => runtime.dataUrlToBlob(dataUrl));
const idbDelete = (key: string) => withPhotoGalleryRuntime((runtime) => runtime.idbDelete(key));
const idbGet = (key: string) => withPhotoGalleryRuntime((runtime) => runtime.idbGet(key));
const idbPut = (key: string, value: Blob | File | FileSystemFileHandle) => withPhotoGalleryRuntime((runtime) => runtime.idbPut(key, value));
const readBlobArrayBuffer = (blob: Blob | File) => withPhotoGalleryRuntime((runtime) => runtime.readBlobArrayBuffer(blob));
const runBrowserProcessing = (...args: Parameters<PhotoGalleryRuntime['runBrowserProcessing']>) => (
    withPhotoGalleryRuntime((runtime) => runtime.runBrowserProcessing(...args))
);
const sha256ArrayBuffer = (buffer: ArrayBuffer) => withPhotoGalleryRuntime((runtime) => runtime.sha256ArrayBuffer(buffer));
const acquireBrowserAiModel = (onProgress?: (stage: BrowserAiLoadStage) => void) => withPhotoGalleryRuntime(
    (runtime) => runtime.acquireBrowserAiModel(onProgress) as Promise<SharedBrowserAiModelState>,
);
const createBrowserThumbnail = (source: Blob | File, rotationDegrees?: number) => (
    withPhotoGalleryRuntime((runtime) => runtime.createBrowserThumbnail(source, rotationDegrees))
);
const preloadNativeFaceModels = () => withPhotoGalleryRuntime((runtime) => runtime.preloadNativeFaceModels());
const createPersistentBrowserAiWorker = () => withPhotoGalleryRuntime((runtime) => runtime.createPersistentBrowserAiWorker());
type BrowserAiWorkerHandle = ReturnType<PhotoGalleryRuntime['createPersistentBrowserAiWorker']>;

const fetchProcessingBlob = async (url: string): Promise<Blob> => {
    const resolvedUrl = resolveApiUrl(url);
    const headers: Record<string, string> = {};
    const shouldAttachAuth = !/^https?:\/\//i.test(url);
    if (shouldAttachAuth) {
        const passwordToken = typeof window !== 'undefined'
            ? window.localStorage.getItem('photostore.passwordAuthToken') || ''
            : '';
        if (passwordToken) {
            headers.Authorization = `Bearer ${passwordToken}`;
        } else if (isAuthEnabled()) {
            const token = await getAccessToken();
            if (token) {
                headers.Authorization = `Bearer ${token}`;
            }
        }
    }
    const response = await fetch(resolvedUrl, {
        headers,
        credentials: /^https?:\/\//i.test(resolvedUrl) ? 'omit' : 'include',
    });
    if (!response.ok) {
        throw new Error(`Failed to fetch processing image: ${response.status}`);
    }
    return response.blob();
};

// RAW/HEIC files have no reliable orientation in their own embedded preview (the browser-side
// byte-scan fallback in resolveBrowserVisionSource/extractEmbeddedJpegPreview finds the same
// embedded JPEG a RAW container carries but can't correct its orientation the way the backend's
// extract_raw_preview_bytes does), so always prefer this backend-converted preview for them.
// Falls back to undefined (letting the caller fall through to the client-side extraction) on any
// failure -- this must not block thumbnail generation just because the preview endpoint hiccuped.
const fetchConvertedRawPreview = async (filename: string): Promise<Blob | undefined> => {
    if (!requiresBackendPreview(filename)) {
        return undefined;
    }
    try {
        const previewAccess = await get(`/api/photos/access/preview/${encodeURIComponent(filename)}`);
        const previewUrl = String(previewAccess?.url || '');
        if (!previewUrl) {
            return undefined;
        }
        const previewBlob = await fetchProcessingBlob(previewUrl);
        if (/^image\/jpe?g$/i.test(previewBlob.type || '') || previewBlob.size > 0) {
            return previewBlob;
        }
        return undefined;
    } catch (previewErr) {
        console.warn(`Converted JPEG preview was not available for ${filename}; falling back to embedded RAW preview.`, previewErr);
        return undefined;
    }
};

// Whether we can currently produce an Authorization header. When auth is
// enabled (password or entra mode) but the user has not logged in yet, there is
// no token, so automatic background calls like the browser-processing scheduler
// must not fire — otherwise they hit the backend and get a 401 before login.
const canAuthenticateRequests = async (): Promise<boolean> => {
    if (!isAuthEnabled()) {
        return true;
    }
    if (typeof window !== 'undefined'
        && window.localStorage.getItem('photostore.passwordAuthToken')) {
        return true;
    }
    return Boolean(await getAccessToken());
};

interface PendingUploadSummary {
    fileCount: number;
    failedCount: number;
}

interface BrowserProcessingNotificationState {
    id: string;
    totalCount: number;
    processedCount: number;
    failedCount: number;
}

interface AppServicesContextValue {
    notifications: AppNotification[];
    unreadCount: number;
    addNotification: (title: string, details: string, progress?: UploadProgress) => string;
    updateNotification: (id: string, updates: Partial<AppNotification>) => void;
    clearNotifications: () => void;
    markAllNotificationsRead: () => void;
    browserAiModelState: SharedBrowserAiModelState;
    browserAiLoadProgress: BrowserAiLoadStage | null;
    browserAiButtonDisabled: boolean;
    browserAiButtonLabel: string;
    browserAiButtonClass: string;
    loadBrowserAiModel: () => Promise<SharedBrowserAiModelState>;
    uploading: boolean;
    pendingUploadSummary: PendingUploadSummary | null;
    pendingUploadFailedFiles: PersistedUploadFile[];
    queuedUploadFileCount: number;
    requestUpload: () => void;
    resumeAllPendingUploads: () => Promise<void>;
    stopActiveUpload: () => void;
    retryPersistedUploadSession: () => Promise<void>;
    discardPersistedUploadSession: () => Promise<void>;
    registerUploadCompletionHandler: (handler: () => void | Promise<void>) => () => void;
    registerUploadErrorHandler: (handler: (message: string | null) => void) => () => void;
    startBrowserProcessing: (options?: BrowserProcessingStartOptions) => Promise<number>;
    browserProcessingActive: boolean;
    // In-flight background jobs from the last /api/jobs/status poll, so views can
    // show live progress (e.g. a "Grouping people…" indicator) rather than only a
    // toast on completion.
    activeJobs: JobStatusRecord[];
    clusteringActive: boolean;
    clusteringStatusLabel: string;
    ipworkActive: boolean;
    ipworkStatusLabel: string;
}

export type BrowserProcessingAction = 'thumbnails' | 'exif' | 'ocr' | 'vision' | 'map' | 'faces';

interface BrowserProcessingStartOptions {
    actions?: BrowserProcessingAction[];
    filenames?: string[];
    items?: Array<{
        filename: string;
        rotation?: number;
    }>;
    force?: boolean;
}

const AppServicesContext = createContext<AppServicesContextValue | null>(null);

export const useAppServices = () => {
    const context = useContext(AppServicesContext);
    if (!context) {
        throw new Error('useAppServices must be used inside AppServicesProvider.');
    }
    return context;
};

const getUploadErrorMessage = (err: unknown, fallback: string): string => {
    if (typeof err === 'string') {
        return err;
    }
    if (err instanceof Error && err.message) {
        return err.message;
    }
    const responseMessage = (err as any)?.response?.data?.error || (err as any)?.response?.data?.message;
    if (typeof responseMessage === 'string' && responseMessage.trim()) {
        return responseMessage;
    }
    const message = (err as any)?.message;
    return typeof message === 'string' && message.trim() ? message : fallback;
};

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

// Direct-to-blob uploads talk to Azure Blob over HTTP/1.1, where browsers cap
// concurrent connections per origin at ~6. Staging a file's blocks one at a time
// leaves ~5 of those idle, which is why a single large file crawls. We instead
// stage several blocks in flight and size the per-file concurrency so the total
// across active file lanes stays near this cap.
const TARGET_CONCURRENT_UPLOAD_BLOCKS = 6;

// Cap the staging block size so files split into enough blocks for the parallel
// pool to engage. Very large blocks (the desktop profile uses tens of MB) would
// otherwise make a medium file a single block that can't be parallelised, and a
// huge block is also slow to retry on a flaky link. 8 MB is the well-worn sweet
// spot for browser block uploads. Slower-network profiles pick smaller chunks
// than this, so the cap only ever lowers the large desktop sizes.
const PARALLEL_UPLOAD_BLOCK_BYTES = 8 * MB;

// Shared IndexedDB-access concurrency for the upload resume cache (writing it
// in cacheUploadFilesForResume/attachSelectedFilesToPendingSession, and
// checking it in retryPersistedUploadSession) -- a 1000-file batch firing
// every idbPut/idbGet concurrently used to open ~1000 simultaneous IndexedDB
// connections at once, exactly the kind of burst that trips iOS Safari's much
// tighter per-tab memory/storage ceilings.
const IDB_RESUME_CACHE_CONCURRENCY = 4;

// Shared with the same two write sites above: even throttled to the
// concurrency above, a thousand-plus-photo mobile batch (each full-res
// HEIC/RAW file several MB+) still pushes gigabytes of blob writes through
// IndexedDB while competing for the same tight memory budget the upload
// itself needs -- iOS Safari kills the tab outright, with no JS error to
// catch. Cap the total bytes cached on constrained devices instead of
// skipping the feature outright, so small mobile uploads still get resume
// support.
const CONSTRAINED_DEVICE_CACHE_BUDGET_BYTES = 200 * MB;

// A fresh (non-queued) mobile Safari picker selection above this size was
// observed live to fail with zero network calls ever firing -- no upload
// endpoint hit, nothing logged, tab stays alive (see
// mobile-safari-large-batch-upload-crash memory). 937 files was the largest
// confirmed-reliable fresh selection; this is set a little below that as a
// margin of safety, not a precisely measured ceiling. Telling: the *same*
// device successfully handled 1300+ files when they were queued onto an
// already-running upload (deferred) rather than started immediately off a
// fresh pick -- consistent with an iOS memory-pressure window right after the
// native picker finishes transcoding a huge RAW/HEIC selection, which settles
// by the time a queued batch's turn comes up. Splitting a huge fresh
// selection into chunks and only starting the first one immediately (queuing
// the rest through the exact same deferred mechanism) gives every chunk after
// the first that same "settled" treatment.
const MAX_FRESH_UPLOAD_BATCH_SIZE = 900;

// A file that fails mid-batch almost always still has its bytes available in
// this same tab right then (uploadSourceFilesRef/uploadHandlesRef are only
// cleared once the whole runUploadSession call finishes) -- unlike the manual
// Retry banner, which runs long after that cleanup and so frequently finds
// nothing left to retry (see mobile-safari-large-batch-upload-crash memory).
// Automatically retrying failed files a couple of times, still inside the
// same run, turns most transient failures (a flaky connection, a brief 503)
// into a non-event instead of a stuck paused session.
const AUTO_RETRY_MAX_ROUNDS = 2;
const AUTO_RETRY_DELAY_MS = 1500;

// Failed-file previews are stored as JPEG data URLs inside the persisted
// session (localStorage), so a run with many failures (e.g. the network
// dropped entirely) can't be allowed to balloon that unboundedly -- capped
// well under any realistic localStorage quota. Remaining failed files still
// show by name, just without a thumbnail.
const MAX_FAILED_FILE_PREVIEWS = 50;

// ---- Turbo mode --------------------------------------------------------------
// On-device processing normally drains one photo at a time to keep resource use
// gentle. Turbo trades that for throughput: it processes several photos at once,
// each running its own AI worker, so a multi-core machine finishes a backlog far
// faster at the cost of higher CPU/GPU/memory use. Opt-in and persisted.
const BROWSER_PROCESSING_TURBO_KEY = 'keepsake.browserProcessingTurbo';

export const isBrowserProcessingTurboEnabled = (): boolean => {
    try {
        return window.localStorage.getItem(BROWSER_PROCESSING_TURBO_KEY) === '1';
    } catch {
        return false;
    }
};

export const setBrowserProcessingTurbo = (enabled: boolean): void => {
    try {
        window.localStorage.setItem(BROWSER_PROCESSING_TURBO_KEY, enabled ? '1' : '0');
    } catch {
        // Ignore storage failures (private mode / disabled storage); turbo simply
        // stays at its previous value for this session.
    }
};

// How many photos to process concurrently. Capped by both core count and device
// memory so a low-end device can't spawn enough concurrent model instances to
// crash the tab. Returns 1 (the original serial behaviour) when turbo is off.
//
// On top of that interactive-safe baseline, an unattended ramp (see
// unattendedProcessingScaler) adds more workers once the tab has been
// hidden/idle long enough that there's no interactive workload sharing the
// machine -- useful for draining a large backlog overnight. The ramp is
// capped separately (and lower on low-memory devices) and backs itself off
// if it turns out to be too aggressive for this specific machine.
export const getBrowserProcessingConcurrency = (): number => {
    if (!isBrowserProcessingTurboEnabled()) {
        return 1;
    }
    const nav = navigator as Navigator & { deviceMemory?: number };
    const cores = typeof navigator.hardwareConcurrency === 'number' && navigator.hardwareConcurrency > 0
        ? navigator.hardwareConcurrency
        : 4;
    const memoryGb = typeof nav.deviceMemory === 'number' && nav.deviceMemory > 0 ? nav.deviceMemory : 4;
    const byCores = Math.min(Math.max(cores - 1, 2), 6);
    const byMemory = memoryGb <= 4 ? 2 : (memoryGb <= 8 ? 4 : 6);
    const baseline = Math.max(1, Math.min(byCores, byMemory));
    const unattendedCeiling = memoryGb <= 4 ? 3 : (memoryGb <= 8 ? 8 : 12);
    return Math.max(1, Math.min(baseline + getUnattendedConcurrencyBonus(), unattendedCeiling));
};

// How many pending photos to claim in one /upload/processing/pending round
// trip. Deliberately decoupled from lane count (concurrency) -- this is about
// minimizing repeat fetches and keeping each lane's persistent AI worker fed
// across a real batch, not about how many photos process at once.
const PENDING_PROCESSING_BATCH_SIZE = 40;

const WARMUP_POLL_INTERVAL_MS = 5000;
// The shared HTTP client's own timeout is 600s (createHttpClient's default)
// and a "no response" failure is retried up to COLD_START_RETRIES times with
// backoff (httpClient.ts) -- fine for real work, but catastrophic for a
// liveness ping: a warm-up request that merely *hangs* (a flaky mobile
// connection switching wifi/cellular, not a clean failure) could take up to
// ~50 minutes to ever settle. Every startUpload/requestUpload call made
// while it's stuck is silently blocked by uploadStartInProgressRef the whole
// time. This is a real ping, not real work -- give it its own short leash
// instead of inheriting the client-wide budget meant for actual data
// transfers. AbortSignal.timeout is supported iOS Safari 16+.
const WARMUP_REQUEST_TIMEOUT_MS = 8000;
const BACKEND_KEEPALIVE_INTERVAL_MS = 30000;
// While a batch is draining, the keep-alive heartbeat worker drives the loop at
// this cadence so it keeps running at full speed in a hidden/minimized tab.
const BROWSER_PROCESSING_HEARTBEAT_INTERVAL_MS = 1000;
// Same storm as clustering (see CLUSTER_FLUSH_QUIET_MS): startBrowserProcessing
// drains in small batches and calls itself again only while there's continued
// progress, so the queue can look "empty" for a beat between individual photos
// (each detect+align+embed cycle takes real seconds) even mid-batch. Finalizing
// to "Browser processing finished" and clearing the notification the instant
// that happens produced a fresh "started" toast for every subsequent photo.
// Debounced the same way: only finalize after a real quiet period with no new
// completions.
const BROWSER_PROCESSING_FINALIZE_QUIET_MS = 15000;
export const browserProcessingActionSteps: Record<BrowserProcessingAction, string[]> = {
    thumbnails: ['thumbnail'],
    exif: ['exif'],
    ocr: ['ocr'],
    vision: ['ai_vision'],
    map: ['map_detection'],
    faces: ['face'],
};

// On-device AI steps that require the user to have loaded browser AI. Until then
// only baseline steps (thumbnail, exif, map geocode) run; these stay pending so
// they can be backfilled once the model is loaded.
const BROWSER_AI_GATED_STEPS = new Set(['ocr', 'ai_vision', 'face']);
const ALL_BROWSER_PROCESSING_STEPS = ['thumbnail', 'exif', 'ocr', 'ai_vision', 'map_detection', 'face'];

// Restrict the steps a processing pass may run to what the current model state allows.
// When browser AI is available this is a no-op (returns the request unchanged, where
// null means "all steps"). When it isn't loaded, gated (on-device AI) steps are dropped
// so only baseline steps run; the gated steps stay pending for backfill once loaded.
const restrictStepsForModel = (requestedSteps: Set<string> | null, modelAvailable: boolean): Set<string> | null => {
    if (modelAvailable) {
        return requestedSteps;
    }
    const base = requestedSteps ? Array.from(requestedSteps) : ALL_BROWSER_PROCESSING_STEPS;
    return new Set(base.filter((step) => !BROWSER_AI_GATED_STEPS.has(step)));
};

// thumbnail/exif aren't on-device-AI-gated (no model dependency), but
// 'backend' mode still hands them to ipworker exclusively (see the matching
// processingMode checks inside runBrowserProcessing, PhotoGallery.tsx).
// Filtered out here too so this queue-picker doesn't download a photo (a
// real bandwidth cost -- see fetchProcessingBlob below) just to discover
// there's nothing left the browser can actually do with it.
const BACKEND_MODE_OWNED_STEPS = new Set(['thumbnail', 'exif']);
const restrictStepsForProcessingMode = (requestedSteps: Set<string> | null, processingMode: string): Set<string> | null => {
    if (processingMode !== 'backend') {
        return requestedSteps;
    }
    const base = requestedSteps ? Array.from(requestedSteps) : ALL_BROWSER_PROCESSING_STEPS;
    return new Set(base.filter((step) => !BACKEND_MODE_OWNED_STEPS.has(step)));
};

const normalizeRotationDegrees = (value: unknown): number => {
    const rotation = Number(value || 0);
    if (!Number.isFinite(rotation)) {
        return 0;
    }
    return ((rotation % 360) + 360) % 360;
};

const waitForOnline = () => new Promise<void>((resolve) => {
    if (navigator.onLine) {
        resolve();
        return;
    }
    const onOnline = () => {
        window.removeEventListener('online', onOnline);
        resolve();
    };
    window.addEventListener('online', onOnline);
});

const isRetriableUploadError = (err: unknown): boolean => {
    const anyErr = err as any;
    const status = anyErr?.response?.status;
    const code = anyErr?.code;
    if (status === 408 || status === 425 || status === 429 || status === 500 || status === 502 || status === 503 || status === 504) {
        return true;
    }
    return code === 'ECONNABORTED' || code === 'ERR_NETWORK' || !navigator.onLine;
};

const buildRequestedBrowserProcessingSteps = (actions?: BrowserProcessingAction[]): Set<string> | null => {
    if (!actions || actions.length === 0) {
        return null;
    }
    const steps = new Set<string>();
    actions.forEach((action) => {
        browserProcessingActionSteps[action]?.forEach((step) => steps.add(step));
    });
    return steps.size > 0 ? steps : null;
};

const filterBrowserProcessingResult = (result: any, requestedSteps: Set<string> | null) => {
    if (!requestedSteps) {
        return result;
    }
    return {
        ...result,
        clientProcessing: Object.fromEntries(
            Object.entries(result?.clientProcessing || {}).filter(([step]) => requestedSteps.has(step)),
        ),
        clientProcessingReport: Array.isArray(result?.clientProcessingReport)
            ? result.clientProcessingReport.filter((item: any) => requestedSteps.has(String(item?.step || '')))
            : [],
    };
};

const uploadBrowserThumbnailDirectly = async (
    filename: string,
    result: any,
    thumbnailUploadUrl?: string,
    signal?: AbortSignal,
): Promise<boolean> => {
    const thumbnailPayload = result?.clientProcessing?.thumbnail;
    const thumbnailData = typeof thumbnailPayload?.data === 'string' ? thumbnailPayload.data : '';
    if (!thumbnailData) {
        return false;
    }
    if (!thumbnailUploadUrl) {
        throw new Error(`Thumbnail upload URL was not returned for ${filename}.`);
    }
    const thumbnailBlob = await dataUrlToBlob(thumbnailData);
    const BlockBlobClient = await loadBlockBlobClient();
    const thumbnailClient = new BlockBlobClient(thumbnailUploadUrl);
    await thumbnailClient.uploadData(thumbnailBlob, {
        abortSignal: signal,
        blobHTTPHeaders: {
            blobContentType: thumbnailBlob.type || 'image/jpeg',
        },
    });
    return true;
};

const browserProcessingTerminalStatuses = new Set(['done', 'no_data', 'deleted', 'skipped', 'unsupported', 'failed', 'timeout']);
const browserFaceRetryStatuses = new Set(['no_data', 'failed']);

const browserProcessingPayloadKey = (step: string): string => (
    step === 'ai_vision'
        ? 'aiVision'
        : step === 'map_detection'
            ? 'mapDetection'
            : step
);

const hasRunnableBrowserStep = (statuses: Record<string, unknown>, requestedSteps: Set<string> | null): boolean => {
    const steps = requestedSteps ? Array.from(requestedSteps) : ['thumbnail', 'exif', 'ocr', 'ai_vision', 'map_detection', 'face'];
    return steps.some((step) => {
        const status = String(statuses?.[browserProcessingPayloadKey(step)] || '').toLowerCase();
        if (step === 'face' && browserFaceRetryStatuses.has(status) && shouldRetryBrowserFaceProcessing(statuses)) {
            return true;
        }
        return !status || !browserProcessingTerminalStatuses.has(status);
    });
};

const buildBrowserProcessingFailureReport = (
    filename: string,
    statuses: Record<string, unknown>,
    requestedSteps: Set<string> | null,
    startedAt: number,
) => {
    const steps = requestedSteps ? Array.from(requestedSteps) : ['thumbnail', 'exif', 'ocr', 'ai_vision', 'map_detection', 'face'];
    return steps
        .filter((step) => {
            const status = String(statuses?.[browserProcessingPayloadKey(step)] || '').toLowerCase();
            return !status || !browserProcessingTerminalStatuses.has(status);
        })
        .map((step) => ({
            clientAssetId: `browser-${filename}`,
            step,
            status: 'failed',
            reason: 'unknown_error',
            durationMs: Math.max(0, Math.round(performance.now() - startedAt)),
            runtime: 'browser-processing-scheduler',
        }));
};

export const NotificationBell: React.FC = () => {
    const {
        notifications,
        unreadCount,
        clearNotifications,
        markAllNotificationsRead,
    } = useAppServices();
    const [showNotifications, setShowNotifications] = useState<boolean>(false);
    const wrapRef = useRef<HTMLDivElement | null>(null);

    const closeNotifications = useCallback(() => {
        setShowNotifications(false);
    }, []);

    // Close the pane when clicking outside of it or pressing Escape. The pane
    // itself is anchored to the bell in normal flow, so it scrolls along with
    // the header rather than floating over the page.
    useEffect(() => {
        if (!showNotifications || typeof document === 'undefined') {
            return undefined;
        }

        const handlePointerDown = (event: MouseEvent) => {
            if (wrapRef.current && !wrapRef.current.contains(event.target as Node)) {
                closeNotifications();
            }
        };

        const handleKeyDown = (event: KeyboardEvent) => {
            if (event.key === 'Escape') {
                closeNotifications();
            }
        };

        document.addEventListener('mousedown', handlePointerDown);
        document.addEventListener('keydown', handleKeyDown);

        return () => {
            document.removeEventListener('mousedown', handlePointerDown);
            document.removeEventListener('keydown', handleKeyDown);
        };
    }, [showNotifications, closeNotifications]);

    const overlay = showNotifications
        ? (
            <div
                className="notification-pane card-glass"
                role="dialog"
                aria-label="Notifications"
            >
                    <div className="notification-head">
                        <h3 className="toolbar-title">Notifications</h3>
                        <div className="notification-actions">
                            <button
                                type="button"
                                className="btn btn-soft icon-btn"
                                onClick={clearNotifications}
                                aria-label="Clear notifications"
                            >
                                <TrashIcon className="toolbar-icon" />
                                <span className="sr-only">Clear notifications</span>
                            </button>
                            <button
                                type="button"
                                className="btn btn-soft icon-btn notification-close"
                                onClick={closeNotifications}
                                aria-label="Close notifications"
                            >
                                <XMarkIcon className="toolbar-icon" />
                                <span className="sr-only">Close notifications</span>
                            </button>
                        </div>
                    </div>

                    {notifications.length === 0 ? (
                        <p className="status">No notifications yet.</p>
                    ) : (
                        <div className="notification-list">
                            {notifications.map((notification) => (
                                <article
                                    key={notification.id}
                                    className={`notification-item ${notification.unread ? 'unread' : ''}`}
                                >
                                    <p className="notification-title">{notification.title}</p>
                                    <p className="notification-details">{notification.details}</p>
                                    {notification.progress && (
                                        <>
                                            <div className="progress-track notification-progress-track">
                                                <div
                                                    className="progress-bar"
                                                    style={{
                                                        width: `${(notification.progress.uploadedCount / notification.progress.totalCount) * 100}%`,
                                                    }}
                                                />
                                            </div>
                                            <p className="notification-details">
                                                {notification.progress.uploadedCount}/{notification.progress.totalCount}
                                                {notification.progress.failedCount > 0
                                                    ? ` (Failed ${notification.progress.failedCount})`
                                                    : ''}
                                                {notification.progress.skippedDuplicateCount > 0
                                                    ? ` (Skipped duplicates ${notification.progress.skippedDuplicateCount})`
                                                    : ''}
                                                {` · ${formatMegabytesPerSecond(notification.progress.mbPerSecond)}`}
                                            </p>
                                        </>
                                    )}
                                </article>
                            ))}
                        </div>
                    )}
            </div>
        )
        : null;

    return (
        <div className="notification-wrap" ref={wrapRef}>
            <button
                type="button"
                onClick={() => {
                    // Deliberately not the setShowNotifications(prev => ...)
                    // updater form: React can invoke that updater outside a
                    // normal event context, and it was calling
                    // markAllNotificationsRead() (a DIFFERENT component's
                    // state setter, from AppServicesProvider) from inside it
                    // -- triggering "Cannot update a component while
                    // rendering a different component". A plain click handler
                    // always sees the latest showNotifications from this
                    // render, so there's no staleness risk in reading it
                    // directly instead.
                    const next = !showNotifications;
                    setShowNotifications(next);
                    if (next) {
                        markAllNotificationsRead();
                    }
                }}
                className="btn btn-soft notification-bell"
                aria-label="Open notifications"
            >
                <BellIcon className="toolbar-icon" />
                {unreadCount > 0 && <span className="notification-badge">{unreadCount}</span>}
            </button>
            {overlay}
        </div>
    );
};

// Global icon shown while a people-clustering job (initial cluster, recluster,
// or "find more faces") is queued/running on the worker. Clustering runs out of
// band, so without this the user was blind to it happening. Data comes from the
// existing /api/jobs/status poll (see pollJobStatusesOnce).
export const ClusteringActivityIndicator: React.FC = () => {
    const { clusteringActive, clusteringStatusLabel, loadBrowserAiModel, browserAiButtonDisabled } = useAppServices();
    if (!clusteringActive) {
        return null;
    }
    return (
        <button
            type="button"
            className="btn btn-soft icon-btn"
            onClick={() => void loadBrowserAiModel()}
            disabled={browserAiButtonDisabled}
            aria-live="polite"
            aria-label={clusteringStatusLabel}
            title={clusteringStatusLabel}
        >
            <UserGroupIcon className="toolbar-icon bg-activity-icon" aria-hidden="true" />
            <span className="sr-only">{clusteringStatusLabel}</span>
        </button>
    );
};

// Global icon shown while ipwork (backend/both processing mode) is generating
// thumbnails/faces/vision data for at least one photo. ipwork runs off-screen
// on a scale-to-zero server container with no other persistent signal: the
// per-tile "processing on server" badge only lights up for a tile that's both
// on-screen and actively leased, and ipwork completions are deliberately kept
// out of the notification bell (one job per photo — see jobNotifications.ts)
// to avoid a toast-per-photo storm on a bulk upload. Without this, a large
// background batch was entirely invisible. Not a button — there's no action
// to take here, just a status.
export const IpworkActivityIndicator: React.FC = () => {
    const { ipworkActive, ipworkStatusLabel } = useAppServices();
    if (!ipworkActive) {
        return null;
    }
    return (
        <span
            className="btn btn-soft icon-btn"
            role="status"
            aria-live="polite"
            aria-label={ipworkStatusLabel}
            title={ipworkStatusLabel}
        >
            <ServerIcon className="toolbar-icon bg-activity-icon" aria-hidden="true" />
            <span className="sr-only">{ipworkStatusLabel}</span>
        </span>
    );
};

const shouldRetryBrowserFaceProcessing = (statuses: Record<string, unknown>): boolean => {
    const faceStatus = String(statuses?.face || statuses?.face_status || '').trim().toLowerCase();
    const faceDeferredReason = String(statuses?.faceDeferredReason || statuses?.clientFaceDeferredReason || '').trim().toLowerCase();
    if (faceDeferredReason === 'background_throttled') {
        return true;
    }
    if (!browserFaceRetryStatuses.has(faceStatus)) {
        return false;
    }
    const rawFaceCount = Number(statuses?.rawFaceCount ?? statuses?.faceRawCount ?? 0);
    const faceCount = Number(statuses?.faceCount ?? 0);
    return rawFaceCount > 0 || faceCount > 0;
};

// Face outcomes that mean "retry later" rather than "failed": the tab was
// background-throttled, or a detector/embedder model wasn't ready in time
// (cold-cache load timeout / still downloading). These are queued for retry
// when the tab regains focus or the browser AI model becomes available.
const DEFERRABLE_FACE_REASONS = new Set(['background_throttled', 'browser_ai_not_loaded', 'inference_timeout']);
const DEFERRABLE_FACE_FAILURE_STAGES = new Set(['background_throttled', 'timeout', 'model_load_failed', 'embedding_model_load_failed', 'face_api_load_failed']);

const isDeferrableFaceResult = (result: any): boolean => {
    const facePayload = result?.clientProcessing?.face;
    if (facePayload && typeof facePayload === 'object') {
        if (DEFERRABLE_FACE_REASONS.has(String(facePayload.deferredReason || '').toLowerCase())
            || DEFERRABLE_FACE_FAILURE_STAGES.has(String(facePayload.faceFailureStage || '').toLowerCase())) {
            return true;
        }
    }
    return Array.isArray(result?.clientProcessingReport)
        && result.clientProcessingReport.some((item: any) => {
            if (String(item?.step || '') !== 'face') {
                return false;
            }
            return DEFERRABLE_FACE_REASONS.has(String(item?.reason || '').toLowerCase())
                || DEFERRABLE_FACE_FAILURE_STAGES.has(String(item?.faceFailureStage || '').toLowerCase());
        });
};

const formatBrowserProcessingNotificationDetails = (
    processedCount: number,
    totalCount: number,
    failedCount: number,
    status?: string,
) => {
    const details: string[] = [];
    if (status) {
        details.push(status.endsWith('.') ? status : `${status}.`);
    }
    details.push(`Processed ${processedCount}/${plural(totalCount, 'photo')}.`);
    if (failedCount > 0) {
        details.push(`Failed ${failedCount}.`);
    }
    return details.join(' ');
};

export const AppServicesProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
    const [notifications, setNotifications] = useState<AppNotification[]>([]);
    const [uploading, setUploading] = useState<boolean>(false);
    const [pendingUploadSession, setPendingUploadSession] = useState<PersistedUploadSession | null>(null);
    // Live mirror of pendingUploadSession for startUpload's re-entrancy guard.
    // React-state closures go stale inside a long async chain (e.g.
    // handleUploadSelection awaits retryPersistedUploadSession -- which clears
    // pendingUploadSession via setState -- then calls startUpload for files
    // that weren't part of the paused session; that startUpload closure was
    // created before the clear, so the plain state variable would still read
    // the OLD truthy session and wrongly bail with "Upload already starting").
    const pendingUploadSessionRef = useRef<PersistedUploadSession | null>(null);
    useEffect(() => {
        pendingUploadSessionRef.current = pendingUploadSession;
    }, [pendingUploadSession]);
    const [browserAiModelState, setBrowserAiModelState] = useState<SharedBrowserAiModelState>({
        status: 'checking',
        modelAvailability: 'skipped',
        modelCacheStatus: 'miss',
        runtime: 'browser-ai-worker',
    });
    // Only meaningful while browserAiModelState.status === 'loading'; tracks how far a
    // cold (uncached) download+warm-up has gotten so the UI can show real progress
    // instead of an indeterminate spinner for up to ~90s.
    const [browserAiLoadProgress, setBrowserAiLoadProgress] = useState<BrowserAiLoadStage | null>(null);

    const warmEndpoint = useCallback(async (
        runner: () => Promise<any>,
        inFlightRef: React.MutableRefObject<boolean>,
    ) => {
        if (inFlightRef.current) {
            return true;
        }
        inFlightRef.current = true;
        try {
            await runner();
            return true;
        } catch {
            return false;
        } finally {
            inFlightRef.current = false;
        }
    }, []);

    const pollUntilWarm = useCallback(async (
        runner: () => Promise<any>,
        inFlightRef: React.MutableRefObject<boolean>,
        stopRef?: React.MutableRefObject<boolean>,
    ) => {
        if (!navigator.onLine) {
            await waitForOnline();
        }
        while (!stopRef?.current) {
            const ok = await warmEndpoint(runner, inFlightRef);
            if (ok) {
                return;
            }
            await sleep(WARMUP_POLL_INTERVAL_MS);
        }
    }, [warmEndpoint]);

    const warmBackend = useCallback(() => (
        get('/health', { signal: AbortSignal.timeout(WARMUP_REQUEST_TIMEOUT_MS) }).then(() => undefined)
    ), []);
    const warmUpload = useCallback(() => (
        getUpload('/health', { signal: AbortSignal.timeout(WARMUP_REQUEST_TIMEOUT_MS) }).then(() => undefined)
    ), []);
    const startBackendKeepalive = useCallback(() => {
        if (backendKeepaliveTimerRef.current !== null) {
            return;
        }
        void warmEndpoint(warmBackend, backendWarmupInFlightRef);
        backendKeepaliveTimerRef.current = window.setInterval(() => {
            void warmEndpoint(warmBackend, backendWarmupInFlightRef);
        }, BACKEND_KEEPALIVE_INTERVAL_MS);
    }, [warmBackend, warmEndpoint]);

    const stopBackendKeepalive = useCallback(() => {
        if (backendKeepaliveTimerRef.current !== null) {
            window.clearInterval(backendKeepaliveTimerRef.current);
            backendKeepaliveTimerRef.current = null;
        }
    }, []);

    const uploadInputRef = useRef<HTMLInputElement | null>(null);
    // Mirrors the `uploading` state synchronously (no render-commit delay), so
    // startBrowserProcessing's automatic-pull gate can check it mid-tick without
    // a stale closure. Written directly alongside every setUploading(...) call
    // rather than via a useEffect on `uploading` -- see the gate in
    // startBrowserProcessing for why the ordering matters.
    const uploadingRef = useRef<boolean>(false);
    const uploadSessionRef = useRef<PersistedUploadSession | null>(null);
    const isResumingUploadRef = useRef<boolean>(false);
    const uploadStartInProgressRef = useRef<boolean>(false);
    const uploadSourceFilesRef = useRef<Map<string, File>>(new Map());
    // Mirrors uploadSourceFilesRef but for FileSystemFileHandle-backed
    // selections (Chrome/Edge desktop only, see fileSystemAccess.ts) --
    // cleared alongside it once a session concludes, since the durable
    // source of truth for resuming beyond this tab session is IndexedDB
    // (cacheUploadFilesForResume persists these handles there too).
    const uploadHandlesRef = useRef<Map<string, FileSystemFileHandle>>(new Map());
    const uploadStopRequestedRef = useRef<boolean>(false);
    const uploadAbortControllersRef = useRef<Set<AbortController>>(new Set());
    // Prefetched once per batch (see fetchKnownHashes) so uploadFileInChunks can
    // skip a known duplicate before spending any transfer bandwidth on it, not
    // just backend compute. Grown (never cleared) as files finish uploading, so
    // within-batch duplicates are caught without a second network round trip.
    const knownHashesRef = useRef<Map<string, string>>(new Map());
    // Coalesces concurrent /upload/init calls (one per upload lane) into
    // /upload/init-batch requests -- see requestBatchedInit below.
    const pendingBatchInitRef = useRef<Array<{
        filename: string;
        totalSize: number;
        sha256: string;
        resolve: (value: any) => void;
        reject: (err: unknown) => void;
    }>>([]);
    const batchInitDebounceTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    const batchInitMaxWaitTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    // Files picked (e.g. from a second folder) while a batch is already
    // uploading. Drained one batch at a time by the effect below once the
    // active session finishes cleanly -- see queuedUploadBatchesRef usage.
    const queuedUploadBatchesRef = useRef<File[][]>([]);
    const [queuedUploadFileCount, setQueuedUploadFileCount] = useState<number>(0);
    const backendKeepaliveTimerRef = useRef<number | null>(null);
    const backendWarmupInFlightRef = useRef<boolean>(false);
    const uploadWarmupInFlightRef = useRef<boolean>(false);
    const autoResumeAttemptedRef = useRef<boolean>(false);
    const browserAiModelStateRef = useRef<SharedBrowserAiModelState>(browserAiModelState);
    const browserAiLoadInFlightRef = useRef<boolean>(false);
    const uploadCompletionHandlersRef = useRef<Set<() => void | Promise<void>>>(new Set());
    const uploadErrorHandlerRef = useRef<((message: string | null) => void) | null>(null);
    const browserProcessingStartInFlightRef = useRef<boolean>(false);
    const browserProcessingCancelRef = useRef<boolean>(false);
    const browserProcessingTimerRef = useRef<number | null>(null);
    const browserProcessingDeferredRef = useRef<Map<string, number>>(new Map());
    const browserProcessingNotificationRef = useRef<BrowserProcessingNotificationState | null>(null);
    // Pending debounced "finished" finalize (see BROWSER_PROCESSING_FINALIZE_QUIET_MS).
    const browserProcessingFinalizeTimerRef = useRef<number | null>(null);
    const keepAliveRef = useRef<BackgroundKeepAlive | null>(null);
    const pumpBrowserProcessingRef = useRef<(() => void) | null>(null);
    // True while a browser-processing batch has actual pending work to drain
    // and on-device AI is available. Driven by the BackgroundKeepAlive instance
    // (see keepAliveRef) via onStateChange; used to keep the backend warm during
    // a batch, show a "Processing in background" status, and gate the heartbeat
    // worker + screen wake lock that keep the batch draining in a hidden/
    // minimized tab.
    const [browserProcessingActive, setBrowserProcessingActive] = useState<boolean>(false);

    const addNotification = useCallback((title: string, details: string, progress?: UploadProgress) => {
        const id = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
        const created: AppNotification = {
            id,
            title,
            details,
            timestamp: Date.now(),
            unread: true,
            progress,
        };
        setNotifications((prev) => [created, ...prev].slice(0, 40));
        return id;
    }, []);

    const updateNotification = useCallback((id: string, updates: Partial<AppNotification>) => {
        setNotifications((prev) => prev.map((notification) => (
            notification.id === id
                ? { ...notification, ...updates, unread: true, timestamp: Date.now() }
                : notification
        )));
    }, []);

    // --- Background job completion notifier ---------------------------------
    // Long-running worker jobs (reclustering, "find more faces", library
    // cleanup, preview generation) record their lifecycle server-side. We poll
    // for finished ones and surface each exactly once in the bell (+ a toast for
    // the higher-signal kinds). See services/jobNotifications for the contract.
    const jobPollTimerRef = useRef<number | null>(null);
    const jobPollInFlightRef = useRef<boolean>(false);
    // Guards startJobPolling itself against a second caller racing in before the
    // first has finished setting up jobPollTimerRef — see startJobPolling below.
    const jobPollStartingRef = useRef<boolean>(false);
    const notifiedJobIdsRef = useRef<Set<string>>(loadSeenJobIds());
    // In-flight jobs from the latest poll, surfaced to the UI for live progress.
    const [activeJobs, setActiveJobs] = useState<JobStatusRecord[]>([]);
    // Buffered clustering-kind completions awaiting a coalesced bell + toast,
    // flushed after CLUSTER_FLUSH_QUIET_MS of no new completions (see
    // scheduleClusterFlush).
    const pendingClusterCompletionsRef = useRef<Array<{ title: string; detail: string; failed: boolean }>>([]);
    const clusterFlushTimerRef = useRef<number | null>(null);
    // Whether the latest poll saw an ipwork (backend/both processing mode)
    // job still queued/running -- see scheduleClusterFlush below. This is the
    // backend-mode equivalent of browserProcessingNotificationRef: as long as
    // ipworker still has photos left to process, more per-photo clustering
    // completions are likely still coming.
    const ipworkJobsInFlightRef = useRef<boolean>(false);

    const flushClusterCompletions = useCallback(() => {
        if (clusterFlushTimerRef.current !== null) {
            window.clearTimeout(clusterFlushTimerRef.current);
            clusterFlushTimerRef.current = null;
        }
        const pending = pendingClusterCompletionsRef.current;
        pendingClusterCompletionsRef.current = [];
        if (pending.length === 0) {
            return;
        }
        if (pending.length === 1) {
            const only = pending[0];
            addNotification(only.title, only.detail);
            showToast(only.detail ? `${only.title} — ${only.detail}` : only.title, {
                variant: only.failed ? 'error' : 'default',
            });
            return;
        }
        const failedCount = pending.filter((entry) => entry.failed).length;
        const summary = failedCount > 0
            ? `Finished ${pending.length} background people updates (${failedCount} failed).`
            : `Finished ${pending.length} background people updates.`;
        addNotification('People grouping finished', summary);
        showToast(summary, { variant: failedCount > 0 ? 'error' : 'default' });
    }, [addNotification]);

    // Pushes the flush out to CLUSTER_FLUSH_QUIET_MS from now. Called each time
    // a new clustering completion is buffered, so a steady trickle of separate
    // jobs (spaced further apart than one poll interval, but closer together
    // than the quiet window) keeps deferring the flush instead of each one
    // settling into its own toast.
    // A fixed quiet-time alone isn't reliable here: real gaps between
    // individual clustering-job completions during an active upload session
    // are highly variable (measured 25s-400s+ depending on upload/network
    // pace), so any single timeout either fires too early during a slow
    // stretch (spamming one toast per job -- what CLUSTER_FLUSH_QUIET_MS=40s
    // alone still did) or is so long it delays a genuinely isolated action's
    // toast unacceptably. Instead of guessing from elapsed time, defer to
    // browser processing's own (already-debounced) notification lifecycle as
    // the authoritative "is there more coming" signal: clustering is always a
    // downstream consequence of photo processing, so if processing is still
    // considered active, just check again later rather than flushing yet.
    // Processing-unrelated clustering (Repair people clusters, Find more
    // faces) has no active processing notification to wait on, so it flushes
    // on the same quiet-time schedule as before, unaffected.
    const scheduleClusterFlush = useCallback(() => {
        if (clusterFlushTimerRef.current !== null) {
            window.clearTimeout(clusterFlushTimerRef.current);
        }
        clusterFlushTimerRef.current = window.setTimeout(() => {
            clusterFlushTimerRef.current = null;
            if (browserProcessingNotificationRef.current !== null || ipworkJobsInFlightRef.current) {
                scheduleClusterFlush();
                return;
            }
            flushClusterCompletions();
        }, CLUSTER_FLUSH_QUIET_MS);
    }, [flushClusterCompletions]);

    const pollJobStatusesOnce = useCallback(async (): Promise<boolean> => {
        // Resolves to whether any jobs are still in flight (so the caller knows
        // whether to keep polling).
        if (jobPollInFlightRef.current) {
            return true;
        }
        if (!isLikelyAuthenticated() || !navigator.onLine) {
            return false;
        }
        jobPollInFlightRef.current = true;
        try {
            const jobs = await fetchJobStatuses();
            const seen = notifiedJobIdsRef.current;
            const inFlight: JobStatusRecord[] = [];
            let active = false;
            let changed = false;
            for (const job of jobs) {
                if (!TERMINAL_JOB_STATUSES.has(job.status)) {
                    active = true;
                    inFlight.push(job);
                    continue;
                }
                if (!job.jobId || seen.has(job.jobId)) {
                    continue;
                }
                // Mark seen first so an unannounced-but-stale finish (below) is
                // never re-evaluated, and a completion is only ever shown once.
                seen.add(job.jobId);
                changed = true;
                const finishedAt = job.updatedAt ? Date.parse(job.updatedAt) : NaN;
                const isRecent = !Number.isFinite(finishedAt)
                    || (Date.now() - finishedAt) <= JOB_NOTIFY_WINDOW_MS;
                if (!isRecent) {
                    continue;
                }
                const detail = job.message || (job.status === 'failed' ? 'Something went wrong.' : '');
                if (CLUSTERING_JOB_KINDS.has(job.kind)) {
                    // Bulk actions (a big upload, approving several merge
                    // suggestions, a library-wide re-embed sweep) can finish many
                    // of these over time, closely spaced or trickling in one at a
                    // time; buffer and coalesce into one bell entry + toast
                    // instead of one per job (see scheduleClusterFlush/
                    // CLUSTER_FLUSH_QUIET_MS above). Skip intermediate batches
                    // (marked suppressNotification) — only notify on the final one.
                    if (!job.suppressNotification) {
                        pendingClusterCompletionsRef.current.push({
                            title: job.title,
                            detail,
                            failed: job.status === 'failed',
                        });
                        scheduleClusterFlush();
                    }
                } else if (job.kind !== 'preview' && job.kind !== 'ipwork') {
                    // ipwork (backend/both processing mode) runs one job per photo --
                    // same "frequent, low-signal" shape as preview, and the gallery
                    // tile's own processing badge already covers per-photo feedback.
                    addNotification(job.title, detail);
                    if (TOASTABLE_JOB_KINDS.has(job.kind)) {
                        showToast(detail ? `${job.title} — ${detail}` : job.title, {
                            variant: job.status === 'failed' ? 'error' : 'default',
                            dedupeKey: `job:${job.jobId}`,
                        });
                    }
                }
            }
            if (changed) {
                persistSeenJobIds(seen);
            }
            ipworkJobsInFlightRef.current = inFlight.some((job) => job.kind === 'ipwork');
            // Publish the in-flight set so the People page + global pill can show
            // that clustering is running. Terminal jobs are excluded, so this
            // empties (hiding the indicator) on the poll that observes completion.
            // Only replace when the id+status signature changed, so an idle poll
            // (or an unchanged in-flight set) doesn't fan a re-render app-wide.
            setActiveJobs((prev) => {
                const signature = (list: JobStatusRecord[]) => list.map((job) => `${job.jobId}:${job.status}`).join('|');
                return signature(prev) === signature(inFlight) ? prev : inFlight;
            });
            return active;
        } catch {
            return false;
        } finally {
            jobPollInFlightRef.current = false;
        }
    }, [addNotification, scheduleClusterFlush]);

    const stopJobPolling = useCallback(() => {
        if (jobPollTimerRef.current !== null) {
            window.clearInterval(jobPollTimerRef.current);
            jobPollTimerRef.current = null;
        }
    }, []);

    const startJobPolling = useCallback(() => {
        if (!isLikelyAuthenticated()) {
            return;
        }
        // Already polling, or another caller is mid-startup (between kicking off
        // the first poll and this callback deciding whether to arm the interval)
        // — let that one finish setting up jobPollTimerRef instead of racing it.
        if (jobPollTimerRef.current !== null || jobPollStartingRef.current) {
            return;
        }
        jobPollStartingRef.current = true;
        // Poll immediately, then keep polling while jobs remain in flight and go
        // idle once none are left (a nudge or refocus restarts the loop).
        void pollJobStatusesOnce().then((active) => {
            if (!active) {
                stopJobPolling();
                return;
            }
            if (jobPollTimerRef.current !== null) {
                return;
            }
            const startedAt = Date.now();
            jobPollTimerRef.current = window.setInterval(() => {
                if (Date.now() - startedAt > JOB_POLL_MAX_MS) {
                    stopJobPolling();
                    return;
                }
                void pollJobStatusesOnce().then((stillActive) => {
                    if (!stillActive) {
                        stopJobPolling();
                    }
                });
            }, JOB_POLL_INTERVAL_MS);
        }).finally(() => {
            jobPollStartingRef.current = false;
        });
    }, [pollJobStatusesOnce, stopJobPolling]);

    useEffect(() => {
        const unsubscribe = onJobPollRequested(startJobPolling);
        const handleVisibility = () => {
            if (document.visibilityState === 'visible') {
                startJobPolling();
            }
        };
        window.addEventListener('focus', startJobPolling);
        document.addEventListener('visibilitychange', handleVisibility);
        // Catch up on anything that finished while the app wasn't open.
        startJobPolling();
        return () => {
            unsubscribe();
            window.removeEventListener('focus', startJobPolling);
            document.removeEventListener('visibilitychange', handleVisibility);
            stopJobPolling();
            if (clusterFlushTimerRef.current !== null) {
                window.clearTimeout(clusterFlushTimerRef.current);
                clusterFlushTimerRef.current = null;
            }
            if (browserProcessingFinalizeTimerRef.current !== null) {
                window.clearTimeout(browserProcessingFinalizeTimerRef.current);
                browserProcessingFinalizeTimerRef.current = null;
            }
        };
    }, [startJobPolling, stopJobPolling]);

    // `pendingRemaining` is a live "not yet processed" count re-queried from the
    // server on every poll -- it shrinks as work completes and can look small at
    // any single instant even while more work keeps trickling in (e.g. uploads
    // still landing). It is NOT the total size of the job, so it must never be
    // used as the notification's totalCount directly (that previously let
    // processedCount race past totalCount, e.g. "Processed 127/102"). The true
    // total-to-date is always processed + failed + whatever's still remaining.
    const ensureBrowserProcessingNotification = useCallback((pendingRemaining: number) => {
        // Real activity is happening (or about to) -- cancel any debounced
        // "finished" finalize from a prior quiet-looking beat so it doesn't fire
        // out from under this notification (see BROWSER_PROCESSING_FINALIZE_QUIET_MS).
        if (browserProcessingFinalizeTimerRef.current !== null) {
            window.clearTimeout(browserProcessingFinalizeTimerRef.current);
            browserProcessingFinalizeTimerRef.current = null;
        }
        const current = browserProcessingNotificationRef.current;
        if (current) {
            current.totalCount = current.processedCount + current.failedCount + pendingRemaining;
            return current;
        }
        const next: BrowserProcessingNotificationState = {
            id: addNotification(
                'Browser processing started',
                `Preparing ${pendingRemaining} photo${pendingRemaining === 1 ? '' : 's'} for local processing.`,
                {
                    uploadedCount: 0,
                    totalCount: pendingRemaining,
                    failedCount: 0,
                    skippedDuplicateCount: 0,
                },
            ),
            totalCount: pendingRemaining,
            processedCount: 0,
            failedCount: 0,
        };
        browserProcessingNotificationRef.current = next;
        return next;
    }, [addNotification]);

    const syncBrowserProcessingNotification = useCallback((
        state: BrowserProcessingNotificationState,
        status?: string,
        title = 'Browser processing',
    ) => {
        updateNotification(state.id, {
            title,
            details: formatBrowserProcessingNotificationDetails(
                state.processedCount,
                state.totalCount,
                state.failedCount,
                status,
            ),
            progress: {
                uploadedCount: state.processedCount,
                totalCount: state.totalCount,
                failedCount: state.failedCount,
                skippedDuplicateCount: 0,
            },
        });
    }, [updateNotification]);

    const clearNotifications = useCallback(() => {
        setNotifications([]);
    }, []);

    const markAllNotificationsRead = useCallback(() => {
        setNotifications((prev) => prev.map((notification) => ({ ...notification, unread: false })));
    }, []);

    const unreadCount = notifications.filter((notification) => notification.unread).length;

    const pendingUploadSummary = useMemo(() => {
        if (!pendingUploadSession) {
            return null;
        }
        const unfinished = pendingUploadSession.files.filter((file) => file.status !== 'done');
        const failed = unfinished.filter((file) => file.status === 'failed');
        return {
            fileCount: unfinished.length,
            failedCount: failed.length,
        };
    }, [pendingUploadSession]);

    const pendingUploadFailedFiles = useMemo(() => (
        pendingUploadSession
            ? pendingUploadSession.files.filter((file) => file.status === 'failed')
            : []
    ), [pendingUploadSession]);

    // uploadErrorHandlerRef is only ever wired up by PhotoGallery.tsx's own
    // mount effect -- it's a no-op with the message silently discarded on any
    // other route (Tools, Library, Albums...), even though the global Upload
    // button in the header (RootServiceActions) is reachable from all of
    // them. A toast is route-independent, so it's the fallback that actually
    // guarantees an upload failure is visible instead of vanishing when the
    // user isn't on the Gallery page at that moment.
    const setUploadError = useCallback((message: string | null) => {
        uploadErrorHandlerRef.current?.(message);
        if (message) {
            showToast(message, { variant: 'error' });
        }
    }, []);

    const invalidatePhotoCache = useCallback(() => {
        try {
            // Boot caches are scoped per library (`${base}.${libraryId}`); clear
            // the base key and every per-library variant so none lingers stale.
            for (let i = localStorage.length - 1; i >= 0; i -= 1) {
                const key = localStorage.key(i);
                if (key && (key === PHOTO_CACHE_STORAGE_KEY || key.startsWith(`${PHOTO_CACHE_STORAGE_KEY}.`))) {
                    localStorage.removeItem(key);
                }
            }
        } catch {
            // Cache invalidation should not block upload completion.
        }
    }, []);

    const notifyUploadComplete = useCallback(async () => {
        invalidatePhotoCache();
        // Finalizing a photo with faces enqueues a clustering job server-side;
        // wake the job poller so the "Grouping people…" indicator can appear.
        requestJobPoll();
        const handlers = Array.from(uploadCompletionHandlersRef.current);
        await Promise.all(handlers.map((handler) => Promise.resolve(handler()).catch(() => undefined)));
    }, [invalidatePhotoCache]);

    const registerUploadCompletionHandler = useCallback((handler: () => void | Promise<void>) => {
        uploadCompletionHandlersRef.current.add(handler);
        return () => {
            uploadCompletionHandlersRef.current.delete(handler);
        };
    }, []);

    const registerUploadErrorHandler = useCallback((handler: (message: string | null) => void) => {
        uploadErrorHandlerRef.current = handler;
        return () => {
            if (uploadErrorHandlerRef.current === handler) {
                uploadErrorHandlerRef.current = null;
            }
        };
    }, []);

    const loadPersistedSession = useCallback((): PersistedUploadSession | null => {
        try {
            const raw = localStorage.getItem(UPLOAD_SESSION_STORAGE_KEY);
            if (!raw) {
                return null;
            }
            const parsed = JSON.parse(raw) as PersistedUploadSession;
            if (!parsed || !Array.isArray(parsed.files)) {
                return null;
            }
            return parsed;
        } catch {
            return null;
        }
    }, []);

    const persistSession = useCallback((session: PersistedUploadSession | null) => {
        uploadSessionRef.current = session;
        try {
            if (!session) {
                localStorage.removeItem(UPLOAD_SESSION_STORAGE_KEY);
                return;
            }
            localStorage.setItem(UPLOAD_SESSION_STORAGE_KEY, JSON.stringify(session));
        } catch {
            // Storage quota or serialization errors just mean this session won't
            // resume after a refresh -- the in-memory ref above still drives the
            // active upload, so a full localStorage must not abort the transfer.
        }
    }, []);

    const updatePersistedFile = useCallback((fileKey: string, updates: Partial<PersistedUploadFile>) => {
        const current = uploadSessionRef.current;
        if (!current) {
            return;
        }
        const next: PersistedUploadSession = {
            ...current,
            files: current.files.map((file) => (file.key === fileKey ? { ...file, ...updates } : file)),
        };
        persistSession(next);
    }, [persistSession]);

    const clearPersistedSession = useCallback(async () => {
        const current = uploadSessionRef.current;
        if (current) {
            await Promise.all(current.files.map((file) => idbDelete(file.key).catch(() => undefined)));
        }
        persistSession(null);
    }, [persistSession]);

    const computeSha256 = async (file: File): Promise<string> => (
        sha256ArrayBuffer(await readBlobArrayBuffer(file))
    );

    // GET analogue of postUploadWithRetry below: /upload/known-hashes can hit
    // the same cold-start 502/503 as init/finalize (the backend scales to
    // zero), and without a retry here that would silently disable client-side
    // dedup for the WHOLE batch instead of just costing one extra round trip.
    const getUploadWithRetry = async <T = any>(url: string): Promise<T> => {
        let lastError: unknown = null;
        for (let attempt = 0; attempt < MAX_FINALIZE_RETRIES; attempt += 1) {
            if (uploadStopRequestedRef.current) {
                throw new Error(UPLOAD_STOPPED_ERROR);
            }
            try {
                return await getUpload<T>(url);
            } catch (err) {
                lastError = err;
                if (!isRetriableUploadError(err)) {
                    break;
                }
                if (!navigator.onLine) {
                    await waitForOnline();
                }
                await sleep(Math.min(1000 * Math.pow(2, attempt), 15000));
            }
        }
        throw lastError || new Error(`Request to ${url} failed.`);
    };

    // Best-effort prefetch of the library's dedup index (see backend
    // list_known_file_hashes), merged into knownHashesRef so uploadFileInChunks
    // can skip a known duplicate before staging a single byte. A failed/slow
    // fetch just means this batch misses the early-skip optimization -- the
    // backend's own point-lookup at finalize time stays authoritative, so
    // correctness never depends on this succeeding.
    const fetchKnownHashes = async (): Promise<void> => {
        try {
            const response = await getUploadWithRetry<{ hashes?: Record<string, string> }>('/upload/known-hashes');
            const hashes = response?.hashes;
            if (hashes && typeof hashes === 'object') {
                for (const [hash, filename] of Object.entries(hashes)) {
                    knownHashesRef.current.set(hash, filename);
                }
            }
        } catch {
            // Ignore -- see comment above.
        }
    };

    // Shared by /upload/init and /upload/finalize: with the backend allowed to
    // scale to zero during a long-running upload (see the keepalive gate below),
    // either call can land on a cold replica and get a 502/503 while it starts.
    // Same exponential backoff (capped 15s, MAX_FINALIZE_RETRIES attempts -- up
    // to ~75s total) for both, since a cold start is a cold start either way.
    const postUploadWithRetry = async (url: string, payload: any, signal?: AbortSignal) => {
        let lastError: unknown = null;
        for (let attempt = 0; attempt < MAX_FINALIZE_RETRIES; attempt += 1) {
            if (uploadStopRequestedRef.current || signal?.aborted) {
                throw new Error(UPLOAD_STOPPED_ERROR);
            }
            try {
                return await postUpload(url, payload);
            } catch (err) {
                lastError = err;
                if (!isRetriableUploadError(err)) {
                    break;
                }
                if (!navigator.onLine) {
                    await waitForOnline();
                }
                await sleep(Math.min(1000 * Math.pow(2, attempt), 15000));
            }
        }
        throw lastError || new Error(`Request to ${url} failed.`);
    };
    const postInitWithRetry = (payload: any, signal?: AbortSignal) => postUploadWithRetry('/upload/init', payload, signal);
    const postFinalizeWithRetry = (payload: any, signal?: AbortSignal) => postUploadWithRetry('/upload/finalize', payload, signal);

    // Fresh (non-resumed) uploads register in chunks via /upload/init-batch
    // instead of one /upload/init HTTP round trip per file -- each of the
    // (up to ~20) concurrent upload lanes calls requestBatchedInit for its own
    // file at roughly the same moment. Resumed uploads (existingUploadId
    // already set) skip this and call postInitWithRetry directly -- see
    // uploadFileInChunks.
    //
    // Debounced with a hard cap, not a single fixed window: each lane only
    // reaches this call after its own file finishes hashing (crypto.subtle),
    // and hash time scales with file size -- a batch of files with varied
    // sizes arrives staggered, not all at once. A single short fixed window
    // (previously 30ms) closed before slower lanes' hashes finished, so most
    // batches ended up size 1 in practice (confirmed live: every batch in a
    // production HAR capture had exactly 1 file). Instead, every new arrival
    // resets a short debounce (catches lanes finishing within a few ms of
    // each other); a longer max-wait timer from the FIRST arrival guarantees
    // the batch still flushes promptly even if requests keep trickling in
    // one at a time rather than clustering.
    const INIT_BATCH_MAX_SIZE = 12;
    const INIT_BATCH_DEBOUNCE_MS = 50;
    const INIT_BATCH_MAX_WAIT_MS = 250;

    const flushInitBatch = async () => {
        const batch = pendingBatchInitRef.current;
        pendingBatchInitRef.current = [];
        if (batchInitDebounceTimerRef.current) {
            clearTimeout(batchInitDebounceTimerRef.current);
            batchInitDebounceTimerRef.current = null;
        }
        if (batchInitMaxWaitTimerRef.current) {
            clearTimeout(batchInitMaxWaitTimerRef.current);
            batchInitMaxWaitTimerRef.current = null;
        }
        if (batch.length === 0) {
            return;
        }
        try {
            const response = await postUploadWithRetry('/upload/init-batch', {
                files: batch.map(({ filename, totalSize, sha256 }) => ({ filename, totalSize, sha256 })),
            });
            const results = Array.isArray(response?.results) ? response.results : [];
            batch.forEach((req, index) => {
                const result = results[index];
                if (result && !result.error) {
                    req.resolve(result);
                } else {
                    req.reject(new Error(result?.error || 'Batch init failed for this file.'));
                }
            });
        } catch (err) {
            batch.forEach((req) => req.reject(err));
        }
    };

    const requestBatchedInit = (filename: string, totalSize: number, sha256: string): Promise<any> => (
        new Promise((resolve, reject) => {
            // Never let two entries for the identical filename land in the same
            // /upload/init-batch request -- the backend reserves blob names keyed
            // purely by filename per request, so two same-named files in one call
            // corrupt each other's reservation (both can end up staging blocks to
            // the same blob). This happens more than it sounds: Apple Photos
            // exports many distinct edited photos all as "FullSizeRender.heic",
            // and a folder-select can easily land two of them in the same debounce
            // window. Flushing the in-flight batch first guarantees the duplicate
            // starts a fresh batch instead.
            if (pendingBatchInitRef.current.some((req) => req.filename === filename)) {
                void flushInitBatch();
            }
            pendingBatchInitRef.current.push({ filename, totalSize, sha256, resolve, reject });
            if (pendingBatchInitRef.current.length >= INIT_BATCH_MAX_SIZE) {
                void flushInitBatch();
                return;
            }
            if (batchInitDebounceTimerRef.current) {
                clearTimeout(batchInitDebounceTimerRef.current);
            }
            batchInitDebounceTimerRef.current = setTimeout(() => { void flushInitBatch(); }, INIT_BATCH_DEBOUNCE_MS);
            if (!batchInitMaxWaitTimerRef.current) {
                batchInitMaxWaitTimerRef.current = setTimeout(() => { void flushInitBatch(); }, INIT_BATCH_MAX_WAIT_MS);
            }
        })
    );

    const startBrowserProcessing = useCallback(async (options: BrowserProcessingStartOptions = {}) => {
        if (browserProcessingStartInFlightRef.current) {
            return 0;
        }
        // Don't pull pending work before the user is authenticated; the request
        // would 401 and the scheduler would log a spurious failure on every tick.
        if (!(await canAuthenticateRequests())) {
            return 0;
        }
        browserProcessingStartInFlightRef.current = true;
        browserProcessingCancelRef.current = false;
        let processedCount = 0;
        try {
            const requestedItems = Array.isArray(options.items) && options.items.length > 0
                ? Array.from(new Map(options.items
                    .map((item) => ({
                        filename: String(item?.filename || '').trim(),
                        rotation: normalizeRotationDegrees(item?.rotation || 0),
                    }))
                    .filter((item) => Boolean(item.filename))
                    .map((item) => [item.filename, item] as const)).values())
                : Array.isArray(options.filenames)
                    ? Array.from(new Map(options.filenames
                        .map((filename) => String(filename || '').trim())
                        .filter(Boolean)
                        .map((filename) => [filename, { filename, rotation: 0 }] as const)).values())
                    : [];
            const requestedFilenames = requestedItems.map((item) => item.filename);
            const requestedSteps = buildRequestedBrowserProcessingSteps(options.actions);
            const isAutomaticPull = requestedFilenames.length === 0;
            // Turbo processes several photos at once; pull enough pending work to
            // keep every concurrent slot fed. Off (concurrency 1) preserves the
            // original one-at-a-time drain exactly.
            const concurrency = getBrowserProcessingConcurrency();
            // Fetch a decent-sized batch up front, independent of lane count --
            // otherwise a low concurrency (e.g. 1-2 lanes, the common case with
            // turbo off) means re-polling /upload/processing/pending after almost
            // every photo. A bigger batch also gives each lane's persistent AI
            // worker (see runDrainWorker below) more photos to stay warm across
            // before the batch drains and it has to fetch again.
            const pendingBatchSize = Math.max(concurrency, PENDING_PROCESSING_BATCH_SIZE);
            // When browser AI isn't loaded, skip the automatic background pull: it
            // would download pending images just to run baseline (thumbnail/EXIF/
            // geocode) steps. Explicit, user-initiated requests still run so on-demand
            // actions work regardless of model state; automatic backfill (including
            // baseline steps) resumes once the model becomes available.
            // Defer the automatic background pull while an upload session is
            // actively transferring files: running CPU-heavy detection/embedding
            // work (WASM) concurrently with in-flight uploads was making the app
            // sluggish. runUploadSession explicitly recomputes keep-alive state
            // once it finishes, so nothing is lost -- this just sequences the two
            // phases instead of interleaving them. Leave the keep-alive alone
            // here: the upload owns it for the duration of the transfer, and a
            // heartbeat tick landing mid-upload must not release the wake lock.
            // Explicit, user-initiated requests (isAutomaticPull false) still run
            // immediately regardless.
            if (isAutomaticPull && uploadingRef.current) {
                return 0;
            }
            if (isAutomaticPull && browserAiModelStateRef.current.status !== 'available') {
                keepAliveRef.current?.stop();
                return 0;
            }
            const pendingResponse = requestedFilenames.length > 0
                ? { pending: requestedItems.map((item) => ({ ...item, statuses: {} })) }
                : await get(`/upload/processing/pending?limit=${pendingBatchSize}`);
            const pending = Array.isArray(pendingResponse?.pending) ? pendingResponse.pending : [];
            if (pending.length === 0) {
                // Queue drained: release the background keep-alive so the tab stops
                // holding a wake lock / heartbeat worker while idle. Any faces that
                // were deferred (a rare genuine throttle/failure) are retried when
                // the tab next becomes visible.
                keepAliveRef.current?.stop();
                return 0;
            }
            // There is work to drain: keep the tab processing at full speed even
            // when it is hidden/minimized, and keep the device from sleeping/
            // locking. Only meaningful once on-device AI is on.
            if (browserAiModelStateRef.current.status === 'available') {
                keepAliveRef.current?.start();
            }
            const pendingTotal = Number(pendingResponse?.totalPending);
            const pendingRemaining = isAutomaticPull && Number.isFinite(pendingTotal)
                ? Math.max(pending.length, pendingTotal)
                : pending.length;
            const browserProcessingNotification = ensureBrowserProcessingNotification(pendingRemaining);
            const processOnePendingItem = async (
                item: any,
                aiWorkerHandle?: BrowserAiWorkerHandle,
                preClaimed?: { claimed?: boolean; leaseId?: string; thumbnailUploadUrl?: string; reason?: string },
            ): Promise<void> => {
                if (browserProcessingCancelRef.current) {
                    return;
                }
                const filename = String(item?.filename || '');
                const itemRotation = normalizeRotationDegrees(item?.rotation || 0);
                if (!filename) {
                    return;
                }
                // Only run the steps the current model state / deploy-time processing
                // mode allow. When browser AI isn't loaded, on-device AI steps are
                // dropped and left pending; in 'backend' mode, thumbnail/exif are also
                // dropped (ipworker owns them there). A null result means "all steps"
                // (browser AI available, non-backend mode); an empty set means nothing
                // runnable right now (only gated/owned steps were requested).
                const effectiveSteps = restrictStepsForModel(
                    restrictStepsForProcessingMode(requestedSteps, getRuntimeConfig().processingMode || 'browser'),
                    browserAiModelStateRef.current.status === 'available',
                );
                if (effectiveSteps && effectiveSteps.size === 0) {
                    return;
                }
                if (!options.force) {
                    const statuses = item?.statuses || {};
                    if (!hasRunnableBrowserStep(statuses, effectiveSteps) && requestedFilenames.length === 0) {
                        return;
                    }
                }
                let claim: { claimed?: boolean; leaseId?: string; thumbnailUploadUrl?: string } | null = null;
                if (preClaimed) {
                    // Already claimed in the batch call runDrainWorker made for this
                    // lane's first item -- skip the redundant per-item network call.
                    claim = preClaimed;
                } else {
                    try {
                        claim = await post('/upload/processing/claim', {
                            filename,
                            steps: effectiveSteps ? Array.from(effectiveSteps) : undefined,
                        });
                    } catch (err) {
                        if (shouldSuppressLeaseWarning(err)) {
                            return;
                        }
                        throw err;
                    }
                }
                if (!claim?.claimed) {
                    return;
                }
                const processingStartedAt = performance.now();
                // /upload/client-processing already clears the lease server-side once
                // it succeeds (apply_client_processing_results_for_file) -- so once
                // that call has gone through, the explicit /release below in `finally`
                // has nothing left to do. Skip it then and save the round trip; only
                // fall back to it when no report was ever sent (e.g. every requested
                // step was already terminal before this attempt started).
                let leaseReleasedByReport = false;
                try {
                    syncBrowserProcessingNotification(browserProcessingNotification, `Processing ${filename}`);
                    let imageUrl = String(item?.sourceUrl || '');
                    if (!imageUrl) {
                        const access = await get(`/api/photos/access/image/${encodeURIComponent(filename)}`);
                        imageUrl = String(access?.url || '');
                    }
                    if (!imageUrl) {
                        throw new Error('Image access URL was not returned.');
                    }
                    const blob = await fetchProcessingBlob(imageUrl);
                    const file = new File([blob], filename, { type: blob.type || 'application/octet-stream' });
                    const convertedPreview = await fetchConvertedRawPreview(filename);
                    await post('/upload/processing/heartbeat', { filename, leaseId: claim?.leaseId || '' }).catch(() => undefined);
                    const result = await runBrowserProcessing(
                        file,
                        `browser-${filename}`,
                        performance.now(),
                        browserAiModelStateRef.current as Parameters<PhotoGalleryRuntime['runBrowserProcessing']>[3],
                        {
                            clientProcessing: {},
                            clientProcessingReport: [],
                        },
                        {
                            faceRotationDegrees: itemRotation,
                            thumbnailRotationDegrees: itemRotation,
                            convertedPreview,
                            aiWorkerHandle,
                        },
                    );
                    const filteredResult = filterBrowserProcessingResult(result, effectiveSteps);
                    const faceDeferred = effectiveSteps?.has('face') && isDeferrableFaceResult(filteredResult);
                    let thumbnailAlreadyUploaded = false;
                    if (filteredResult?.clientProcessing?.thumbnail) {
                        try {
                            thumbnailAlreadyUploaded = await uploadBrowserThumbnailDirectly(
                                filename,
                                filteredResult,
                                String(claim?.thumbnailUploadUrl || item?.thumbnailUploadUrl || ''),
                            );
                        } catch (thumbnailErr) {
                            console.warn(`Direct thumbnail upload failed for ${filename}.`, thumbnailErr);
                            delete filteredResult.clientProcessing.thumbnail;
                            filteredResult.clientProcessingReport = [
                                ...(Array.isArray(filteredResult.clientProcessingReport) ? filteredResult.clientProcessingReport : []),
                                {
                                    clientAssetId: `browser-${filename}`,
                                    step: 'thumbnail',
                                    status: 'failed',
                                    reason: 'direct_thumbnail_upload_failed',
                                    durationMs: Math.max(0, Math.round(performance.now() - processingStartedAt)),
                                    runtime: 'browser-processing-scheduler',
                                },
                            ];
                        }
                    }
                    await postUpload('/upload/client-processing', {
                        filename,
                        clientProcessingSchemaVersion: CLIENT_PROCESSING_SCHEMA_VERSION,
                        clientProcessing: filteredResult.clientProcessing,
                        clientProcessingReport: filteredResult.clientProcessingReport,
                        clientAssetId: `browser-${filename}`,
                        uploadId: `browser-${filename}`,
                        thumbnailAlreadyUploaded,
                    });
                    leaseReleasedByReport = true;
                    reportBrowserProcessingOutcome(true);
                    if (faceDeferred) {
                        browserProcessingDeferredRef.current.set(filename, itemRotation);
                        syncBrowserProcessingNotification(browserProcessingNotification, `Deferred ${filename} for later face retry`);
                        return;
                    }
                    processedCount += 1;
                    browserProcessingNotification.processedCount += 1;
                    syncBrowserProcessingNotification(browserProcessingNotification);
                } catch (err) {
                    const errorName = String((err as Error)?.name || '');
                    const errorMessage = String((err as Error)?.message || '');
                    if (
                        errorName === 'FaceModelDeferredError'
                        || errorMessage.includes('face_model_unavailable')
                        || errorMessage.includes('background_throttled')
                    ) {
                        browserProcessingDeferredRef.current.set(filename, itemRotation);
                        updatePersistedFile(filename, { status: 'pending', error: undefined });
                        syncBrowserProcessingNotification(browserProcessingNotification, `Deferred ${filename} for later face retry`);
                        return;
                    }
                    browserProcessingNotification.failedCount += 1;
                    reportBrowserProcessingOutcome(false);
                    const statuses = item?.statuses || {};
                    const failureReport = buildBrowserProcessingFailureReport(filename, statuses, effectiveSteps, processingStartedAt);
                    if (failureReport.length > 0) {
                        try {
                            await postUpload('/upload/client-processing', {
                                filename,
                                clientProcessingSchemaVersion: CLIENT_PROCESSING_SCHEMA_VERSION,
                                clientProcessing: {},
                                clientProcessingReport: failureReport,
                                clientAssetId: `browser-${filename}`,
                                uploadId: `browser-${filename}`,
                            });
                            leaseReleasedByReport = true;
                        } catch (postErr) {
                            console.warn(`Browser processing failure report was not accepted for ${filename}.`, postErr);
                        }
                    }
                    console.warn(`Browser processing failed for ${filename}.`, err);
                    syncBrowserProcessingNotification(browserProcessingNotification, `Failed ${filename}`);
                } finally {
                    if (!leaseReleasedByReport) {
                        await post('/upload/processing/release', { filename, leaseId: claim?.leaseId || '' }).catch(() => undefined);
                    }
                }
            };
            // Drain `pending` with up to `concurrency` items in flight at once. Each
            // lane pulls the next index and processes it; with concurrency 1 this is
            // exactly the original serial drain. Items are independent (each holds its
            // own server-side lease), so distinct filenames never collide.
            //
            // Each lane is its own "conveyor belt": one persistent AI worker created
            // once per lane and reused for every photo that lane pulls, instead of a
            // fresh Worker (and a from-scratch CLIP/vocabulary reload) per photo --
            // see createPersistentBrowserAiWorker. Skipped entirely when this batch
            // can't use it anyway (model not loaded, or backend-owns-AI mode), so an
            // idle/ineligible drain never pays for a worker it won't use.
            const shouldUseAiWorker = browserAiModelStateRef.current.status === 'available'
                && (getRuntimeConfig().processingMode || 'browser') !== 'backend';
            const workerCount = Math.max(1, Math.min(concurrency, pending.length));
            // Batch-claim only the first `workerCount` items -- as many as are
            // about to start being worked on right now (every lane begins
            // pulling from index 0 simultaneously via Promise.all below), not
            // the whole fetched batch: claiming further ahead than a lane is
            // about to reach would hold leases on photos nobody's processing
            // yet, risking the 120s lease expiring before a lane gets there.
            // Skipped at concurrency 1 (the default, Turbo off) -- there's
            // only one lane and one first item, so there's nothing to batch.
            // Falls back to each lane's own per-item claim call (unchanged)
            // if this fails or a filename isn't in the response.
            const preClaimedByFilename = new Map<string, { claimed?: boolean; leaseId?: string; thumbnailUploadUrl?: string; reason?: string }>();
            if (workerCount > 1) {
                const firstWaveSteps = restrictStepsForModel(
                    restrictStepsForProcessingMode(requestedSteps, getRuntimeConfig().processingMode || 'browser'),
                    browserAiModelStateRef.current.status === 'available',
                );
                try {
                    const batchResponse = await post('/upload/processing/claim-batch', {
                        items: pending.slice(0, workerCount).map((item: any) => ({
                            filename: String(item?.filename || ''),
                            steps: firstWaveSteps ? Array.from(firstWaveSteps) : undefined,
                        })),
                    });
                    const results = Array.isArray(batchResponse?.results) ? batchResponse.results : [];
                    for (const result of results) {
                        const filename = String(result?.filename || '');
                        if (filename) {
                            preClaimedByFilename.set(filename, result);
                        }
                    }
                } catch {
                    // Fall back to each lane claiming its own first item individually.
                }
            }
            let nextIndex = 0;
            const runDrainWorker = async (): Promise<void> => {
                const aiWorkerHandle = shouldUseAiWorker ? await createPersistentBrowserAiWorker() : undefined;
                try {
                    while (!browserProcessingCancelRef.current) {
                        const index = nextIndex;
                        nextIndex += 1;
                        if (index >= pending.length) {
                            return;
                        }
                        const filename = String(pending[index]?.filename || '');
                        const preClaimed = preClaimedByFilename.get(filename);
                        preClaimedByFilename.delete(filename);
                        await processOnePendingItem(pending[index], aiWorkerHandle, preClaimed);
                    }
                } finally {
                    aiWorkerHandle?.dispose();
                }
            };
            await Promise.all(Array.from({ length: workerCount }, () => runDrainWorker()));
            // Posting face-detection results enqueues a clustering job server-side;
            // wake the job poller so the "Grouping people…" indicator can show.
            if (browserProcessingNotification.processedCount > 0) {
                requestJobPoll();
            }
            const hasDeferredWork = browserProcessingDeferredRef.current.size > 0;
            const hasObservedWork = browserProcessingNotification.processedCount > 0
                || browserProcessingNotification.failedCount > 0
                || hasDeferredWork;
            const shouldKeepNotification = hasDeferredWork
                || (isAutomaticPull
                    && hasObservedWork
                    && browserProcessingNotification.processedCount + browserProcessingNotification.failedCount < browserProcessingNotification.totalCount);
            if (shouldKeepNotification) {
                syncBrowserProcessingNotification(
                    browserProcessingNotification,
                    hasDeferredWork ? 'Waiting for deferred face retries' : 'Continuing browser processing',
                );
            } else {
                // Looks done as of this batch, but don't finalize yet -- the next
                // photo (upload trickling in, or the next drain cycle) may still be
                // moments away. Debounce: only actually finalize after a real quiet
                // period with no further activity on this notification.
                if (browserProcessingFinalizeTimerRef.current !== null) {
                    window.clearTimeout(browserProcessingFinalizeTimerRef.current);
                }
                const notificationToFinalize = browserProcessingNotification;
                browserProcessingFinalizeTimerRef.current = window.setTimeout(() => {
                    browserProcessingFinalizeTimerRef.current = null;
                    syncBrowserProcessingNotification(
                        notificationToFinalize,
                        undefined,
                        'Browser processing finished',
                    );
                    if (browserProcessingNotificationRef.current === notificationToFinalize) {
                        browserProcessingNotificationRef.current = null;
                    }
                }, BROWSER_PROCESSING_FINALIZE_QUIET_MS);
            }
        } catch (err) {
            console.warn('Browser processing scheduler failed.', err);
        } finally {
            browserProcessingStartInFlightRef.current = false;
            const isAutomaticPull = !Array.isArray(options.filenames) || options.filenames.length === 0;
            if (isAutomaticPull && processedCount > 0 && !browserProcessingCancelRef.current) {
                window.setTimeout(() => {
                    void startBrowserProcessing();
                }, 0);
            }
        }
        return processedCount;
    }, [
        ensureBrowserProcessingNotification,
        syncBrowserProcessingNotification,
        updateNotification,
    ]);

    useEffect(() => {
        browserAiModelStateRef.current = browserAiModelState;
    }, [browserAiModelState]);


    useEffect(() => {
        const refreshLightweightBrowserAiState = () => {
            const unsupportedDetail = getBrowserAiUnsupportedReason();
            if (unsupportedDetail) {
                setBrowserAiModelState(browserAiUnsupportedState(unsupportedDetail));
                return;
            }
            setBrowserAiModelState((current: SharedBrowserAiModelState) => (
                current.status === 'checking' || current.status === 'unsupported'
                    ? browserAiIdleState()
                    : current
            ));
        };
        refreshLightweightBrowserAiState();
        const connection = (navigator as Navigator & { connection?: EventTarget }).connection;
        window.addEventListener('online', refreshLightweightBrowserAiState);
        window.addEventListener('offline', refreshLightweightBrowserAiState);
        connection?.addEventListener?.('change', refreshLightweightBrowserAiState);
        return () => {
            window.removeEventListener('online', refreshLightweightBrowserAiState);
            window.removeEventListener('offline', refreshLightweightBrowserAiState);
            connection?.removeEventListener?.('change', refreshLightweightBrowserAiState);
        };
    }, []);

    const loadBrowserAiModel = useCallback(async (): Promise<SharedBrowserAiModelState> => {
        const current = browserAiModelStateRef.current;
        if (browserAiLoadInFlightRef.current || current.status === 'loading' || current.status === 'checking' || current.status === 'available' || current.status === 'unsupported') {
            return current;
        }
        if (getRuntimeConfig().processingMode === 'backend') {
            // Deployment is configured to process everything server-side (ipworker);
            // never download the onnx/wasm/CLIP assets on the client at all --
            // that's the whole point of this mode for weak/low-power devices.
            const unsupported = browserAiUnsupportedState('Browser AI is disabled by this deployment (server-side processing mode).');
            setBrowserAiModelState(unsupported);
            return unsupported;
        }
        browserAiLoadInFlightRef.current = true;
        setBrowserAiModelState(browserAiLoadingState(current));
        setBrowserAiLoadProgress(null);
        // Warm the native face models (BlazeFace/ArcFace) alongside the vision worker
        // so all on-device AI is ready together once the user opts in.
        preloadNativeFaceModels();
        const notificationId = addNotification('Loading browser AI', 'Checking model manifest, cache, runtime, and warm-up.');
        const onProgress = (stage: BrowserAiLoadStage) => {
            setBrowserAiLoadProgress(stage);
            updateNotification(notificationId, { details: `${stage.label}… (${stage.index}/${stage.total})` });
        };
        try {
            let result: SharedBrowserAiModelState = current;
            for (let attempt = 1; attempt <= BROWSER_AI_LOAD_MAX_RETRIES + 1; attempt += 1) {
                try {
                    result = await acquireBrowserAiModel(onProgress);
                } catch (err) {
                    const detail = err instanceof Error ? err.message : 'unknown_error';
                    result = {
                        status: 'unavailable',
                        reason: 'unknown_error',
                        detail,
                        modelAvailability: 'unavailable',
                        modelCacheStatus: 'failed',
                        runtime: 'browser-ai-worker',
                        modelAcquisitionMs: 0,
                    };
                }
                // Only retry failures that look transient (a cold-tab race, a load that
                // simply broke). Network-gated reasons (offline/poor-network/save-data)
                // and unsupported-runtime are excluded -- retrying blindly won't fix
                // those, and the online/connection-change listener above already
                // re-arms the button once conditions actually change.
                const canRetry = result.status === 'unavailable' && !isBrowserAiNetworkRetryReason(result.reason);
                if (!canRetry || attempt > BROWSER_AI_LOAD_MAX_RETRIES) {
                    break;
                }
                updateNotification(notificationId, {
                    details: `${formatBrowserAiReason(result)} — retrying (${attempt}/${BROWSER_AI_LOAD_MAX_RETRIES})…`,
                });
                await new Promise((resolve) => window.setTimeout(resolve, browserAiLoadRetryDelayMs(attempt)));
            }
            setBrowserAiModelState(result);
            if (result.status === 'available') {
                updateNotification(notificationId, {
                    title: 'Browser AI ready',
                    details: `Model loaded${result.modelVersion ? ` (${result.modelVersion})` : ''}.`,
                });
                // Dynamically import tesseract.js so OCR becomes available when
                // the user loads Browser AI. Await the import so the initial
                // backfill run can include OCR; if import fails, still proceed
                // to start processing.
                try {
                    if (typeof window !== 'undefined' && !(window as any).Tesseract) {
                        const mod = await import('tesseract.js');
                        (window as any).Tesseract = (mod as any).default || mod;
                        updateNotification(notificationId, {
                            title: 'Browser AI ready (OCR available)',
                            details: `Model loaded${result.modelVersion ? ` (${result.modelVersion})` : ''}. OCR engine ready.`,
                        });
                    }
                } catch {
                    // Ignore failures; proceed to start processing without OCR.
                }
                // Drain any photos that were uploaded while AI was off (backfill).
                window.setTimeout(() => { void startBrowserProcessing(); }, 0);
            } else {
                updateNotification(notificationId, {
                    title: result.status === 'unsupported' ? 'Browser AI unsupported' : 'Browser AI unavailable',
                    details: String(formatBrowserAiReason(result)),
                });
            }
        } finally {
            browserAiLoadInFlightRef.current = false;
            setBrowserAiLoadProgress(null);
        }
        return browserAiModelStateRef.current;
    }, [addNotification, updateNotification, startBrowserProcessing]);

    // Driven by the keep-alive heartbeat worker on every tick. startBrowserProcessing
    // self-guards against overlapping runs, so a no-op tick is cheap.
    const pumpBrowserProcessing = useCallback(() => {
        void startBrowserProcessing();
    }, [startBrowserProcessing]);

    useEffect(() => {
        pumpBrowserProcessingRef.current = pumpBrowserProcessing;
    }, [pumpBrowserProcessing]);

    // Own a single keep-alive controller for the lifetime of the provider. It is
    // started/stopped from startBrowserProcessing as the queue gains/loses work.
    useEffect(() => {
        const keepAlive = new BackgroundKeepAlive({
            onTick: () => pumpBrowserProcessingRef.current?.(),
            intervalMs: BROWSER_PROCESSING_HEARTBEAT_INTERVAL_MS,
            onStateChange: (active) => {
                setBrowserProcessingActive(active);
            },
        });
        keepAliveRef.current = keepAlive;
        return () => {
            keepAlive.dispose();
            keepAliveRef.current = null;
        };
    }, []);

    useEffect(() => {
        if (browserAiModelState.status !== 'available' || browserProcessingDeferredRef.current.size === 0) {
            return;
        }
        const items = Array.from(browserProcessingDeferredRef.current.entries()).map(([filename, rotation]) => ({
            filename,
            rotation,
        }));
        browserProcessingDeferredRef.current.clear();
        void startBrowserProcessing({ actions: ['faces'], items, force: true });
    }, [browserAiModelState.status, startBrowserProcessing]);

    useEffect(() => {
        // Flush faces that were deferred (e.g. background-throttled) when the tab
        // regains focus. Only runs when browser AI is already loaded; we never
        // auto-load the model here — that stays a manual, user-initiated action.
        const retryDeferredBrowserFaces = () => {
            if (document.visibilityState !== 'visible' || browserProcessingDeferredRef.current.size === 0) {
                return;
            }
            if (browserAiModelStateRef.current.status !== 'available') {
                return;
            }
            const items = Array.from(browserProcessingDeferredRef.current.entries()).map(([filename, rotation]) => ({
                filename,
                rotation,
            }));
            browserProcessingDeferredRef.current.clear();
            void startBrowserProcessing({ actions: ['faces'], items, force: true });
        };
        window.addEventListener('visibilitychange', retryDeferredBrowserFaces);
        window.addEventListener('focus', retryDeferredBrowserFaces);
        return () => {
            window.removeEventListener('visibilitychange', retryDeferredBrowserFaces);
            window.removeEventListener('focus', retryDeferredBrowserFaces);
        };
    }, [startBrowserProcessing]);

    const uploadFileInChunks = useCallback(async (
        file: File,
        options?: {
            startByte?: number;
            existingUploadId?: string;
            existingBlockIds?: string[];
            existingBlobName?: string;
            onChunkCommitted?: (bytesReceived: number) => void;
            onUploadInitialized?: (uploadId: string, blobName: string) => void;
            onBlockCommitted?: (blockIds: string[], chunkSizeBytes: number) => void;
            onFinalizeStarted?: () => void;
            signal?: AbortSignal;
            chunkSizeBytes?: number;
            blockConcurrency?: number;
        },
    ): Promise<{ skippedDuplicate: true } | { skippedDuplicate: false; sha256: string; uploadId: string }> => {
        if (uploadStopRequestedRef.current || options?.signal?.aborted) {
            throw new Error(UPLOAD_STOPPED_ERROR);
        }
        const sha256 = await computeSha256(file);
        if (uploadStopRequestedRef.current || options?.signal?.aborted) {
            throw new Error(UPLOAD_STOPPED_ERROR);
        }
        if (knownHashesRef.current.has(sha256)) {
            // Already have this content under some filename -- skip the network
            // transfer entirely (no /upload/init, no blob staging, no finalize).
            // The backend's own finalize-time check is what actually enforces
            // this for real; this is purely an earlier, cheaper exit.
            return { skippedDuplicate: true };
        }
        // Claim this hash immediately, synchronously with the check above (no
        // await in between) -- otherwise two concurrent lanes uploading
        // byte-identical NEW files would both pass the .has() check before
        // either finishes, and neither would see the other's claim. Rolled
        // back in the catch below if this upload doesn't end up succeeding,
        // so a failed upload can't leave its content permanently unclaimed.
        knownHashesRef.current.set(sha256, file.name);
        try {
            // Resumes carry an existing uploadId and must go straight to
            // single-file /upload/init (it needs to reuse the SAME reserved
            // blob/uploadId, which /upload/init-batch doesn't support -- see
            // its docstring). Fresh uploads coalesce into /upload/init-batch.
            const initResponse = options?.existingUploadId
                ? await postInitWithRetry({
                    filename: file.name,
                    totalSize: file.size,
                    sha256,
                    directToBlob: true,
                    uploadId: options.existingUploadId,
                }, options?.signal)
                : await requestBatchedInit(file.name, file.size, sha256);
            const blobName = typeof initResponse?.blobName === 'string' ? initResponse.blobName : '';
            if (typeof initResponse?.uploadId === 'string' && initResponse.uploadId) {
                options?.onUploadInitialized?.(initResponse.uploadId, blobName);
            }
            if (typeof initResponse?.blobUrl !== 'string' || !initResponse.blobUrl) {
                throw new Error('Blob upload URL was not returned by the backend.');
            }
            const configuredChunkSize = Math.min(
                MAX_BACKEND_UPLOAD_CHUNK_BYTES,
                Math.max(512 * 1024, options?.chunkSizeBytes || DEFAULT_UPLOAD_PROFILE.chunkSizeBytes),
            );
            // Cap to a parallel-friendly block size. This is resume-stable: the block
            // size we settle on is persisted (via onBlockCommitted) and fed back as
            // options.chunkSizeBytes on resume, so the block layout never shifts
            // between runs.
            const stagingBlockSize = Math.min(configuredChunkSize, PARALLEL_UPLOAD_BLOCK_BYTES);
            const chunkSize = file.size <= stagingBlockSize ? file.size : stagingBlockSize;
            const BlockBlobClient = await loadBlockBlobClient();
            const blockBlobClient = new BlockBlobClient(initResponse.blobUrl);

            // Enumerate every block up front. Block IDs are derived from the block
            // index, so blocks can be staged out of order and in parallel — the final
            // commitBlockList still lists them in index order.
            const allBlocks: Array<{ blockIndex: number; start: number; end: number }> = [];
            for (let start = 0; start < file.size; start += chunkSize) {
                const end = Math.min(start + chunkSize, file.size);
                allBlocks.push({ blockIndex: Math.floor(start / chunkSize), start, end });
            }

            // Index-aligned block-id map (dense, '' = not yet staged). Keeping it
            // aligned by block index — instead of compacting with filter(Boolean) —
            // is what lets out-of-order parallel staging persist a correct resume
            // checkpoint and commit the blocks in the right order.
            //
            // Only trust locally-cached block IDs from a prior run if this init
            // handed back the SAME blob they were staged against. A resumed
            // /upload/init can legitimately return a DIFFERENT blob than last
            // time -- e.g. reserve_pending_anonymous_blob's content-hash check
            // (see backend/storage_utils.py) decides the filename's pending
            // reservation actually belongs to a different upload and mints a
            // fresh one. Blindly replaying old block IDs against a blob they
            // were never staged on is exactly what produces Azure's
            // "InvalidBlockList" error at commit time -- and since it never
            // gets recorded here as a match, this run correctly restages
            // everything from scratch instead of repeating the failure.
            const priorBlockIds = (options?.existingBlobName && options.existingBlobName === blobName)
                ? (options?.existingBlockIds || [])
                : [];
            const blockIds: string[] = allBlocks.map(({ blockIndex }) => priorBlockIds[blockIndex] || '');

            // Resume: skip blocks already staged in a previous run and seed progress
            // with their bytes.
            let committedBytes = 0;
            for (const { blockIndex, start, end } of allBlocks) {
                if (blockIds[blockIndex]) {
                    committedBytes += end - start;
                }
            }
            if (committedBytes > 0) {
                options?.onChunkCommitted?.(committedBytes);
            }

            const pendingBlocks = allBlocks.filter(({ blockIndex }) => !blockIds[blockIndex]);
            const stageConcurrency = Math.max(1, Math.min(
                options?.blockConcurrency || TARGET_CONCURRENT_UPLOAD_BLOCKS,
                pendingBlocks.length,
            ));

            // Bounded worker pool: workers pull blocks off a shared cursor so at most
            // `stageConcurrency` stageBlock requests are in flight for this file.
            let blockCursor = 0;
            const stageWorker = async () => {
                while (true) {
                    if (uploadStopRequestedRef.current || options?.signal?.aborted) {
                        throw new Error(UPLOAD_STOPPED_ERROR);
                    }
                    const cursor = blockCursor;
                    blockCursor += 1;
                    if (cursor >= pendingBlocks.length) {
                        return;
                    }
                    const { blockIndex, start, end } = pendingBlocks[cursor];
                    const blockId = btoa(`block-${String(blockIndex).padStart(8, '0')}`);

                    let lastError: unknown = null;
                    for (let attempt = 0; attempt < 3; attempt += 1) {
                        try {
                            await blockBlobClient.stageBlock(blockId, file.slice(start, end), end - start, {
                                abortSignal: options?.signal,
                            });
                            blockIds[blockIndex] = blockId;
                            // committedBytes is a running total (not a contiguous
                            // offset), so it stays monotonic even as blocks finish out
                            // of order — safe for the progress + resume bookkeeping.
                            committedBytes += end - start;
                            options?.onChunkCommitted?.(committedBytes);
                            // Persist the index-aligned map (with '' holes) so a
                            // resume can tell exactly which blocks still need staging.
                            options?.onBlockCommitted?.([...blockIds], chunkSize);
                            lastError = null;
                            break;
                        } catch (err) {
                            if (isRetriableUploadError(err)) {
                                if (!navigator.onLine) {
                                    await waitForOnline();
                                }
                                await sleep(100 * Math.pow(2, attempt));
                            }
                            lastError = err;
                        }
                    }

                    if (lastError) {
                        throw lastError;
                    }
                }
            };

            await Promise.all(Array.from({ length: stageConcurrency }, () => stageWorker()));

            const finalBlockIds = blockIds.filter(Boolean);
            if (finalBlockIds.length === 0) {
                throw new Error('No upload blocks were staged.');
            }
            options?.onFinalizeStarted?.();
            await blockBlobClient.commitBlockList(finalBlockIds, {
                abortSignal: options?.signal,
                blobHTTPHeaders: {
                    blobContentType: file.type || 'application/octet-stream',
                },
            });
            // The bytes are now durably committed to blob storage -- everything
            // left (register/dedup/queue this upload via /upload/finalize) is
            // backend bookkeeping, not data transfer, and measured at 20-90s+
            // per file under load (see the upload-speed investigation this
            // session). Returning here instead of also awaiting finalize lets
            // the caller immediately reuse this lane for the next file's
            // blocks while finalize runs in the background (finalizeUploadedFile
            // in runUploadSession) -- staying in this function to await it was
            // leaving lanes idle ~80% of the time even though each chunk itself
            // transfers fast.
            return { skippedDuplicate: false, sha256, uploadId: initResponse.uploadId };
        } catch (err) {
            // This upload didn't finish, so nothing was actually stored under
            // this hash -- release the claim (only if it's still ours; a
            // concurrent lane can't have taken over the same hash, since the
            // synchronous check-then-claim above means at most one lane ever
            // holds a given hash's claim within a batch) so a later file with
            // the same content is retried instead of silently skipped.
            if (knownHashesRef.current.get(sha256) === file.name) {
                knownHashesRef.current.delete(sha256);
            }
            throw err;
        }
    }, []);

    const cleanupUnfinishedUploadArtifacts = useCallback(async (session: PersistedUploadSession) => {
        const unfinished = session.files
            .filter((file) => file.status !== 'done')
            .filter((file) => Boolean(file.uploadId))
            .map((file) => ({ filename: file.name, uploadId: file.uploadId || '' }));
        if (unfinished.length > 0) {
            try {
                await postUpload('/upload/cancel', { files: unfinished });
            } catch (err) {
                addNotification('Upload cleanup warning', getUploadErrorMessage(err, 'Some upload records could not be removed.'));
            }
        }
    }, [addNotification]);

    const runUploadSession = useCallback(async (
        session: PersistedUploadSession,
        options: { resumed?: boolean; notificationId?: string; uploadProfile?: UploadProfile; fileKeys?: Set<string> } = {},
    ) => {
        if (isResumingUploadRef.current) {
            return;
        }

        const filesToProcess = options.fileKeys
            ? session.files.filter((file) => file.status !== 'done' && options.fileKeys?.has(file.key))
            : session.files.filter((file) => file.status !== 'done');
        if (filesToProcess.length === 0) {
            return;
        }

        isResumingUploadRef.current = true;
        // Kick off in parallel with the setup below (notification creation,
        // session persistence, block-size planning) and only await it right
        // before the worker pool starts -- overlaps a network round trip with
        // work we'd be doing anyway instead of adding it to the critical path.
        const knownHashesPromise = fetchKnownHashes();
        setUploading(true);
        uploadingRef.current = true;
        // Hold the wake lock for the whole transfer, not just the post-upload
        // processing pass -- a large batch can take long enough for the laptop
        // to autolock mid-transfer otherwise.
        keepAliveRef.current?.start();
        setPendingUploadSession(null);
        const totalCount = session.files.length;
        let totalUploaded = session.files.filter((file) => file.status === 'done').length;
        let totalFailed = 0;
        let totalSkippedDuplicates = session.files.filter((file) => file.skippedDuplicate).length;
        const uploadProfile = options.uploadProfile || getAdaptiveUploadProfile(session.files);
        const uploadStartedAt = Date.now();
        let transferredBytesThisRun = 0;
        const lastCommittedBytesByFile = new Map<string, number>();

        // Sliding window, not a lifetime average -- a lifetime average (total
        // bytes / total elapsed since uploadStartedAt) means a slow patch
        // (e.g. the tab was backgrounded and the browser throttled it) drags
        // the reported rate down for the rest of the run, and it only
        // recovers asymptotically once conditions improve -- for a large
        // batch that reads as "speed never comes back" even though the real
        // transfer is back to full speed. Only the last RATE_WINDOW_MS of
        // progress is used, so the displayed number tracks current
        // conditions instead of the whole session's history.
        const RATE_WINDOW_MS = 10000;
        const rateSamples: Array<{ t: number; bytes: number }> = [{ t: uploadStartedAt, bytes: 0 }];
        const recordRateSample = () => {
            const now = Date.now();
            rateSamples.push({ t: now, bytes: transferredBytesThisRun });
            while (rateSamples.length > 1 && now - rateSamples[0].t > RATE_WINDOW_MS) {
                rateSamples.shift();
            }
        };
        const currentUploadRate = () => {
            const now = Date.now();
            const oldest = rateSamples[0];
            const elapsedSeconds = Math.max((now - oldest.t) / 1000, 0.001);
            return Math.max(0, transferredBytesThisRun - oldest.bytes) / elapsedSeconds / MB;
        };

        const notificationId = options.notificationId || addNotification(
            options.resumed ? 'Upload resumed' : 'Upload started',
            options.resumed ? `Processing ${plural(totalCount, 'persisted file')}.` : `Uploading ${plural(totalCount, 'file')} with ${plural(uploadProfile.fileParallelism, 'lane')}.`,
            {
                uploadedCount: totalUploaded,
                totalCount,
                failedCount: totalFailed,
                skippedDuplicateCount: totalSkippedDuplicates,
                mbPerSecond: 0,
            },
        );
        if (options.notificationId) {
            updateNotification(notificationId, {
                title: options.resumed ? 'Upload resumed' : 'Upload started',
                details: options.resumed ? `Processing ${plural(totalCount, 'persisted file')}.` : `Uploading ${plural(totalCount, 'file')} with ${plural(uploadProfile.fileParallelism, 'lane')}.`,
                progress: {
                    uploadedCount: totalUploaded,
                    totalCount,
                    failedCount: totalFailed,
                    skippedDuplicateCount: totalSkippedDuplicates,
                    mbPerSecond: 0,
                },
            });
        }

        const normalized: PersistedUploadSession = {
            ...session,
            files: session.files.map((file) => (
                file.status === 'done' || (options.fileKeys && !options.fileKeys.has(file.key))
                    ? file
                    : { ...file, status: 'pending', error: undefined }
            )),
        };
        persistSession(normalized);

        try {
            const pendingFiles = normalized.files.filter((file) => (
                file.status !== 'done' && (!options.fileKeys || options.fileKeys.has(file.key))
            ));

            // Split the connection budget across the active file lanes: with one
            // big file the lane gets the full budget (so its blocks stage in
            // parallel), while many files each get one connection.
            const activeLaneCount = Math.min(uploadProfile.fileParallelism, Math.max(1, pendingFiles.length));
            const perFileBlockConcurrency = Math.max(1, Math.round(TARGET_CONCURRENT_UPLOAD_BLOCKS / activeLaneCount));

            const parallelThumbnailTasks = new Set<Promise<void>>();

            const kickOffThumbnailForFile = (file: File, filename: string) => {
                if (getRuntimeConfig().processingMode === 'backend') {
                    // ipworker owns thumbnails entirely in this mode (queued
                    // automatically by the upload finalize call, same as its
                    // other steps) -- don't claim the lease client-side just to
                    // no-op, which would otherwise leave it held (and ipworker
                    // locked out) until it naturally expires.
                    return;
                }
                const task: Promise<void> = (async () => {
                    let claim: { claimed?: boolean; leaseId?: string; thumbnailUploadUrl?: string } | null = null;
                    try {
                        claim = await post('/upload/processing/claim', { filename, steps: ['thumbnail'] });
                    } catch {
                        return;
                    }
                    if (!claim?.claimed) return;
                    const processingStartedAt = performance.now();
                    // Same reasoning as the drain-loop path above: a successful
                    // /upload/client-processing call already releases the lease
                    // server-side, so the explicit /release in `finally` below is
                    // only needed when that report never went out.
                    let leaseReleasedByReport = false;
                    try {
                        const convertedPreview = await fetchConvertedRawPreview(filename);
                        const result = await runBrowserProcessing(
                            file,
                            `browser-${filename}`,
                            processingStartedAt,
                            browserAiModelStateRef.current as Parameters<PhotoGalleryRuntime['runBrowserProcessing']>[3],
                            { clientProcessing: {}, clientProcessingReport: [] },
                            { thumbnailRotationDegrees: 0, faceRotationDegrees: 0, convertedPreview },
                        );
                        const filteredResult = filterBrowserProcessingResult(result, new Set(['thumbnail']));
                        let thumbnailAlreadyUploaded = false;
                        if (filteredResult?.clientProcessing?.thumbnail) {
                            try {
                                thumbnailAlreadyUploaded = await uploadBrowserThumbnailDirectly(
                                    filename,
                                    filteredResult,
                                    String(claim?.thumbnailUploadUrl || ''),
                                );
                            } catch (thumbnailErr) {
                                console.warn(`Direct thumbnail upload failed for ${filename}.`, thumbnailErr);
                                delete filteredResult.clientProcessing.thumbnail;
                                // Without this, clientProcessingReport still carries the
                                // canvas-render step's earlier 'done' entry (that step did
                                // succeed -- only the network upload failed), so the backend
                                // would trust a stale "done" and never learn the blob never
                                // landed. See the sibling report-correction above.
                                filteredResult.clientProcessingReport = [
                                    ...(Array.isArray(filteredResult.clientProcessingReport) ? filteredResult.clientProcessingReport : []),
                                    {
                                        clientAssetId: `browser-${filename}`,
                                        step: 'thumbnail',
                                        status: 'failed',
                                        reason: 'direct_thumbnail_upload_failed',
                                        durationMs: Math.max(0, Math.round(performance.now() - processingStartedAt)),
                                        runtime: 'browser-processing-scheduler',
                                    },
                                ];
                            }
                        }
                        await postUpload('/upload/client-processing', {
                            filename,
                            clientProcessingSchemaVersion: CLIENT_PROCESSING_SCHEMA_VERSION,
                            clientProcessing: filteredResult.clientProcessing,
                            clientProcessingReport: filteredResult.clientProcessingReport,
                            clientAssetId: `browser-${filename}`,
                            uploadId: `browser-${filename}`,
                            thumbnailAlreadyUploaded,
                        });
                        leaseReleasedByReport = true;
                    } catch (err) {
                        console.warn(`Parallel thumbnail generation failed for ${filename}.`, err);
                    } finally {
                        if (!leaseReleasedByReport) {
                            await post('/upload/processing/release', { filename, leaseId: claim?.leaseId || '' }).catch(() => undefined);
                        }
                    }
                })();
                parallelThumbnailTasks.add(task);
                void task.finally(() => parallelThumbnailTasks.delete(task));
            };

            let failedPreviewCount = 0;

            // Shared by the worker's own catch (staging/commit failures) and
            // finalizeUploadedFile's catch (finalize failures) below -- both
            // need to report a file as failed the same way, just from
            // different points in the (now-decoupled) per-file pipeline.
            const markFileFailed = async (fileMeta: PersistedUploadFile, resolvedFile: File | null, err: unknown) => {
                totalFailed += 1;
                let previewDataUrl: string | undefined;
                if (resolvedFile && failedPreviewCount < MAX_FAILED_FILE_PREVIEWS) {
                    failedPreviewCount += 1;
                    try {
                        const thumb = await createBrowserThumbnail(resolvedFile);
                        if (thumb?.dataUrl) {
                            previewDataUrl = thumb.dataUrl;
                        }
                    } catch {
                        // Best-effort only (e.g. RAW formats have no client-side
                        // decoder) -- the failed file still shows by name.
                    }
                }
                updatePersistedFile(fileMeta.key, {
                    status: 'failed',
                    error: uploadSourceFilesRef.current.has(fileMeta.key)
                        ? `${getUploadErrorMessage(err, 'Upload failed for this file.')} Please reselect this file to retry.`
                        : getUploadErrorMessage(err, 'Upload failed for this file.'),
                    previewDataUrl,
                });
                updateNotification(notificationId, {
                    title: 'Upload in progress',
                    details: `Uploaded ${totalUploaded}/${plural(totalCount, 'file')}. Skipped duplicates: ${totalSkippedDuplicates}.`,
                    progress: {
                        uploadedCount: totalUploaded,
                        totalCount,
                        failedCount: totalFailed,
                        skippedDuplicateCount: totalSkippedDuplicates,
                        mbPerSecond: currentUploadRate(),
                    },
                });
            };

            // Runs in the background, NOT awaited by the worker loop below --
            // see the comment at uploadFileInChunks's return for why. Tracked in
            // pendingFinalizeTasks so runUploadSession can wait for all of them
            // to settle before deciding the session is actually complete (a file
            // isn't really done just because its lane moved on).
            const pendingFinalizeTasks = new Set<Promise<void>>();
            const finalizeUploadedFile = async (
                file: File,
                fileMeta: PersistedUploadFile,
                controller: AbortController,
                staged: { sha256: string; uploadId: string },
            ): Promise<void> => {
                try {
                    const completeResponse = await postFinalizeWithRetry({
                        filename: file.name,
                        uploadId: staged.uploadId,
                        totalSize: file.size,
                        contentType: file.type || 'application/octet-stream',
                        sha256: staged.sha256,
                    }, controller.signal);
                    const skippedDuplicate = Array.isArray(completeResponse?.duplicates) && completeResponse.duplicates.length > 0;

                    totalUploaded += 1;
                    if (skippedDuplicate) {
                        totalSkippedDuplicates += 1;
                    }
                    updatePersistedFile(fileMeta.key, {
                        status: 'done',
                        uploadedBytes: fileMeta.size,
                        skippedDuplicate,
                        error: undefined,
                    });
                    if (!skippedDuplicate) {
                        kickOffThumbnailForFile(file, fileMeta.name);
                    }
                    uploadSourceFilesRef.current.delete(fileMeta.key);
                    await idbDelete(fileMeta.key);
                    updateNotification(notificationId, {
                        title: 'Upload in progress',
                        details: `Uploaded ${totalUploaded}/${plural(totalCount, 'file')}. Skipped duplicates: ${totalSkippedDuplicates}.`,
                        progress: {
                            uploadedCount: totalUploaded,
                            totalCount,
                            failedCount: totalFailed,
                            skippedDuplicateCount: totalSkippedDuplicates,
                            mbPerSecond: currentUploadRate(),
                        },
                    });
                } catch (err) {
                    // This upload didn't finish, so nothing was actually stored
                    // under this hash -- release the claim so a later file with
                    // the same content is retried instead of silently skipped
                    // (mirrors uploadFileInChunks's own rollback for a
                    // staging-phase failure).
                    if (knownHashesRef.current.get(staged.sha256) === file.name) {
                        knownHashesRef.current.delete(staged.sha256);
                    }
                    if (uploadStopRequestedRef.current || isUploadStoppedError(err)) {
                        // A deliberate Stop, not a real failure -- leave the file
                        // at whatever status it already had ('finalizing', set
                        // by onFinalizeStarted below) so a future resume retries
                        // it naturally (its already-committed blocks are still
                        // persisted via onBlockCommitted, so resume won't
                        // re-stage them, just re-attempt finalize).
                        return;
                    }
                    await markFileFailed(fileMeta, file, err);
                } finally {
                    uploadAbortControllersRef.current.delete(controller);
                }
            };

            let nextFileIndex = 0;
            const worker = async () => {
                while (true) {
                    if (uploadStopRequestedRef.current) {
                        return;
                    }
                    const currentIndex = nextFileIndex;
                    nextFileIndex += 1;
                    if (currentIndex >= pendingFiles.length) {
                        return;
                    }

                    const fileMeta = pendingFiles[currentIndex];
                    updatePersistedFile(fileMeta.key, { status: 'uploading', error: undefined });
                    let resolvedFileForFailurePreview: File | null = null;

                    try {
                        const sourceFile = uploadSourceFilesRef.current.get(fileMeta.key);
                        let file: File | null = sourceFile || null;
                        if (!file) {
                            // Prefer the in-memory handle over a round trip
                            // through IndexedDB when this tab still has it
                            // (e.g. resuming right after a Stop, not a
                            // reload) -- only one of a handle or a cached
                            // blob will exist per key, so a single idbGet
                            // covers the reload case either way.
                            const handleFromMemory = uploadHandlesRef.current.get(fileMeta.key);
                            if (handleFromMemory) {
                                file = await resolveFileFromHandle(handleFromMemory);
                            } else {
                                const cached = await idbGet(fileMeta.key);
                                if (cached && isFileSystemFileHandle(cached)) {
                                    file = await resolveFileFromHandle(cached);
                                } else if (cached) {
                                    file = new File([cached], fileMeta.name, { type: fileMeta.type, lastModified: fileMeta.lastModified });
                                }
                            }
                        }
                        if (!file) {
                            throw new Error(`Missing cached file for ${fileMeta.name}. Please reselect this file.`);
                        }
                        // Reassigned to a non-null const: `file` is a `let`
                        // (needed above to accumulate it from several
                        // possible sources), so TS can't carry the null
                        // check's narrowing into the onFinalizeStarted
                        // closure below.
                        const resolvedFile = file;
                        resolvedFileForFailurePreview = resolvedFile;
                        const controller = new AbortController();
                        uploadAbortControllersRef.current.add(controller);
                        const chunkSizeBytes = fileMeta.chunkSizeBytes || uploadProfile.chunkSizeBytes;
                        let staged: Awaited<ReturnType<typeof uploadFileInChunks>>;
                        try {
                            staged = await uploadFileInChunks(resolvedFile, {
                                startByte: fileMeta.uploadedBytes,
                                existingUploadId: fileMeta.uploadId,
                                existingBlockIds: fileMeta.blockIds,
                                existingBlobName: fileMeta.blobName,
                                onUploadInitialized: (uploadId, blobName) => {
                                    updatePersistedFile(fileMeta.key, { uploadId, blobName });
                                },
                                onChunkCommitted: (bytesReceived) => {
                                    const previousBytes = lastCommittedBytesByFile.get(fileMeta.key) || 0;
                                    transferredBytesThisRun += Math.max(0, bytesReceived - previousBytes);
                                    lastCommittedBytesByFile.set(fileMeta.key, bytesReceived);
                                    recordRateSample();
                                    updatePersistedFile(fileMeta.key, { uploadedBytes: bytesReceived });
                                },
                                onBlockCommitted: (blockIds, nextChunkSizeBytes) => {
                                    updatePersistedFile(fileMeta.key, { blockIds, chunkSizeBytes: nextChunkSizeBytes });
                                },
                                onFinalizeStarted: () => {
                                    updatePersistedFile(fileMeta.key, { status: 'finalizing', uploadedBytes: resolvedFile.size });
                                },
                                signal: controller.signal,
                                chunkSizeBytes,
                                blockConcurrency: perFileBlockConcurrency,
                            });
                        } catch (err) {
                            // Ownership of removing this controller from
                            // uploadAbortControllersRef passes to whichever path
                            // runs next (the inline duplicate branch, or
                            // finalizeUploadedFile's own finally) -- but neither
                            // of those runs if staging/commit itself throws, so
                            // this is the one path that must clean it up itself.
                            uploadAbortControllersRef.current.delete(controller);
                            throw err;
                        }

                        if (staged.skippedDuplicate) {
                            // No network work left for this file at all (the
                            // client-side known-hash check short-circuited
                            // before init/stage/finalize) -- resolve it inline,
                            // nothing to background.
                            uploadAbortControllersRef.current.delete(controller);
                            totalUploaded += 1;
                            totalSkippedDuplicates += 1;
                            updatePersistedFile(fileMeta.key, {
                                status: 'done',
                                uploadedBytes: fileMeta.size,
                                skippedDuplicate: true,
                                error: undefined,
                            });
                            uploadSourceFilesRef.current.delete(fileMeta.key);
                            await idbDelete(fileMeta.key);
                            updateNotification(notificationId, {
                                title: 'Upload in progress',
                                details: `Uploaded ${totalUploaded}/${plural(totalCount, 'file')}. Skipped duplicates: ${totalSkippedDuplicates}.`,
                                progress: {
                                    uploadedCount: totalUploaded,
                                    totalCount,
                                    failedCount: totalFailed,
                                    skippedDuplicateCount: totalSkippedDuplicates,
                                    mbPerSecond: currentUploadRate(),
                                },
                            });
                        } else {
                            // Bytes are committed to blob storage -- hand the
                            // slow part (register/dedup/queue via
                            // /upload/finalize) to the background and grab the
                            // next file immediately instead of idling this
                            // lane's connection budget for 20-90s+.
                            const finalizeTask = finalizeUploadedFile(resolvedFile, fileMeta, controller, staged)
                                .finally(() => { pendingFinalizeTasks.delete(finalizeTask); });
                            pendingFinalizeTasks.add(finalizeTask);
                        }
                    } catch (err) {
                        if (uploadStopRequestedRef.current || isUploadStoppedError(err)) {
                            throw new Error(UPLOAD_STOPPED_ERROR);
                        }
                        await markFileFailed(fileMeta, resolvedFileForFailurePreview, err);
                    }
                }
            };

            await knownHashesPromise;
            await Promise.all(Array.from({ length: activeLaneCount }, () => worker()));
            // Lanes only stage+commit bytes now; the backend confirmation for
            // each file is still finishing in the background (see
            // finalizeUploadedFile above) -- wait for all of it before treating
            // any completion/retry/failure count below as final.
            await Promise.all(Array.from(pendingFinalizeTasks));

            // Automatically retry files that just failed, still inside this same
            // run -- their bytes are still in uploadSourceFilesRef/uploadHandlesRef
            // right now (only cleared in the `finally` below), which is exactly
            // what the *manual* Retry banner usually can't rely on, since by the
            // time a user clicks it this cleanup has long since run. This turns
            // most transient failures (a flaky connection, a brief 503) into a
            // non-event instead of a stuck paused session.
            let autoRetryRound = 0;
            while (autoRetryRound < AUTO_RETRY_MAX_ROUNDS && !uploadStopRequestedRef.current) {
                const latestForRetry = uploadSessionRef.current || normalized;
                const retryCandidates = latestForRetry.files.filter((retryFile) => (
                    retryFile.status === 'failed'
                    && (!options.fileKeys || options.fileKeys.has(retryFile.key))
                    && (uploadSourceFilesRef.current.has(retryFile.key) || uploadHandlesRef.current.has(retryFile.key))
                ));
                if (retryCandidates.length === 0) {
                    break;
                }
                autoRetryRound += 1;
                totalFailed -= retryCandidates.length;
                updateNotification(notificationId, {
                    title: 'Retrying failed files',
                    details: `Automatically retrying ${plural(retryCandidates.length, 'file')} (attempt ${autoRetryRound}/${AUTO_RETRY_MAX_ROUNDS}).`,
                    progress: {
                        uploadedCount: totalUploaded,
                        totalCount,
                        failedCount: totalFailed,
                        skippedDuplicateCount: totalSkippedDuplicates,
                        mbPerSecond: currentUploadRate(),
                    },
                });
                await sleep(AUTO_RETRY_DELAY_MS);
                pendingFiles.length = 0;
                pendingFiles.push(...retryCandidates);
                nextFileIndex = 0;
                const retryLaneCount = Math.max(1, Math.min(activeLaneCount, retryCandidates.length));
                await Promise.all(Array.from({ length: retryLaneCount }, () => worker()));
                await Promise.all(Array.from(pendingFinalizeTasks));
            }

            // Deliberately NOT awaited: parallelThumbnailTasks (the browser-side
            // thumbnail claim/generate/report dance kicked off per-file above)
            // used to gate "Upload complete" and clearing the persisted session
            // on every file's client-processing report finishing -- observed at
            // 33-37s each in production, so a batch of even a handful of files
            // could leave the UI showing "uploading" long after every byte had
            // actually transferred and been finalized. Each task already cleans
            // up after itself (self-removes from the Set, releases its own
            // processing lease in a finally) and reports failures gracefully, so
            // there's nothing here that needs harvesting -- they keep running in
            // the background exactly as before, just without blocking the
            // upload-complete UI state on their completion. If a claimed lease is
            // still in flight when the tab closes, it expires naturally and
            // ipworker's already-queued fallback (see _queue_upload_processing)
            // picks up that file's thumbnail instead -- the same fallback path
            // that already handles any client-side thumbnail failure.

            if (uploadStopRequestedRef.current) {
                const latest = uploadSessionRef.current || normalized;
                await cleanupUnfinishedUploadArtifacts(latest);
                await clearPersistedSession();
                setPendingUploadSession(null);
                updateNotification(notificationId, {
                    title: 'Upload stopped',
                    details: `Stopped after uploading ${totalUploaded}/${plural(totalCount, 'file')}.`,
                    progress: {
                        uploadedCount: totalUploaded,
                        totalCount,
                        failedCount: totalFailed,
                        skippedDuplicateCount: totalSkippedDuplicates,
                        mbPerSecond: currentUploadRate(),
                    },
                });
                setUploadError(null);
                return;
            }

            const latest = uploadSessionRef.current;
            const hasPending = (latest?.files || []).some((file) => file.status !== 'done');
            if (!hasPending) {
                await clearPersistedSession();
                setPendingUploadSession(null);
            }

            if (totalFailed === 0 && !hasPending) {
                updateNotification(notificationId, {
                    title: 'Upload complete',
                    details: `Uploaded ${plural(totalUploaded, 'file')} successfully. Skipped duplicates: ${totalSkippedDuplicates}.`,
                });
                setUploadError(null);
            } else {
                if (latest) {
                    setPendingUploadSession(latest);
                }
                updateNotification(notificationId, {
                    title: 'Upload paused',
                    details: `Uploaded ${totalUploaded}/${plural(totalCount, 'file')}, failed ${totalFailed}, skipped duplicates ${totalSkippedDuplicates}. Reselect any missing files to retry.`,
                });
                setUploadError(totalFailed > 0
                    ? 'Some files failed to upload. Retry cached files or reselect missing files.'
                    : 'Some files still need to be reselected before retry.'
                );
            }

            await notifyUploadComplete();
        } catch (err) {
            if (uploadStopRequestedRef.current || isUploadStoppedError(err)) {
                const latest = uploadSessionRef.current || session;
                await cleanupUnfinishedUploadArtifacts(latest);
                await clearPersistedSession();
                setPendingUploadSession(null);
                updateNotification(notificationId, {
                    title: 'Upload stopped',
                    details: `Stopped after uploading ${totalUploaded}/${plural(totalCount, 'file')}.`,
                    progress: {
                        uploadedCount: totalUploaded,
                        totalCount,
                        failedCount: totalFailed,
                        skippedDuplicateCount: totalSkippedDuplicates,
                        mbPerSecond: currentUploadRate(),
                    },
                });
                setUploadError(null);
                return;
            }
            const message = getUploadErrorMessage(err, 'Upload failed unexpectedly.');
            updateNotification(notificationId, {
                title: 'Upload failed',
                details: message,
            });
            const latest = uploadSessionRef.current;
            if (latest && latest.files.some((file) => file.status !== 'done')) {
                setPendingUploadSession(latest);
            }
            setUploadError(message);
        } finally {
            setUploading(false);
            uploadingRef.current = false;
            isResumingUploadRef.current = false;
            uploadSourceFilesRef.current.clear();
            uploadHandlesRef.current.clear();
            uploadAbortControllersRef.current.clear();
            uploadStopRequestedRef.current = false;
            // Recompute keep-alive state now that the transfer has ended (on
            // every exit path -- success, stopped, or errored): resumes the
            // background processing pull, and releases the wake lock if there
            // is nothing left to protect.
            void startBrowserProcessing();
        }
    }, [
        addNotification,
        cleanupUnfinishedUploadArtifacts,
        clearPersistedSession,
        notifyUploadComplete,
        persistSession,
        setUploadError,
        updateNotification,
        updatePersistedFile,
        uploadFileInChunks,
    ]);

    const fileMatchesPersistedUpload = (file: File, meta: PersistedUploadFile) => (
        file.name === meta.name
        && file.size === meta.size
        && file.lastModified === meta.lastModified
    );

    const attachSelectedFilesToPendingSession = useCallback(async (
        selectedFiles: File[],
    ): Promise<{ matchedCount: number; unmatchedFiles: File[] }> => {
        const session = pendingUploadSession || loadPersistedSession();
        if (!session) {
            return { matchedCount: 0, unmatchedFiles: selectedFiles };
        }

        const matchedKeys = new Set<string>();
        const matchedPairs: Array<{ key: string; file: File }> = [];
        const unfinishedFiles = session.files.filter((file) => file.status !== 'done');
        const doneFiles = session.files.filter((file) => file.status === 'done');
        const unmatchedFiles: File[] = [];

        // Fast, synchronous matching pass only -- no IndexedDB writes here.
        // uploadSourceFilesRef is set immediately per match, which is all
        // retryPersistedUploadSession actually needs to proceed; the blob
        // persistence below is a slower best-effort extra, done afterward.
        for (const file of selectedFiles) {
            const meta = unfinishedFiles.find((candidate) => (
                !matchedKeys.has(candidate.key) && fileMatchesPersistedUpload(file, candidate)
            ));
            if (meta) {
                matchedKeys.add(meta.key);
                uploadSourceFilesRef.current.set(meta.key, file);
                matchedPairs.push({ key: meta.key, file });
                updatePersistedFile(meta.key, { status: 'pending', error: undefined });
                continue;
            }
            // Already uploaded under this session -- nothing to do (and not a
            // "new" file either, so it shouldn't be handed to startUpload).
            if (doneFiles.some((candidate) => fileMatchesPersistedUpload(file, candidate))) {
                continue;
            }
            // Not part of this paused session at all -- e.g. the user
            // reselected a broader folder than their original pick. The
            // caller starts these as their own fresh batch instead of
            // silently dropping them (see handleUploadSelection).
            unmatchedFiles.push(file);
        }

        // Writing every matched file's full bytes into IndexedDB -- same
        // hazard cacheUploadFilesForResume guards against, and this is
        // exactly the path a retry of a batch that failed for that reason
        // hits (reselecting the SAME large batch always matches here, not
        // startUpload). Previously this ran sequentially and unthrottled,
        // both slow (one open/write/close IndexedDB cycle at a time for
        // potentially 1000+ files -- looks hung) and uncapped in total bytes
        // (the actual crash risk). Throttled + budget-capped the same way,
        // and NOT awaited -- uploadSourceFilesRef above already lets the
        // upload proceed without waiting for this.
        void (async () => {
            const budgetBytes = isConstrainedUploadDevice() ? CONSTRAINED_DEVICE_CACHE_BUDGET_BYTES : Infinity;
            let cachedBytes = 0;
            let cursor = 0;
            const worker = async () => {
                while (true) {
                    const index = cursor;
                    cursor += 1;
                    if (index >= matchedPairs.length) {
                        return;
                    }
                    if (cachedBytes >= budgetBytes) {
                        continue;
                    }
                    const { key, file } = matchedPairs[index];
                    try {
                        await idbPut(key, file);
                        cachedBytes += file.size;
                    } catch {
                        // The in-memory File reference still allows retry in this tab.
                    }
                }
            };
            await Promise.all(Array.from(
                { length: Math.min(IDB_RESUME_CACHE_CONCURRENCY, matchedPairs.length) },
                () => worker(),
            ));
        })();

        const matchedCount = matchedPairs.length;
        const latest = uploadSessionRef.current || session;
        persistSession(latest);
        setPendingUploadSession(latest);
        return { matchedCount, unmatchedFiles };
    }, [loadPersistedSession, pendingUploadSession, persistSession, updatePersistedFile]);

    const retryPersistedUploadSession = useCallback(async () => {
        const session = pendingUploadSession || loadPersistedSession();
        if (!session || uploading) {
            return;
        }
        const unfinishedFiles = session.files.filter((file) => file.status !== 'done');
        // Throttled the same way as cacheUploadFilesForResume: an unfinished
        // batch of 1000+ files, most missing from uploadSourceFilesRef after a
        // reload, used to fire that many simultaneous idbGet calls via a bare
        // Promise.all -- the same IndexedDB-connection burst iOS Safari can't
        // handle, just on the read side this time.
        const cacheChecks: Array<{ file: PersistedUploadFile; hasCachedBlob: boolean }> = new Array(unfinishedFiles.length);
        let cacheCheckCursor = 0;
        const cacheCheckWorker = async () => {
            while (true) {
                const index = cacheCheckCursor;
                cacheCheckCursor += 1;
                if (index >= unfinishedFiles.length) {
                    return;
                }
                const file = unfinishedFiles[index];
                const hasCachedBlob = uploadSourceFilesRef.current.has(file.key)
                    || uploadHandlesRef.current.has(file.key)
                    || Boolean(await idbGet(file.key).catch(() => null));
                cacheChecks[index] = { file, hasCachedBlob };
            }
        };
        await Promise.all(Array.from(
            { length: Math.min(IDB_RESUME_CACHE_CONCURRENCY, unfinishedFiles.length) },
            () => cacheCheckWorker(),
        ));
        const missingCachedFiles = cacheChecks.filter((item) => !item.hasCachedBlob).map((item) => item.file);
        const retryableFiles = cacheChecks.filter((item) => item.hasCachedBlob).map((item) => item.file);
        if (retryableFiles.length === 0) {
            addNotification('Reselect files', `${plural(missingCachedFiles.length, 'file')} need to be selected again before retry.`);
            setUploadError('Upload files are no longer cached. Use Upload to reselect those files, then retry.');
            return;
        }
        if (missingCachedFiles.length > 0) {
            addNotification('Partial retry', `${plural(retryableFiles.length, 'cached file')} will retry. Still to reselect: ${plural(missingCachedFiles.length, 'file')}.`);
        }
        uploadStopRequestedRef.current = false;
        const retryableKeys = new Set(retryableFiles.map((file) => file.key));
        await runUploadSession(session, { resumed: true, fileKeys: retryableKeys });
    }, [addNotification, loadPersistedSession, pendingUploadSession, runUploadSession, setUploadError, uploading]);

    const discardPersistedUploadSession = useCallback(async () => {
        if (uploading) {
            return;
        }
        const session = pendingUploadSession || loadPersistedSession();
        if (session) {
            await cleanupUnfinishedUploadArtifacts(session);
        }
        await clearPersistedSession();
        setPendingUploadSession(null);
        setUploadError(null);
        addNotification('Upload discarded', 'Paused upload files were removed.');
    }, [addNotification, cleanupUnfinishedUploadArtifacts, clearPersistedSession, loadPersistedSession, pendingUploadSession, setUploadError, uploading]);

    const stopActiveUpload = useCallback(() => {
        if (!uploading && !uploadStartInProgressRef.current) {
            return;
        }
        uploadStopRequestedRef.current = true;
        uploadStartInProgressRef.current = false;
        uploadAbortControllersRef.current.forEach((controller) => controller.abort());
        const queuedCount = queuedUploadBatchesRef.current.reduce((sum, batch) => sum + batch.length, 0);
        queuedUploadBatchesRef.current = [];
        setQueuedUploadFileCount(0);
        addNotification(
            'Stopping upload',
            queuedCount > 0
                ? `Cancelling active requests and cleaning up unfinished files. ${plural(queuedCount, 'queued file')} also cleared.`
                : 'Cancelling active requests and cleaning up unfinished files.',
        );
    }, [addNotification, uploading]);

    // Best-effort resume cache: writes each file's bytes into IndexedDB so an
    // interrupted upload can resume after a page reload (uploadSourceFilesRef
    // already covers the CURRENT tab session regardless -- this is only for
    // surviving a reload/crash). Throttled to a small worker pool instead of
    // firing every idbPut concurrently: a 1000-file batch used to open ~1000
    // simultaneous IndexedDB connections at once, exactly the kind of burst
    // that trips iOS Safari's much tighter per-tab memory/storage ceilings.
    //
    // That throttle alone wasn't enough for a large mobile batch -- see
    // CONSTRAINED_DEVICE_CACHE_BUDGET_BYTES's own comment above.
    const cacheUploadFilesForResume = async (
        session: PersistedUploadSession,
        filesToUpload: File[],
        handlesToUpload: Map<string, FileSystemFileHandle>,
    ) => {
        const budgetBytes = isConstrainedUploadDevice() ? CONSTRAINED_DEVICE_CACHE_BUDGET_BYTES : Infinity;
        let cachedBytes = 0;
        let cursor = 0;
        const worker = async () => {
            while (true) {
                if (uploadStopRequestedRef.current) {
                    return;
                }
                const index = cursor;
                cursor += 1;
                if (index >= session.files.length) {
                    return;
                }
                const key = session.files[index].key;
                // A FileSystemFileHandle (Chrome/Edge desktop) is a few bytes,
                // not the file's content -- persist it unconditionally,
                // bypassing the blob budget entirely, since it can re-derive
                // a live File later instead of needing its bytes duplicated
                // now.
                const handle = handlesToUpload.get(key);
                if (handle) {
                    try {
                        await idbPut(key, handle);
                    } catch {
                        // Falls back to the in-memory uploadHandlesRef entry
                        // for this tab's own session -- not fatal.
                    }
                    continue;
                }
                if (cachedBytes >= budgetBytes) {
                    // Past the budget: in-memory uploadSourceFilesRef still
                    // covers this tab's own upload, this file just won't
                    // survive a reload/crash for resume purposes.
                    continue;
                }
                const file = filesToUpload[index];
                try {
                    await idbPut(key, file);
                    cachedBytes += file.size;
                } catch {
                    // Resume-after-reload won't find this file, but the
                    // in-memory File reference still covers this tab's
                    // current session -- not fatal, just less resilient.
                }
            }
        };
        await Promise.all(Array.from(
            { length: Math.min(IDB_RESUME_CACHE_CONCURRENCY, session.files.length) },
            () => worker(),
        ));
    };

    const startUpload = useCallback(async (filesToUpload: File[], handlesToUpload?: Map<File, FileSystemFileHandle>) => {
        if (filesToUpload.length === 0) {
            return;
        }
        if (uploadStartInProgressRef.current || pendingUploadSessionRef.current) {
            const message = pendingUploadSessionRef.current
                ? 'A paused upload is waiting -- use Retry or Discard on the banner before starting a new one.'
                : 'The selected files are already being prepared or uploaded.';
            addNotification('Upload already starting', message);
            // A bell-only notification is easy to miss here in particular: a
            // stuck pendingUploadSession silently blocks every subsequent
            // Upload click with no other feedback -- this is the exact "I
            // picked files and nothing happened" dead end reported live.
            showToast(message, { variant: 'error' });
            return;
        }
        if (uploading) {
            queuedUploadBatchesRef.current.push(filesToUpload);
            setQueuedUploadFileCount((count) => count + filesToUpload.length);
            addNotification(
                'Upload queued',
                `${plural(filesToUpload.length, 'file')} queued from another folder. They'll start automatically once the current upload finishes.`,
            );
            return;
        }

        uploadStartInProgressRef.current = true;
        uploadStopRequestedRef.current = false;
        setUploading(true);
        uploadingRef.current = true;
        setUploadError(null);
        const warmupNotificationId = addNotification(
            'Warming up upload path',
            'Waiting for backend storage and upload readiness before upload starts.',
        );
        // Piggyback a real-latency reading on this warm-up call rather than
        // firing a dedicated probe request: only trust the timing when we
        // know it's actually making a fresh network call (not a dedup
        // short-circuit off some other concurrent warm-up) -- otherwise a
        // near-instant "0ms" short-circuit would look like great latency.
        const measuringRtt = !backendWarmupInFlightRef.current;
        const warmupStartedAt = performance.now();
        try {
            await Promise.all([
                warmEndpoint(warmBackend, backendWarmupInFlightRef),
            ]);
        } catch (err) {
            const message = getUploadErrorMessage(err, 'Warm-up failed before upload could start.');
            setUploadError(message);
            setUploading(false);
            uploadingRef.current = false;
            // Without this, a single warm-up failure (cold-start timeout, a
            // transient network blip, backend capacity pressure) wedges this
            // flag true forever -- every future requestUpload()/startUpload()
            // call bails at its own early-return guard for the rest of the
            // tab session, with no visible way out: `uploading` is already
            // false here, so the Stop button (the only other place this gets
            // reset) isn't even rendered to click.
            uploadStartInProgressRef.current = false;
            updateNotification(warmupNotificationId, {
                title: 'Upload warm-up failed',
                details: message,
            });
            return;
        }
        const measuredRttMs = measuringRtt ? performance.now() - warmupStartedAt : undefined;
        updateNotification(warmupNotificationId, {
            title: 'Upload path ready',
            details: 'Backend storage is ready. Starting upload.',
        });
        const totalBytes = filesToUpload.reduce((sum, file) => sum + file.size, 0);
        const uploadProfile = getAdaptiveUploadProfile(filesToUpload, { measuredRttMs });
        const preparingNotificationId = addNotification(
            'Preparing upload',
            `Preparing ${plural(filesToUpload.length, 'file')}, ${formatBytes(totalBytes)}. ${plural(uploadProfile.fileParallelism, 'upload lane')}, ${formatBytes(uploadProfile.chunkSizeBytes)} chunks: ${uploadProfile.reason}.`,
            {
                uploadedCount: 0,
                totalCount: filesToUpload.length,
                failedCount: 0,
                skippedDuplicateCount: 0,
            },
        );

        const sessionId = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
        const session: PersistedUploadSession = {
            id: sessionId,
            createdAt: Date.now(),
            files: filesToUpload.map((file, index) => ({
                key: `${sessionId}:${index}:${file.name}:${file.size}:${file.lastModified}`,
                name: file.name,
                size: file.size,
                type: file.type,
                lastModified: file.lastModified,
                uploadedBytes: 0,
                skippedDuplicate: false,
                status: 'pending',
            })),
        };
        const keyedHandles = new Map<string, FileSystemFileHandle>();
        if (handlesToUpload) {
            filesToUpload.forEach((file, index) => {
                const handle = handlesToUpload.get(file);
                if (handle) {
                    keyedHandles.set(session.files[index].key, handle);
                }
            });
        }

        try {
            persistSession(session);
            uploadSourceFilesRef.current = new Map(session.files.map((meta, index) => [meta.key, filesToUpload[index]]));
            uploadHandlesRef.current = keyedHandles;
            // Not awaited: the actual upload only needs uploadSourceFilesRef
            // (already set above), so there's no reason to block starting on
            // this -- resume-after-reload is a nice-to-have, not a
            // prerequisite for this tab's own upload to proceed.
            void cacheUploadFilesForResume(session, filesToUpload, keyedHandles);
            if (uploadStopRequestedRef.current) {
                await cleanupUnfinishedUploadArtifacts(session);
                await clearPersistedSession();
                updateNotification(preparingNotificationId, {
                    title: 'Upload stopped',
                    details: 'Upload was stopped before file transfer started.',
                });
                setUploading(false);
                uploadingRef.current = false;
                return;
            }
            // From here on, `uploading` (already true) plus runUploadSession's
            // own isResumingUploadRef guard cover re-entrancy for the rest of
            // the transfer -- clear the "starting" flag now instead of waiting
            // for this whole (possibly long) session to finish, so requestUpload
            // can let the picker open again to queue files from another folder.
            uploadStartInProgressRef.current = false;
            await runUploadSession(session, { notificationId: preparingNotificationId, uploadProfile });
        } catch (err) {
            const message = getUploadErrorMessage(err, 'Upload could not start.');
            setUploadError(message);
            setUploading(false);
            uploadingRef.current = false;
            updateNotification(preparingNotificationId, {
                title: 'Upload could not start',
                details: message,
            });
        } finally {
            uploadStartInProgressRef.current = false;
        }
    }, [
        addNotification,
        cleanupUnfinishedUploadArtifacts,
        clearPersistedSession,
        persistSession,
        warmBackend,
        warmEndpoint,
        runUploadSession,
        setUploadError,
        updateNotification,
        uploading,
    ]);

    const requestUpload = useCallback(() => {
        if (uploadStartInProgressRef.current) {
            const message = 'Wait for the current upload preparation to finish before selecting more files.';
            addNotification('Upload already starting', message);
            showToast(message, { variant: 'error' });
            return;
        }
        // While actively uploading, skip the warm-up poll (the backend is
        // already warm) but still let the picker open so files from another
        // folder can be queued -- see startUpload's `uploading` branch.
        if (!uploading) {
            void pollUntilWarm(warmUpload, uploadWarmupInFlightRef);
        }
        // Chrome/Edge desktop only (unsupported in Safari/Firefox and on
        // mobile -- isFileSystemAccessSupported() is false there, so this
        // always falls through to the classic <input> picker below). Picking
        // via File System Access instead of the hidden <input> gets each file
        // a persistable handle, which is what lets a large desktop batch (the
        // camera-import case) resume after a reload without the manual
        // reselect-and-match flow the <input> path still needs. Not run
        // through attachSelectedFilesToPendingSession's paused-session
        // matching: any earlier paused session on this browser is expected to
        // resume on its own via those same handles (see retryPersistedUploadSession/
        // the auto-resume effect), so a fresh pick here is always a new batch.
        if (isFileSystemAccessSupported()) {
            void (async () => {
                const picked = await pickUploadFilesViaFileSystemAccess();
                if (picked.length === 0) {
                    return;
                }
                const handles = new Map(picked.map(({ file, handle }) => [file, handle]));
                await startUpload(picked.map(({ file }) => file), handles);
            })();
            return;
        }
        uploadInputRef.current?.click();
    }, [addNotification, pollUntilWarm, startUpload, uploading, warmUpload]);

    const resumeAllPendingUploads = useCallback(async () => {
        await retryPersistedUploadSession();
    }, [retryPersistedUploadSession]);

    // Splits a large fresh selection into <=MAX_FRESH_UPLOAD_BATCH_SIZE
    // chunks instead of handing it to startUpload in one shot: only the
    // first chunk starts immediately, the rest queue through
    // queuedUploadBatchesRef -- the exact mechanism startUpload's own
    // `uploading` branch already uses for files picked while an upload is
    // running, which is the one code path confirmed live to handle 1300+
    // files reliably. See MAX_FRESH_UPLOAD_BATCH_SIZE's own comment for why
    // immediate-vs-deferred seems to matter here.
    // Pure queuing, no immediate start -- for callers that already know
    // something else is about to occupy runUploadSession's shared refs (e.g.
    // resuming a handful of matched stale files below) and so can't safely
    // start these immediately even though `uploading` may still read false
    // at the moment of the call.
    const enqueueUploadFilesInBatches = useCallback((files: File[]) => {
        if (files.length === 0) {
            return;
        }
        for (let i = 0; i < files.length; i += MAX_FRESH_UPLOAD_BATCH_SIZE) {
            queuedUploadBatchesRef.current.push(files.slice(i, i + MAX_FRESH_UPLOAD_BATCH_SIZE));
        }
        setQueuedUploadFileCount((count) => count + files.length);
    }, []);

    const startUploadInBatches = useCallback((files: File[]) => {
        if (files.length <= MAX_FRESH_UPLOAD_BATCH_SIZE) {
            void startUpload(files);
            return;
        }
        const firstBatch = files.slice(0, MAX_FRESH_UPLOAD_BATCH_SIZE);
        const remainder = files.slice(MAX_FRESH_UPLOAD_BATCH_SIZE);
        enqueueUploadFilesInBatches(remainder);
        addNotification(
            'Large selection split into batches',
            `${plural(files.length, 'file')} split into batches of up to ${MAX_FRESH_UPLOAD_BATCH_SIZE}. The first batch is starting now; the rest will follow automatically as each one finishes.`,
        );
        void startUpload(firstBatch);
    }, [addNotification, startUpload, enqueueUploadFilesInBatches]);

    const handleUploadSelection = useCallback((event: React.ChangeEvent<HTMLInputElement>) => {
        const selectedFiles = event.target.files ? Array.from(event.target.files) : [];
        if (selectedFiles.length === 0) {
            // Indistinguishable from here whether the user cancelled the
            // picker (routine, happens constantly -- must not toast on this)
            // or the native picker itself failed to return any files for a
            // very large selection (seen live with 1888 items: no upload
            // network call ever fired, and the tab stayed alive the whole
            // time per a HAR capture, ruling out the earlier OOM-crash
            // theory for this specific case -- see
            // mobile-safari-large-batch-upload-crash). A console breadcrumb
            // costs nothing for ordinary users but gives the next devtools
            // session something HAR alone can't show.
            console.warn('Upload picker closed with no files selected.', { rawFileListLength: event.target.files?.length ?? null });
        }
        if (selectedFiles.length > 0) {
            // loadPersistedSession() reflects the live session while a healthy
            // upload is actively running too, so gate that fallback on !uploading
            // -- otherwise files picked from a second folder mid-upload would be
            // matched against the in-flight session's files (and fail to match)
            // instead of being queued via startUpload below.
            if (pendingUploadSession || (!uploading && loadPersistedSession())) {
                void (async () => {
                    try {
                        const { matchedCount, unmatchedFiles } = await attachSelectedFilesToPendingSession(selectedFiles);
                        // A broader reselection (e.g. the whole folder, or a
                        // much larger date range, instead of just the
                        // original picks) can include files that were never
                        // part of this paused session at all -- upload those
                        // as their own batch instead of silently dropping
                        // them. startUpload's own dedupe check still applies,
                        // so anything already fully uploaded gets skipped
                        // there too.
                        //
                        // Queued/started BEFORE awaiting the resume below,
                        // not after: runUploadSession can't safely run twice
                        // at once (it shares refs like uploadSourceFilesRef
                        // across calls), so if a resume is about to run this
                        // bulk of new files must be queued rather than
                        // started immediately -- but queuing itself must
                        // happen right away. A large new selection (e.g.
                        // 4000 files) that happens to overlap by a handful of
                        // names with a stale paused session used to sit
                        // silently behind `await retryPersistedUploadSession()`
                        // here, with nothing visibly starting until that
                        // resume (of only the few matched files) finished --
                        // indistinguishable from a total failure if that
                        // resume was itself slow.
                        if (unmatchedFiles.length > 0) {
                            if (matchedCount > 0) {
                                enqueueUploadFilesInBatches(unmatchedFiles);
                                addNotification(
                                    'New files found',
                                    `${plural(unmatchedFiles.length, 'file')} in this selection weren't part of the paused upload -- queued to start once it resumes.`,
                                );
                            } else {
                                startUploadInBatches(unmatchedFiles);
                            }
                        }
                        if (matchedCount > 0) {
                            addNotification('Files reselected', `${plural(matchedCount, 'file')} reattached to the paused upload.`);
                            await retryPersistedUploadSession();
                        } else if (unmatchedFiles.length === 0) {
                            addNotification('No paused files matched', 'Select the same file names, sizes, and modified dates from the paused upload.');
                            setUploadError('Selected files did not match the paused upload. Choose the original files to retry.');
                        }
                    } catch (err) {
                        // Unguarded before this fix: a throw anywhere above
                        // (matching, resuming) became a silent unhandled
                        // promise rejection -- the whole selection would just
                        // do nothing with zero visible feedback.
                        console.error('Reselecting files against the paused upload failed.', err);
                        setUploadError(getUploadErrorMessage(err, 'Reselecting files failed unexpectedly. Try Upload again.'));
                    }
                })();
            } else {
                startUploadInBatches(selectedFiles);
            }
        }

        event.target.value = '';
    }, [
        addNotification,
        attachSelectedFilesToPendingSession,
        enqueueUploadFilesInBatches,
        loadPersistedSession,
        pendingUploadSession,
        retryPersistedUploadSession,
        setUploadError,
        startUploadInBatches,
        uploading,
    ]);

    // Drain queued folder/batch selections (queuedUploadBatchesRef) once the
    // active session is fully clear -- i.e. it finished cleanly and isn't
    // sitting in a paused/needs-retry state. See startUpload's `uploading`
    // branch for how batches get queued, and stopActiveUpload for why an
    // explicit stop clears the queue instead of letting it drain here.
    useEffect(() => {
        if (uploading || pendingUploadSession) {
            return;
        }
        if (queuedUploadBatchesRef.current.length === 0) {
            return;
        }
        const nextBatch = queuedUploadBatchesRef.current.shift();
        if (nextBatch && nextBatch.length > 0) {
            setQueuedUploadFileCount((count) => Math.max(0, count - nextBatch.length));
            void startUpload(nextBatch);
        }
    }, [uploading, pendingUploadSession, startUpload]);

    useEffect(() => {
        const restored = loadPersistedSession();
        if (!restored) {
            return;
        }

        const incomplete = restored.files.some((file) => file.status !== 'done');
        if (!incomplete) {
            void clearPersistedSession();
            return;
        }

        persistSession(restored);
        setPendingUploadSession(restored);
        addNotification('Upload paused', `${plural(restored.files.filter((file) => file.status !== 'done').length, 'file')} need retry approval.`);
        if (!autoResumeAttemptedRef.current) {
            autoResumeAttemptedRef.current = true;
            void retryPersistedUploadSession();
        }
    }, [addNotification, clearPersistedSession, loadPersistedSession, persistSession]);

    // Keep the backend warm only for browser-side processing, not for uploads.
    // Uploads are already direct-to-blob (see uploadFileInChunks) -- the backend
    // is only touched briefly per file (init + finalize), and those calls now
    // retry through a cold start (postInitWithRetry/postFinalizeWithRetry), so
    // there's no need to pay to keep a replica warm for the whole transfer.
    // Pinging /health every 30s for the duration of a large batch (which can run
    // for a long time, especially backgrounded via BackgroundKeepAlive) was
    // billing the backend as continuously active even though it does almost no
    // work during the transfer itself -- the single biggest lever in the backend
    // cost complaint this was built to address. Pinging /health around the clock
    // kept the scaled-to-zero backend billing 24/7 whenever a tab stayed open;
    // idle browsing wakes it on demand and the cold-start handling covers the rest.
    useEffect(() => {
        if (!browserProcessingActive) {
            stopBackendKeepalive();
            return undefined;
        }
        startBackendKeepalive();
        return () => stopBackendKeepalive();
    }, [browserProcessingActive, startBackendKeepalive, stopBackendKeepalive]);

    useEffect(() => {
        void startBrowserProcessing();
    }, [startBrowserProcessing]);

    useEffect(() => {
        if (browserProcessingTimerRef.current !== null) {
            return undefined;
        }
        browserProcessingTimerRef.current = window.setInterval(() => {
            void startBrowserProcessing();
        }, 30000);
        return () => {
            if (browserProcessingTimerRef.current !== null) {
                window.clearInterval(browserProcessingTimerRef.current);
                browserProcessingTimerRef.current = null;
            }
        };
    }, [startBrowserProcessing]);

    useEffect(() => {
        const active = uploading || uploadStartInProgressRef.current;
        try {
            sessionStorage.setItem('photostore.upload.active', active ? '1' : '0');
        } catch {
            // Ignore storage failures; the unload guard below still protects the current tab.
        }
        if (!active) {
            return undefined;
        }
        const handleBeforeUnload = (event: BeforeUnloadEvent) => {
            event.preventDefault();
            event.returnValue = '';
        };
        window.addEventListener('beforeunload', handleBeforeUnload);
        return () => window.removeEventListener('beforeunload', handleBeforeUnload);
    }, [uploading]);

    const browserAiReasonText = formatBrowserAiReason(browserAiModelState);
    const browserAiButtonDisabled = browserAiModelState.status === 'checking'
        || browserAiModelState.status === 'loading'
        || browserAiModelState.status === 'available'
        || browserAiModelState.status === 'unsupported';
    const browserAiButtonLabel = browserAiModelState.status === 'available'
        ? (browserProcessingActive ? 'Processing in background' : 'Browser AI ready')
        : browserAiModelState.status === 'loading' || browserAiModelState.status === 'checking'
            ? 'Loading browser AI'
            : browserAiModelState.status === 'unsupported'
                ? 'Browser AI not supported'
                : browserAiModelState.status === 'unavailable'
                    ? `Retry browser AI: ${browserAiReasonText}`
                    : 'Browser AI not loaded. Click to load';
    const browserAiButtonClass = `btn btn-soft icon-btn browser-ai-model-btn browser-ai-model-${browserAiModelState.status}`;

    const clusteringStatusLabel = useMemo(() => clusteringActivityLabel(activeJobs), [activeJobs]);
    const clusteringActive = clusteringStatusLabel !== '';
    const ipworkStatusLabel = useMemo(() => ipworkActivityLabel(activeJobs), [activeJobs]);
    const ipworkActive = ipworkStatusLabel !== '';

    const value = useMemo<AppServicesContextValue>(() => ({
        notifications,
        unreadCount,
        addNotification,
        updateNotification,
        clearNotifications,
        markAllNotificationsRead,
        browserAiModelState,
        browserAiLoadProgress,
        browserAiButtonDisabled,
        browserAiButtonLabel,
        browserAiButtonClass,
        loadBrowserAiModel,
        uploading,
        pendingUploadSummary,
        pendingUploadFailedFiles,
        queuedUploadFileCount,
        requestUpload,
        resumeAllPendingUploads,
        stopActiveUpload,
        retryPersistedUploadSession,
        discardPersistedUploadSession,
        registerUploadCompletionHandler,
        registerUploadErrorHandler,
        startBrowserProcessing,
        browserProcessingActive,
        activeJobs,
        clusteringActive,
        clusteringStatusLabel,
        ipworkActive,
        ipworkStatusLabel,
    }), [
        notifications,
        unreadCount,
        addNotification,
        updateNotification,
        clearNotifications,
        markAllNotificationsRead,
        browserAiModelState,
        browserAiLoadProgress,
        browserAiButtonDisabled,
        browserAiButtonLabel,
        browserAiButtonClass,
        loadBrowserAiModel,
        uploading,
        pendingUploadSummary,
        pendingUploadFailedFiles,
        queuedUploadFileCount,
        requestUpload,
        resumeAllPendingUploads,
        stopActiveUpload,
        retryPersistedUploadSession,
        discardPersistedUploadSession,
        registerUploadCompletionHandler,
        registerUploadErrorHandler,
        startBrowserProcessing,
        browserProcessingActive,
        activeJobs,
        clusteringActive,
        clusteringStatusLabel,
        ipworkActive,
        ipworkStatusLabel,
    ]);

    return (
        <AppServicesContext.Provider value={value}>
            {children}
            <input
                ref={uploadInputRef}
                type="file"
                accept={FILE_ACCEPT_FILTER}
                multiple
                onChange={handleUploadSelection}
                className="hidden-upload-input"
            />
        </AppServicesContext.Provider>
    );
};
