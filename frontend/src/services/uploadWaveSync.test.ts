import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { PersistedUploadSession } from '../types/browserProcessing';

const mocks = vi.hoisted(() => ({
    open: vi.fn(),
    abandon: vi.fn(),
    legacyAbandon: vi.fn(),
    live: false,
    register: vi.fn(),
    resolve: vi.fn(),
    batch: vi.fn(),
    close: vi.fn(),
}));

vi.mock('./uploadWaveClient', () => ({
    waveLiveEnabled: () => mocks.live,
    waveProtocol: async () => 'live',
    discardWaveSession: mocks.abandon,
    abandonWaveShadowSession: mocks.legacyAbandon,
    WAVE_RESOLVE_MAX_BATCH: 100,
    wireItemId: async (key: string) => `wire:${key}`,
    openWaveShadowSession: mocks.open,
    registerWaveShadowItems: mocks.register,
    resolveWaveShadowItems: async (id: string, items: any[]) => {
        mocks.batch(id, items);
        const results = await Promise.all(items.map(item => mocks.resolve(id, item.itemId, item.outcome, item.filename)));
        return results.every(Boolean) ? results[results.length - 1] : null;
    },
    closeWaveShadowSession: mocks.close,
}));

import {
    __resetWaveShadowStateForTests,
    ensureOpened,
    registerWaveItemsBatch,
    finalizeWaveShadowSession,
    discardWaveShadowSession,
    stopWaveShadowSession,
    resolveWaveShadowFile,
} from './uploadWaveSync';

function session(count: number, id: string): PersistedUploadSession {
    return {
        id, createdAt: 1700000000000,
        files: Array.from({ length: count }, (_, index) => ({
            key: `${id}:${index}`, name: `original-${index}.heic`, size: 50000,
            type: 'image/heic', lastModified: 1700000000000, uploadedBytes: 0,
            skippedDuplicate: false, status: 'pending' as const,
        })),
    };
}

beforeEach(() => {
    mocks.live = false;
    __resetWaveShadowStateForTests();
    vi.clearAllMocks();
    mocks.abandon.mockReset();
    mocks.legacyAbandon.mockReset();
    mocks.open.mockReset().mockResolvedValue({ waveId: 'wave-1', manifestRevision: 'rev-0' });
    mocks.register.mockImplementation(async (_sessionId: string, items: unknown[]) =>
        ({ manifestRevision: `rev-register-${items.length}`, registeredCount: items.length }));
    mocks.resolve.mockResolvedValue({ manifestRevision: 'rev-resolve' });
    mocks.close.mockResolvedValue({ state: 'closed' });
});

afterEach(() => { vi.restoreAllMocks(); });

it('coalesces 250 duplicate outcomes into three bounded requests without cleanup replays', async () => {
    const s = session(250, 'many-duplicates');
    s.files = s.files.map(file => ({ ...file, status: 'done', skippedDuplicate: true }));
    await registerWaveItemsBatch(s, s.files);
    await Promise.all(s.files.map(file => resolveWaveShadowFile(s, file)));
    expect(mocks.batch.mock.calls.map(([, items]) => items.length)).toEqual([100, 100, 50]);
    await finalizeWaveShadowSession(s);
    expect(mocks.batch).toHaveBeenCalledTimes(3);
    expect(mocks.close).toHaveBeenCalledTimes(1);
});

describe('registerWaveItemsBatch', () => {
    it('opens once and registers exactly the given batch in one call', async () => {
        const s = session(5, 'sess-a');
        const result = await registerWaveItemsBatch(s, s.files);
        expect(mocks.open).toHaveBeenCalledTimes(1);
        expect(mocks.open).toHaveBeenCalledWith('sess-a', 5);
        expect(mocks.register).toHaveBeenCalledTimes(1);
        expect(mocks.register.mock.calls[0][1]).toHaveLength(5);
        expect(result.size).toBe(5);
        expect(result.get(s.files[0].key)).toMatchObject({ waveSessionId: 'sess-a' });
    });

    it('does not re-open or re-register items already registered', async () => {
        const s = session(5, 'sess-b');
        await registerWaveItemsBatch(s, s.files);
        await registerWaveItemsBatch(s, s.files);
        expect(mocks.open).toHaveBeenCalledTimes(1);
        expect(mocks.register).toHaveBeenCalledTimes(1);
    });

    it('registers only the not-yet-registered subset when batches overlap', async () => {
        const s = session(10, 'sess-overlap');
        await registerWaveItemsBatch(s, s.files.slice(0, 5));
        await registerWaveItemsBatch(s, s.files.slice(3, 10));
        expect(mocks.register).toHaveBeenCalledTimes(2);
        expect(mocks.register.mock.calls[1][1]).toHaveLength(5); // files 5..9 -- 3..4 already registered, filtered out
    });

    it('a failed registration call rejects the whole batch and registers nothing', async () => {
        const s = session(3, 'sess-fail');
        mocks.register.mockResolvedValueOnce(null);
        await expect(registerWaveItemsBatch(s, s.files)).rejects.toThrow('admission is unavailable');
        // A retry re-attempts the same (still-unregistered) batch, not a resume offset.
        await registerWaveItemsBatch(s, s.files);
        expect(mocks.register).toHaveBeenCalledTimes(2);
        expect(mocks.register.mock.calls[1][1]).toHaveLength(3);
    });

    it('does nothing once the session is already closed', async () => {
        const s = session(1, 'sess-closed');
        await registerWaveItemsBatch(s, s.files);
        s.files[0].status = 'done';
        await finalizeWaveShadowSession(s);
        mocks.open.mockClear();
        await expect(registerWaveItemsBatch(s, s.files)).rejects.toThrow('admission is unavailable');
        expect(mocks.open).not.toHaveBeenCalled();
    });
});

