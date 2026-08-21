import type {
    BrowserAiModelState,
    BrowserAiModelUiStatus,
    BrowserAiNetworkGate,
    UploadProfile,
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

const RESOURCE_TIMING_MIN_BYTES = 100 * 1024;
const RESOURCE_TIMING_MIN_DURATION_MS = 20;
// The connection.rtt gate below (rtt < 100) is calibrated for the Network
// Information API's raw TCP RTT. A caller-supplied measuredRttMs instead
// times a full HTTPS round trip (TLS handshake + request/response), which
// runs higher for the same underlying link, so it gets its own, looser
// threshold rather than being compared against the same number.
const ACTIVE_PROBE_RTT_FAST_MS = 250;

// navigator.connection (the Network Information API) is unavailable in
// Safari (desktop and iOS) and Firefox, so those browsers would otherwise
// always fall through to the fixed "assume it's fine" guess below regardless
// of how slow their actual link is. Mine the Resource Timing entries for
// assets the page already fetched (JS bundles, images) to get a real
// observed transfer rate instead of guessing -- transferSize is 0 for cache
// hits and for cross-origin entries without a Timing-Allow-Origin header, so
// this only reflects entries that actually hit the network.
const measureObservedDownlinkMbps = (): number | null => {
    if (typeof performance === 'undefined' || typeof performance.getEntriesByType !== 'function') {
        return null;
    }
    let bestBytes = 0;
    let bestDurationMs = 0;
    for (const entry of performance.getEntriesByType('resource') as PerformanceResourceTiming[]) {
        const bytes = entry.transferSize || 0;
        const durationMs = entry.responseEnd - entry.requestStart;
        if (bytes < RESOURCE_TIMING_MIN_BYTES || durationMs < RESOURCE_TIMING_MIN_DURATION_MS) {
            continue;
        }
        if (bytes > bestBytes) {
            bestBytes = bytes;
            bestDurationMs = durationMs;
        }
    }
    if (bestBytes === 0 || bestDurationMs === 0) {
        return null;
    }
    return (bestBytes * 8) / (bestDurationMs / 1000) / 1_000_000;
};

export const getAdaptiveUploadProfile = (
    files: Array<{ size: number }> = [],
    options?: { measuredRttMs?: number },
): UploadProfile => {
    if (typeof navigator === 'undefined' || typeof window === 'undefined') {
        return DEFAULT_UPLOAD_PROFILE;
    }

    const nav = navigator as Navigator & {
        connection?: {
            effectiveType?: string;
            saveData?: boolean;
            downlink?: number;
            rtt?: number;
            type?: string;
        };
        deviceMemory?: number;
    };
    const connection = nav.connection;
    const effectiveType = (connection?.effectiveType || '').toLowerCase();
    const connectionType = (connection?.type || '').toLowerCase();
    let downlink = Number(connection?.downlink || 0);
    const rtt = Number(connection?.rtt || 0);
    const isWifi = connectionType === 'wifi';
    const isEthernet = connectionType === 'ethernet';
    let hasNetworkInfo = Boolean(connection && Number.isFinite(downlink) && downlink > 0);
    const isMobileViewport = window.matchMedia?.('(max-width: 760px)').matches || false;
    const coarsePointer = window.matchMedia?.('(pointer: coarse)').matches || false;
    const lowMemory = typeof nav.deviceMemory === 'number' && nav.deviceMemory <= 4;
    const largestFile = files.reduce((max, file) => Math.max(max, file.size || 0), 0);
    const hasLargeFiles = largestFile >= 40 * MB;
    const totalBytes = files.reduce((sum, file) => sum + (file.size || 0), 0);
    const isHugeBatch = totalBytes > 150 * MB;

    // A fast round trip on the caller's active probe (e.g. timing the
    // backend warm-up call that already happens before an upload starts) is
    // solid evidence of low latency -- but only ever used as *positive*
    // evidence. A slow or missing reading just leaves this false rather than
    // being treated as evidence of a bad connection, since a slow warm-up
    // call is just as likely to be a cold-started backend container as an
    // actually slow network.
    let probedLowLatency = false;
    if (!hasNetworkInfo && !isMobileViewport && !coarsePointer) {
        const observed = measureObservedDownlinkMbps();
        if (observed !== null) {
            downlink = observed;
            hasNetworkInfo = true;
        }
        if (typeof options?.measuredRttMs === 'number' && options.measuredRttMs > 0 && options.measuredRttMs < ACTIVE_PROBE_RTT_FAST_MS) {
            probedLowLatency = true;
        }
    }

    // Bigger batches pay relatively more in per-block commit overhead, so
    // floor the chunk size regardless of which tier below is picked -- this
    // never reduces fileParallelism, it only affects chunking.
    const withChunkFloor = (profile: UploadProfile): UploadProfile => (
        isHugeBatch && profile.chunkSizeBytes < 16 * MB
            ? { ...profile, chunkSizeBytes: 16 * MB }
            : profile
    );

    if (connection?.saveData || effectiveType === 'slow-2g' || effectiveType === '2g') {
        return { fileParallelism: 1, chunkSizeBytes: 1 * MB, reason: 'data saver or very slow network' };
    }
    if (hasNetworkInfo && (rtt >= 400 || downlink < 1.5)) {
        return { fileParallelism: 1, chunkSizeBytes: 1 * MB, reason: 'high latency or congested network' };
    }
    if (effectiveType === '3g' || connectionType === 'cellular') {
        if (downlink >= 10 && effectiveType === '4g') {
            return withChunkFloor({ fileParallelism: 3, chunkSizeBytes: MAX_BACKEND_UPLOAD_CHUNK_BYTES, reason: 'fast cellular with moderate parallelism' });
        }
        return { fileParallelism: 1, chunkSizeBytes: 2 * MB, reason: 'mobile or slow network' };
    }
    if (isMobileViewport || coarsePointer || lowMemory) {
        return withChunkFloor({ fileParallelism: hasLargeFiles ? 1 : 2, chunkSizeBytes: 2 * MB, reason: 'mobile device profile' });
    }
    if (!hasNetworkInfo) {
        return withChunkFloor({
            fileParallelism: hasLargeFiles ? 6 : 8,
            chunkSizeBytes: MAX_BACKEND_UPLOAD_CHUNK_BYTES,
            reason: 'no network info available, assuming desktop wifi or ethernet',
        });
    }
    if (downlink >= 10 && ((rtt > 0 && rtt < 100 && (isEthernet || isWifi)) || probedLowLatency)) {
        return withChunkFloor({
            fileParallelism: hasLargeFiles ? 12 : 20,
            chunkSizeBytes: MAX_BACKEND_UPLOAD_CHUNK_BYTES,
            reason: probedLowLatency && !(rtt > 0 && rtt < 100)
                ? 'fast connection (measured round trip)'
                : 'reliable wired or strong wifi connection',
        });
    }
    if (effectiveType === '4g' && downlink >= 20 && !hasLargeFiles) {
        return withChunkFloor({ fileParallelism: 12, chunkSizeBytes: MAX_BACKEND_UPLOAD_CHUNK_BYTES, reason: 'fast network' });
    }
    if (downlink >= 8 && !hasLargeFiles) {
        return withChunkFloor({ fileParallelism: 6, chunkSizeBytes: MAX_BACKEND_UPLOAD_CHUNK_BYTES, reason: 'good network' });
    }
    if (hasLargeFiles) {
        return withChunkFloor({ fileParallelism: 16, chunkSizeBytes: MAX_BACKEND_UPLOAD_CHUNK_BYTES, reason: 'large files' });
    }
    return withChunkFloor(DEFAULT_UPLOAD_PROFILE);
};

