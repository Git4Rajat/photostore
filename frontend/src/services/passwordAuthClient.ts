// Single-owner password authentication client (AUTH_MODE=password).
//
// Talks to the backend /auth/* endpoints and stores the signed session token
// in localStorage under the SAME key the HTTP clients already attach as a
// Bearer token (`photostore.passwordAuthToken`), so authenticated API calls
// work without any further wiring.

import { getRuntimeConfig } from '../config/appConfig';

const TOKEN_KEY = 'photostore.passwordAuthToken';
// A non-sensitive hint (the owner's email) kept across logout so the login form
// can always show and pre-fill the email field instead of relying on the
// browser's password-manager autofill, which only appears once a credential is
// saved and disappears when the cache is cleared.
const EMAIL_HINT_KEY = 'photostore.ownerEmail';

export const getEmailHint = (): string => {
    try {
        return window.localStorage.getItem(EMAIL_HINT_KEY) || '';
    } catch {
        return '';
    }
};

const setEmailHint = (email: string): void => {
    try {
        if (email) {
            window.localStorage.setItem(EMAIL_HINT_KEY, email);
        }
    } catch {
        // ignore storage failures
    }
};

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

// Shared session-token setter used by the library endpoints (switch/leave/
// accept/change-password) and the Entra token exchange, all of which re-issue
// the Photostore session token that every API call attaches as its Bearer.
export const setSessionToken = (token: string): void => setToken(token);

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

// The account id (`sub` claim) the current session token belongs to. Used to
// detect when a cached token no longer matches the signed-in account.
export const getSubjectFromToken = (): string => {
    const token = getToken();
    if (!token) {
        return '';
    }
    return String(decodePayload(token)?.sub || '');
};

const parseError = async (response: Response, fallback: string): Promise<string> => {
    try {
        const body = await response.json();
        return body?.error || fallback;
    } catch {
        return fallback;
    }
};

export const login = async (email: string, password: string): Promise<void> => {
    const response = await fetch(url('/auth/login'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email, password }),
    });
    if (!response.ok) {
        throw new Error(await parseError(response, 'Incorrect password.'));
    }
    const body = await response.json();
    if (!body?.token) {
        throw new Error('Login did not return a session token.');
    }
    setToken(body.token);
    // Remember the authoritative owner email the backend resolved (falling back
    // to what the user typed) so the field stays pre-filled on the next visit.
    setEmailHint(String(body.email || email || ''));
};

export const logout = (): void => {
    clearToken();
};

export const requestPasswordReset = async (email = ''): Promise<void> => {
    // The backend is single-owner and always responds 200 to avoid leaking
    // configuration/state; the email is sent for parity but the server resolves
    // the owner address itself.
    await fetch(url('/auth/forgot'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(email ? { email } : {}),
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
    // The server bumps the account's token version (invalidating other sessions)
    // and returns a fresh token for THIS session so the user stays signed in.
    try {
        const body = await response.json();
        if (body?.token) {
            setToken(String(body.token));
        }
    } catch {
        // No token in the response is fine; the current one remains valid.
    }
};
