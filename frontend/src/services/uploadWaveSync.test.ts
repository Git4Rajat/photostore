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
    WAVE_SHADOW_MAX_PAGE: 2000, WAVE_RESOLVE_MAX_BATCH: 100,
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
    ensureWaveShadowSessionOpened,
    finalizeWaveShadowSession,
    discardWaveShadowSession,
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

it('coalesces 250 duplicate outcomes into three bounded requests without cleanup replays', async () => {
    const s = session(250, 'many-duplicates');
    s.files = s.files.map(file => ({ ...file, status: 'done', skippedDuplicate: true }));
    await Promise.all(s.files.map(file => resolveWaveShadowFile(s, file)));
    expect(mocks.batch.mock.calls.map(([, items]) => items.length)).toEqual([100, 100, 50]);
    await finalizeWaveShadowSession(s);
    expect(mocks.batch).toHaveBeenCalledTimes(3);
    expect(mocks.close).toHaveBeenCalledTimes(1);
});

it('retries registration from the last acknowledged HTTP batch', async () => {
    const s = session(4005, 'registration-progress');
    mocks.register.mockResolvedValueOnce({ manifestRevision: 'first' }).mockResolvedValueOnce(null);
    await ensureWaveShadowSessionOpened(s);
    await ensureWaveShadowSessionOpened(s);
    expect(mocks.register.mock.calls.map(([, items]) => items[0].itemId))
        .toEqual([s.files[0].key, s.files[2000].key, s.files[2000].key, s.files[4000].key]);
});

afterEach(() => { vi.restoreAllMocks(); });

describe('ensureWaveShadowSessionOpened', () => {
    it('opens once and registers every item, paged at WAVE_SHADOW_MAX_PAGE', async () => {
        const s = session(4005, 'sess-a');
        await ensureWaveShadowSessionOpened(s);
        expect(mocks.open).toHaveBeenCalledTimes(1);
        expect(mocks.open).toHaveBeenCalledWith('sess-a', 4005);
        expect(mocks.register).toHaveBeenCalledTimes(3); // 2000 + 2000 + 5
        expect(mocks.register.mock.calls[0][1]).toHaveLength(2000);
        expect(mocks.register.mock.calls[2][1]).toHaveLength(5);
    });

    it('does not re-open or re-register on a second call once fully registered', async () => {
        const s = session(5, 'sess-b');
        await ensureWaveShadowSessionOpened(s);
        await ensureWaveShadowSessionOpened(s);
        expect(mocks.open).toHaveBeenCalledTimes(1);
        expect(mocks.register).toHaveBeenCalledTimes(1);
    });

    it('resumes registration from where it left off after a failed page', async () => {
        const s = session(2005, 'sess-c');
        mocks.register.mockResolvedValueOnce(null); // first page fails (network/disabled)
        await ensureWaveShadowSessionOpened(s);
        expect(mocks.register).toHaveBeenCalledTimes(1);
        await ensureWaveShadowSessionOpened(s);
        expect(mocks.register).toHaveBeenCalledTimes(3); // retried first page + second page
    });

    it('does nothing once the session is already closed', async () => {
        const s = session(1, 'sess-closed');
        await ensureWaveShadowSessionOpened(s);
        s.files[0].status = 'done';
        await finalizeWaveShadowSession(s);
        mocks.open.mockClear();
        await ensureWaveShadowSessionOpened(s);
        expect(mocks.open).not.toHaveBeenCalled();
    });
});

describe('resolveWaveShadowFile', () => {
    it('resolves a done, non-duplicate file as accepted with its filename', async () => {
        const s = session(1, 'sess-d');
        await resolveWaveShadowFile(s, { ...s.files[0], status: 'done', skippedDuplicate: false });
        expect(mocks.resolve).toHaveBeenCalledWith('sess-d', s.files[0].key, 'accepted', s.files[0].name);
    });

    it('resolves a done, duplicate file as duplicate', async () => {
        const s = session(1, 'sess-e');
        await resolveWaveShadowFile(s, { ...s.files[0], status: 'done', skippedDuplicate: true });
        expect(mocks.resolve).toHaveBeenCalledWith('sess-e', s.files[0].key, 'duplicate', s.files[0].name);
    });

    it('resolves a failed file as failed without a filename', async () => {
        const s = session(1, 'sess-f');
        await resolveWaveShadowFile(s, { ...s.files[0], status: 'failed' });
        expect(mocks.resolve).toHaveBeenCalledWith('sess-f', s.files[0].key, 'failed', undefined);
    });

    it('does not call resolve for a non-terminal status', async () => {
        const s = session(1, 'sess-g');
        await resolveWaveShadowFile(s, { ...s.files[0], status: 'uploading' });
        expect(mocks.resolve).not.toHaveBeenCalled();
    });
});

