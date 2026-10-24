import { waveParentFor, recordWaveItemResolved, waveParentResolved, waveSelectionSession, waveFileBelongs, loadWaveOperation } from './uploadWaveOperations';
import type { PersistedUploadFile, PersistedUploadSession } from '../types/browserProcessing';
import {
    WAVE_SHADOW_MAX_PAGE,
    type WaveShadowOutcome,
    closeWaveShadowSession,
    openWaveShadowSession,
    registerWaveShadowItems,
    resolveWaveShadowItem,
} from './uploadWaveClient';

// Optional shadow protocol for full-selection parents and their transport children.
// Legacy checkpoints remain readable. Requests are serialized per parent; errors
// hold diagnostic intake open and never fail the underlying byte upload.

interface WaveShadowSessionState {
    manifestRevision: string | null;
    opened: boolean;
    registered: boolean;
    closed: boolean;
    pending: Promise<void>;
}

const sessionState = new Map<string, WaveShadowSessionState>();

const stateFor = (sessionId: string): WaveShadowSessionState => {
    let state = sessionState.get(sessionId);
    if (!state) {
        state = { manifestRevision: null, opened: false, registered: false, closed: false, pending: Promise.resolve() };
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
const ensureRegistered = async (child: PersistedUploadSession): Promise<void> => {
    const session = waveParentFor(child);
    if (!session) return;
    const state = stateFor(session.id);
    if (state.closed || session.files.length === 0) return;
    if (!state.opened) {
        // A previous admission may have failed before this browser retried.
        // Ensure the linked parent exists server-side before opening its retry.
        if (session.resumedFromSessionId && 'scope' in session && typeof session.scope === 'string') {
            const previous = loadWaveOperation(session.resumedFromSessionId, session.scope);
            if (!previous) return;
            await ensureRegistered(previous);
            if (!stateFor(previous.id).opened) return;
        }
        const opened = session.resumedFromSessionId
            ? await openWaveShadowSession(session.id, session.files.length, session.resumedFromSessionId)
            : await openWaveShadowSession(session.id, session.files.length);
        if (!opened) return; // disabled, or a transient failure -- a later call retries
        state.opened = true;
        state.manifestRevision = opened.manifestRevision;
    }
    if (state.registered) return;
    for (let i = 0; i < session.files.length; i += WAVE_SHADOW_MAX_PAGE) {
        if (!waveParentFor(child)) return;
        const page = session.files.slice(i, i + WAVE_SHADOW_MAX_PAGE)
            .map((file) => ({ itemId: file.key, filename: file.name }));
        const result = await registerWaveShadowItems(session.id, page);
        if (!result) return; // stop paging; a later call resumes from the top (idempotent)
        state.manifestRevision = result.manifestRevision;
    }
    state.registered = true;
};

// Registration, resolutions and close share one ordered chain. The upload
// may await admission before starting bytes. Parallel responses cannot regress the close
// revision or resolve a file before its manifest registration has finished.
const serialized = (session: PersistedUploadSession, action: () => Promise<void>): Promise<void> => {
    const parent = waveParentFor(session);
    if (!parent) return Promise.resolve();
    const state = stateFor(parent.id);
    state.pending = state.pending.then(action).catch(() => { /* diagnostics must not fail uploading */ });
    return state.pending;
};

export const ensureWaveShadowSessionOpened = (session: PersistedUploadSession): Promise<void> =>
    serialized(session, () => ensureRegistered(session));

// Resolves one file's outcome as it settles. A single cheap call -- not a
// full-session rescan. Call once per file, from finalizeUploadedFile/
// markFileFailed as each file reaches a terminal status.
export const resolveWaveShadowFile = (session: PersistedUploadSession, file: PersistedUploadFile): Promise<void> => serialized(session, async () => {
    const parent = waveParentFor(session);
    if (!parent) return;
    const state = stateFor(parent.id);
    if (state.closed) return;
    const outcome = liveOutcomeForFile(file);
    if (!outcome || !waveFileBelongs(session, file)) return;
    await ensureRegistered(session);
    if (!state.registered || !waveParentFor(session)) return;
    const result = await resolveWaveShadowItem(
        parent.id, file.waveItemKey || file.key, outcome,
        outcome === 'accepted' || outcome === 'duplicate' ? (file.waveAcceptedFilename || file.name) : undefined,
    );
    if (result) {
        state.manifestRevision = result.manifestRevision;
        recordWaveItemResolved(session, file);
    }
});

// Called once, from runUploadSession's `finally`, regardless of how the
// session ended (completed, stopped, discarded, or errored). Defensively
// resolves every file this module hasn't confirmed as settled -- anything
// not accepted/duplicate/failed is a cancellation (never started, or stopped
// mid-transfer) -- then closes the shadow session. A session with items that
// still can't resolve (e.g. the shadow API was never reachable) simply never
// closes; that's an observational gap, not a failure this surfaces.
export const finalizeWaveShadowSession = (session: PersistedUploadSession): Promise<void> => serialized(session, async () => {
    const parent = waveParentFor(session);
    if (!parent) return;
    const state = stateFor(parent.id);
    if (state.closed) return;
    await ensureRegistered(session);
    if (!state.registered || !waveParentFor(session)) return;
    for (const file of session.files) {
        if (!waveParentFor(session)) return;
        if (!waveFileBelongs(session, file)) continue;
        const outcome = liveOutcomeForFile(file) ?? 'cancelled';
        const result = await resolveWaveShadowItem(
            parent.id, file.waveItemKey || file.key, outcome,
            outcome === 'accepted' || outcome === 'duplicate' ? (file.waveAcceptedFilename || file.name) : undefined,
        );
        if (result) {
            state.manifestRevision = result.manifestRevision;
            recordWaveItemResolved(session, file);
        }
    }
    if (!waveParentResolved(session)) return;
    if (!state.manifestRevision) return;
    const result = await closeWaveShadowSession(parent.id, state.manifestRevision, parent.id);
    if (result) state.closed = true;
});

export const cancelQueuedWaveSelection = (files: File[]): Promise<void> => {
    const child = waveSelectionSession(files);
    return child ? finalizeWaveShadowSession(child) : Promise.resolve();
};

// Test-only: clear in-memory state between cases so one test's session ids
// (which deliberately reuse fixed values) don't leak into the next.
export const __resetWaveShadowStateForTests = (): void => {
    sessionState.clear();
};

// Live uploads fail closed before init; advisory shadow diagnostics retain their
// existing behavior. Each binding uses the original whole-selection item key.
export const liveWaveAdmission = async (session: PersistedUploadSession, file: PersistedUploadFile): Promise<Record<string,string>> => {
    const {waveLiveEnabled,waveProtocol,wireItemId}=await import('./uploadWaveClient');
    if (!waveLiveEnabled() || await waveProtocol()==='legacy') return {};
    await ensureWaveShadowSessionOpened(session);
    const parent=waveParentFor(session);
    if (!parent || !session.waveOperationId || !waveFileBelongs(session,file)
        || !stateFor(parent.id).registered || session.waveAdmissionFailed) {
        throw new Error('Upload wave admission is unavailable. Retry before uploading files.');
    }
    return {waveSessionId:parent.id,waveItemId:await wireItemId(file.waveItemKey || file.key)};
};
