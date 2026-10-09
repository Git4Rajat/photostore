import { getRuntimeConfig } from '../config/appConfig';
import { createHttpClient, requestJson } from './httpClient';
import { configureBackendStatus } from './backendStatus';
import { perf } from './perf';
const runtimeConfig = getRuntimeConfig();
const env = import.meta.env as Record<string, string | undefined>;
const apiUrl =
    runtimeConfig.apiBaseUrl ||
    env.VITE_API_BASE_URL ||
    env.VITE_API_URL ||
    env.VITE_FUNCTION_APP_URL ||
    env.REACT_APP_API_BASE_URL ||
    env.REACT_APP_API_URL ||
    env.REACT_APP_FUNCTION_APP_URL ||
    (import.meta.env.MODE === 'development' ? 'http://127.0.0.1:5001' : '');
const uploadUrl =
    runtimeConfig.uploadBaseUrl ||
    env.VITE_UPLOAD_BASE_URL ||
    env.REACT_APP_UPLOAD_BASE_URL ||
    apiUrl;
// Falls back to apiUrl when no dedicated indexer container app is deployed for
// this environment -- see routes/tools.py's APP_ROLE=indexer split.
const indexerUrl =
    runtimeConfig.indexerApiBaseUrl ||
    env.VITE_INDEXER_API_BASE_URL ||
    env.REACT_APP_INDEXER_API_BASE_URL ||
    apiUrl;
// Same fallback pattern as indexerUrl -- see routes/admin.py's APP_ROLE=recovery split.
const recoveryUrl =
    runtimeConfig.recoveryApiBaseUrl ||
    env.VITE_RECOVERY_API_BASE_URL ||
    env.REACT_APP_RECOVERY_API_BASE_URL ||
    apiUrl;
// Same fallback pattern as indexerUrl -- see routes/people.py|library.py|public.py's APP_ROLE=archive split.
const archiveUrl =
    runtimeConfig.archiveApiBaseUrl ||
    env.VITE_ARCHIVE_API_BASE_URL ||
    env.REACT_APP_ARCHIVE_API_BASE_URL ||
    apiUrl;

if (!apiUrl && import.meta.env.MODE !== 'development') {
    console.warn(
        'No API base URL configured. Set REACT_APP_API_BASE_URL or deploy env.js with your Container App endpoint.'
    );
}

const API_BASE_URL = apiUrl || '';
const UPLOAD_BASE_URL = uploadUrl || '';
const INDEXER_BASE_URL = indexerUrl || '';
const RECOVERY_BASE_URL = recoveryUrl || '';
const ARCHIVE_BASE_URL = archiveUrl || '';

// Path prefixes exclusively owned by the `archive` container app (library,
// public share-link routes -- see library.py|public.py's APP_ROLE=archive
// split). resolveApiUrl uses this so callers that already have a raw path
// (e.g. a public album's thumbnail URL) resolve to the right origin without
// every call site having to know which service serves which prefix.
//
// people/faces ('/api/persons', '/api/faces', '/api/people', '/persons',
// '/people') moved off this list 2026-10-08 -- routes/people.py's
// APP_ROLE=archive split was undone (people_bp moved back to 'core'), so
// those paths now resolve to API_BASE_URL via the plain get/post, not
// getArchive/postArchive. See faceService.ts, store.tsx, localPeopleIndex.ts,
// faceMediaCache.ts for the call sites that switched off getArchive/postArchive.
const ARCHIVE_PATH_PREFIXES = ['/api/library', '/public', '/api/public'];

export const resolveApiUrl = (url?: string): string => {
    if (!url) {
        return '';
    }
    if (/^https?:\/\//i.test(url)) {
        return url;
    }
    const normalized = url.startsWith('/') ? url : `/${url}`;
    const base = ARCHIVE_PATH_PREFIXES.some((prefix) => normalized.startsWith(prefix)) ? ARCHIVE_BASE_URL : API_BASE_URL;
    if (!base) {
        return url;
    }
    return `${base.replace(/\/$/, '')}/${url.replace(/^\/+/, '')}`;
};

