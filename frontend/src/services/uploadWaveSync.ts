import { waveParentFor, recordWaveItemResolved, waveParentResolved, waveSelectionSession, waveFileBelongs, loadWaveOperation, retireWaveOperation, type Operation } from './uploadWaveOperations';
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
    WAVE_REGISTER_MAX_BATCH,
    WAVE_RESOLVE_MAX_BATCH,
    type WaveShadowOutcome,
    closeWaveShadowSession,
    openWaveShadowSession,
    registerWaveShadowItems,
    resolveWaveShadowItems,
} from './uploadWaveClient';

// Full-selection intake shared by transport children. Advisory shadow errors
// remain best effort; live admission and cancellation fail closed. Registration,
// outcome reporting and close are ordered per parent.

// waveParentFor can return either a real Operation (declaredCount present)
// or, for a session with no waveOperationId yet, the plain checkpoint itself
// ("pre-parent diagnostic sessions") -- fall back to its own files.length in
// that case, since there's no separate declared manifest to consult yet.
const declaredCountFor = (parent: PersistedUploadSession): number =>
    'declaredCount' in parent ? (parent as Operation).declaredCount : parent.files.length;

interface WaveShadowSessionState {
    manifestRevision: string | null;
    opened: boolean;
    opening?: Promise<void>;
    // Populated per item as each admission page succeeds. Identity-based,
    // rather than a count/offset, so partial retries cannot drift from the
    // server's truth.
    registeredItems: Set<string>;
    queued: Map<string, PersistedUploadFile>;
    resolutionScheduled: boolean;
    closed: boolean;
    discarding?: boolean;
    resolved: Set<string>;
    pending: Promise<void>;
    // Registration has a separate chain from outcome/close ordering. Init
    // batches and the rolling look-ahead can arrive concurrently, but Azure
    // Table session/wave rows are optimistic-concurrency hot spots. Serialize
    // admission locally so this browser never races itself on those rows.
    registrationPending: Promise<void>;
    admissionError?: Error;
    leaseCheckAt?: number;
    expired?: boolean;
    renewal?: Promise<void>;
}

const sessionState = new Map<string, WaveShadowSessionState>();

const stateFor = (sessionId: string): WaveShadowSessionState => {
    let state = sessionState.get(sessionId);
    if (!state) {
        state = { manifestRevision: null, opened: false, registeredItems: new Set(), queued: new Map(), resolutionScheduled: false, closed: false, resolved: new Set(), pending: Promise.resolve(), registrationPending: Promise.resolve() };
        sessionState.set(sessionId, state);
    }
    return state;
};

const liveOutcomeForFile = (file: PersistedUploadFile): WaveShadowOutcome | null => {
    if (file.status === 'done') return file.skippedDuplicate ? 'duplicate' : 'accepted';
    if (file.status === 'failed') return 'failed';
    return null;
};

