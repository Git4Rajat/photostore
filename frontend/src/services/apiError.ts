import axios from 'axios';

// A single, typed representation of a failed API call. `httpClient.requestJson`
// throws one of these on every terminal failure (after its cold-start retries),
// so call sites no longer receive a bare string and can reason about *what kind*
// of failure happened instead of pattern-matching a message.
//
// `message` is a human-friendly, already-classified string safe to show a user
// (so `String(err)` and `err.message` at existing call sites automatically get
// the friendly copy). `rawMessage` keeps the original backend/axios text for
// logging and for call sites that still want the specifics.

export type ApiErrorKind =
    // No HTTP response reached us (connection failure, or a gateway
    // 502/503/504 before a warm replica) — the request never ran server-side.
    | 'unreachable'
    // The request timed out client-side (axios ECONNABORTED).
    | 'timeout'
    // The app answered with a 5xx it produced itself (e.g. an unhandled 500).
    | 'server'
    // The app answered with a 4xx (validation, auth, not-found, …).
    | 'client'
    // The caller aborted the request (AbortController / component unmount).
    | 'canceled'
    // Anything we could not classify.
    | 'unknown';

// Copy shown to the user, keyed by kind. 4xx uses the backend's own message
// when present (it is usually specific and actionable), falling back here.
const FRIENDLY_MESSAGE: Record<ApiErrorKind, string> = {
    unreachable: "Can't reach the server right now — it may be waking up. We'll keep trying.",
    timeout: 'The server took too long to respond. Please try again in a moment.',
    server: 'Something went wrong on our end. This is usually temporary — please try again.',
    client: "That didn't work. Please check your input and try again.",
    canceled: 'Request canceled.',
    unknown: 'Something went wrong. Please try again.',
};

export class ApiError extends Error {
    readonly kind: ApiErrorKind;
    readonly status?: number;
    // Whether re-issuing the same request could plausibly succeed. Note that
    // "retriable" does not imply "safe to auto-replay" — a 500 on a mutation may
    // already have applied, so replay is a user decision, not an automatic one.
    readonly retriable: boolean;
    // Short correlation id shown to the user and logged, so a report of "it
    // failed" can be tied to a specific request in the console/support flow.
    readonly requestId: string;
    // Original, unclassified message (backend text or axios error string).
    readonly rawMessage: string;

    constructor(params: {
        kind: ApiErrorKind;
        message: string;
        rawMessage: string;
        status?: number;
        retriable: boolean;
        requestId: string;
    }) {
        super(params.message);
        this.name = 'ApiError';
        this.kind = params.kind;
        this.status = params.status;
        this.retriable = params.retriable;
        this.requestId = params.requestId;
        this.rawMessage = params.rawMessage;
        // So `String(err)` yields the friendly message, not "ApiError: …".
        Object.setPrototypeOf(this, ApiError.prototype);
    }

    toString(): string {
        return this.message;
    }
}

export const isApiError = (value: unknown): value is ApiError => value instanceof ApiError;

export const newRequestId = (): string => Math.random().toString(36).slice(2, 6);

const RETRIABLE_KINDS: ReadonlySet<ApiErrorKind> = new Set<ApiErrorKind>([
    'unreachable',
    'timeout',
    'server',
]);

// Pull the most useful human string out of an arbitrary error/response payload.
const extractRawMessage = (error: unknown): string => {
    if (axios.isAxiosError(error)) {
        const data = error.response?.data as { error?: unknown; message?: unknown } | string | undefined;
        if (typeof data === 'string' && data.trim()) {
            return data;
        }
        if (data && typeof data === 'object') {
            const inner = data.error || data.message;
            if (typeof inner === 'string' && inner.trim()) {
                return inner;
            }
        }
        return error.message || 'Request failed';
    }
    if (error instanceof Error && error.message) {
        return error.message;
    }
    if (typeof error === 'string') {
        return error;
    }
    return 'Request failed';
};

// Turn any thrown value into a classified ApiError. Idempotent: passing an
// ApiError back through returns it unchanged.
export const classifyApiError = (error: unknown, requestId: string = newRequestId()): ApiError => {
    if (isApiError(error)) {
        return error;
    }

    const rawMessage = extractRawMessage(error);

    if (axios.isAxiosError(error)) {
        if (error.code === 'ERR_CANCELED') {
            return new ApiError({ kind: 'canceled', message: FRIENDLY_MESSAGE.canceled, rawMessage, retriable: false, requestId });
        }
        const status = error.response?.status;
        if (status === undefined) {
            // A timeout surfaces as ECONNABORTED with no response; otherwise it's
            // a connection failure that never reached the app.
            const kind: ApiErrorKind = error.code === 'ECONNABORTED' ? 'timeout' : 'unreachable';
            return new ApiError({ kind, message: FRIENDLY_MESSAGE[kind], rawMessage, retriable: true, requestId });
        }
        if (status >= 500) {
            // 502/503/504 mean the gateway never got a healthy reply — treat as
            // unreachable (recoverable, banner-worthy); a plain 500 is the app.
            const kind: ApiErrorKind = status === 502 || status === 503 || status === 504 ? 'unreachable' : 'server';
            return new ApiError({ kind, message: FRIENDLY_MESSAGE[kind], rawMessage, status, retriable: RETRIABLE_KINDS.has(kind), requestId });
        }
        // 4xx: prefer the backend's own message; it is usually the useful one.
        const message = rawMessage && !/^request failed/i.test(rawMessage) ? rawMessage : FRIENDLY_MESSAGE.client;
        return new ApiError({ kind: 'client', message, rawMessage, status, retriable: false, requestId });
    }

    return new ApiError({ kind: 'unknown', message: rawMessage || FRIENDLY_MESSAGE.unknown, rawMessage, retriable: false, requestId });
};
