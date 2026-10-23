import { sessionScope } from './sessionScope';
import { postUpload } from './apiClient';

// Thin wrapper over the backend's opt-in shadow wave API
// (backend/routes/upload_wave_shadow.py). The shadow protocol is disabled by
// default (UPLOAD_WAVE_SHADOW_ENABLED) and, even when enabled, never gates or
// blocks the real upload path -- every call here is best-effort: a 404
// (disabled), a network error, or any other failure is swallowed so it can
// never affect uploading, finalizing, or resuming a real file. See
// docs/indexing-clustering-target-state.md step 1 ("connect the browser").

export const waveShadowEnabled = () => import.meta.env.VITE_UPLOAD_WAVE_SHADOW_ENABLED === 'true';

const SHADOW_REQUEST_CONFIG = { singleAttempt: true, timeout: 15000 };

const callShadow = async <T = unknown>(
    path: string, body: unknown | (() => Promise<unknown>),
): Promise<T | null> => {
    if (!waveShadowEnabled()) return null;
    try {
        const scope = sessionScope();
        const payload = typeof body === 'function' ? await body() : body;
        if (sessionScope() !== scope) return null;
        return await postUpload<T>(path, payload, SHADOW_REQUEST_CONFIG);
    } catch (err) {
        // Best-effort diagnostics only -- never surfaced to the user and
        // never thrown into the real upload path's callers.
        if (typeof console !== 'undefined' && console.debug) {
            console.debug('[uploadWaveClient] shadow call failed (ignored)', path, err);
        }
        return null;
    }
};

// Persisted file keys include separators and filenames. Hash their complete
// stable identity for transport; never truncate or modify the upload checkpoint.
const wireItemId = async (key: string): Promise<string> => {
    const bytes = new TextEncoder().encode(key);
    const hash = await crypto.subtle.digest('SHA-256', bytes);
    return Array.from(new Uint8Array(hash), byte => byte.toString(16).padStart(2, '0')).join('');
};

export interface WaveShadowItem {
    itemId: string;
    filename: string;
}

export type WaveShadowOutcome = 'accepted' | 'duplicate' | 'cancelled' | 'failed';

// MAX_PAGE in backend/upload_waves.py -- registration is paged at this size.
export const WAVE_SHADOW_MAX_PAGE = 20;

export const openWaveShadowSession = (sessionId: string, declaredCount: number, resumedFromSessionId?: string) =>
    callShadow<{ waveId: string; manifestRevision: string }>(
        `/upload/waves/shadow/sessions`,
        { sessionId, declaredCount, ...(resumedFromSessionId ? { resumedFromSessionId } : {}) },
    );

export const registerWaveShadowItems = (sessionId: string, items: WaveShadowItem[]) =>
    callShadow<{ manifestRevision: string; registeredCount: number }>(
        `/upload/waves/shadow/sessions/${encodeURIComponent(sessionId)}/items`,
        async () => ({ items: await Promise.all(items.map(async item => ({
            ...item, itemId: await wireItemId(item.itemId),
        }))) }),
    );

export const resolveWaveShadowItem = (
    sessionId: string,
    itemId: string,
    outcome: WaveShadowOutcome,
    filename?: string,
) =>
    callShadow<{ manifestRevision: string }>(
        `/upload/waves/shadow/sessions/${encodeURIComponent(sessionId)}/resolve`,
        async () => ({ itemId: await wireItemId(itemId), outcome, filename }),
    );

export const closeWaveShadowSession = (sessionId: string, manifestRevision: string, closeId: string) =>
    callShadow<{ state: string }>(
        `/upload/waves/shadow/sessions/${encodeURIComponent(sessionId)}/close`,
        { manifestRevision, closeId },
    );
