import React, { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { BellIcon, TrashIcon, UserGroupIcon, XMarkIcon } from '@heroicons/react/24/outline';
import { get, getUpload, post, postUpload, resolveApiUrl } from '../services/apiClient';
import { getAccessToken, isAuthEnabled } from '../services/authClient';
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
    isUploadStoppedError,
} from './browserAiShared';
import { FILE_ACCEPT_FILTER, requiresBackendPreview } from '../utils/photoDisplay';
import { plural } from '../utils/format';
import { shouldSuppressLeaseWarning } from '../utils/processingLease';
import { BackgroundKeepAlive } from '../services/backgroundKeepAlive';
import { showToast } from '../services/toast';
import {
    fetchJobStatuses,
    isLikelyAuthenticated,
    loadSeenJobIds,
    persistSeenJobIds,
    onJobPollRequested,
    requestJobPoll,
    clusteringActivityLabel,
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
const idbPut = (key: string, value: Blob | File) => withPhotoGalleryRuntime((runtime) => runtime.idbPut(key, value));
const readBlobArrayBuffer = (blob: Blob | File) => withPhotoGalleryRuntime((runtime) => runtime.readBlobArrayBuffer(blob));
const runBrowserProcessing = (...args: Parameters<PhotoGalleryRuntime['runBrowserProcessing']>) => (
    withPhotoGalleryRuntime((runtime) => runtime.runBrowserProcessing(...args))
);
const sha256ArrayBuffer = (buffer: ArrayBuffer) => withPhotoGalleryRuntime((runtime) => runtime.sha256ArrayBuffer(buffer));
const acquireBrowserAiModel = (onProgress?: (stage: BrowserAiLoadStage) => void) => withPhotoGalleryRuntime(
    (runtime) => runtime.acquireBrowserAiModel(onProgress) as Promise<SharedBrowserAiModelState>,
);
const preloadNativeFaceModels = () => withPhotoGalleryRuntime((runtime) => runtime.preloadNativeFaceModels());

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
    return Math.max(1, Math.min(byCores, byMemory));
};

