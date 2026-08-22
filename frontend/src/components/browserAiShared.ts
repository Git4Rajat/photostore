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
// Only used to sanity-check (never replace) a live connection.downlink
// reading -- caps how old a Resource Timing entry can be before we stop
// trusting it as still representative of the current network state.
const RESOURCE_TIMING_RECENT_WINDOW_MS = 60_000;

// navigator.connection (the Network Information API) is unavailable in
// Safari (desktop and iOS) and Firefox, so those browsers would otherwise
// always fall through to the fixed "assume it's fine" guess below regardless
// of how slow their actual link is. Mine the Resource Timing entries for
// assets the page already fetched (JS bundles, images) to get a real
// observed transfer rate instead of guessing -- transferSize is 0 for cache
// hits and for cross-origin entries without a Timing-Allow-Origin header, so
// this only reflects entries that actually hit the network.
//
// Also used (with maxAgeMs set) as a cross-check even when connection.downlink
// IS available: Chrome's downlink is a coarse, traffic-history-based estimate
// from its Network Quality Estimator, not a link-speed test, and commonly
// under-reports an otherwise-idle fast connection. maxAgeMs guards against the
// opposite mistake there -- using a since-stale reading (e.g. from a fast wifi
// network the page loaded on, before the user switched to slow cellular) to
// override a live, lower, and now-correct connection.downlink.
const measureObservedDownlinkMbps = (maxAgeMs?: number): number | null => {
    if (typeof performance === 'undefined' || typeof performance.getEntriesByType !== 'function') {
        return null;
    }
    const now = performance.now();
    let bestBytes = 0;
    let bestDurationMs = 0;
    for (const entry of performance.getEntriesByType('resource') as PerformanceResourceTiming[]) {
        const bytes = entry.transferSize || 0;
        const durationMs = entry.responseEnd - entry.requestStart;
        if (bytes < RESOURCE_TIMING_MIN_BYTES || durationMs < RESOURCE_TIMING_MIN_DURATION_MS) {
            continue;
        }
        if (typeof maxAgeMs === 'number' && (now - entry.startTime) > maxAgeMs) {
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

// Shared with the upload resume-cache (AppServicesProvider.tsx): mobile
// Safari's per-tab memory ceiling is far tighter than desktop's, so the same
// signals that cap upload fileParallelism below are reused there to decide
// when background work needs a tighter leash too.
export const isConstrainedUploadDevice = (): boolean => {
    if (typeof navigator === 'undefined' || typeof window === 'undefined') {
        return false;
    }
    const deviceMemory = (navigator as Navigator & { deviceMemory?: number }).deviceMemory;
    const isMobileViewport = window.matchMedia?.('(max-width: 760px)').matches || false;
    const coarsePointer = window.matchMedia?.('(pointer: coarse)').matches || false;
    const lowMemory = typeof deviceMemory === 'number' && deviceMemory <= 4;
    return isMobileViewport || coarsePointer || lowMemory;
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
    const reportedDownlink = Number(connection?.downlink || 0);
    const rtt = Number(connection?.rtt || 0);
    const isWifi = connectionType === 'wifi';
    const isEthernet = connectionType === 'ethernet';
    let hasNetworkInfo = Boolean(connection && Number.isFinite(reportedDownlink) && reportedDownlink > 0);
    const isMobileViewport = window.matchMedia?.('(max-width: 760px)').matches || false;
    const coarsePointer = window.matchMedia?.('(pointer: coarse)').matches || false;
    const lowMemory = typeof nav.deviceMemory === 'number' && nav.deviceMemory <= 4;
    const largestFile = files.reduce((max, file) => Math.max(max, file.size || 0), 0);
    const hasLargeFiles = largestFile >= 40 * MB;
    const totalBytes = files.reduce((sum, file) => sum + (file.size || 0), 0);
    const isHugeBatch = totalBytes > 150 * MB;

    // Always cross-check against a real observed transfer rate, even when
    // connection.downlink is already available -- see measureObservedDownlinkMbps
    // for why. When there's a live reading to cross-check against, only trust
    // *recent* Resource Timing entries and only ever raise the number, never
    // lower it (never contradict a live, lower, possibly-since-degraded
    // reading); with no live reading at all (Safari/Firefox), any real
    // observed transfer is better than the static guess, however old it is.
    // Mobile devices get this same cross-check now too (previously skipped
    // here entirely) -- a touch device on strong wifi deserves the same
    // chance to prove it via real evidence as desktop does, rather than
    // being blocked from ever reaching the fast tiers below purely on form
    // factor. lowMemory is still excluded from this, on purpose: a
    // genuinely memory-constrained device shouldn't get a bandwidth-earned
    // reason to raise parallelism when the real risk there is concurrent
    // Blob/hash memory pressure, not link speed.
    let downlink = reportedDownlink;
    if (!lowMemory) {
        const observed = measureObservedDownlinkMbps(hasNetworkInfo ? RESOURCE_TIMING_RECENT_WINDOW_MS : undefined);
        if (observed !== null) {
            downlink = hasNetworkInfo ? Math.max(downlink, observed) : observed;
            hasNetworkInfo = true;
        }
    }

    // Good evidence of low latency, from whichever source has it. rtt>0 means
    // the Network Information API actually reported a number (trust it as-is,
    // even a bad one, over a substitute); only fall back to the caller's
    // active probe (e.g. timing the backend warm-up call that already happens
    // before an upload starts) when the API gave us no rtt at all -- a slow or
    // missing probe reading just leaves this false rather than being treated
    // as evidence of a bad connection, since a slow warm-up call is just as
    // likely to be a cold-started backend container as an actually slow
    // network.
    const hasGoodApiRtt = rtt > 0 && rtt < 100;
    let lowLatencyEvidence = hasGoodApiRtt;
    if (!lowLatencyEvidence && rtt === 0 && !lowMemory
        && typeof options?.measuredRttMs === 'number' && options.measuredRttMs > 0 && options.measuredRttMs < ACTIVE_PROBE_RTT_FAST_MS) {
        lowLatencyEvidence = true;
    }

    // Cross-check a BAD reported rtt the same way downlink already is: a real
    // production upload ran at fileParallelism 1 for its entire ~28min
    // duration even though the earliest, pre-congestion round-trips in the
    // same capture measured 25-350ms -- well under the >=400ms this gate
    // treats as "high latency". connection.rtt is a coarse NQE estimate, not
    // a ping, and can misreport just like downlink can. Only ever LOWER a bad
    // reported rtt using a fast measured probe (never raise a good one into a
    // false negative) -- a SLOW probe still isn't used as evidence of bad
    // latency above, since that's just as likely to be a cold-started backend
    // as an actually slow link.
    let effectiveRtt = rtt;
    if (rtt >= 400 && !lowMemory
        && typeof options?.measuredRttMs === 'number' && options.measuredRttMs > 0 && options.measuredRttMs < ACTIVE_PROBE_RTT_FAST_MS) {
        effectiveRtt = options.measuredRttMs;
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
    if (hasNetworkInfo && (effectiveRtt >= 400 || downlink < 1.5)) {
        return { fileParallelism: 1, chunkSizeBytes: 1 * MB, reason: 'high latency or congested network' };
    }
    if (effectiveType === '3g' || connectionType === 'cellular') {
        if (downlink >= 10 && effectiveType === '4g') {
            return withChunkFloor({ fileParallelism: 3, chunkSizeBytes: MAX_BACKEND_UPLOAD_CHUNK_BYTES, reason: 'fast cellular with moderate parallelism' });
        }
        return { fileParallelism: 1, chunkSizeBytes: 2 * MB, reason: 'mobile or slow network' };
    }
    // A genuinely memory-constrained device (not just a touch/mobile form
    // factor) still gets capped hard regardless of connection quality -- the
    // risk there is concurrent Blob/hash memory pressure, which a fast link
    // doesn't fix.
    if (lowMemory) {
        return withChunkFloor({ fileParallelism: hasLargeFiles ? 1 : 2, chunkSizeBytes: 2 * MB, reason: 'low device memory' });
    }
    // Mobile/touch devices with no real signal either way (no Network
    // Information API -- true for all of iOS Safari and Chrome-on-iOS, since
    // iOS forces the WebKit engine regardless of browser -- and no
    // Resource Timing evidence yet) get a distinct, more modest fallback
    // than desktop's "assume wifi or ethernet" guess just below: a phone or
    // tablet with zero evidence could just as plausibly be on cellular,
    // where desktop's blind assumption is reasonable because a wired/wifi
    // desktop machine on cellular is physically unlikely. This is not a
    // ceiling -- real evidence (the cross-checked downlink/rtt above, or a
    // measured cellular effectiveType) still reaches the same top tier
    // below as any other device; this only covers the true-zero-evidence
    // case.
    if ((isMobileViewport || coarsePointer) && !hasNetworkInfo) {
        return withChunkFloor({ fileParallelism: hasLargeFiles ? 2 : 4, chunkSizeBytes: 2 * MB, reason: 'mobile device, no network signal' });
    }
    if (!hasNetworkInfo) {
        return withChunkFloor({
            fileParallelism: hasLargeFiles ? 6 : 8,
            chunkSizeBytes: MAX_BACKEND_UPLOAD_CHUNK_BYTES,
            reason: 'no network info available, assuming desktop wifi or ethernet',
        });
    }
    if (downlink >= 10 && lowLatencyEvidence) {
        // connection.type ('wifi'/'ethernet') is only ever used for the log
        // message, never as a gate -- it's "partial support, experimental" on
        // desktop Chrome and unavailable on Firefox/Safari. Reachable by
        // mobile/touch devices too now (deliberately) as long as they clear
        // this same bar with real evidence -- only lowMemory, cellular, and
        // no-signal-at-all mobile were filtered out above; a touch device on
        // demonstrably fast, low-latency wifi lands here same as desktop.
        return withChunkFloor({
            fileParallelism: hasLargeFiles ? 12 : 20,
            chunkSizeBytes: MAX_BACKEND_UPLOAD_CHUNK_BYTES,
            reason: hasGoodApiRtt && (isEthernet || isWifi)
                ? 'reliable wired or strong wifi connection'
                : 'fast connection (measured)',
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

