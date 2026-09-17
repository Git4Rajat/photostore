import { getRuntimeConfig } from '../config/appConfig';
import { createHttpClient, requestJson } from './httpClient';
import { configureBackendStatus } from './backendStatus';
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
// Falls back to apiUrl when no dedicated tools container app is deployed for
// this environment -- see routes/tools.py's APP_ROLE=tools split.
const toolsUrl =
    runtimeConfig.toolsApiBaseUrl ||
    env.VITE_TOOLS_API_BASE_URL ||
    env.REACT_APP_TOOLS_API_BASE_URL ||
    apiUrl;
// Same fallback pattern as toolsUrl -- see routes/admin.py's APP_ROLE=admin split.
const adminUrl =
    runtimeConfig.adminApiBaseUrl ||
    env.VITE_ADMIN_API_BASE_URL ||
    env.REACT_APP_ADMIN_API_BASE_URL ||
    apiUrl;
// Same fallback pattern as toolsUrl -- see routes/people.py|library.py|public.py's APP_ROLE=extras split.
const extrasUrl =
    runtimeConfig.extrasApiBaseUrl ||
    env.VITE_EXTRAS_API_BASE_URL ||
    env.REACT_APP_EXTRAS_API_BASE_URL ||
    apiUrl;

if (!apiUrl && import.meta.env.MODE !== 'development') {
    console.warn(
        'No API base URL configured. Set REACT_APP_API_BASE_URL or deploy env.js with your Container App endpoint.'
    );
}

const API_BASE_URL = apiUrl || '';
const UPLOAD_BASE_URL = uploadUrl || '';
const TOOLS_BASE_URL = toolsUrl || '';
const ADMIN_BASE_URL = adminUrl || '';
const EXTRAS_BASE_URL = extrasUrl || '';

// Path prefixes exclusively owned by the `extras` container app (people/
// faces, library, public share-link routes -- see routes/people.py|
// library.py|public.py's APP_ROLE=extras split). resolveApiUrl uses this so
// callers that already have a raw path (e.g. a face-crop cover URL, or a
// public album's thumbnail URL) resolve to the right origin without every
// call site having to know which service serves which prefix.
const EXTRAS_PATH_PREFIXES = ['/api/persons', '/api/faces', '/api/people', '/persons', '/people', '/api/library', '/public', '/api/public'];

export const resolveApiUrl = (url?: string): string => {
    if (!url) {
        return '';
    }
    if (/^https?:\/\//i.test(url)) {
        return url;
    }
    const normalized = url.startsWith('/') ? url : `/${url}`;
    const base = EXTRAS_PATH_PREFIXES.some((prefix) => normalized.startsWith(prefix)) ? EXTRAS_BASE_URL : API_BASE_URL;
    if (!base) {
        return url;
    }
    return `${base.replace(/\/$/, '')}/${url.replace(/^\/+/, '')}`;
};

const apiClient = createHttpClient(API_BASE_URL);
const uploadClient = createHttpClient(UPLOAD_BASE_URL);
const toolsClient = createHttpClient(TOOLS_BASE_URL);
const adminClient = createHttpClient(ADMIN_BASE_URL);
const extrasClient = createHttpClient(EXTRAS_BASE_URL);

// Give the app-wide availability tracker the absolute /health URL so its
// recovery probes hit the API origin (not the SPA origin) when a base URL is
// configured, and same-origin in local dev.
configureBackendStatus({ healthUrl: resolveApiUrl('health') });

// Kept for backwards compatibility.
const LOCAL_USER_KEY = 'photostore.localUserId';

const setDefaultHeader = (userId: string | null) => {
    // apiClient/toolsClient/adminClient/extrasClient, matching the existing
    // (pre-tools-split) behavior of leaving uploadClient out of this -- not
    // touching that here, unrelated to the tools/admin/extras splits.
    for (const client of [apiClient, toolsClient, adminClient, extrasClient]) {
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

export const get = async <T = any>(url: string, config?: Parameters<typeof apiClient.get>[1]) => requestJson<T>(apiClient, 'get', url, undefined, config);
export const getUpload = async <T = any>(url: string, config?: Parameters<typeof uploadClient.get>[1]) => requestJson<T>(uploadClient, 'get', url, undefined, config);
export const post = async <T = any, D = unknown>(url: string, data: D) => requestJson<T>(apiClient, 'post', url, data);
export const postUpload = async <T = any, D = unknown>(url: string, data: D) => requestJson<T>(uploadClient, 'post', url, data);
export const getUploadJson = async <T = any>(url: string) => requestJson<T>(uploadClient, 'get', url);
export const postUploadJson = async <T = any, D = unknown>(url: string, data: D) => requestJson<T>(uploadClient, 'post', url, data);
export const getTools = async <T = any>(url: string, config?: Parameters<typeof toolsClient.get>[1]) => requestJson<T>(toolsClient, 'get', url, undefined, config);
export const postTools = async <T = any, D = unknown>(url: string, data: D) => requestJson<T>(toolsClient, 'post', url, data);
export const getAdmin = async <T = any>(url: string, config?: Parameters<typeof adminClient.get>[1]) => requestJson<T>(adminClient, 'get', url, undefined, config);
export const postAdmin = async <T = any, D = unknown>(url: string, data: D) => requestJson<T>(adminClient, 'post', url, data);
export const getExtras = async <T = any>(url: string, config?: Parameters<typeof extrasClient.get>[1]) => requestJson<T>(extrasClient, 'get', url, undefined, config);
export const postExtras = async <T = any, D = unknown>(url: string, data: D) => requestJson<T>(extrasClient, 'post', url, data);
