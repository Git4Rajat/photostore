import { sessionScope } from './sessionScope';
import axios, { type AxiosHeaders, type AxiosRequestConfig, type InternalAxiosRequestConfig } from 'axios';
import { getAccessToken, isAuthEnabled } from './authClient';
import { reportBackendReachable, reportBackendUnreachable } from './backendStatus';
import { classifyApiError, newRequestId } from './apiError';
import { reportUnauthorized } from './sessionExpiry';
import { parseServerTiming, perf, perfEnabled, perfNow, requestKey, stripQuery } from './perf';

const PERF_REPORT_PATH = '/api/perf/client';

const normalizePath = (url: string): string => {
    if (/^https?:\/\//i.test(url)) {
        return url;
    }
    return `/${url.replace(/^\/+/, '')}`;
};

export const createHttpClient = (baseURL: string, timeout = 600000) => {
    const client = axios.create({
        baseURL,
        timeout,
        withCredentials: false,
    });

    const attachAuth = async (config: InternalAxiosRequestConfig) => {
        const expectedScope = (config as InternalAxiosRequestConfig & { expectedSessionScope?: string }).expectedSessionScope ?? sessionScope();
        const assertScope = () => {
            if (expectedScope !== sessionScope()) throw new axios.CanceledError('The active account or library changed.');
        };
        assertScope();
        const passwordToken = typeof window !== 'undefined'
            ? window.localStorage.getItem('photostore.passwordAuthToken')
            : '';
        const headers = config.headers as AxiosHeaders;
        if (passwordToken) {
            headers.set?.('Authorization', `Bearer ${passwordToken}`);
            if (typeof headers.set !== 'function') {
                headers.Authorization = `Bearer ${passwordToken}`;
            }
            return config;
        }

        if (isAuthEnabled()) {
            const token = await getAccessToken();
            assertScope();
            if (token) {
                headers.set?.('Authorization', `Bearer ${token}`);
                if (typeof headers.set !== 'function') {
                    headers.Authorization = `Bearer ${token}`;
                }
            }
        }

        return config;
    };

    client.interceptors.request.use(attachAuth);
    return client;
};

// In-flight GET dedup: identical concurrent GETs (double-fired effects, several
// components loading the same list, rapid re-clicks) share one network request
// instead of stacking duplicate calls onto the backend. Entries are removed as
// soon as the request settles, so this never serves stale data — it only
// coalesces requests that overlap in time. Requests with abort signals opt out
// (sharing would let one caller cancel another's request).
const inFlightGets = new Map<string, Promise<unknown>>();

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

// Reads can retry through scale-from-zero failures. A missing response or a
// 503 cannot prove that a mutation never committed: the reply may have been
// lost, or the app may have failed after writing. Mutation-specific protocols
// (such as upload admission/finalization) own their replay identities/retries;
// this shared transport must not replay arbitrary writes.
const COLD_START_RETRIES = 14;
const COLD_START_BASE_DELAY_MS = 800;
const COLD_START_MAX_DELAY_MS = 8000;

const isRetriableColdStart = (error: unknown, method: string): boolean => {
    if (method !== 'get' || !axios.isAxiosError(error)) {
        return false;
    }
    if (error.code === 'ERR_CANCELED') {
        return false;
    }
    const status = error.response?.status;
    if (status === undefined) {
        return true;
    }
    if (status === 503) {
        return true;
    }
    if (status === 502 || status === 504) {
        return true;
    }
    return false;
};

// Whether a *terminal* failure (retries already exhausted) means the backend
// was unreachable, as opposed to an ordinary app error (4xx, or a 5xx the app
// itself produced). Used to drive the app-wide "backend unavailable" banner.
// Unlike isRetriableColdStart this treats gateway failures as unreachable for every
// method — for the banner we only care that the app couldn't be reached, not
// whether the request was safe to auto-retry.
const isBackendUnreachable = (error: unknown): boolean => {
    if (!axios.isAxiosError(error)) {
        return false;
    }
    if (error.code === 'ERR_CANCELED') {
        return false;
    }
    const status = error.response?.status;
    if (status === undefined) {
        return true;
    }
    return status === 502 || status === 503 || status === 504;
};

const isCanceled = (error: unknown): boolean =>
    axios.isAxiosError(error) && error.code === 'ERR_CANCELED';

// Set by backgroundRequestQueue.ts: that queue is its own retry authority
// (requeue-to-back on a retryable failure, drop on a terminal one) and needs
// a single attempt per run to make that decision on, not this cold-start
// loop's ~90s budget hidden underneath it -- two independent retry layers
// stacked on each other just compounds load on a backend that's already
// struggling, which is the opposite of what the queue exists to prevent.
// Every other caller is unaffected; this only skips the loop when explicitly
// opted in.
export interface RequestConfig extends AxiosRequestConfig {
    singleAttempt?: boolean;
    expectedSessionScope?: string;
}