describe('resolveWaveShadowFile', () => {
    it('resolves a done, non-duplicate file as accepted with its filename', async () => {
        const s = session(1, 'sess-d');
        await registerWaveItemsBatch(s, s.files);
        await resolveWaveShadowFile(s, { ...s.files[0], status: 'done', skippedDuplicate: false });
        expect(mocks.resolve).toHaveBeenCalledWith('sess-d', s.files[0].key, 'accepted', s.files[0].name);
    });

    it('resolves a done, duplicate file as duplicate', async () => {
        const s = session(1, 'sess-e');
        await registerWaveItemsBatch(s, s.files);
        await resolveWaveShadowFile(s, { ...s.files[0], status: 'done', skippedDuplicate: true });
        expect(mocks.resolve).toHaveBeenCalledWith('sess-e', s.files[0].key, 'duplicate', s.files[0].name);
    });

    it('resolves a failed file as failed without a filename', async () => {
        const s = session(1, 'sess-f');
        await registerWaveItemsBatch(s, s.files);
        await resolveWaveShadowFile(s, { ...s.files[0], status: 'failed' });
        expect(mocks.resolve).toHaveBeenCalledWith('sess-f', s.files[0].key, 'failed', undefined);
    });

    it('does not call resolve for a non-terminal status', async () => {
        const s = session(1, 'sess-g');
        await registerWaveItemsBatch(s, s.files);
        await resolveWaveShadowFile(s, { ...s.files[0], status: 'uploading' });
        expect(mocks.resolve).not.toHaveBeenCalled();
    });

    it('never resolves a file whose batch was never registered', async () => {
        const s = session(1, 'sess-unregistered');
        await resolveWaveShadowFile(s, { ...s.files[0], status: 'done' });
        expect(mocks.resolve).not.toHaveBeenCalled();
    });
});

describe('finalizeWaveShadowSession', () => {
    it('resolves every registered outstanding file as cancelled, then closes', async () => {
        const s = session(3, 'sess-h');
        await registerWaveItemsBatch(s, s.files);
        s.files[0].status = 'done';
        s.files[1].status = 'failed';
        // files[2] stays 'pending' -- e.g. the upload was stopped mid-transfer.
        await finalizeWaveShadowSession(s);
        expect(mocks.resolve).toHaveBeenCalledWith('sess-h', s.files[0].key, 'accepted', s.files[0].name);
        expect(mocks.resolve).toHaveBeenCalledWith('sess-h', s.files[1].key, 'failed', undefined);
        expect(mocks.resolve).toHaveBeenCalledWith('sess-h', s.files[2].key, 'cancelled', undefined);
        expect(mocks.close).toHaveBeenCalledTimes(1);
    });

    it('does nothing if nothing was ever registered -- no open, no resolve, no close', async () => {
        const s = session(1, 'sess-i');
        await finalizeWaveShadowSession(s);
        expect(mocks.open).not.toHaveBeenCalled();
        expect(mocks.resolve).not.toHaveBeenCalled();
        expect(mocks.close).not.toHaveBeenCalled();
    });

    it('does not close when no manifest revision was ever observed', async () => {
        const s = session(1, 'sess-j');
        mocks.open.mockResolvedValueOnce({ waveId: 'wave-1', manifestRevision: undefined });
        mocks.resolve.mockResolvedValue(null); // shadow call failed
        await registerWaveItemsBatch(s, s.files);
        s.files[0].status = 'done';
        await finalizeWaveShadowSession(s);
        expect(mocks.close).not.toHaveBeenCalled();
    });

    it('is idempotent -- calling it twice only closes once', async () => {
        const s = session(1, 'sess-k');
        await registerWaveItemsBatch(s, s.files);
        s.files[0].status = 'done';
        await finalizeWaveShadowSession(s);
        await finalizeWaveShadowSession(s);
        expect(mocks.close).toHaveBeenCalledTimes(1);
    });
});

