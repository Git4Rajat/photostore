import { sessionScope } from './sessionScope';
import { getRuntimeConfig } from '../config/appConfig';
import { getUpload, postUpload } from './apiClient';

// Shadow calls are advisory. Live uploads separately require acknowledged
// admission, and discover durable ownership per authenticated library so a
// shared frontend build can continue uploading into legacy libraries.

export const waveLiveEnabled = () => getRuntimeConfig().uploadWaveExecutionEnabled === true
    || import.meta.env.VITE_UPLOAD_WAVE_EXECUTION_ENABLED === 'true';
export const waveShadowEnabled = () => waveLiveEnabled() || import.meta.env.VITE_UPLOAD_WAVE_SHADOW_ENABLED === 'true';
const base = () => `/upload/waves/${waveLiveEnabled() ? 'live' : 'shadow'}`;

const SHADOW_REQUEST_CONFIG = { singleAttempt: true, timeout: 15000 };
const BATCH_REQUEST_CONFIG = { singleAttempt: true, timeout: 120000 };
const protocols = new Map<string, Promise<'live' | 'legacy'>>();
export const waveProtocol = async (): Promise<'live' | 'legacy'> => {
    const scope = sessionScope();
    let pending = protocols.get(scope);
    if (!pending) {
        pending = getUpload<{ protocol: string }>('/upload/waves/protocol', SHADOW_REQUEST_CONFIG).then(result => {
            if (sessionScope() !== scope || !['live', 'legacy'].includes(result.protocol)) {
                throw new Error('Upload library protocol could not be verified.');
            }
            return result.protocol as 'live' | 'legacy';
        }).catch(error => { protocols.delete(scope); throw error; });
        protocols.set(scope, pending);
    }
    return pending;
};

const callShadow = async <T = unknown>(
    path: string, body: unknown | (() => Promise<unknown>), config = SHADOW_REQUEST_CONFIG,
): Promise<T | null> => {
    if (!waveShadowEnabled()) return null;
    try {
        const scope = sessionScope();
        if (waveLiveEnabled() && await waveProtocol() === 'legacy') return null;
        const payload = typeof body === 'function' ? await body() : body;
        if (sessionScope() !== scope) return null;
        return await postUpload<T>(path, payload, config);
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
export const wireItemId = async (key: string): Promise<string> => {
    const bytes = new TextEncoder().encode(key);
    const hash = await crypto.subtle.digest('SHA-256', bytes);
    return Array.from(new Uint8Array(hash), byte => byte.toString(16).padStart(2, '0')).join('');
};

export interface WaveShadowItem {
    itemId: string;
    filename: string;
}

export type WaveShadowOutcome = 'accepted' | 'duplicate' | 'cancelled' | 'failed';

// HTTP limits are independent of the smaller transactional storage pages.
export const WAVE_SHADOW_MAX_PAGE = 2000;
export const WAVE_RESOLVE_MAX_BATCH = 100;

export const openWaveShadowSession = (sessionId: string, declaredCount: number, resumedFromSessionId?: string) =>
    callShadow<{ waveId: string; manifestRevision: string }>(
        `${base()}/sessions`,
        { sessionId, declaredCount, ...(resumedFromSessionId ? { resumedFromSessionId } : {}) },
    );

export const registerWaveShadowItems = (sessionId: string, items: WaveShadowItem[]) =>
    callShadow<{ manifestRevision: string; registeredCount: number }>(
        `${base()}/sessions/${encodeURIComponent(sessionId)}/items`,
        async () => ({ items: await Promise.all(items.map(async item => ({
            ...item, itemId: await wireItemId(item.itemId),
        }))) }),
        BATCH_REQUEST_CONFIG,
    );

export interface WaveShadowResolution {
    itemId: string;
    outcome: WaveShadowOutcome;
    filename?: string;
}

export const resolveWaveShadowItems = (sessionId: string, items: WaveShadowResolution[]) =>
    callShadow<{ manifestRevision: string; state?: string }>(
        `${base()}/sessions/${encodeURIComponent(sessionId)}/resolve-batch`,
        async () => ({ items: await Promise.all(items.map(async item => ({
            ...item, itemId: await wireItemId(item.itemId),
        }))) }),
        BATCH_REQUEST_CONFIG,
    );

export const resolveWaveShadowItem = (
    sessionId: string,
    itemId: string,
    outcome: WaveShadowOutcome,
    filename?: string,
) =>
    callShadow<{ manifestRevision: string }>(
        `${base()}/sessions/${encodeURIComponent(sessionId)}/resolve`,
        async () => ({ itemId: await wireItemId(itemId), outcome, filename }),
    );

export const closeWaveShadowSession = (sessionId: string, manifestRevision: string, closeId: string) =>
    callShadow<{ state: string }>(
        `${base()}/sessions/${encodeURIComponent(sessionId)}/close`,
        { manifestRevision, closeId },
    );