export const requestJson = async <T = any>(
    client: ReturnType<typeof createHttpClient>,
    method: 'get' | 'post' | 'put' | 'delete',
    url: string,
    data?: unknown,
    config?: RequestConfig,
): Promise<T> => {
    // One correlation id for the whole call, shared across cold-start retries,
    // so a user-visible "ref" ties to a single logical request.
    config = { ...config, expectedSessionScope: config?.expectedSessionScope ?? sessionScope() };
    const requestId = newRequestId();
    const traced = perfEnabled() && !url.includes(PERF_REPORT_PATH);
    const perfKey = traced ? requestKey(method, normalizePath(url), data) : '';
    const recordPerf = (
        started: number, attempt: number, status: number, ok: boolean, rid: string,
        headers?: Record<string, unknown>, payload?: unknown, coalesced = false,
    ) => {
        if (!traced) return;
        const timing = parseServerTiming(String(headers?.['server-timing'] ?? ''));
        let bytes = Number(headers?.['content-length'] ?? 0) || 0;
        if (!bytes && payload !== undefined && payload !== null) {
            try { bytes = typeof payload === 'string' ? payload.length : JSON.stringify(payload).length; } catch { bytes = 0; }
        }
        perf.recordRequest({
            method: method.toUpperCase(), path: stripQuery(normalizePath(url)), status, ok,
            ms: perfNow() - started, bytes, rid, attempt, coalesced, view: perf.currentView, at: perfNow(),
            serverMs: timing.app, storageMs: timing.storage,
        }, perfKey);
    };
    const performRequest = async (): Promise<T> => {
        for (let attempt = 0; ; attempt += 1) {
            // A fresh id per attempt: the backend logs one line per attempt, and a retry that
            // reused the id would be indistinguishable from a duplicate request.
            const rid = traced ? perf.nextRequestId() : '';
            const started = perfNow();
            const tracedConfig: RequestConfig | undefined = traced
                ? {
                    ...config,
                    headers: {
                        ...(config?.headers as Record<string, string> | undefined),
                        'X-Request-ID': rid, 'X-Client-View': perf.currentView, 'X-Client-Session': perf.session,
                    },
                }
                : config;
            try {
                const response = method === 'get'
                    ? await client.get<T>(normalizePath(url), tracedConfig)
                    : method === 'post'
                        ? await client.post<T>(normalizePath(url), data, tracedConfig)
                        : method === 'put'
                            ? await client.put<T>(normalizePath(url), data, tracedConfig)
                            : await client.delete<T>(normalizePath(url), tracedConfig);
                if (config?.expectedSessionScope !== sessionScope()) throw new axios.CanceledError('The active account or library changed.');
                recordPerf(started, attempt, response.status, true, rid, response.headers as Record<string, unknown>, response.data);
                // A real response (any status) proves the backend is up; clear
                // any outstanding "backend unavailable" state immediately.
                reportBackendReachable();
                return response.data;
            } catch (error: unknown) {
                if (axios.isAxiosError(error)) {
                    recordPerf(started, attempt, error.response?.status ?? 0, false, rid, error.response?.headers as Record<string, unknown> | undefined);
                }
                if (!config?.singleAttempt && attempt < COLD_START_RETRIES && isRetriableColdStart(error, method)) {
                    // First sign of trouble: surface the "waking up" banner right
                    // away rather than only after the full ~90s budget below is
                    // exhausted, so a long wake doesn't just look like a hang.
                    // reportBackendUnreachable is a no-op if we're already
                    // offline, and reportBackendReachable (above, on success)
                    // clears it the moment any request gets through.
                    if (attempt === 0) {
                        reportBackendUnreachable(classifyApiError(error, requestId).message);
                    }
                    // Capped exponential backoff (0.8s, 1.6s, 3.2s, 6.4s, then
                    // 8s) spanning ~90s total to span a real scale-from-zero
                    // wake, not just a warm-replica hiccup.
                    await sleep(Math.min(COLD_START_MAX_DELAY_MS, COLD_START_BASE_DELAY_MS * 2 ** attempt));
                    continue;
                }
                // A 401 means the session token is missing/invalid (the backend
                // uses 403 for permission failures), so the app can't recover in
                // place -- hand off to the global session-expiry handler, which
                // clears the dead session and reloads into login exactly once.
                // Guarded there against public routes and no-auth deployments.
                // The perf-telemetry beacon is exempt: it's best-effort background
                // reporting, and a single dropped/missing-header beacon (seen on a
                // mobile network handover) is not evidence the session is dead --
                // it must not force-logout an otherwise active session.
                if (axios.isAxiosError(error) && error.response?.status === 401 && !url.includes(PERF_REPORT_PATH)) {
                    reportUnauthorized();
                }
                // Terminal outcome: classify once, then feed the app-wide
                // availability tracker and throw the typed error to the caller.
                const apiError = classifyApiError(error, requestId);
                if (isBackendUnreachable(error)) {
                    reportBackendUnreachable(apiError.message);
                } else if (!isCanceled(error)) {
                    // The app answered (an ordinary 4xx/5xx), so it is reachable.
                    reportBackendReachable();
                }
                throw apiError;
            }
        }
    };

    if (method !== 'get' || config?.signal) {
        return performRequest();
    }

    const dedupeKey = `${config.expectedSessionScope}|${client.defaults.baseURL || ''}|${normalizePath(url)}`;
    const existing = inFlightGets.get(dedupeKey);
    if (existing) {
        // Shared an already in-flight identical GET: no network cost, but record it so
        // "who asked for this twice at once" is visible.
        recordPerf(perfNow(), 0, 0, true, '', undefined, undefined, true);
        return existing as Promise<T>;
    }
    const request = performRequest().finally(() => {
        inFlightGets.delete(dedupeKey);
    });
    inFlightGets.set(dedupeKey, request);
    return request;
};