it('a batch registers, resolves and closes end to end once its file is done', async () => {
    const s = session(1, 'end-to-end');
    const events: string[] = [];
    mocks.register.mockImplementation(async () => {
        events.push('registered');
        return { manifestRevision: 'registered', registeredCount: 1 };
    });
    let nextRevision = 0;
    mocks.resolve.mockImplementation(async () => {
        events.push('resolved');
        return { manifestRevision: `resolved-${++nextRevision}` };
    });
    mocks.close.mockImplementation(async (_id: string, revision: string) => {
        events.push('closed');
        expect(revision).toBe(`resolved-${nextRevision}`);
        return { state: 'closed' };
    });
    await registerWaveItemsBatch(s, s.files);
    const file = { ...s.files[0], status: 'done' as const };
    await resolveWaveShadowFile(s, file);
    await finalizeWaveShadowSession({ ...s, files: [file] });
    expect(events).toEqual(['registered', 'resolved', 'closed']);
    expect(mocks.open).toHaveBeenCalledTimes(1);
});

it('admits the next file without waiting for a slow duplicate resolution', async () => {
    const s = session(2, 'slow-duplicate');
    await registerWaveItemsBatch(s, s.files);
    let release!: (value: { manifestRevision: string }) => void;
    mocks.resolve.mockImplementationOnce(() => new Promise(resolve => { release = resolve; }));
    const resolving = resolveWaveShadowFile(s, { ...s.files[0], status: 'done', skippedDuplicate: true });
    await vi.waitFor(() => expect(mocks.resolve).toHaveBeenCalledTimes(1));
    // A stalled outcome request must not stall a fresh registration call.
    await registerWaveItemsBatch(s, s.files);
    expect(mocks.register).toHaveBeenCalledTimes(1); // already fully registered -- no-op
    release({ manifestRevision: 'duplicate' });
    await resolving;
});

it('resolves duplicate and failed files once across callbacks and cleanup', async () => {
    const s = session(2, 'settled-files');
    await registerWaveItemsBatch(s, s.files);
    s.files[0] = { ...s.files[0], status: 'done', skippedDuplicate: true };
    s.files[1].status = 'failed';
    await Promise.all(s.files.flatMap(file => [resolveWaveShadowFile(s, file), resolveWaveShadowFile(s, file)]));
    await finalizeWaveShadowSession(s);
    expect(mocks.resolve).toHaveBeenCalledTimes(2);
    expect(mocks.close).toHaveBeenCalledTimes(1);
});

it('retries an unacknowledged outcome at cleanup and holds close until it succeeds', async () => {
    const s = session(1, 'retry-resolution');
    await registerWaveItemsBatch(s, s.files);
    s.files[0].status = 'failed';
    mocks.resolve.mockResolvedValueOnce(null).mockResolvedValueOnce(null);
    await resolveWaveShadowFile(s, s.files[0]);
    await finalizeWaveShadowSession(s);
    expect(mocks.close).not.toHaveBeenCalled();
    await finalizeWaveShadowSession(s);
    expect(mocks.resolve).toHaveBeenCalledTimes(3);
    expect(mocks.close).toHaveBeenCalledTimes(1);
});

it('discard surfaces server failure and can retry from the existing parent', async () => {
    mocks.live = true;
    const s = session(25, 'retry-discard');
    mocks.abandon.mockRejectedValueOnce(new Error('storage unavailable'))
        .mockResolvedValueOnce({manifestRevision: 'closed', state: 'abandoned'});
    await expect(discardWaveShadowSession(s)).rejects.toThrow('storage unavailable');
    await expect(discardWaveShadowSession(s)).resolves.toBeUndefined();
    expect(mocks.register).not.toHaveBeenCalled();
});

it('library members retain resolve/close discard when owner abandonment is forbidden', async () => {
    mocks.live = true;
    const s = session(1, 'member-discard');
    await registerWaveItemsBatch(s, s.files);
    mocks.abandon.mockRejectedValue(Object.assign(new Error('forbidden'), {status: 403}));
    await discardWaveShadowSession(s);
    expect(mocks.resolve).toHaveBeenCalledWith(s.id, s.files[0].key, 'cancelled', undefined);
    expect(mocks.close).toHaveBeenCalledTimes(1);
});

it('member cancellation never resolves unregistered files when owner abandonment is forbidden', async () => {
    mocks.live = true;
    const s = session(450, 'member-partial-discard');
    mocks.abandon.mockRejectedValue(Object.assign(new Error('forbidden'), {status: 403}));
    await expect(discardWaveShadowSession(s)).rejects.toThrow('forbidden');
    expect(mocks.register).not.toHaveBeenCalled();
    expect(mocks.resolve).not.toHaveBeenCalled();
});