const apiClient = createHttpClient(API_BASE_URL);
const uploadClient = createHttpClient(UPLOAD_BASE_URL);
const indexerClient = createHttpClient(INDEXER_BASE_URL);
const recoveryClient = createHttpClient(RECOVERY_BASE_URL);
const archiveClient = createHttpClient(ARCHIVE_BASE_URL);

// Give the app-wide availability tracker the absolute /health URL so its
// recovery probes hit the API origin (not the SPA origin) when a base URL is
// configured, and same-origin in local dev.
configureBackendStatus({ healthUrl: resolveApiUrl('health') });

// Kept for backwards compatibility.
const LOCAL_USER_KEY = 'photostore.localUserId';

const setDefaultHeader = (userId: string | null) => {
    // apiClient/indexerClient/recoveryClient/archiveClient, matching the existing
    // (pre-indexer-split) behavior of leaving uploadClient out of this -- not
    // touching that here, unrelated to the indexer/recovery/archive splits.
    for (const client of [apiClient, indexerClient, recoveryClient, archiveClient]) {
        const headers = client.defaults.headers as Record<string, string | undefined>;
        if (userId) {
            headers['X-User-ID'] = userId;
        } else {
            delete headers['X-User-ID'];
        }
    }
};

export const setUserId = (userId: string | null) => {
    if (typeof window === 'undefined') {
        return;
    }
    if (userId) {
        try {
            localStorage.setItem(LOCAL_USER_KEY, userId);
        } catch (e) {
            // ignore
        }
        setDefaultHeader(userId);
    } else {
        try {
            localStorage.removeItem(LOCAL_USER_KEY);
        } catch (e) {
            // ignore
        }
        setDefaultHeader(null);
    }
};

// Initialize from local storage if present
try {
    if (typeof window !== 'undefined') {
        const stored = localStorage.getItem(LOCAL_USER_KEY);
        if (stored) {
            setDefaultHeader(stored);
        }
    }
} catch (e) {
    // ignore
}

// Instrumentation reports go to the main API origin; registered here (not imported by perf.ts)
// because the HTTP client already imports perf.
perf.setSender((body) => requestJson(apiClient, 'post', '/api/perf/client', body, { singleAttempt: true, timeout: 15000 }));

export const get = async <T = any>(url: string, config?: Parameters<typeof apiClient.get>[1]) => requestJson<T>(apiClient, 'get', url, undefined, config);
export const getUpload = async <T = any>(url: string, config?: Parameters<typeof uploadClient.get>[1]) => requestJson<T>(uploadClient, 'get', url, undefined, config);
export const post = async <T = any, D = unknown>(url: string, data: D, config?: Parameters<typeof apiClient.post>[2]) => requestJson<T>(apiClient, 'post', url, data, config);
export const postUpload = async <T = any, D = unknown>(url: string, data: D) => requestJson<T>(uploadClient, 'post', url, data);
export const getUploadJson = async <T = any>(url: string) => requestJson<T>(uploadClient, 'get', url);
export const postUploadJson = async <T = any, D = unknown>(url: string, data: D) => requestJson<T>(uploadClient, 'post', url, data);
export const getTools = async <T = any>(url: string, config?: Parameters<typeof indexerClient.get>[1]) => requestJson<T>(indexerClient, 'get', url, undefined, config);
export const postTools = async <T = any, D = unknown>(url: string, data: D) => requestJson<T>(indexerClient, 'post', url, data);
export const getAdmin = async <T = any>(url: string, config?: Parameters<typeof recoveryClient.get>[1]) => requestJson<T>(recoveryClient, 'get', url, undefined, config);
export const postAdmin = async <T = any, D = unknown>(url: string, data: D) => requestJson<T>(recoveryClient, 'post', url, data);
export const getExtras = async <T = any>(url: string, config?: Parameters<typeof archiveClient.get>[1]) => requestJson<T>(archiveClient, 'get', url, undefined, config);
export const postExtras = async <T = any, D = unknown>(url: string, data: D, config?: Parameters<typeof archiveClient.post>[2]) => requestJson<T>(archiveClient, 'post', url, data, config);
