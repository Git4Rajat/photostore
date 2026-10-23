import { postUpload } from './apiClient';

// Thin wrapper over the backend's opt-in shadow wave API
// (backend/routes/upload_wave_shadow.py). The shadow protocol is disabled by
// default (UPLOAD_WAVE_SHADOW_ENABLED) and, even when enabled, never gates or
// blocks the real upload path -- every call here is best-effort: a 404
// (disabled), a network error, or any other failure is swallowed so it can
// never affect uploading, finalizing, or resuming a real file. See
// docs/indexing-clustering-target-state.md step 1 ("connect the browser").

const SHADOW_REQUEST_CONFIG = { singleAttempt: true, timeout: 15000 };

const callShadow = async <T = unknown>(path: string, body: unknown): Promise<T | null> => {
    try {
        return await postUpload<T>(path, body, SHADOW_REQUEST_CONFIG);
    } catch (err) {
        // Best-effort diagnostics only -- never surfaced to the user and
        // never thrown into the real upload path's callers.
        if (typeof console !== 'undefined' && console.debug) {
            console.debug('[uploadWaveClient] shadow call failed (ignored)', path, err);
        }
        return null;
    }
};

export interface WaveShadowItem {
    itemId: string;
    filename: string;
}

export type WaveShadowOutcome = 'accepted' | 'duplicate' | 'cancelled' | 'failed';

// MAX_PAGE in backend/upload_waves.py -- registration is paged at this size.
export const WAVE_SHADOW_MAX_PAGE = 20;

export const openWaveShadowSession = (sessionId: string, declaredCount: number) =>
    callShadow<{ waveId: string; manifestRevision: string }>(
        `/upload/waves/shadow/sessions`,
        { sessionId, declaredCount },
    );

export const registerWaveShadowItems = (sessionId: string, items: WaveShadowItem[]) =>
    callShadow<{ manifestRevision: string; registeredCount: number }>(
        `/upload/waves/shadow/sessions/${encodeURIComponent(sessionId)}/items`,
        { items },
    );

export const resolveWaveShadowItem = (
    sessionId: string,
    itemId: string,
    outcome: WaveShadowOutcome,
    filename?: string,
) =>
    callShadow<{ manifestRevision: string }>(
        `/upload/waves/shadow/sessions/${encodeURIComponent(sessionId)}/resolve`,
        { itemId, outcome, filename },
    );

export const closeWaveShadowSession = (sessionId: string, manifestRevision: string, closeId: string) =>
    callShadow<{ state: string }>(
        `/upload/waves/shadow/sessions/${encodeURIComponent(sessionId)}/close`,
        { manifestRevision, closeId },
    );
