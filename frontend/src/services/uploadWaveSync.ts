import { waveParentFor, recordWaveItemResolved, waveParentResolved, waveSelectionSession, waveFileBelongs, loadWaveOperation, retireWaveOperation, retryWaveOperation, type Operation } from './uploadWaveOperations';
import { sessionScope } from './sessionScope';
import type { PersistedUploadFile, PersistedUploadSession } from '../types/browserProcessing';
import {
    abandonWaveShadowSession,
    discardWaveSession,
    type WaveDiscardResult,
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

// waveParentFor can return either a real Operation (declaredCount present)
// or, for a session with no waveOperationId yet, the plain checkpoint itself
// ("pre-parent diagnostic sessions") -- fall back to its own files.length in
// that case, since there's no separate declared manifest to consult yet.
const declaredCountFor = (parent: PersistedUploadSession): number =>
    'declaredCount' in parent ? (parent as Operation).declaredCount : parent.files.length;

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
    registrationWaiters: Array<{ through: number; resolve: () => void }>;
}

const sessionState = new Map<string, WaveShadowSessionState>();

const stateFor = (sessionId: string): WaveShadowSessionState => {
    let state = sessionState.get(sessionId);
    if (!state) {
        state = { manifestRevision: null, opened: false, registered: false, registeredCount: 0, queued: new Map(), resolutionScheduled: false, closed: false, resolved: new Set(), pending: Promise.resolve(), registrationWaiters: [] };
        sessionState.set(sessionId, state);
    }
    return state;
};

// Lets an admission call for file N proceed as soon as the page containing
// item N has registered, without waiting for the rest of the manifest.
const waitForRegisteredThrough = (state: WaveShadowSessionState, through: number): Promise<void> => {
    if (state.registered || state.registeredCount >= through) return Promise.resolve();
    return new Promise<void>((resolve) => { state.registrationWaiters.push({ through, resolve }); });
};

const notifyRegistrationWaiters = (state: WaveShadowSessionState): void => {
    const remaining: WaveShadowSessionState['registrationWaiters'] = [];
    for (const waiter of state.registrationWaiters) {
        if (state.registered || state.registeredCount >= waiter.through) waiter.resolve();
        else remaining.push(waiter);
    }
    state.registrationWaiters = remaining;
};

// waveItemKey is always `${operationId}-${index}` (declareWaveSelection/
// retryWaveOperation). Recovering the index lets admission wait only for
// this file's own registration page instead of the whole manifest.
const itemIndex = (operationId: string, key: string): number => {
    if (!key.startsWith(operationId + '-')) return -1;
    const index = Number(key.slice(operationId.length + 1));
    return Number.isInteger(index) && index >= 0 ? index : -1;
};

const liveOutcomeForFile = (file: PersistedUploadFile): WaveShadowOutcome | null => {
    if (file.status === 'done') return file.skippedDuplicate ? 'duplicate' : 'accepted';
    if (file.status === 'failed') return 'failed';
    return null;
};

// Opens the shadow session (if not already). Safe to call more than once for
// the same session.id (e.g. at creation and again on mount-restore) -- the
// state.opened guard makes every repeat call after the first a near-instant
// no-op.
// Exported for callers that need to confirm a session is admitted before
// starting byte uploads, without blocking on the whole manifest -- see
// liveWaveAdmission below for the same pattern (open, then kick the full
// registration loop in the background via ensureWaveShadowSessionOpened).
export const ensureOpened = async (child: PersistedUploadSession): Promise<void> => {
    const parent = waveParentFor(child);
    if (!parent) return;
    const state = stateFor(parent.id);
    if ('closed' in parent && parent.closed === true) {
        state.opened = state.registered = state.closed = true;
        return;
    }
    // declaredCount, not parent.files.length/child.files.length: the
    // Operation's persisted files array is dropped on reload (see
    // uploadWaveOperations.ts's encode()), and the checkpoint's files array
    // is intentionally the WRONG size once any file is done and removed
    // (see removePersistedFile) -- declaredCount is the one field that
    // survives both and still means "how many files this wave has."
    if (state.closed || state.expired || state.discarding || declaredCountFor(parent) === 0 || state.opened) return;
    // A previous admission may have failed before this browser retried.
    // Ensure the linked parent exists server-side before opening its retry.
    if (parent.resumedFromSessionId && 'scope' in parent && typeof parent.scope === 'string') {
        const previous = loadWaveOperation(parent.resumedFromSessionId, parent.scope);
        if (!previous) return;
        await ensureOpened(previous);
        if (!stateFor(previous.id).opened) return;
    }
    const opened = parent.resumedFromSessionId
        ? await openWaveShadowSession(parent.id, declaredCountFor(parent), parent.resumedFromSessionId)
        : await openWaveShadowSession(parent.id, declaredCountFor(parent));
    if (!opened) return; // disabled, or a transient failure -- a later call retries
    state.opened = true;
    state.manifestRevision = opened.manifestRevision;
    observeLease(state,opened);
    if (opened.leaseActive === false || ['closed','abandoned'].includes(opened.state || '')) {
        state.expired = true;
    }
};

