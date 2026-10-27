import { waveParentFor, recordWaveItemResolved, waveParentResolved, waveSelectionSession, waveFileBelongs, loadWaveOperation, retireWaveOperation } from './uploadWaveOperations';
import type { PersistedUploadFile, PersistedUploadSession } from '../types/browserProcessing';
import {
    WAVE_SHADOW_MAX_PAGE,
    WAVE_RESOLVE_MAX_BATCH,
    type WaveShadowOutcome,
    closeWaveShadowSession,
    openWaveShadowSession,
    registerWaveShadowItems,
    resolveWaveShadowItems,
} from './uploadWaveClient';

// Optional shadow protocol for full-selection parents and their transport children.
// Legacy checkpoints remain readable. Requests are serialized per parent; errors
// hold diagnostic intake open and never fail the underlying byte upload.

interface WaveShadowSessionState {
    manifestRevision: string | null;
    opened: boolean;
    registered: boolean;
    registeredCount: number;
    queued: Map<string, PersistedUploadFile>;
    resolutionScheduled: boolean;
    closed: boolean;
    resolved: Set<string>;
    pending: Promise<void>;
    admissionError?: Error;
}

const sessionState = new Map<string, WaveShadowSessionState>();

const stateFor = (sessionId: string): WaveShadowSessionState => {
    let state = sessionState.get(sessionId);
    if (!state) {
        state = { manifestRevision: null, opened: false, registered: false, registeredCount: 0, queued: new Map(), resolutionScheduled: false, closed: false, resolved: new Set(), pending: Promise.resolve() };
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
    if ('closed' in session && session.closed === true) {
        state.opened = state.registered = state.closed = true;
        return;
    }
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
    for (let i = state.registeredCount; i < session.files.length; i += WAVE_SHADOW_MAX_PAGE) {
        if (!waveParentFor(child)) return;
        const page = session.files.slice(i, i + WAVE_SHADOW_MAX_PAGE)
            .map((file) => ({ itemId: file.key, filename: file.name }));
        const result = await registerWaveShadowItems(session.id, page);
        if (!result) return; // Replay only the unacknowledged HTTP batch on retry.
        state.manifestRevision = result.manifestRevision;
        state.registeredCount = i + page.length;
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
    state.pending = state.pending.then(action).catch(error => {
        state.admissionError = error instanceof Error ? error : new Error('Upload admission failed.');
    });
    return state.pending;
};

export const ensureWaveShadowSessionOpened = (session: PersistedUploadSession): Promise<void> => {
    const parent = waveParentFor(session);
    // Admission is immutable once the complete manifest is acknowledged. Do not
    // put hashing/init/finalize behind the queue draining earlier file outcomes.
    if (!parent || stateFor(parent.id).registered) return Promise.resolve();
    return serialized(session, () => ensureRegistered(session));
};

// Called only from the parent's ordered chain. Failed batches remain unresolved
// and are retried during cleanup; replay is idempotent even after partial commits.
const resolveFiles = async (session: PersistedUploadSession, files: PersistedUploadFile[]): Promise<void> => {
    const parent = waveParentFor(session);
    if (!parent) return;
    const state = stateFor(parent.id);
    await ensureRegistered(session);
    if (!state.registered || !waveParentFor(session)) return;
    const outstanding = files.filter(file => waveFileBelongs(session, file)
        && !state.resolved.has(file.waveItemKey || file.key));
    for (let i = 0; i < outstanding.length; i += WAVE_RESOLVE_MAX_BATCH) {
        if (!waveParentFor(session)) return;
        const page = outstanding.slice(i, i + WAVE_RESOLVE_MAX_BATCH);
        const result = await resolveWaveShadowItems(parent.id, page.map(file => {
            const outcome = liveOutcomeForFile(file) ?? 'cancelled';
            return { itemId: file.waveItemKey || file.key, outcome,
                filename: outcome === 'accepted' || outcome === 'duplicate' ? (file.waveAcceptedFilename || file.name) : undefined };
        }));
        if (!result) continue;
        state.manifestRevision = result.manifestRevision;
        for (const file of page) {
            state.resolved.add(file.waveItemKey || file.key);
            recordWaveItemResolved(session, file);
        }
        if (result.state === 'closed') {
            state.closed = true;
            retireWaveOperation(session);
            return;
        }
    }
};

// Coalesce outcomes while lanes continue hashing and transferring. The ordered
// chain still fences cleanup and close, but admission no longer waits on it.
export const resolveWaveShadowFile = (session: PersistedUploadSession, file: PersistedUploadFile): Promise<void> => {
    const parent = waveParentFor(session);
    if (!parent || !liveOutcomeForFile(file) || !waveFileBelongs(session, file)) return Promise.resolve();
    const state = stateFor(parent.id);
    if (state.closed || state.resolved.has(file.waveItemKey || file.key)) return Promise.resolve();
    state.queued.set(file.waveItemKey || file.key, file);
    if (state.resolutionScheduled) return state.pending;
    state.resolutionScheduled = true;
    return serialized(session, async () => {
        await new Promise(resolve => setTimeout(resolve, 25));
        const files = Array.from(state.queued.values());
        state.queued.clear();
        state.resolutionScheduled = false;
        if (!state.closed) await resolveFiles(session, files);
    });
};

// The live finalize response proves that the backend already resolved this item.
// Verify its binding before using it; old servers fall back to explicit resolve.
export const acknowledgeWaveFinalizedFile = (session: PersistedUploadSession, file: PersistedUploadFile,
    receipt: { sessionId: string; itemId: string; manifestRevision: string; state: string }): Promise<void> => serialized(session, async () => {
    const parent = waveParentFor(session);
    const { wireItemId } = await import('./uploadWaveClient');
    if (!parent || !waveFileBelongs(session, file) || receipt.sessionId !== parent.id
        || receipt.itemId !== await wireItemId(file.waveItemKey || file.key) || !receipt.manifestRevision) return;
    const state = stateFor(parent.id);
    state.manifestRevision = receipt.manifestRevision;
    state.resolved.add(file.waveItemKey || file.key);
    recordWaveItemResolved(session, file);
    if (receipt.state === 'closed') {
        state.closed = true;
        retireWaveOperation(session);
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
    await resolveFiles(session, session.files);
    // Retry local checkpoint writes without repeating acknowledged HTTP calls.
    for (const file of session.files) {
        if (state.resolved.has(file.waveItemKey || file.key)) recordWaveItemResolved(session, file);
    }
    if (state.closed) return;
    if (session.files.some(file => waveFileBelongs(session, file)
        && !state.resolved.has(file.waveItemKey || file.key))) return;
    if (!waveParentResolved(session)) return;
    if (!state.manifestRevision) return;
    const result = await closeWaveShadowSession(parent.id, state.manifestRevision, parent.id);
    if (result) {
        state.closed = true;
        retireWaveOperation(session);
    }
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
    if (session.waveAdmissionFailed) {
        throw new Error('Upload admission could not be saved in browser storage. Free space for this site, then retry.');
    }
    if (!parent || !session.waveOperationId || !waveFileBelongs(session,file)
        || !stateFor(parent.id).registered) {
        if (parent && stateFor(parent.id).admissionError) throw stateFor(parent.id).admissionError;
        throw new Error('Upload wave admission is unavailable. Retry before uploading files.');
    }
    return {waveSessionId:parent.id,waveItemId:await wireItemId(file.waveItemKey || file.key)};
};