const WARMUP_POLL_INTERVAL_MS = 5000;
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
const browserProcessingActionSteps: Record<BrowserProcessingAction, string[]> = {
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
                    setShowNotifications((prev) => {
                        const next = !prev;
                        if (next) {
                            markAllNotificationsRead();
                        }
                        return next;
                    });
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

    const warmBackend = useCallback(() => get('/health').then(() => undefined), []);
    const warmUpload = useCallback(() => getUpload('/health').then(() => undefined), []);
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
    const uploadStopRequestedRef = useRef<boolean>(false);
    const uploadAbortControllersRef = useRef<Set<AbortController>>(new Set());
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
            if (browserProcessingNotificationRef.current !== null) {
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
                } else if (job.kind !== 'preview') {
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

    const setUploadError = useCallback((message: string | null) => {
        uploadErrorHandlerRef.current?.(message);
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
        if (!session) {
            localStorage.removeItem(UPLOAD_SESSION_STORAGE_KEY);
            return;
        }
        localStorage.setItem(UPLOAD_SESSION_STORAGE_KEY, JSON.stringify(session));
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

    const postFinalizeWithRetry = async (payload: any, signal?: AbortSignal) => {
        let lastError: unknown = null;
        for (let attempt = 0; attempt < MAX_FINALIZE_RETRIES; attempt += 1) {
            if (uploadStopRequestedRef.current || signal?.aborted) {
                throw new Error(UPLOAD_STOPPED_ERROR);
            }
            try {
                return await postUpload('/upload/finalize', payload);
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
        throw lastError || new Error('Finalize failed.');
    };

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
            // When browser AI isn't loaded, skip the automatic background pull: it
            // would download pending images just to run baseline (thumbnail/EXIF/
            // geocode) steps. Explicit, user-initiated requests still run so on-demand
            // actions work regardless of model state; automatic backfill (including
            // baseline steps) resumes once the model becomes available.
            if (isAutomaticPull && browserAiModelStateRef.current.status !== 'available') {
                keepAliveRef.current?.stop();
                return 0;
            }
            // Defer the automatic background pull while an upload session is
            // actively transferring files: running CPU-heavy detection/embedding
            // work (WASM) concurrently with in-flight uploads was making the app
            // sluggish. runUploadSession explicitly kicks off a fresh
            // startBrowserProcessing() call the moment it finishes, so nothing is
            // lost -- this just sequences the two phases instead of interleaving
            // them. Explicit, user-initiated requests (isAutomaticPull false)
            // still run immediately regardless.
            if (isAutomaticPull && uploadingRef.current) {
                keepAliveRef.current?.stop();
                return 0;
            }
            const pendingResponse = requestedFilenames.length > 0
                ? { pending: requestedItems.map((item) => ({ ...item, statuses: {} })) }
                : await get(`/upload/processing/pending?limit=${Math.max(1, concurrency)}`);
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
            const processOnePendingItem = async (item: any): Promise<void> => {
                if (browserProcessingCancelRef.current) {
                    return;
                }
                const filename = String(item?.filename || '');
                const itemRotation = normalizeRotationDegrees(item?.rotation || 0);
                if (!filename) {
                    return;
                }
                // Only run the steps the current model state allows. When browser AI
                // isn't loaded, on-device AI steps are dropped and left pending. A null
                // result means "all steps" (browser AI available); an empty set means
                // nothing runnable right now (only gated steps were requested).
                const effectiveSteps = restrictStepsForModel(requestedSteps, browserAiModelStateRef.current.status === 'available');
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
                if (!claim?.claimed) {
                    return;
                }
                const processingStartedAt = performance.now();
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
                    if (faceDeferred) {
                        browserProcessingDeferredRef.current.set(filename, itemRotation);
                        syncBrowserProcessingNotification(browserProcessingNotification, `Deferred ${filename} for later face retry`);
                        return;
                    }
                    processedCount += 1;
                    browserProcessingNotification.processedCount += 1;
                    syncBrowserProcessingNotification(browserProcessingNotification);
                    await post('/upload/processing/heartbeat', { filename, leaseId: claim?.leaseId || '' }).catch(() => undefined);
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
                    const statuses = item?.statuses || {};
                    const failureReport = buildBrowserProcessingFailureReport(filename, statuses, effectiveSteps, processingStartedAt);
                    if (failureReport.length > 0) {
                        await postUpload('/upload/client-processing', {
                            filename,
                            clientProcessingSchemaVersion: CLIENT_PROCESSING_SCHEMA_VERSION,
                            clientProcessing: {},
                            clientProcessingReport: failureReport,
                            clientAssetId: `browser-${filename}`,
                            uploadId: `browser-${filename}`,
                        }).catch((postErr) => {
                            console.warn(`Browser processing failure report was not accepted for ${filename}.`, postErr);
                        });
                    }
                    console.warn(`Browser processing failed for ${filename}.`, err);
                    syncBrowserProcessingNotification(browserProcessingNotification, `Failed ${filename}`);
                } finally {
                    await post('/upload/processing/release', { filename, leaseId: claim?.leaseId || '' }).catch(() => undefined);
                }
            };
            // Drain `pending` with up to `concurrency` items in flight at once. Each
            // worker pulls the next index and processes it; with concurrency 1 this is
            // exactly the original serial drain. Items are independent (each holds its
            // own server-side lease), so distinct filenames never collide.
            let nextIndex = 0;
            const runDrainWorker = async (): Promise<void> => {
                while (!browserProcessingCancelRef.current) {
                    const index = nextIndex;
                    nextIndex += 1;
                    if (index >= pending.length) {
                        return;
                    }
                    await processOnePendingItem(pending[index]);
                }
            };
            const workerCount = Math.max(1, Math.min(concurrency, pending.length));
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
            onChunkCommitted?: (bytesReceived: number) => void;
            onUploadInitialized?: (uploadId: string) => void;
            onBlockCommitted?: (blockIds: string[], chunkSizeBytes: number) => void;
            onFinalizeStarted?: () => void;
            signal?: AbortSignal;
            chunkSizeBytes?: number;
            blockConcurrency?: number;
        },
    ): Promise<{ skippedDuplicate: boolean }> => {
        if (uploadStopRequestedRef.current || options?.signal?.aborted) {
            throw new Error(UPLOAD_STOPPED_ERROR);
        }
        const sha256 = await computeSha256(file);
        if (uploadStopRequestedRef.current || options?.signal?.aborted) {
            throw new Error(UPLOAD_STOPPED_ERROR);
        }
        const initResponse = await postUpload('/upload/init', {
            filename: file.name,
            totalSize: file.size,
            sha256,
            directToBlob: true,
            uploadId: options?.existingUploadId,
        });
        if (typeof initResponse?.uploadId === 'string' && initResponse.uploadId) {
            options?.onUploadInitialized?.(initResponse.uploadId);
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
        let skippedDuplicate = false;

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
        const priorBlockIds = options?.existingBlockIds || [];
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
        const completeResponse = await postFinalizeWithRetry({
            filename: file.name,
            uploadId: initResponse.uploadId,
            totalSize: file.size,
            contentType: file.type || 'application/octet-stream',
            sha256,
        }, options?.signal);
        if (Array.isArray(completeResponse?.duplicates) && completeResponse.duplicates.length > 0) {
            skippedDuplicate = true;
        }
        return { skippedDuplicate };
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
        setUploading(true);
        uploadingRef.current = true;
        setPendingUploadSession(null);
        const totalCount = session.files.length;
        let totalUploaded = session.files.filter((file) => file.status === 'done').length;
        let totalFailed = 0;
        let totalSkippedDuplicates = session.files.filter((file) => file.skippedDuplicate).length;
        const uploadProfile = options.uploadProfile || getAdaptiveUploadProfile(session.files);
        const uploadStartedAt = Date.now();
        let transferredBytesThisRun = 0;
        const lastCommittedBytesByFile = new Map<string, number>();

        const currentUploadRate = () => {
            const elapsedSeconds = Math.max((Date.now() - uploadStartedAt) / 1000, 0.001);
            return transferredBytesThisRun / elapsedSeconds / MB;
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
                const task: Promise<void> = (async () => {
                    let claim: { claimed?: boolean; leaseId?: string; thumbnailUploadUrl?: string } | null = null;
                    try {
                        claim = await post('/upload/processing/claim', { filename, steps: ['thumbnail'] });
                    } catch {
                        return;
                    }
                    if (!claim?.claimed) return;
                    const processingStartedAt = performance.now();
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
                            } catch {
                                delete filteredResult.clientProcessing.thumbnail;
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
                    } catch (err) {
                        console.warn(`Parallel thumbnail generation failed for ${filename}.`, err);
                    } finally {
                        await post('/upload/processing/release', { filename, leaseId: claim?.leaseId || '' }).catch(() => undefined);
                    }
                })();
                parallelThumbnailTasks.add(task);
                void task.finally(() => parallelThumbnailTasks.delete(task));
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

                    try {
                        const sourceFile = uploadSourceFilesRef.current.get(fileMeta.key);
                        const cachedBlob = sourceFile ? null : await idbGet(fileMeta.key);
                        if (!sourceFile && !cachedBlob) {
                            throw new Error(`Missing cached file for ${fileMeta.name}. Please reselect this file.`);
                        }

                        const file = sourceFile || new File([cachedBlob as Blob], fileMeta.name, { type: fileMeta.type, lastModified: fileMeta.lastModified });
                        const controller = new AbortController();
                        uploadAbortControllersRef.current.add(controller);
                        let result: { skippedDuplicate: boolean };
                        const chunkSizeBytes = fileMeta.chunkSizeBytes || uploadProfile.chunkSizeBytes;
                        try {
                            result = await uploadFileInChunks(file, {
                                startByte: fileMeta.uploadedBytes,
                                existingUploadId: fileMeta.uploadId,
                                existingBlockIds: fileMeta.blockIds,
                                onUploadInitialized: (uploadId) => {
                                    updatePersistedFile(fileMeta.key, { uploadId });
                                },
                                onChunkCommitted: (bytesReceived) => {
                                    const previousBytes = lastCommittedBytesByFile.get(fileMeta.key) || 0;
                                    transferredBytesThisRun += Math.max(0, bytesReceived - previousBytes);
                                    lastCommittedBytesByFile.set(fileMeta.key, bytesReceived);
                                    updatePersistedFile(fileMeta.key, { uploadedBytes: bytesReceived });
                                },
                                onBlockCommitted: (blockIds, nextChunkSizeBytes) => {
                                    updatePersistedFile(fileMeta.key, { blockIds, chunkSizeBytes: nextChunkSizeBytes });
                                },
                                onFinalizeStarted: () => {
                                    updatePersistedFile(fileMeta.key, { status: 'finalizing', uploadedBytes: file.size });
                                },
                                signal: controller.signal,
                                chunkSizeBytes,
                                blockConcurrency: perFileBlockConcurrency,
                            });
                        } finally {
                            uploadAbortControllersRef.current.delete(controller);
                        }

                        totalUploaded += 1;
                        if (result.skippedDuplicate) {
                            totalSkippedDuplicates += 1;
                        }

                        updatePersistedFile(fileMeta.key, {
                            status: 'done',
                            uploadedBytes: fileMeta.size,
                            skippedDuplicate: result.skippedDuplicate,
                            error: undefined,
                        });
                        if (!result.skippedDuplicate) {
                            kickOffThumbnailForFile(file, fileMeta.name);
                        }
                        uploadSourceFilesRef.current.delete(fileMeta.key);
                        await idbDelete(fileMeta.key);
                    } catch (err) {
                        if (uploadStopRequestedRef.current || isUploadStoppedError(err)) {
                            throw new Error(UPLOAD_STOPPED_ERROR);
                        }
                        totalFailed += 1;
                        updatePersistedFile(fileMeta.key, {
                            status: 'failed',
                            error: uploadSourceFilesRef.current.has(fileMeta.key)
                                ? `${getUploadErrorMessage(err, 'Upload failed for this file.')} Please reselect this file to retry.`
                                : getUploadErrorMessage(err, 'Upload failed for this file.'),
                        });
                    }

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
                }
            };

            await Promise.all(Array.from({ length: activeLaneCount }, () => worker()));
            await Promise.allSettled(Array.from(parallelThumbnailTasks));

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
            void startBrowserProcessing();
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
            uploadAbortControllersRef.current.clear();
            uploadStopRequestedRef.current = false;
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

    const attachSelectedFilesToPendingSession = useCallback(async (selectedFiles: File[]): Promise<number> => {
        const session = pendingUploadSession || loadPersistedSession();
        if (!session) {
            return 0;
        }

        const matchedKeys = new Set<string>();
        let matchedCount = 0;
        const unfinishedFiles = session.files.filter((file) => file.status !== 'done');

        for (const file of selectedFiles) {
            const meta = unfinishedFiles.find((candidate) => (
                !matchedKeys.has(candidate.key) && fileMatchesPersistedUpload(file, candidate)
            ));
            if (!meta) {
                continue;
            }
            matchedKeys.add(meta.key);
            matchedCount += 1;
            uploadSourceFilesRef.current.set(meta.key, file);
            try {
                await idbPut(meta.key, file);
            } catch {
                // The in-memory File reference still allows retry in this tab.
            }
            updatePersistedFile(meta.key, { status: 'pending', error: undefined });
        }

        const latest = uploadSessionRef.current || session;
        persistSession(latest);
        setPendingUploadSession(latest);
        return matchedCount;
    }, [loadPersistedSession, pendingUploadSession, persistSession, updatePersistedFile]);

    const retryPersistedUploadSession = useCallback(async () => {
        const session = pendingUploadSession || loadPersistedSession();
        if (!session || uploading) {
            return;
        }
        const unfinishedFiles = session.files.filter((file) => file.status !== 'done');
        const cacheChecks = await Promise.all(unfinishedFiles.map(async (file) => ({
            file,
            hasCachedBlob: uploadSourceFilesRef.current.has(file.key) || Boolean(await idbGet(file.key).catch(() => null)),
        })));
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

    const startUpload = useCallback(async (filesToUpload: File[]) => {
        if (filesToUpload.length === 0) {
            return;
        }
        if (uploadStartInProgressRef.current || pendingUploadSession) {
            addNotification('Upload already starting', 'The selected files are already being prepared or uploaded.');
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
        try {
            await Promise.all([
                warmEndpoint(warmBackend, backendWarmupInFlightRef),
            ]);
        } catch (err) {
            const message = getUploadErrorMessage(err, 'Warm-up failed before upload could start.');
            setUploadError(message);
            setUploading(false);
            uploadingRef.current = false;
            updateNotification(warmupNotificationId, {
                title: 'Upload warm-up failed',
                details: message,
            });
            return;
        }
        updateNotification(warmupNotificationId, {
            title: 'Upload path ready',
            details: 'Backend storage is ready. Starting upload.',
        });
        const totalBytes = filesToUpload.reduce((sum, file) => sum + file.size, 0);
        const uploadProfile = getAdaptiveUploadProfile(filesToUpload);
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

        try {
            persistSession(session);
            uploadSourceFilesRef.current = new Map(session.files.map((meta, index) => [meta.key, filesToUpload[index]]));
            await Promise.all(session.files.map((meta, index) => (
                idbPut(meta.key, filesToUpload[index]).catch(() => undefined)
            )));
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
        pendingUploadSession,
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
            addNotification('Upload already starting', 'Wait for the current upload preparation to finish before selecting more files.');
            return;
        }
        // While actively uploading, skip the warm-up poll (the backend is
        // already warm) but still let the picker open so files from another
        // folder can be queued -- see startUpload's `uploading` branch.
        if (!uploading) {
            void pollUntilWarm(warmUpload, uploadWarmupInFlightRef);
        }
        uploadInputRef.current?.click();
    }, [addNotification, pollUntilWarm, uploading, warmUpload]);

    const resumeAllPendingUploads = useCallback(async () => {
        await retryPersistedUploadSession();
    }, [retryPersistedUploadSession]);

    const handleUploadSelection = useCallback((event: React.ChangeEvent<HTMLInputElement>) => {
        const selectedFiles = event.target.files ? Array.from(event.target.files) : [];
        if (selectedFiles.length > 0) {
            // loadPersistedSession() reflects the live session while a healthy
            // upload is actively running too, so gate that fallback on !uploading
            // -- otherwise files picked from a second folder mid-upload would be
            // matched against the in-flight session's files (and fail to match)
            // instead of being queued via startUpload below.
            if (pendingUploadSession || (!uploading && loadPersistedSession())) {
                void (async () => {
                    const matchedCount = await attachSelectedFilesToPendingSession(selectedFiles);
                    if (matchedCount === 0) {
                        addNotification('No paused files matched', 'Select the same file names, sizes, and modified dates from the paused upload.');
                        setUploadError('Selected files did not match the paused upload. Choose the original files to retry.');
                        return;
                    }
                    addNotification('Files reselected', `${plural(matchedCount, 'file')} reattached to the paused upload.`);
                    await retryPersistedUploadSession();
                })();
            } else {
                void startUpload(selectedFiles);
            }
        }

        event.target.value = '';
    }, [
        addNotification,
        attachSelectedFilesToPendingSession,
        loadPersistedSession,
        pendingUploadSession,
        retryPersistedUploadSession,
        setUploadError,
        startUpload,
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

    // Keep the backend warm only while work is actually in flight (an upload
    // batch or browser-side processing). Pinging /health around the clock kept
    // the scaled-to-zero backend billing 24/7 whenever a tab stayed open; idle
    // browsing wakes it on demand and the cold-start handling covers the rest.
    useEffect(() => {
        if (!uploading && !browserProcessingActive) {
            stopBackendKeepalive();
            return undefined;
        }
        startBackendKeepalive();
        return () => stopBackendKeepalive();
    }, [uploading, browserProcessingActive, startBackendKeepalive, stopBackendKeepalive]);

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