// Registers every declared item, paged by WAVE_SHADOW_MAX_PAGE. Safe to call
// more than once for the same session.id -- backend registration is
// idempotent per itemId, and this module also tracks what it already
// believes is done so a healthy tab doesn't re-send on every call. Runs in
// the background relative to byte uploads -- liveWaveAdmission only waits
// for each file's own page via waitForRegisteredThrough, not this whole loop.
const ensureRegistered = async (child: PersistedUploadSession): Promise<void> => {
    const parent = waveParentFor(child);
    if (!parent) return;
    const state = stateFor(parent.id);
    try {
        await ensureOpened(child);
        if (state.closed || state.expired || state.discarding || !state.opened || state.registered) return;
        // child.files (the checkpoint), not parent.files: always a safe,
        // idempotent subset of what's already registered -- a file can't reach
        // `done` (and be dropped from the checkpoint) before its whole
        // registration round has already completed, so re-registering only
        // what's left in the checkpoint never skips anything that still needs it.
        for (let i = state.registeredCount; i < child.files.length; i += WAVE_SHADOW_MAX_PAGE) {
            if (state.discarding || !waveParentFor(child)) return;
            // waveItemKey (not the bare checkpoint key) is what every other
            // reference to this file's item -- resolve, admission, finalize
            // -- hashes into the wire itemId (see wireItemId call sites
            // below). Registering under file.key instead would register a
            // different identity than anything ever looks up again.
            const page = child.files.slice(i, i + WAVE_SHADOW_MAX_PAGE)
                .map((file) => ({ itemId: file.waveItemKey || file.key, filename: file.name }));
            const result = await registerWaveShadowItems(parent.id, page);
            if (!result) return; // Replay only the unacknowledged HTTP batch on retry.
            state.manifestRevision = result.manifestRevision;
            state.registeredCount = i + page.length;
            notifyRegistrationWaiters(state);
        }
        if (!state.discarding) state.registered = true;
    } catch (error) {
        // Set synchronously here (not left to serialized()'s own .catch,
        // which runs a tick later) so admissionError is already visible by
        // the time the finally below wakes any waiter -- otherwise
        // liveWaveAdmission can resume and check admissionError before it's
        // actually set.
        state.admissionError = error instanceof Error ? error : new Error('Upload admission failed.');
        throw error;
    } finally {
        // Unconditional: on early return/throw, release every waiter even if
        // unsatisfied -- liveWaveAdmission re-checks progress and throws its
        // own "admission unavailable" rather than hanging forever. A later
        // retry re-kicks registration and sets up a fresh, satisfiable wait.
        for (const waiter of state.registrationWaiters) waiter.resolve();
        state.registrationWaiters = [];
    }
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
    const scope=sessionScope();
    const visited=new Set<string>();
    const held=new Set<WaveShadowSessionState>();
    let legacyApi=false;
    let currentId=parent.id;
    try {
        // Quiesce every locally known parent before sending cancellation.
        let local: PersistedUploadSession | null=parent;
        const localIds=new Set<string>();
        while (local && !localIds.has(local.id)) {
            localIds.add(local.id);
            const state=stateFor(local.id); state.discarding=true; held.add(state);
            const previous: string | undefined=local.resumedFromSessionId;
            local=previous ? loadWaveOperation(previous,session.waveScope || scope) : null;
        }
        await Promise.all(Array.from(held,state => state.pending));
        while (currentId) {
            if (scope!==sessionScope()) throw new Error('Upload library changed.');
            if (visited.has(currentId)) throw new Error('Upload recovery lineage contains a cycle.');
            const candidate=currentId===parent.id ? parent : loadWaveOperation(currentId,session.waveScope || scope);
            const state=stateFor(currentId); state.discarding=true; held.add(state);
            await state.pending;
            let revision: string | undefined;
            let staleRefreshes=0;
            let result: WaveDiscardResult | null=null;
            for (;;) {
                try {
                    if (legacyApi) {
                        // Previous APIs require the exact manifest revision.
                        // Reopen only as a compatibility read; failures retain
                        // the checkpoint, never infer that an intake is absent.
                        if (!revision) {
                            if (!candidate || ('closed' in candidate && candidate.closed)) {
                                if (candidate && candidate.closed) {
                                    result={state:'closed',manifestRevision:'',resumedFromSessionId:candidate.resumedFromSessionId};
                                    break;
                                }
                                throw new Error('Upload recovery information requires an updated upload service.');
                            }
                            // declaredCountFor, not candidate.files.length -- candidate
                            // may be a cold-reloaded Operation whose persisted
                            // files array is now [] (see uploadWaveOperations.ts's
                            // encode()).
                            const opened=await openWaveShadowSession(currentId,declaredCountFor(candidate),candidate.resumedFromSessionId);
                            if (!opened) throw new Error('Upload discard could not be acknowledged. Retry Discard.');
                            revision=opened.manifestRevision;
                            if (['closed','abandoned'].includes(opened.state || '')) {
                                result={...opened,state:opened.state!};break;
                            }
                        }
                        result=await abandonWaveShadowSession(currentId,revision);
                    } else {
                        result=await discardWaveSession(currentId,candidate?.resumedFromSessionId);
                    }
                } catch (error) {
                    const failure=error as Error & {status?: number};
                    if (!legacyApi && [404,405].includes(failure.status || 0)) {
                        legacyApi=true; continue;
                    }
                    if ([403].includes(failure.status || 0) || failure.message?.includes('Only the library owner may abandon intake')) {
                        const child=currentId===parent.id ? session : candidate;
                        if (!child) throw error;
                        state.discarding=false;
                        await finalizeWaveShadowSession(child);
                        state.discarding=true;
                        if (!state.closed) throw error;
                        result={state:'closed',manifestRevision:'',resumedFromSessionId:candidate?.resumedFromSessionId};
                        break;
                    }
                    if (!failure.message?.includes('Abandonment revision is stale') || staleRefreshes++>=3) throw error;
                    revision=undefined;continue;
                }
                if (!result) throw new Error('Upload discard could not be acknowledged. Retry Discard.');
                if (['closed','abandoned','absent'].includes(result.state)) break;
                if (!result.abandonmentNeedsMore) throw new Error('Upload discard is incomplete. Retry Discard.');
                revision=result.manifestRevision;
            }
            if (scope!==sessionScope()) throw new Error('Upload library changed.');
            visited.add(currentId);
            // Follow committed links even when this browser lost the older
            // local manifest. An unadmitted retry supplies its persisted link.
            currentId=result?.resumedFromSessionId || candidate?.resumedFromSessionId || '';
        }
        // Do not retire any link until the whole chain is acknowledged. A
        // failed ancestor cleanup can be retried after refresh from the root.
        for (const id of Array.from(visited)) {
            stateFor(id).closed=true;
            retireWaveOperation({...session,waveOperationId:session.waveOperationId ? id : undefined});
        }
    } finally {
        for (const state of Array.from(held)) state.discarding=false;
    }
};

