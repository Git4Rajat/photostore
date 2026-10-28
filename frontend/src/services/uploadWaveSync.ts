import { waveParentFor, recordWaveItemResolved, waveParentResolved, waveSelectionSession, waveFileBelongs, loadWaveOperation, retireWaveOperation, retryWaveOperation } from './uploadWaveOperations';
import type { PersistedUploadFile, PersistedUploadSession } from '../types/browserProcessing';
import {
    abandonWaveShadowSession,
    renewUploadWaveLease,
    wireItemId,
    waveLiveEnabled,
    waveProtocol,
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
    discarding?: boolean;
    resolved: Set<string>;
    pending: Promise<void>;
    admissionError?: Error;
    leaseCheckAt?: number;
    expired?: boolean;
    renewal?: Promise<void>;
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
    if (state.closed || state.expired || state.discarding || session.files.length === 0) return;
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
        observeLease(state,opened);
        if (opened.leaseActive === false || ['closed','abandoned'].includes(opened.state || '')) {
            state.expired = true;
            return;
        }
    }
    if (state.registered) return;
    for (let i = state.registeredCount; i < session.files.length; i += WAVE_SHADOW_MAX_PAGE) {
        if (state.discarding || !waveParentFor(child)) return;
        const page = session.files.slice(i, i + WAVE_SHADOW_MAX_PAGE)
            .map((file) => ({ itemId: file.key, filename: file.name }));
        const result = await registerWaveShadowItems(session.id, page);
        if (!result) return; // Replay only the unacknowledged HTTP batch on retry.
        state.manifestRevision = result.manifestRevision;
        state.registeredCount = i + page.length;
    }
    if (!state.discarding) state.registered = true;
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
    if (state.expired || state.discarding || !state.registered || !waveParentFor(session)) return;
    const outstanding = files.filter(file => waveFileBelongs(session, file)
        && !state.resolved.has(file.waveItemKey || file.key));
    for (let i = 0; i < outstanding.length; i += WAVE_RESOLVE_MAX_BATCH) {
        if (state.discarding || !waveParentFor(session)) return;
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
    if (state.closed || state.discarding) return;
    await ensureRegistered(session);
    if (state.expired || state.discarding || !state.registered || !waveParentFor(session)) return;
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

// Discard needs a durable server acknowledgment, not a best-effort outcome flush.
// Stop restored registration after its in-flight HTTP page and preserve server-
// resolved uploads even if this browser lost their local completion checkpoint.
export const discardWaveShadowSession = async (session: PersistedUploadSession): Promise<void> => {
    const parent = waveParentFor(session);
    if (!parent) {
        if (waveLiveEnabled() && !session.waveAdmissionFailed) throw new Error('Upload recovery information is unavailable.');
        return;
    }
    if (!waveLiveEnabled() || await waveProtocol() === 'legacy') {
        await finalizeWaveShadowSession(session);
        return;
    }
    const state = stateFor(parent.id);
    if (state.closed || ('closed' in parent && parent.closed)) return;
    state.discarding = true;
    await state.pending;
    try {
        if (state.closed) return;
        // Read-and-cancel is owned by the server. Reopening an absent intake
        // would require admission infrastructure and could fail on old lineage.
        let revision: string | undefined;
        let staleRefreshes = 0;
        for (;;) {
            let result: Awaited<ReturnType<typeof abandonWaveShadowSession>>;
            try { result = await abandonWaveShadowSession(parent.id, revision); }
            catch (error) {
                if (!(error instanceof Error) || !error.message.includes('Abandonment revision is stale')
                    || staleRefreshes++ >= 3) throw error;
                revision = undefined; // Let the server read the current revision.
                continue;
            }
            if (!result) throw new Error('Upload discard could not be acknowledged. Retry Discard.');
            revision = result.manifestRevision;
            if (['closed','abandoned','absent'].includes(result.state)) {
                state.closed = true;
                retireWaveOperation(session);
                return;
            }
            if (!result.abandonmentNeedsMore) throw new Error('Upload discard is incomplete. Retry Discard.');
        }
    } catch (error) {
        // The existing abandonment endpoint is owner-only. Other library
        // uploaders retain the ordinary resolve/close permissions and path.
        if ((error as { status?: number }).status === 403
            || (error instanceof Error && error.message.includes('Only the library owner may abandon intake'))) {
            state.discarding = false;
            await finalizeWaveShadowSession(session);
            if (state.closed) return;
        }
        throw error;
    } finally {
        state.discarding = false;
    }
};

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
const observeLease = (state: WaveShadowSessionState, lease: {
    leaseActive?: boolean; leaseExpiresAt?: number; serverNow?: number;
}) => {
    if (lease.leaseActive === false) state.expired = true;
    if (lease.leaseExpiresAt && lease.serverNow !== undefined) {
        // Use server-relative duration so browser clock skew cannot extend a lease.
        state.leaseCheckAt = Date.now() + Math.max(0,lease.leaseExpiresAt-lease.serverNow)*1000;
    }
};

export const heartbeatWaveSession = async (session: PersistedUploadSession): Promise<void> => {
    if (!waveLiveEnabled() || await waveProtocol() === 'legacy') return;
    const parent = waveParentFor(session);
    if (!parent) return;
    const state = stateFor(parent.id);
    if (!state.opened || state.expired || state.closed || state.discarding) return;
    if (!state.renewal) {
        state.renewal = renewUploadWaveLease(parent.id).then(result => observeLease(state,result))
            .finally(() => { state.renewal = undefined; });
    }
    await state.renewal;
};

const admissions = new WeakMap<PersistedUploadSession, Promise<void>>();

// A returning browser cannot resurrect a timed-out parent. Preserve transport
// keys/bytes and move unfinished files to a linked admission, repeatedly.
export const liveWaveAdmission = async (session: PersistedUploadSession, file: PersistedUploadFile,
    onRebind?: () => void, onBeforeRebind?: () => void): Promise<Record<string,string>> => {
    if (!waveLiveEnabled() || await waveProtocol()==='legacy') return {};
    let pending=admissions.get(session);
    if (!pending) {
        pending=(async () => {
            await ensureWaveShadowSessionOpened(session);
            let parent=waveParentFor(session);
            if (parent) {
                let state=stateFor(parent.id);
                if (!state.expired && state.leaseCheckAt !== undefined && Date.now()>=state.leaseCheckAt) {
                    await heartbeatWaveSession(session);
                }
                if (state.expired) {
                    onBeforeRebind?.();
                    const next=retryWaveOperation(session);
                    if (next.waveOperationId===session.waveOperationId) throw new Error('Upload resume could not be saved.');
                    // Keep references held by hashing/transfer lanes valid.
                    session.waveOperationId=next.waveOperationId;
                    session.waveScope=next.waveScope;
                    session.files.forEach((entry,index) => { entry.waveItemKey=next.files[index].waveItemKey; });
                    onRebind?.();
                    await ensureWaveShadowSessionOpened(session);
                    parent=waveParentFor(session);
                    state=stateFor(parent!.id);
                }
            }
        })().finally(() => { admissions.delete(session); });
        admissions.set(session,pending);
    }
    await pending;
    // Automatic retry lanes can hold a progress-store copy of this record.
    if (!waveFileBelongs(session,file)) {
        const current=session.files.find(entry => entry.key===file.key);
        if (current) file.waveItemKey=current.waveItemKey;
    }
    const parent=waveParentFor(session);
    if (session.waveAdmissionFailed) {
        throw new Error('Upload admission could not be saved in browser storage. Free space for this site, then retry.');
    }
    if (!parent || !session.waveOperationId || !waveFileBelongs(session,file)
        || !stateFor(parent.id).registered || stateFor(parent.id).expired) {
        if (parent && stateFor(parent.id).admissionError) throw stateFor(parent.id).admissionError;
        throw new Error('Upload wave admission is unavailable. Retry before uploading files.');
    }
    return {waveSessionId:parent.id,waveItemId:await wireItemId(file.waveItemKey || file.key)};
};