describe('finalizeWaveShadowSession', () => {
    it('resolves every outstanding file as cancelled, then closes', async () => {
        const s = session(3, 'sess-h');
        await ensureWaveShadowSessionOpened(s);
        s.files[0].status = 'done';
        s.files[1].status = 'failed';
        // files[2] stays 'pending' -- e.g. the upload was stopped mid-transfer.
        await finalizeWaveShadowSession(s);
        expect(mocks.resolve).toHaveBeenCalledWith('sess-h', s.files[0].key, 'accepted', s.files[0].name);
        expect(mocks.resolve).toHaveBeenCalledWith('sess-h', s.files[1].key, 'failed', undefined);
        expect(mocks.resolve).toHaveBeenCalledWith('sess-h', s.files[2].key, 'cancelled', undefined);
        expect(mocks.close).toHaveBeenCalledTimes(1);
    });

    it('retries opening before finalization if the initial open was missed', async () => {
        const s = session(1, 'sess-i');
        await finalizeWaveShadowSession(s);
        expect(mocks.open).toHaveBeenCalled();
        expect(mocks.resolve).toHaveBeenCalled();
        expect(mocks.close).toHaveBeenCalled();
    });

    it('does not close when no manifest revision was ever observed', async () => {
        const s = session(1, 'sess-j');
        mocks.open.mockResolvedValueOnce({ waveId: 'wave-1', manifestRevision: undefined });
        mocks.register.mockResolvedValue(null); // shadow call failed
        mocks.resolve.mockResolvedValue(null); // shadow call failed
        await ensureWaveShadowSessionOpened(s);
        s.files[0].status = 'done';
        await finalizeWaveShadowSession(s);
        expect(mocks.close).not.toHaveBeenCalled();
    });

    it('is idempotent -- calling it twice only closes once', async () => {
        const s = session(1, 'sess-k');
        await ensureWaveShadowSessionOpened(s);
        s.files[0].status = 'done';
        await finalizeWaveShadowSession(s);
        await finalizeWaveShadowSession(s);
        expect(mocks.close).toHaveBeenCalledTimes(1);
    });
});

it('serializes a delayed open, registration, live resolution and close', async () => {
    const s = session(1, 'serialized-session');
    const events: string[] = [];
    let releaseOpen!: (value: { waveId: string; manifestRevision: string }) => void;
    mocks.open.mockImplementation(() => new Promise(resolve => { releaseOpen = resolve; }));
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
    const opening = ensureWaveShadowSessionOpened(s);
    const file = { ...s.files[0], status: 'done' as const };
    const resolving = resolveWaveShadowFile(s, file);
    const closing = finalizeWaveShadowSession({ ...s, files: [file] });
    await Promise.resolve();
    expect(events).toEqual([]);
    expect(mocks.resolve).not.toHaveBeenCalled();
    releaseOpen({ waveId: 'wave', manifestRevision: 'opened' });
    await Promise.all([opening, resolving, closing]);
    expect(events).toEqual(['registered', 'resolved', 'closed']);
    expect(mocks.open).toHaveBeenCalledTimes(1);
});

it('admits the next file without waiting for a slow duplicate resolution', async () => {
    const s = session(2, 'slow-duplicate');
    await ensureWaveShadowSessionOpened(s);
    let release!: (value: { manifestRevision: string }) => void;
    mocks.resolve.mockImplementationOnce(() => new Promise(resolve => { release = resolve; }));
    const resolving = resolveWaveShadowFile(s, { ...s.files[0], status: 'done', skippedDuplicate: true });
    await vi.waitFor(() => expect(mocks.resolve).toHaveBeenCalledTimes(1));
    // A stalled outcome request must not stall admission for the next hash check.
    await ensureWaveShadowSessionOpened(s);
    expect(mocks.register).toHaveBeenCalledTimes(1);
    release({ manifestRevision: 'duplicate' });
    await resolving;
});

it('resolves duplicate and failed files once across callbacks and cleanup', async () => {
    const s = session(2, 'settled-files');
    s.files[0] = { ...s.files[0], status: 'done', skippedDuplicate: true };
    s.files[1].status = 'failed';
    await Promise.all(s.files.flatMap(file => [resolveWaveShadowFile(s, file), resolveWaveShadowFile(s, file)]));
    await finalizeWaveShadowSession(s);
    expect(mocks.resolve).toHaveBeenCalledTimes(2);
    expect(mocks.close).toHaveBeenCalledTimes(1);
});

it('retries an unacknowledged outcome at cleanup and holds close until it succeeds', async () => {
    const s = session(1, 'retry-resolution');
    s.files[0].status = 'failed';
    mocks.resolve.mockResolvedValueOnce(null).mockResolvedValueOnce(null);
    await resolveWaveShadowFile(s, s.files[0]);
    await finalizeWaveShadowSession(s);
    expect(mocks.close).not.toHaveBeenCalled();
    await finalizeWaveShadowSession(s);
    expect(mocks.resolve).toHaveBeenCalledTimes(3);
    expect(mocks.close).toHaveBeenCalledTimes(1);
});


it('discard waits for restored registration but does not replay the remaining manifest', async () => {
    mocks.live = true;
    const s = session(5000, 'restored-discard');
    let release!: (value: {manifestRevision: string; registeredCount: number}) => void;
    mocks.register.mockImplementationOnce(() => new Promise(resolve => { release = resolve; }));
    mocks.abandon.mockResolvedValueOnce({manifestRevision: 'cancel-page', state: 'open', abandonmentNeedsMore: true})
        .mockResolvedValueOnce({manifestRevision: 'cancelled', state: 'abandoned'});
    const restoring = ensureWaveShadowSessionOpened(s);
    for (let turn = 0; turn < 20 && !release; turn += 1) await Promise.resolve();
    expect(release).toBeTypeOf('function');
    const discarding = discardWaveShadowSession(s);
    await Promise.resolve();
    expect(mocks.abandon).not.toHaveBeenCalled();
    release({manifestRevision: 'registered', registeredCount: 2000});
    await restoring; await discarding;
    expect(mocks.register).toHaveBeenCalledTimes(1);
    expect(mocks.abandon).toHaveBeenCalledTimes(2);
    expect(mocks.abandon.mock.calls[1]).toEqual([s.id, undefined]);
    expect(mocks.resolve).not.toHaveBeenCalled();
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
    mocks.abandon.mockRejectedValue(Object.assign(new Error('forbidden'), {status: 403}));
    await discardWaveShadowSession(s);
    expect(mocks.resolve).toHaveBeenCalledWith(s.id, s.files[0].key, 'cancelled', undefined);
    expect(mocks.close).toHaveBeenCalledTimes(1);
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
