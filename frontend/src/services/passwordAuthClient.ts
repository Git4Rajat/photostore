// Single-owner password authentication client (AUTH_MODE=password).
//
// Talks to the backend /auth/* endpoints and stores the signed session token
// in localStorage under the SAME key the HTTP clients already attach as a
// Bearer token (`photostore.passwordAuthToken`), so authenticated API calls
// work without any further wiring.

import { getRuntimeConfig } from '../config/appConfig';

const TOKEN_KEY = 'photostore.passwordAuthToken';

const apiBase = (): string => (getRuntimeConfig().apiBaseUrl || '').replace(/\/$/, '');

const url = (path: string): string => `${apiBase()}${path}`;

export const getToken = (): string | null => {
    try {
        return window.localStorage.getItem(TOKEN_KEY);
    } catch {
        return null;
    }
};

const setToken = (token: string): void => {
    try {
        window.localStorage.setItem(TOKEN_KEY, token);
    } catch {
        // ignore storage failures
    }
};

export const clearToken = (): void => {
    try {
        window.localStorage.removeItem(TOKEN_KEY);
    } catch {
        // ignore
    }
};

// Decode a JWT payload without verifying (verification is the server's job).
// JWT segments are base64url ('-'/'_', no padding), which atob() cannot decode
// directly, so normalize to standard base64 first.
const decodePayload = (token: string): Record<string, unknown> | null => {
    try {
        const segment = token.split('.')[1] || '';
        const base64 = segment.replace(/-/g, '+').replace(/_/g, '/')
            .padEnd(segment.length + ((4 - (segment.length % 4)) % 4), '=');
        return JSON.parse(atob(base64));
    } catch {
        return null;
    }
};

// Used only to check local expiry so we don't present an obviously-dead token.
const decodeExp = (token: string): number => {
    return Number(decodePayload(token)?.exp || 0);
};

export const hasValidSession = (): boolean => {
    const token = getToken();
    if (!token) {
        return false;
    }
    const exp = decodeExp(token);
    return exp > 0 && exp * 1000 > Date.now();
};

export const getEmailFromToken = (): string => {
    const token = getToken();
    if (!token) {
        return '';
    }
    return String(decodePayload(token)?.email || '');
};

const parseError = async (response: Response, fallback: string): Promise<string> => {
    try {
        const body = await response.json();
        return body?.error || fallback;
    } catch {
        return fallback;
    }
};

export const login = async (password: string): Promise<void> => {
    const response = await fetch(url('/auth/login'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password }),
    });
    if (!response.ok) {
        throw new Error(await parseError(response, 'Incorrect password.'));
    }
    const body = await response.json();
    if (!body?.token) {
        throw new Error('Login did not return a session token.');
    }
    setToken(body.token);
};

export const logout = (): void => {
    clearToken();
};

export const requestPasswordReset = async (): Promise<void> => {
    // The backend always responds 200 to avoid leaking configuration/state.
    await fetch(url('/auth/forgot'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
    });
};

export const resetPassword = async (token: string, newPassword: string): Promise<void> => {
    const response = await fetch(url('/auth/reset'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token, newPassword }),
    });
    if (!response.ok) {
        throw new Error(await parseError(response, 'This reset link is invalid or has expired.'));
    }
};

export const changePassword = async (currentPassword: string, newPassword: string): Promise<void> => {
    const token = getToken();
    const response = await fetch(url('/auth/change-password'), {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: JSON.stringify({ currentPassword, newPassword }),
    });
    if (!response.ok) {
        throw new Error(await parseError(response, 'Could not change password.'));
    }
};