// A lost finalize response may leave server-accepted files locally unresolved.
// Never rebind those results to cancelled just to stop the remaining upload.
export const stopWaveShadowSession = async (session: PersistedUploadSession): Promise<void> => {
    await finalizeWaveShadowSession(session);
    const parent=waveParentFor(session);
    if (!waveLiveEnabled() || await waveProtocol()==='legacy') return;
    if (parent && stateFor(parent.id).closed) return;
    await discardWaveShadowSession(session);
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
            // Open eagerly (fast, single call). The registration loop is
            // kicked off later, synchronously adjacent to this file's own
            // wait (see below) -- not here, so there's exactly one kickoff
            // per admission attempt instead of two racing/duplicating calls.
            await ensureOpened(session);
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
                    await ensureOpened(session);
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
    if (!parent || !session.waveOperationId || !waveFileBelongs(session,file)) {
        throw new Error('Upload wave admission is unavailable. Retry before uploading files.');
    }
    const state=stateFor(parent.id);
    // Wait only for THIS file's own registration page, not the whole
    // manifest -- the background loop keeps advancing registeredCount
    // independently of which file is currently waiting. The re-kick here is
    // synchronous and immediately followed by the wait (no await between
    // them) so a kickoff that fails/flushes fast can never race ahead of
    // this waiter being registered -- it's also what retries a registration
    // attempt that failed before this call ever reached state.registered.
    const requiredIndex=itemIndex(parent.id, file.waveItemKey || file.key);
    if (!state.registered) {
        if (requiredIndex>=0) {
            void ensureWaveShadowSessionOpened(session);
            await waitForRegisteredThrough(state, requiredIndex+1);
        } else {
            await ensureWaveShadowSessionOpened(session); // can't determine this file's page; fall back to the full wait
        }
    }
    const admitted=state.registered || (requiredIndex>=0 && state.registeredCount>requiredIndex);
    if (!admitted || state.expired) {
        if (state.admissionError) throw state.admissionError;
        throw new Error('Upload wave admission is unavailable. Retry before uploading files.');
    }
    return {waveSessionId:parent.id,waveItemId:await wireItemId(file.waveItemKey || file.key)};
};
