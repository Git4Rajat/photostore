import type {
    BrowserAiModelState,
    BrowserAiModelUiStatus,
    BrowserAiNetworkGate,
} from '../types/browserProcessing';

export const MB = 1024 * 1024;
export const CLIENT_PROCESSING_SCHEMA_VERSION = 2;
export const DEFAULT_UPLOAD_PROFILE = {
    fileParallelism: 3,
    chunkSizeBytes: 8 * MB,
    reason: 'standard connection',
};
export const MAX_BACKEND_UPLOAD_CHUNK_BYTES = 64 * MB;
export const MAX_FINALIZE_RETRIES = 8;
export const PHOTO_CACHE_STORAGE_KEY = 'photostore.photo.cache.v1';
export const UPLOAD_SESSION_STORAGE_KEY = 'photostore.upload.session.v1';
export const UPLOAD_STOPPED_ERROR = 'upload_stopped_by_user';

const isBrowserAiModelUiStatus = (status: string): status is BrowserAiModelUiStatus => (
    status === 'checking'
    || status === 'idle'
    || status === 'loading'
    || status === 'available'
    || status === 'unavailable'
    || status === 'unsupported'
);

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

export const browserAiIdleState = (detail = 'Browser AI model is not loaded'): BrowserAiModelState => ({
    status: 'idle',
    detail,
    modelAvailability: 'skipped',
    modelCacheStatus: 'miss',
    runtime: 'browser-ai-worker',
});

export const browserAiLoadingState = (current: BrowserAiModelState): BrowserAiModelState => ({
    ...current,
    status: 'loading',
});

export const browserAiUnsupportedState = (detail = 'Browser AI is unsupported in this browser.'): BrowserAiModelState => ({
    status: 'unsupported',
    detail,
    modelAvailability: 'skipped',
    modelCacheStatus: 'miss',
    runtime: 'browser-ai-worker',
});

export const getBrowserAiUnsupportedReason = (): string | null => {
    if (typeof window === 'undefined') {
        return 'Browser AI requires a browser environment.';
    }
    if (!('Worker' in window)) {
        return 'Browser AI worker support is unavailable.';
    }
    if (typeof navigator === 'undefined') {
        return 'Browser AI requires browser APIs that are not available.';
    }
    return null;
};

export const getBrowserAiNetworkGate = (): BrowserAiNetworkGate => {
    if (typeof navigator === 'undefined') {
        return {
            allowed: false,
            reason: 'network_info_unavailable',
            detail: 'Browser AI network checks are unavailable.',
            hasNetworkInfo: false,
        };
    }
    if (!navigator.onLine) {
        return {
            allowed: false,
            reason: 'offline',
            detail: 'Browser is offline.',
            hasNetworkInfo: true,
        };
    }
    const connection = (navigator as Navigator & { connection?: { saveData?: boolean; effectiveType?: string } }).connection;
    if (connection?.saveData) {
        return {
            allowed: false,
            reason: 'save_data_enabled',
            detail: 'Save-Data is enabled.',
            hasNetworkInfo: true,
        };
    }
    return {
        allowed: true,
        reason: null,
        detail: 'Browser AI network access is available.',
        hasNetworkInfo: Boolean(connection),
    };
};

export const isBrowserAiAutoLoadAllowed = () => getBrowserAiNetworkGate().allowed;

export const isBrowserAiNetworkRetryReason = (reason: unknown): boolean => (
    reason === 'offline'
    || reason === 'poor_network'
    || reason === 'network_info_unavailable'
    || reason === 'save_data_enabled'
);

export const isUploadStoppedError = (err: unknown) => (
    err instanceof Error && err.message === UPLOAD_STOPPED_ERROR
);

export const formatBrowserAiReason = (state: BrowserAiModelState): string => {
    if (!state) {
        return 'Browser AI model is not loaded.';
    }
    if (state.detail) {
        return state.detail;
    }
    const status = isBrowserAiModelUiStatus(state.status) ? state.status : 'unavailable';
    return status === 'available'
        ? 'Browser AI model is ready.'
        : status === 'unsupported'
            ? 'Browser AI is unsupported in this browser.'
            : 'Browser AI model is not loaded.';
};

export const getAdaptiveUploadProfile = (files: Array<{ size?: number }>): typeof DEFAULT_UPLOAD_PROFILE => {
    const totalBytes = files.reduce((sum, file) => sum + Number(file?.size || 0), 0);
    if (totalBytes > 150 * MB) {
        return {
            fileParallelism: 2,
            chunkSizeBytes: 16 * MB,
            reason: 'large upload set',
        };
    }
    if (totalBytes > 50 * MB) {
        return {
            fileParallelism: 3,
            chunkSizeBytes: 8 * MB,
            reason: 'moderate upload set',
        };
    }
    return DEFAULT_UPLOAD_PROFILE;
};

