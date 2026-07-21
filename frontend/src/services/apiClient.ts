import { getRuntimeConfig } from '../config/appConfig';
import { createHttpClient, requestJson } from './httpClient';
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

if (!apiUrl && import.meta.env.MODE !== 'development') {
    console.warn(
        'No API base URL configured. Set REACT_APP_API_BASE_URL or deploy env.js with your Container App endpoint.'
    );
}

const API_BASE_URL = apiUrl || '';
const UPLOAD_BASE_URL = uploadUrl || '';

export const resolveApiUrl = (url?: string): string => {
    if (!url) {
        return '';
    }
    if (/^https?:\/\//i.test(url)) {
        return url;
    }
    if (!API_BASE_URL) {
        return url;
    }
    return `${API_BASE_URL.replace(/\/$/, '')}/${url.replace(/^\/+/, '')}`;
};

const apiClient = createHttpClient(API_BASE_URL);
const uploadClient = createHttpClient(UPLOAD_BASE_URL);

// Kept for backwards compatibility.
const LOCAL_USER_KEY = 'photostore.localUserId';

const setDefaultHeader = (userId: string | null) => {
    const headers = apiClient.defaults.headers as Record<string, string | undefined>;
    if (userId) {
        headers['X-User-ID'] = userId;
        return;
    }
    delete headers['X-User-ID'];
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
