import axios, { type AxiosHeaders, type AxiosRequestConfig, type InternalAxiosRequestConfig } from 'axios';
import { getAccessToken, isAuthEnabled } from './authClient';
import { reportBackendReachable, reportBackendUnreachable } from './backendStatus';
import { classifyApiError, newRequestId } from './apiError';

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

// The backend runs scale-to-zero on Azure Container Apps, so after an idle
// period the first request(s) hit the ingress with no healthy replica. The
// ingress answers with a 503 (or the connection fails outright) *before* the
// request ever reaches the app — which also means no CORS headers, so the
// browser mislabels it as a "CORS policy" error. These failures are transient:
// the same request wakes the replica and a retry moments later succeeds. We
// retry only failures that provably never executed server-side, so retrying a
// mutation (merge, not-face, delete) can't double-apply it:
//   - no response at all (connection refused / reset / "CORS" network error)
//   - HTTP 503 from the ingress (no replica available)
// A 502/504 (gateway reached the app but it timed out) is retried only for
// idempotent GETs.
const COLD_START_RETRIES = 4;
const COLD_START_BASE_DELAY_MS = 800;

const isRetriableColdStart = (error: unknown, method: string): boolean => {
    if (!axios.isAxiosError(error)) {
        return false;
    }
    if (error.code === 'ERR_CANCELED') {
        return false;
    }
    const status = error.response?.status;
    if (status === undefined) {
        // No response: connection failure before reaching the app. Safe to
        // retry for any method — the request never executed.
        return true;
    }
    if (status === 503) {
        // Ingress rejected before dispatching to the app: safe for any method.
        return true;
    }
    if (status === 502 || status === 504) {
        return method === 'get';
    }
    return false;
};

// Whether a *terminal* failure (retries already exhausted) means the backend
// was unreachable, as opposed to an ordinary app error (4xx, or a 5xx the app
// itself produced). Used to drive the app-wide "backend unavailable" banner.
// Unlike isRetriableColdStart this treats 502/504 as unreachable for every
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

export const requestJson = async <T = any>(
    client: ReturnType<typeof createHttpClient>,
    method: 'get' | 'post' | 'put' | 'delete',
    url: string,
    data?: unknown,
    config?: AxiosRequestConfig,
): Promise<T> => {
    // One correlation id for the whole call, shared across cold-start retries,
    // so a user-visible "ref" ties to a single logical request.
    const requestId = newRequestId();
    const performRequest = async (): Promise<T> => {
        for (let attempt = 0; ; attempt += 1) {
            try {
                const response = method === 'get'
                    ? await client.get<T>(normalizePath(url), config)
                    : method === 'post'
                        ? await client.post<T>(normalizePath(url), data, config)
                        : method === 'put'
                            ? await client.put<T>(normalizePath(url), data, config)
                            : await client.delete<T>(normalizePath(url), config);
                // A real response (any status) proves the backend is up; clear
                // any outstanding "backend unavailable" state immediately.
                reportBackendReachable();
                return response.data;
            } catch (error: unknown) {
                if (attempt < COLD_START_RETRIES && isRetriableColdStart(error, method)) {
                    // Exponential backoff (0.8s, 1.6s, 3.2s, 6.4s) to span a
                    // typical cold-start window without hammering the ingress.
                    await sleep(COLD_START_BASE_DELAY_MS * 2 ** attempt);
                    continue;
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

    const dedupeKey = `${client.defaults.baseURL || ''}|${normalizePath(url)}`;
    const existing = inFlightGets.get(dedupeKey);
    if (existing) {
        return existing as Promise<T>;
    }
    const request = performRequest().finally(() => {
        inFlightGets.delete(dedupeKey);
    });
    inFlightGets.set(dedupeKey, request);
    return request;
};