// Opens the shadow session (if not already). Safe to call more than once for
// the same session.id (e.g. at creation and again on mount-restore) -- the
// state.opened guard makes every repeat call after the first a near-instant
// no-op. Registration is handled separately by registerWaveItemsBatch.
const openSessionOnce = async (child: PersistedUploadSession): Promise<void> => {
    const parent = waveParentFor(child);
    if (!parent) return;
    const state = stateFor(parent.id);
    if ('closed' in parent && parent.closed === true) {
        state.opened = state.closed = true;
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

export const ensureOpened = (child: PersistedUploadSession): Promise<void> => {
    const parent = waveParentFor(child);
    if (!parent) return Promise.resolve();
    const state = stateFor(parent.id);
    if (!state.opening) {
        state.opening = openSessionOnce(child).finally(() => { state.opening = undefined; });
    }
    return state.opening;
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

const expiredAdmission = () => Object.assign(new Error('Upload session expired. Retry explicitly to start a new upload intake.'),
    { code: 'upload_session_expired' });

// Registers these files under the session parent in bounded pages. Each page
// is recorded only after the server acknowledges it, so a retry skips pages
// that succeeded and resumes with the still-missing item identities.
export const registerWaveItemsBatch = async (
    session: PersistedUploadSession, files: PersistedUploadFile[], includeIdentities = true,
): Promise<Map<string, {waveSessionId: string; waveItemId: string}>> => {
    await ensureOpened(session);
    const parent = waveParentFor(session);
    const state = parent ? stateFor(parent.id) : null;
    if (state?.expired) throw expiredAdmission();
    if (!parent || !state || state.discarding || state.closed) {
        throw new Error('Upload wave admission is unavailable. Retry before uploading files.');
    }
    // The common init path asks for identities inside the already-admitted
    // rolling window. Do not queue that local lookup behind a slower network
    // refill for future files.
    const needsRegistration = files.some(file => waveFileBelongs(session, file)
        && !state.registeredItems.has(file.waveItemKey || file.key));
    const registration = needsRegistration ? state.registrationPending.then(async () => {
        // Compute this inside the chain. A preceding look-ahead or init batch
        // may have admitted some of the same files while this caller waited.
        const items = files.filter(file => waveFileBelongs(session, file))
            .map(file => ({ itemId: file.waveItemKey || file.key, filename: file.name }))
            .filter(item => !state.registeredItems.has(item.itemId));
        for (let offset = 0; offset < items.length; offset += WAVE_REGISTER_MAX_BATCH) {
            const page = items.slice(offset, offset + WAVE_REGISTER_MAX_BATCH);
            let result;
            try {
                result = await registerWaveShadowItems(parent.id, page);
            } catch (error) {
                state.admissionError = error instanceof Error ? error : new Error('Upload admission failed.');
                throw error;
            }
            if (!result) throw new Error('Upload wave admission is unavailable. Retry before uploading files.');
            state.manifestRevision = result.manifestRevision;
            for (const item of page) state.registeredItems.add(item.itemId);
        }
    }) : Promise.resolve();
    // A transient failure rejects this caller without permanently poisoning
    // later admission attempts for the session.
    state.registrationPending = registration.catch(() => undefined);
    await registration;
    // Re-check after the network round trip -- an independent heartbeat
    // renewal can flip this mid-call. Never hand back a binding for a
    // session that's since expired/closed/discarding.
    if (state.expired) throw expiredAdmission();
    if (state.discarding || state.closed) {
        throw new Error('Upload wave admission is unavailable. Retry before uploading files.');
    }
    const out = new Map<string, {waveSessionId: string; waveItemId: string}>();
    // The upfront manifest pass needs only the acknowledgement. Avoid hashing
    // every item again for a return value its caller discards; init batches
    // derive their own small set of transport identities as they start.
    if (!includeIdentities) return out;
    for (const file of files) {
        if (!waveFileBelongs(session, file)) continue;
        out.set(file.key, { waveSessionId: parent.id, waveItemId: await wireItemId(file.waveItemKey || file.key) });
    }
    // Hashing the transport identity yields to the event loop. Cancellation
    // or expiry while it was in flight must not hand back a late binding.
    if (state.expired) throw expiredAdmission();
    if (state.discarding || state.closed || waveParentFor(session)?.id !== parent.id) {
        throw new Error('Upload wave admission is unavailable. Retry before uploading files.');
    }
    return out;
};

// Recomputes the wire identity for a file whose batch already registered it
// at init time (see registerWaveItemsBatch) -- no new registration call,
// just the same deterministic hash finalize needs to reference the item.
export const waveIdentityFor = async (session: PersistedUploadSession, file: PersistedUploadFile)
    : Promise<Record<string,string>> => {
    const parent = waveParentFor(session);
    if (!parent || !waveFileBelongs(session, file)) return {};
    return { waveSessionId: parent.id, waveItemId: await wireItemId(file.waveItemKey || file.key) };
};

// Called only from the parent's ordered chain. Failed batches remain unresolved
// and are retried during cleanup; replay is idempotent even after partial commits.
const resolveFiles = async (session: PersistedUploadSession, files: PersistedUploadFile[]): Promise<void> => {
    const parent = waveParentFor(session);
    if (!parent) return;
    const state = stateFor(parent.id);
    if (state.expired || state.discarding || !waveParentFor(session)) return;
    const outstanding = files.filter(file => waveFileBelongs(session, file)
        && state.registeredItems.has(file.waveItemKey || file.key)
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

// Called from runUploadSession's healthy/error tail; live Stop/Discard uses
// server-authoritative abandonment instead. Defensively
// resolves every file this module hasn't confirmed as settled -- anything
// not accepted/duplicate/failed is a cancellation (never started, or stopped
// mid-transfer) -- then closes intake. A session with items that
// still can't resolve (e.g. the shadow API was never reachable) simply never
// closes; that's an observational gap, not a failure this surfaces.
export const finalizeWaveShadowSession = (session: PersistedUploadSession): Promise<void> => serialized(session, async () => {
    const parent = waveParentFor(session);
    if (!parent) return;
    const state = stateFor(parent.id);
    if (state.closed || state.discarding) return;
    if (state.expired || state.discarding || !waveParentFor(session)) return;
    await resolveFiles(session, session.files);
    // Retry local checkpoint writes without repeating acknowledged HTTP calls.
    for (const file of session.files) {
        if (state.resolved.has(file.waveItemKey || file.key)) recordWaveItemResolved(session, file);
    }
    if (state.closed) return;
    // Only files actually registered gate completeness -- a file whose batch
    // never ran (session stopped before its turn) was never declared to the
    // server at all, so it can't block closing on "not yet resolved."
    if (session.files.some(file => waveFileBelongs(session, file)
        && state.registeredItems.has(file.waveItemKey || file.key)
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
export const quiesceWaveShadowSession = (session: PersistedUploadSession): void => {
    if (!waveLiveEnabled()) return;
    const parent = waveParentFor(session);
    if (!parent) return;
    stateFor(parent.id).discarding = true;
};

export const discardWaveShadowSession = async (session: PersistedUploadSession): Promise<void> => {
    const parent = waveParentFor(session);
    if (!parent) {
        if (waveLiveEnabled() && !session.waveAdmissionFailed) throw new Error('Upload recovery information is unavailable.');
        return;
    }
    if (!waveLiveEnabled() || await waveProtocol() === 'legacy') {
        stateFor(parent.id).discarding = false;
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
            let waitedForFinalizationDrain=false;
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
                    if (!legacyApi && !waitedForFinalizationDrain
                        && failure.message?.includes('A finalization is still in flight')) {
                        // Modern discard durably fences new finalizations, then
                        // gives already-admitted requests a 30-second drain
                        // window. Keep this Discard action alive through that
                        // window instead of asking the user to click again.
                        waitedForFinalizationDrain=true;
                        await new Promise(resolve => setTimeout(resolve,31_000));
                        continue;
                    }
                    if (!legacyApi && [404,405].includes(failure.status || 0)) {
                        legacyApi=true; continue;
                    }
                    if ([403].includes(failure.status || 0) || failure.message?.includes('Only the library owner may abandon intake')) {
                        const child=currentId===parent.id ? session : candidate;
                        if (!child) throw error;
                        // Older services restrict abandonment to owners. The
                        // resolve/close fallback below only ever resolves
                        // items already in registeredItems (see resolveFiles)
                        // and the server's own close() rejects an incomplete
                        // wave -- no separate pre-check needed here.
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
        // Cancellation is terminal locally even if the server acknowledgment
        // was lost. Retry Discard may finish cleanup, never restart intake.
        for (const state of Array.from(held)) state.discarding=true;
    }
};

// A lost finalize response may leave server-accepted files locally unresolved.
// Never rebind those results to cancelled just to stop the remaining upload.
export const stopWaveShadowSession = async (session: PersistedUploadSession): Promise<void> => {
    const parent=waveParentFor(session);
    quiesceWaveShadowSession(session);
    if (!waveLiveEnabled() || await waveProtocol()==='legacy') {
        if (parent) stateFor(parent.id).discarding = false;
        await finalizeWaveShadowSession(session);
        return;
    }
    if (parent && stateFor(parent.id).closed) return;
    await discardWaveShadowSession(session);
};

export const cancelQueuedWaveSelection = (files: File[]): Promise<void> => {
    const child = waveSelectionSession(files);
    return child ? stopWaveShadowSession(child) : Promise.resolve();
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
    if (state.expired) throw expiredAdmission();
    if (!state.opened || state.closed || state.discarding) return;
    if (!state.renewal) {
        state.renewal = renewUploadWaveLease(parent.id).then(result => observeLease(state,result))
            .catch(error => {
                if (state.leaseCheckAt !== undefined && Date.now() >= state.leaseCheckAt) {
                    state.expired = true;
                    throw expiredAdmission();
                }
                throw error;
            })
            .finally(() => { state.renewal = undefined; });
    }
    await state.renewal;
    if (state.expired) throw expiredAdmission();
};
// Lease expiry is terminal for this intake; only explicit retry creates a successor.

