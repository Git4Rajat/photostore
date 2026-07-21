import axios, { type AxiosHeaders, type AxiosRequestConfig, type InternalAxiosRequestConfig } from 'axios';
import { getAccessToken, isAuthEnabled } from './authClient';

type ErrorLike = {
    message?: unknown;
    error?: unknown;
    response?: {
        data?: unknown;
        status?: unknown;
    };
    code?: unknown;
};

const formatErrorPayload = (payload: unknown): string => {
    if (payload == null) {
        return '';
    }
    if (typeof payload === 'string') {
        return payload;
    }
    if (typeof payload === 'number' || typeof payload === 'boolean') {
        return String(payload);
    }
    if (Array.isArray(payload)) {
        return payload.map((item) => formatErrorPayload(item)).filter(Boolean).join(', ');
    }
    if (typeof payload === 'object') {
        const record = payload as { error?: unknown; message?: unknown };
        const maybeMessage = record.error || record.message;
        if (typeof maybeMessage === 'string' && maybeMessage.trim()) {
            return maybeMessage;
        }
        try {
            return JSON.stringify(payload);
        } catch {
            return '[object]';
        }
    }
    return String(payload);
};

const getErrorMessage = (error: unknown): string => {
    if (axios.isAxiosError(error)) {
        return formatErrorPayload(error.response?.data) || error.message;
    }

    const maybeError = error as ErrorLike;
    const responseMessage = maybeError?.response?.data;
    const resolvedResponseMessage = formatErrorPayload(responseMessage);
    if (resolvedResponseMessage) {
        return resolvedResponseMessage;
    }

    if (typeof maybeError?.message === 'string' && maybeError.message.trim()) {
        return maybeError.message;
    }

    return formatErrorPayload(error);
};

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

export const requestJson = async <T = any>(
    client: ReturnType<typeof createHttpClient>,
    method: 'get' | 'post' | 'put' | 'delete',
    url: string,
    data?: unknown,
    config?: AxiosRequestConfig,
): Promise<T> => {
    try {
        const response = method === 'get'
            ? await client.get<T>(normalizePath(url), config)
            : method === 'post'
                ? await client.post<T>(normalizePath(url), data, config)
                : method === 'put'
                    ? await client.put<T>(normalizePath(url), data, config)
                    : await client.delete<T>(normalizePath(url), config);
        return response.data;
    } catch (error: unknown) {
        throw getErrorMessage(error);
    }
};
