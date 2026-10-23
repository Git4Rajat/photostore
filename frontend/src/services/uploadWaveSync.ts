import type { PersistedUploadFile, PersistedUploadSession } from '../types/browserProcessing';
import {
    WAVE_SHADOW_MAX_PAGE,
    type WaveShadowOutcome,
    closeWaveShadowSession,
    openWaveShadowSession,
    registerWaveShadowItems,
    resolveWaveShadowItem,
} from './uploadWaveClient';

// Connects the real upload path to the backend's opt-in shadow wave API,
// reusing the already-persisted PersistedUploadSession/PersistedUploadFile
// (session.id, file.key) as the shadow protocol's session/item identity --
// no new persisted storage surface, so the pinned on-disk checkpoint format
// (AppServicesProvider.upload.test.tsx) is untouched. Every call is
// fire-and-forget and best-effort (see uploadWaveClient.ts); nothing here can
// block, delay, or fail the real upload.

interface WaveShadowSessionState {
    manifestRevision: string | null;
    opened: boolean;
    registered: boolean;
    closed: boolean;
}

const sessionState = new Map<string, WaveShadowSessionState>();

const stateFor = (sessionId: string): WaveShadowSessionState => {
    let state = sessionState.get(sessionId);
    if (!state) {
        state = { manifestRevision: null, opened: false, registered: false, closed: false };
        sessionState.set(sessionId, state);
    }
    return state;
};

const liveOutcomeForFile = (file: PersistedUploadFile): WaveShadowOutcome | null => {
    if (file.status === 'done') return file.skippedDuplicate ? 'duplicate' : 'accepted';
    if (file.status === 'failed') return 'failed';
    return null;
};

// Opens the shadow session (if not already) and registers every declared
// item, paged by WAVE_SHADOW_MAX_PAGE. Safe to call more than once for the
// same session.id (e.g. at creation and again on mount-restore) -- backend
// registration is idempotent per itemId, and this module also tracks what it
// already believes is done so a healthy tab doesn't re-send on every call.
export const ensureWaveShadowSessionOpened = async (session: PersistedUploadSession): Promise<void> => {
    const state = stateFor(session.id);
    if (state.closed || session.files.length === 0) return;
    if (!state.opened) {
        const opened = await openWaveShadowSession(session.id, session.files.length);
        if (!opened) return; // disabled, or a transient failure -- a later call retries
        state.opened = true;
        state.manifestRevision = opened.manifestRevision;
    }
    if (state.registered) return;
    for (let i = 0; i < session.files.length; i += WAVE_SHADOW_MAX_PAGE) {
        const page = session.files.slice(i, i + WAVE_SHADOW_MAX_PAGE)
            .map((file) => ({ itemId: file.key, filename: file.name }));
        const result = await registerWaveShadowItems(session.id, page);
        if (!result) return; // stop paging; a later call resumes from the top (idempotent)
        state.manifestRevision = result.manifestRevision;
    }
    state.registered = true;
};

// Resolves one file's outcome as it settles. A single cheap call -- not a
// full-session rescan. Call once per file, from finalizeUploadedFile/
// markFileFailed as each file reaches a terminal status.
export const resolveWaveShadowFile = async (session: PersistedUploadSession, file: PersistedUploadFile): Promise<void> => {
    const state = stateFor(session.id);
    if (state.closed) return;
    const outcome = liveOutcomeForFile(file);
    if (!outcome) return;
    const result = await resolveWaveShadowItem(
        session.id, file.key, outcome,
        outcome === 'accepted' || outcome === 'duplicate' ? file.name : undefined,
    );
    if (result) state.manifestRevision = result.manifestRevision;
};

// Called once, from runUploadSession's `finally`, regardless of how the
// session ended (completed, stopped, discarded, or errored). Defensively
// resolves every file this module hasn't confirmed as settled -- anything
// not accepted/duplicate/failed is a cancellation (never started, or stopped
// mid-transfer) -- then closes the shadow session. A session with items that
// still can't resolve (e.g. the shadow API was never reachable) simply never
// closes; that's an observational gap, not a failure this surfaces.
export const finalizeWaveShadowSession = async (session: PersistedUploadSession): Promise<void> => {
    const state = stateFor(session.id);
    if (state.closed || !state.opened) return;
    for (const file of session.files) {
        const outcome = liveOutcomeForFile(file) ?? 'cancelled';
        const result = await resolveWaveShadowItem(
            session.id, file.key, outcome,
            outcome === 'accepted' || outcome === 'duplicate' ? file.name : undefined,
        );
        if (result) state.manifestRevision = result.manifestRevision;
    }
    if (!state.manifestRevision) return;
    const result = await closeWaveShadowSession(session.id, state.manifestRevision, session.id);
    if (result) state.closed = true;
};

// Test-only: clear in-memory state between cases so one test's session ids
// (which deliberately reuse fixed values) don't leak into the next.
export const __resetWaveShadowStateForTests = (): void => {
    sessionState.clear();
};
