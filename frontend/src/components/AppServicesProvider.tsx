import React, { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { BellIcon, TrashIcon, XMarkIcon } from '@heroicons/react/24/outline';
import { get, getUpload, post, postUpload, resolveApiUrl } from '../services/apiClient';
import { getAccessToken, isAuthEnabled } from '../services/authClient';
import type {
    AppNotification,
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
    getBrowserAiNetworkGate,
    getBrowserAiUnsupportedReason,
    isBrowserAiAutoLoadAllowed,
    isBrowserAiNetworkRetryReason,
    isUploadStoppedError,
} from './browserAiShared';
import { FILE_ACCEPT_FILTER, requiresBackendPreview } from '../utils/photoDisplay';
import { shouldSuppressLeaseWarning } from '../utils/processingLease';

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
const acquireBrowserAiModel = () => withPhotoGalleryRuntime(
    (runtime) => runtime.acquireBrowserAiModel() as Promise<SharedBrowserAiModelState>,
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
    browserAiButtonDisabled: boolean;
    browserAiButtonLabel: string;
    browserAiButtonClass: string;
    loadBrowserAiModel: () => Promise<SharedBrowserAiModelState>;
    uploading: boolean;
    pendingUploadSummary: PendingUploadSummary | null;
    requestUpload: () => void;
    resumeAllPendingUploads: () => Promise<void>;
    stopActiveUpload: () => void;
    retryPersistedUploadSession: () => Promise<void>;
    discardPersistedUploadSession: () => Promise<void>;
    registerUploadCompletionHandler: (handler: () => void | Promise<void>) => () => void;
    registerUploadErrorHandler: (handler: (message: string | null) => void) => () => void;
    startBrowserProcessing: (options?: BrowserProcessingStartOptions) => Promise<number>;
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
const WARMUP_POLL_INTERVAL_MS = 5000;
const BACKEND_KEEPALIVE_INTERVAL_MS = 30000;
const browserProcessingActionSteps: Record<BrowserProcessingAction, string[]> = {
    thumbnails: ['thumbnail'],
    exif: ['exif'],
    ocr: ['ocr'],
    vision: ['ai_vision'],
    map: ['map_detection'],
    faces: ['face'],
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
    const bellButtonRef = useRef<HTMLButtonElement | null>(null);
    const [notificationAnchorStyle, setNotificationAnchorStyle] = useState<React.CSSProperties>({});

    const closeNotifications = useCallback(() => {
        setShowNotifications(false);
    }, []);

    useEffect(() => {
        if (!showNotifications || typeof window === 'undefined') {
            return undefined;
        }

        let frameId = 0;

        const updateAnchor = () => {
            const button = bellButtonRef.current;
            if (!button) {
                return;
            }

            const rect = button.getBoundingClientRect();
            const preferredLeft = rect.right - 360;
            const clampedLeft = Math.min(
                Math.max(10, preferredLeft),
                Math.max(10, window.innerWidth - 10 - 360),
            );

            setNotificationAnchorStyle({
                '--notification-anchor-top': `${Math.max(rect.bottom + 8, 88)}px`,
                '--notification-anchor-left': `${clampedLeft}px`,
            } as React.CSSProperties);
        };

        const scheduleUpdate = () => {
            window.cancelAnimationFrame(frameId);
            frameId = window.requestAnimationFrame(updateAnchor);
        };

        updateAnchor();
        window.addEventListener('scroll', scheduleUpdate, true);
        window.addEventListener('resize', scheduleUpdate);

        const button = bellButtonRef.current;
        const resizeObserver = button ? new ResizeObserver(scheduleUpdate) : null;
        if (button && resizeObserver) {
            resizeObserver.observe(button);
        }

        return () => {
            window.cancelAnimationFrame(frameId);
            window.removeEventListener('scroll', scheduleUpdate, true);
            window.removeEventListener('resize', scheduleUpdate);
            resizeObserver?.disconnect();
        };
    }, [showNotifications]);

    const overlay = showNotifications && typeof document !== 'undefined'
        ? createPortal(
            <>
                <button
                    type="button"
                    className="notification-backdrop"
                    aria-label="Close notifications"
                    onClick={closeNotifications}
                />
                <div
                    className="notification-pane card-glass"
                    role="dialog"
                    aria-label="Notifications"
                    style={notificationAnchorStyle}
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
            </>,
            document.body,
        )
        : null;

    return (
        <div className="notification-wrap">
            <button
                ref={bellButtonRef}
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

const isBackgroundThrottledFaceResult = (result: any): boolean => {
    const facePayload = result?.clientProcessing?.face;
    if (facePayload && typeof facePayload === 'object' && String(facePayload.deferredReason || '').toLowerCase() === 'background_throttled') {
        return true;
    }
    return Array.isArray(result?.clientProcessingReport)
        && result.clientProcessingReport.some((item: any) => String(item?.step || '') === 'face' && String(item?.reason || '').toLowerCase() === 'background_throttled');
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
    details.push(`Processed ${processedCount}/${totalCount} photo(s).`);
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
    const uploadSessionRef = useRef<PersistedUploadSession | null>(null);
    const isResumingUploadRef = useRef<boolean>(false);
    const uploadStartInProgressRef = useRef<boolean>(false);
    const uploadSourceFilesRef = useRef<Map<string, File>>(new Map());
    const uploadStopRequestedRef = useRef<boolean>(false);
    const uploadAbortControllersRef = useRef<Set<AbortController>>(new Set());
    const backendKeepaliveTimerRef = useRef<number | null>(null);
    const backendWarmupInFlightRef = useRef<boolean>(false);
    const uploadWarmupInFlightRef = useRef<boolean>(false);
    const autoResumeAttemptedRef = useRef<boolean>(false);
    const browserAiModelStateRef = useRef<SharedBrowserAiModelState>(browserAiModelState);
    const browserAiLoadInFlightRef = useRef<boolean>(false);
    const browserAiAutoLoadAttemptedRef = useRef<boolean>(false);
    const uploadCompletionHandlersRef = useRef<Set<() => void | Promise<void>>>(new Set());
    const uploadErrorHandlerRef = useRef<((message: string | null) => void) | null>(null);
    const browserProcessingStartInFlightRef = useRef<boolean>(false);
    const browserProcessingCancelRef = useRef<boolean>(false);
    const browserProcessingTimerRef = useRef<number | null>(null);
    const browserProcessingDeferredRef = useRef<Map<string, number>>(new Map());
    const browserProcessingNotificationRef = useRef<BrowserProcessingNotificationState | null>(null);

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

    const ensureBrowserProcessingNotification = useCallback((totalCount: number) => {
        const current = browserProcessingNotificationRef.current;
        if (current) {
            current.totalCount = Math.max(current.totalCount, totalCount);
            return current;
        }
        const next: BrowserProcessingNotificationState = {
            id: addNotification(
                'Browser processing started',
                `Preparing ${totalCount} photo${totalCount === 1 ? '' : 's'} for local processing.`,
                {
                    uploadedCount: 0,
                    totalCount,
                    failedCount: 0,
                    skippedDuplicateCount: 0,
                },
            ),
            totalCount,
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
            localStorage.removeItem(PHOTO_CACHE_STORAGE_KEY);
        } catch {
            // Cache invalidation should not block upload completion.
        }
    }, []);

    const notifyUploadComplete = useCallback(async () => {
        invalidatePhotoCache();
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
            const pendingResponse = requestedFilenames.length > 0
                ? { pending: requestedItems.map((item) => ({ ...item, statuses: {} })) }
                : await get('/upload/processing/pending?limit=1');
            const pending = Array.isArray(pendingResponse?.pending) ? pendingResponse.pending : [];
            if (pending.length === 0) {
                return 0;
            }
            const pendingTotal = Number(pendingResponse?.totalPending);
            const isAutomaticPull = requestedFilenames.length === 0;
            const totalCount = isAutomaticPull && Number.isFinite(pendingTotal)
                ? Math.max(pending.length, pendingTotal)
                : pending.length;
            const browserProcessingNotification = ensureBrowserProcessingNotification(totalCount);
            browserProcessingNotification.totalCount = Math.max(browserProcessingNotification.totalCount, totalCount);
            for (const item of pending) {
                if (browserProcessingCancelRef.current) {
                    break;
                }
                const filename = String(item?.filename || '');
                const itemRotation = normalizeRotationDegrees(item?.rotation || 0);
                if (!filename) {
                    continue;
                }
                if (!options.force) {
                    const statuses = item?.statuses || {};
                    if (!hasRunnableBrowserStep(statuses, requestedSteps) && requestedFilenames.length === 0) {
                        continue;
                    }
                }
                let claim: { claimed?: boolean; leaseId?: string; thumbnailUploadUrl?: string } | null = null;
                try {
                    claim = await post('/upload/processing/claim', {
                        filename,
                        steps: requestedSteps ? Array.from(requestedSteps) : undefined,
                    });
                } catch (err) {
                    if (shouldSuppressLeaseWarning(err)) {
                        continue;
                    }
                    throw err;
                }
                if (!claim?.claimed) {
                    continue;
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
                    let convertedPreview: Blob | undefined;
                    if (requiresBackendPreview(filename)) {
                        try {
                            const previewAccess = await get(`/api/photos/access/preview/${encodeURIComponent(filename)}`);
                            const previewUrl = String(previewAccess?.url || '');
                            if (previewUrl) {
                                const previewBlob = await fetchProcessingBlob(previewUrl);
                                if (/^image\/jpe?g$/i.test(previewBlob.type || '') || previewBlob.size > 0) {
                                    convertedPreview = previewBlob;
                                }
                            }
                        } catch (previewErr) {
                            console.warn(`Converted JPEG preview was not available for ${filename}; falling back to embedded RAW preview.`, previewErr);
                        }
                    }
                    await post('/upload/processing/heartbeat', { filename, leaseId: claim?.leaseId || '' }).catch(() => undefined);
                    const statuses = item?.statuses || {};
                    const needsBrowserAi = hasRunnableBrowserStep(statuses, new Set(['ai_vision']));
                    if (needsBrowserAi && browserAiModelStateRef.current.status !== 'available') {
                        const gate = getBrowserAiNetworkGate();
                        if (gate.allowed && isBrowserAiAutoLoadAllowed()) {
                            setBrowserAiModelState(browserAiLoadingState(browserAiModelStateRef.current));
                            const modelState = await acquireBrowserAiModel();
                            browserAiModelStateRef.current = modelState;
                            setBrowserAiModelState(modelState);
                        }
                    }
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
                    const filteredResult = filterBrowserProcessingResult(result, requestedSteps);
                    const faceDeferred = requestedSteps?.has('face') && isBackgroundThrottledFaceResult(filteredResult);
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
                        continue;
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
                        continue;
                    }
                    browserProcessingNotification.failedCount += 1;
                    const statuses = item?.statuses || {};
                    const failureReport = buildBrowserProcessingFailureReport(filename, statuses, requestedSteps, processingStartedAt);
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
                syncBrowserProcessingNotification(
                    browserProcessingNotification,
                    undefined,
                    'Browser processing finished',
                );
                browserProcessingNotificationRef.current = null;
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
        const hasRequestIdleCallback = typeof window.requestIdleCallback === 'function';
        const schedule = hasRequestIdleCallback
            ? window.requestIdleCallback(() => {
                preloadNativeFaceModels();
            }, { timeout: 5000 })
            : window.setTimeout(() => {
                preloadNativeFaceModels();
            }, 5000);
        return () => {
            if (hasRequestIdleCallback) {
                window.cancelIdleCallback(schedule);
            } else {
                window.clearTimeout(schedule);
            }
        };
    }, []);

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
        const notificationId = addNotification('Loading browser AI', 'Checking model manifest, cache, runtime, and warm-up.');
        try {
            const result = await acquireBrowserAiModel();
            setBrowserAiModelState(result);
            if (result.status === 'available') {
                updateNotification(notificationId, {
                    title: 'Browser AI ready',
                    details: `Model loaded${result.modelVersion ? ` (${result.modelVersion})` : ''}.`,
                });
            } else {
                updateNotification(notificationId, {
                    title: result.status === 'unsupported' ? 'Browser AI unsupported' : 'Browser AI unavailable',
                    details: String(formatBrowserAiReason(result)),
                });
            }
        } catch (err) {
            const detail = err instanceof Error ? err.message : 'unknown_error';
            const failed: SharedBrowserAiModelState = {
                status: 'unavailable',
                reason: 'unknown_error',
                detail,
                modelAvailability: 'unavailable',
                modelCacheStatus: 'failed',
                runtime: 'browser-ai-worker',
                modelAcquisitionMs: 0,
            };
            setBrowserAiModelState(failed);
            updateNotification(notificationId, {
                title: 'Browser AI unavailable',
                details: detail,
            });
        } finally {
            browserAiLoadInFlightRef.current = false;
        }
        return browserAiModelStateRef.current;
    }, [addNotification, updateNotification]);

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
        const retryDeferredBrowserFaces = () => {
            if (document.visibilityState !== 'visible' || browserProcessingDeferredRef.current.size === 0) {
                return;
            }
            if (browserAiModelStateRef.current.status !== 'available') {
                void loadBrowserAiModel().finally(() => {
                    if (browserAiModelStateRef.current.status === 'available' && browserProcessingDeferredRef.current.size > 0) {
                        const items = Array.from(browserProcessingDeferredRef.current.entries()).map(([filename, rotation]) => ({
                            filename,
                            rotation,
                        }));
                        browserProcessingDeferredRef.current.clear();
                        void startBrowserProcessing({ actions: ['faces'], items, force: true });
                    }
                });
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
    }, [loadBrowserAiModel, startBrowserProcessing]);

    useEffect(() => {
        const tryAutoLoadBrowserAi = () => {
            const unsupportedDetail = getBrowserAiUnsupportedReason();
            if (unsupportedDetail) {
                setBrowserAiModelState(browserAiUnsupportedState(unsupportedDetail));
                return;
            }
            const gate = getBrowserAiNetworkGate();
            const testRuntime = import.meta.env.MODE === 'test';
            if (testRuntime || !gate.allowed || !isBrowserAiAutoLoadAllowed()) {
                const current = browserAiModelStateRef.current;
                if (current.status === 'checking') {
                    setBrowserAiModelState(browserAiIdleState(testRuntime ? 'Browser AI auto-load is disabled in tests' : gate.detail));
                }
                return;
            }
            const current = browserAiModelStateRef.current;
            const canAutoLoad = current.status === 'idle'
                || (current.status === 'unavailable' && isBrowserAiNetworkRetryReason(current.reason));
            if (!canAutoLoad || browserAiAutoLoadAttemptedRef.current || browserAiLoadInFlightRef.current) {
                return;
            }
            browserAiAutoLoadAttemptedRef.current = true;
            void loadBrowserAiModel();
        };

        tryAutoLoadBrowserAi();
        const connection = (navigator as Navigator & { connection?: EventTarget }).connection;
        window.addEventListener('online', tryAutoLoadBrowserAi);
        window.addEventListener('offline', tryAutoLoadBrowserAi);
        connection?.addEventListener?.('change', tryAutoLoadBrowserAi);
        return () => {
            window.removeEventListener('online', tryAutoLoadBrowserAi);
            window.removeEventListener('offline', tryAutoLoadBrowserAi);
            connection?.removeEventListener?.('change', tryAutoLoadBrowserAi);
        };
    }, [browserAiModelState.status, loadBrowserAiModel]);

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
        const chunkSize = file.size <= configuredChunkSize ? file.size : configuredChunkSize;
        const BlockBlobClient = await loadBlockBlobClient();
        const blockBlobClient = new BlockBlobClient(initResponse.blobUrl);
        const blockIds = [...(options?.existingBlockIds || [])];
        let chunkStart = Math.max(0, Math.min(options?.startByte || 0, file.size));
        let skippedDuplicate = false;
        while (chunkStart < file.size) {
            if (uploadStopRequestedRef.current || options?.signal?.aborted) {
                throw new Error(UPLOAD_STOPPED_ERROR);
            }
            const chunkEnd = Math.min(chunkStart + chunkSize, file.size) - 1;

            let lastError: unknown = null;
            for (let attempt = 0; attempt < 3; attempt += 1) {
                try {
                    const blockIndex = Math.floor(chunkStart / chunkSize);
                    const blockId = btoa(`block-${String(blockIndex).padStart(8, '0')}`);
                    await blockBlobClient.stageBlock(blockId, file.slice(chunkStart, chunkEnd + 1), chunkEnd - chunkStart + 1, {
                        abortSignal: options?.signal,
                    });
                    blockIds[blockIndex] = blockId;
                    options?.onChunkCommitted?.(chunkEnd + 1);
                    options?.onBlockCommitted?.(blockIds.filter(Boolean), chunkSize);
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

            chunkStart = chunkEnd + 1;
        }

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
            options.resumed ? `Processing ${totalCount} persisted file(s).` : `Uploading ${totalCount} file(s) with ${uploadProfile.fileParallelism} lane(s).`,
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
                details: options.resumed ? `Processing ${totalCount} persisted file(s).` : `Uploading ${totalCount} file(s) with ${uploadProfile.fileParallelism} lane(s).`,
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
                        const result = await runBrowserProcessing(
                            file,
                            `browser-${filename}`,
                            processingStartedAt,
                            browserAiModelStateRef.current as Parameters<PhotoGalleryRuntime['runBrowserProcessing']>[3],
                            { clientProcessing: {}, clientProcessingReport: [] },
                            { thumbnailRotationDegrees: 0, faceRotationDegrees: 0 },
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
                        details: `Uploaded ${totalUploaded}/${totalCount} file(s). Skipped duplicates: ${totalSkippedDuplicates}.`,
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

            const workerCount = Math.min(uploadProfile.fileParallelism, Math.max(1, pendingFiles.length));
            await Promise.all(Array.from({ length: workerCount }, () => worker()));
            await Promise.allSettled(Array.from(parallelThumbnailTasks));

            if (uploadStopRequestedRef.current) {
                const latest = uploadSessionRef.current || normalized;
                await cleanupUnfinishedUploadArtifacts(latest);
                await clearPersistedSession();
                setPendingUploadSession(null);
                updateNotification(notificationId, {
                    title: 'Upload stopped',
                    details: `Stopped after uploading ${totalUploaded}/${totalCount} file(s).`,
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
                    details: `Uploaded ${totalUploaded} file(s) successfully. Skipped duplicates: ${totalSkippedDuplicates}.`,
                });
                setUploadError(null);
            } else {
                if (latest) {
                    setPendingUploadSession(latest);
                }
                updateNotification(notificationId, {
                    title: 'Upload paused',
                    details: `Uploaded ${totalUploaded}/${totalCount} file(s), failed ${totalFailed}, skipped duplicates ${totalSkippedDuplicates}. Reselect any missing files to retry.`,
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
                    details: `Stopped after uploading ${totalUploaded}/${totalCount} file(s).`,
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
            addNotification('Reselect files', `${missingCachedFiles.length} file(s) need to be selected again before retry.`);
            setUploadError('Upload files are no longer cached. Use Upload to reselect those files, then retry.');
            return;
        }
        if (missingCachedFiles.length > 0) {
            addNotification('Partial retry', `${retryableFiles.length} cached file(s) will retry. ${missingCachedFiles.length} file(s) still need reselecting.`);
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
        addNotification('Stopping upload', 'Cancelling active requests and cleaning up unfinished files.');
    }, [addNotification, uploading]);

    const startUpload = useCallback(async (filesToUpload: File[]) => {
        if (filesToUpload.length === 0) {
            return;
        }
        if (uploadStartInProgressRef.current || uploading || pendingUploadSession) {
            addNotification('Upload already starting', 'The selected files are already being prepared or uploaded.');
            return;
        }

        uploadStartInProgressRef.current = true;
        uploadStopRequestedRef.current = false;
        setUploading(true);
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
            `Preparing ${filesToUpload.length} file(s), ${formatBytes(totalBytes)}. ${uploadProfile.fileParallelism} upload lane(s), ${formatBytes(uploadProfile.chunkSizeBytes)} chunks: ${uploadProfile.reason}.`,
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
                return;
            }
            await runUploadSession(session, { notificationId: preparingNotificationId, uploadProfile });
        } catch (err) {
            const message = getUploadErrorMessage(err, 'Upload could not start.');
            setUploadError(message);
            setUploading(false);
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
        if (uploadStartInProgressRef.current || uploading) {
            addNotification('Upload already starting', 'Wait for the current upload preparation to finish before selecting more files.');
            return;
        }
        void pollUntilWarm(warmUpload, uploadWarmupInFlightRef);
        uploadInputRef.current?.click();
    }, [addNotification, pollUntilWarm, uploading, warmUpload]);

    const resumeAllPendingUploads = useCallback(async () => {
        await retryPersistedUploadSession();
    }, [retryPersistedUploadSession]);

    const handleUploadSelection = useCallback((event: React.ChangeEvent<HTMLInputElement>) => {
        const selectedFiles = event.target.files ? Array.from(event.target.files) : [];
        if (selectedFiles.length > 0) {
            if (pendingUploadSession || loadPersistedSession()) {
                void (async () => {
                    const matchedCount = await attachSelectedFilesToPendingSession(selectedFiles);
                    if (matchedCount === 0) {
                        addNotification('No paused files matched', 'Select the same file names, sizes, and modified dates from the paused upload.');
                        setUploadError('Selected files did not match the paused upload. Choose the original files to retry.');
                        return;
                    }
                    addNotification('Files reselected', `${matchedCount} file(s) reattached to the paused upload.`);
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
    ]);

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
        addNotification('Upload paused', `${restored.files.filter((file) => file.status !== 'done').length} file(s) need retry approval.`);
        if (!autoResumeAttemptedRef.current) {
            autoResumeAttemptedRef.current = true;
            void retryPersistedUploadSession();
        }
    }, [addNotification, clearPersistedSession, loadPersistedSession, persistSession]);

    useEffect(() => {
        startBackendKeepalive();
        return () => stopBackendKeepalive();
    }, [startBackendKeepalive, stopBackendKeepalive]);

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
        ? 'Browser AI ready'
        : browserAiModelState.status === 'loading' || browserAiModelState.status === 'checking'
            ? 'Loading browser AI'
            : browserAiModelState.status === 'unsupported'
                ? 'Browser AI not supported'
                : browserAiModelState.status === 'unavailable'
                    ? `Retry browser AI: ${browserAiReasonText}`
                    : 'Browser AI not loaded. Click to load';
    const browserAiButtonClass = `btn btn-soft icon-btn browser-ai-model-btn browser-ai-model-${browserAiModelState.status}`;

    const value = useMemo<AppServicesContextValue>(() => ({
        notifications,
        unreadCount,
        addNotification,
        updateNotification,
        clearNotifications,
        markAllNotificationsRead,
        browserAiModelState,
        browserAiButtonDisabled,
        browserAiButtonLabel,
        browserAiButtonClass,
        loadBrowserAiModel,
        uploading,
        pendingUploadSummary,
        requestUpload,
        resumeAllPendingUploads,
        stopActiveUpload,
        retryPersistedUploadSession,
        discardPersistedUploadSession,
        registerUploadCompletionHandler,
        registerUploadErrorHandler,
        startBrowserProcessing,
    }), [
        notifications,
        unreadCount,
        addNotification,
        updateNotification,
        clearNotifications,
        markAllNotificationsRead,
        browserAiModelState,
        browserAiButtonDisabled,
        browserAiButtonLabel,
        browserAiButtonClass,
        loadBrowserAiModel,
        uploading,
        pendingUploadSummary,
        requestUpload,
        resumeAllPendingUploads,
        stopActiveUpload,
        retryPersistedUploadSession,
        discardPersistedUploadSession,
        registerUploadCompletionHandler,
        registerUploadErrorHandler,
        startBrowserProcessing,
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