it('refreshes a stale discard revision after an in-flight finalize wins', async () => {
    mocks.live = true;
    const s = session(1, 'finalize-race');
    mocks.open.mockResolvedValueOnce({manifestRevision: 'before-finalize'})
        .mockResolvedValueOnce({manifestRevision: 'after-finalize'});
    mocks.abandon.mockRejectedValueOnce(new Error('Abandonment revision is stale'))
        .mockResolvedValueOnce({manifestRevision: 'closed', state: 'abandoned'});
    await discardWaveShadowSession(s);
    expect(mocks.abandon.mock.calls[1]).toEqual([s.id, undefined]);
    expect(mocks.open).not.toHaveBeenCalled();
});

it('discard never reopens a never-admitted retry whose ancestor is missing', async () => {
    mocks.live=true;
    const s=session(1,'never-admitted-retry');
    s.resumedFromSessionId='missing-ancestor';
    mocks.open.mockRejectedValue(new Error('Upload admission failed: Finalized photo not found'));
    mocks.abandon.mockReset().mockResolvedValue({state:'absent',manifestRevision:''});
    await expect(discardWaveShadowSession(s)).resolves.toBeUndefined();
    expect(mocks.open).not.toHaveBeenCalled();
    expect(mocks.register).not.toHaveBeenCalled();
    expect(mocks.abandon).toHaveBeenCalledWith(s.id,'missing-ancestor');
    expect(mocks.abandon).toHaveBeenCalledWith('missing-ancestor',undefined);
});

it('falls back to exact-revision abandonment when the previous API has no discard route', async () => {
    mocks.live=true;
    const s=session(1,'old-api');
    mocks.abandon.mockReset().mockRejectedValue(Object.assign(new Error('Not found'),{status:404}));
    mocks.open.mockReset().mockResolvedValue({manifestRevision:'exact-revision',state:'open'});
    mocks.legacyAbandon.mockReset().mockResolvedValue({state:'abandoned',manifestRevision:'closed'});
    await discardWaveShadowSession(s);
    expect(mocks.open).toHaveBeenCalledWith(s.id,1,undefined);
    expect(mocks.legacyAbandon).toHaveBeenCalledWith(s.id,'exact-revision');
});

it('does not treat admission failure on an older API as successful discard', async () => {
    mocks.live=true;
    const s=session(1,'old-api-no-queue');
    mocks.abandon.mockReset().mockRejectedValue(Object.assign(new Error('Not found'),{status:404}));
    mocks.open.mockReset().mockRejectedValue(new Error('Upload expiry unavailable'));
    await expect(discardWaveShadowSession(s)).rejects.toThrow('Upload expiry unavailable');
    expect(mocks.legacyAbandon).not.toHaveBeenCalled();
});

it('Stop falls back to server closure when a lost finalize response conflicts with cancellation', async () => {
    mocks.live=true;
    const s=session(2,'stop-lost-response');
    await registerWaveItemsBatch(s, s.files);
    mocks.resolve.mockRejectedValue(new Error('Resolved intake item cannot be rebound'));
    mocks.abandon.mockResolvedValue({state:'abandoned',manifestRevision:'stopped'});
    await expect(stopWaveShadowSession(s)).resolves.toBeUndefined();
    expect(mocks.abandon).toHaveBeenCalledWith('stop-lost-response',undefined);
    expect(mocks.close).not.toHaveBeenCalled();
});

it('Stop surfaces failed server closure so the caller keeps its recovery checkpoint', async () => {
    mocks.live=true;
    const s=session(1,'stop-retry-closure');
    await registerWaveItemsBatch(s, s.files);
    mocks.resolve.mockRejectedValue(new Error('Resolved intake item cannot be rebound'));
    mocks.abandon.mockRejectedValueOnce(new Error('storage unavailable'))
        .mockResolvedValueOnce({state:'abandoned',manifestRevision:'stopped'});
    await expect(stopWaveShadowSession(s)).rejects.toThrow('storage unavailable');
    await expect(stopWaveShadowSession(s)).resolves.toBeUndefined();
    expect(mocks.abandon).toHaveBeenCalledTimes(2);
});

it('ensureOpened opens once and is idempotent', async () => {
    const s = session(3, 'open-only');
    await ensureOpened(s);
    await ensureOpened(s);
    expect(mocks.open).toHaveBeenCalledTimes(1);
    expect(mocks.open).toHaveBeenCalledWith('open-only', 3);
    expect(mocks.register).not.toHaveBeenCalled();
});
