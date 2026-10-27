import { beforeEach, expect, it, vi } from 'vitest';
import type { PersistedUploadSession } from '../types/browserProcessing';
const mocks = vi.hoisted(() => ({ scope: 'library-a', open: vi.fn(), register: vi.fn(), resolve: vi.fn(), close: vi.fn() }));
vi.mock('./sessionScope', () => ({ sessionScope: () => mocks.scope }));
vi.mock('./uploadWaveClient', () => ({ waveShadowEnabled: () => true, waveLiveEnabled: () => true,
    waveProtocol: async () => 'live',
    wireItemId: async (key: string) => `wire:${key}`, WAVE_SHADOW_MAX_PAGE: 2000, WAVE_RESOLVE_MAX_BATCH: 100,
    openWaveShadowSession: mocks.open, registerWaveShadowItems: mocks.register,
    resolveWaveShadowItems: async (id: string, items: any[]) => {
        const results = await Promise.all(items.map(item => mocks.resolve(id, item.itemId, item.outcome, item.filename)));
        return results.every(Boolean) ? results[results.length - 1] : null;
    }, closeWaveShadowSession: mocks.close }));
import { bindWaveChild, clearWaveOperationCachesForTests, declareWaveSelection, loadWaveOperation,
    retryWaveOperation, sliceWaveSelection } from './uploadWaveOperations';
import { __resetWaveShadowStateForTests, ensureWaveShadowSessionOpened, finalizeWaveShadowSession,
    cancelQueuedWaveSelection, liveWaveAdmission, acknowledgeWaveFinalizedFile } from './uploadWaveSync';
const child = (files: File[], id: string): PersistedUploadSession => {
    const session: PersistedUploadSession = { id, createdAt: 1, files: files.map((file, index) => ({
        key: `${id}:${index}`, name: file.name, size: file.size, type: file.type, lastModified: file.lastModified,
        uploadedBytes: file.size, skippedDuplicate: false, status: 'done' })) };
    bindWaveChild(files, session);
    return session;
};
beforeEach(() => {
    const storage = new Map<string, string>();
    vi.stubGlobal("localStorage", { getItem: (key: string) => storage.get(key) ?? null,
        setItem: (key: string, value: string) => storage.set(key, value), clear: () => storage.clear() });
    clearWaveOperationCachesForTests(); __resetWaveShadowStateForTests(); vi.clearAllMocks();
    mocks.scope = 'library-a';
    mocks.open.mockResolvedValue({ manifestRevision: 'open' });
    mocks.register.mockResolvedValue({ manifestRevision: 'registered' });
    mocks.resolve.mockResolvedValue({ manifestRevision: 'resolved' });
    mocks.close.mockResolvedValue({ state: 'closed' });
});
it('admits 901 items before transport and closes only after both 900/1 children settle', async () => {
    const files = Array.from({ length: 901 }, (_, i) => new File(['x'], `${i}.jpg`));
    const parent = declareWaveSelection(files)!;
    const first = child(sliceWaveSelection(files, 0, 900), 'child-1');
    const last = child(sliceWaveSelection(files, 900), 'child-2');
    await ensureWaveShadowSessionOpened(first);
    expect(mocks.open).toHaveBeenCalledWith(parent.id, 901);
    expect(mocks.register).toHaveBeenCalledTimes(1);
    await finalizeWaveShadowSession(first);
    expect(mocks.close).not.toHaveBeenCalled();
    clearWaveOperationCachesForTests(); __resetWaveShadowStateForTests();
    expect(loadWaveOperation(parent.id, mocks.scope)?.files).toHaveLength(901);
    await finalizeWaveShadowSession(last);
    expect(mocks.close).toHaveBeenCalledTimes(1);
    expect(mocks.close.mock.calls[0][0]).toBe(parent.id);
});
it('keeps failed API resolutions in the barrier until a retry is acknowledged', async () => {
    const files = [new File(['x'], 'one.jpg')]; declareWaveSelection(files);
    const session = child(files, 'child'); mocks.resolve.mockResolvedValueOnce(null);
    await finalizeWaveShadowSession(session); expect(mocks.close).not.toHaveBeenCalled();
    await finalizeWaveShadowSession(session); expect(mocks.close).toHaveBeenCalledTimes(1);
});
it('settles explicitly stopped queued children without losing the active child barrier', async () => {
    const files = [new File(['x'], 'one.jpg'), new File(['x'], 'two.jpg')]; declareWaveSelection(files);
    const first = child(sliceWaveSelection(files, 0, 1), 'first');
    await cancelQueuedWaveSelection(sliceWaveSelection(files, 1));
    expect(mocks.close).not.toHaveBeenCalled();
    await finalizeWaveShadowSession(first); expect(mocks.close).toHaveBeenCalledTimes(1);
    expect(mocks.resolve.mock.calls[0][2]).toBe('cancelled');
});
it('never sends a persisted parent under another library scope', async () => {
    const files = [new File(['x'], 'one.jpg')]; declareWaveSelection(files); const session = child(files, 'child');
    mocks.scope = 'library-b'; await finalizeWaveShadowSession(session);
    expect(mocks.open).not.toHaveBeenCalled(); expect(mocks.resolve).not.toHaveBeenCalled();
});
it('retries unfinished items in a fresh linked parent while preserving cached blob keys', async () => {
    const files = [new File(['x'], 'one.jpg'), new File(['x'], 'two.jpg')];
    const original = declareWaveSelection(files)!; const session = child(files, 'child');
    session.files[1].status = 'failed'; const retried = retryWaveOperation(session);
    expect(retried.files.map(file => file.key)).toEqual(session.files.map(file => file.key));
    expect(retried.waveOperationId).not.toBe(original.id);
    await ensureWaveShadowSessionOpened(retried);
    expect(mocks.open).toHaveBeenCalledWith(retried.waveOperationId, 1, original.id);
    await finalizeWaveShadowSession(retried);
    expect(mocks.resolve).toHaveBeenCalledTimes(1);
});

it('does not fall back to independent chunk admissions when the full parent exceeds browser storage', async () => {
    const files = Array.from({ length: 901 }, (_, i) => new File(['x'], `${i}.jpg`));
    vi.spyOn(localStorage, 'setItem').mockImplementationOnce(() => { throw new Error('quota'); });
    expect(declareWaveSelection(files)).toBeNull();
    const firstFiles = sliceWaveSelection(files, 0, 900);
    expect(declareWaveSelection(firstFiles)).toBeNull();
    await finalizeWaveShadowSession(child(firstFiles, 'first'));
    await finalizeWaveShadowSession(child(sliceWaveSelection(files, 900), 'last'));
    expect(mocks.open).not.toHaveBeenCalled();
});

it('admits another selection while the first is open without closing either parent early', async () => {
    const firstFiles = [new File(['a'], 'a.jpg'), new File(['b'], 'b.jpg')];
    const nextFiles = [new File(['c'], 'c.jpg')];
    const first = declareWaveSelection(firstFiles)!; const next = declareWaveSelection(nextFiles)!;
    await ensureWaveShadowSessionOpened(first); await ensureWaveShadowSessionOpened(next);
    expect(mocks.open.mock.calls.map(call => call[1])).toEqual([2, 1]);
    await finalizeWaveShadowSession(child(sliceWaveSelection(firstFiles, 0, 1), 'first-child'));
    expect(mocks.close).not.toHaveBeenCalled();
    await finalizeWaveShadowSession(child(nextFiles, 'next-child'));
    expect(mocks.close.mock.calls.map(call => call[0])).toEqual([next.id]);
    await finalizeWaveShadowSession(child(sliceWaveSelection(firstFiles, 1), 'last-child'));
    expect(mocks.close.mock.calls.map(call => call[0])).toEqual([next.id, first.id]);
});

it('can admit a fresh retry after parent storage becomes available again', async () => {
    const files = [new File(['x'], 'x.jpg')];
    vi.spyOn(localStorage, 'setItem').mockImplementationOnce(() => { throw new Error('quota'); });
    expect(declareWaveSelection(files)).toBeNull(); const session = child(files, 'failed-child');
    session.files[0].status = 'failed'; expect(session.waveAdmissionFailed).toBe(true);
    const retried = retryWaveOperation(session);
    await ensureWaveShadowSessionOpened(retried);
    expect(mocks.open).toHaveBeenCalledWith(retried.waveOperationId, 1);
});

it('binds live upload children across the 900 boundary to the same fully registered parent', async () => {
    const files = Array.from({ length: 901 }, (_, i) => new File(['x'], `${i}.jpg`));
    const parent = declareWaveSelection(files)!;
    const first = child(sliceWaveSelection(files, 0, 900), 'first-live');
    const last = child(sliceWaveSelection(files, 900), 'last-live');
    const a = await liveWaveAdmission(first, first.files[0]);
    const b = await liveWaveAdmission(last, last.files[0]);
    expect(a.waveSessionId).toBe(parent.id); expect(b.waveSessionId).toBe(parent.id);
    expect(a.waveItemId).not.toBe(b.waveItemId);
    expect(mocks.register).toHaveBeenCalledTimes(1);
    expect(mocks.open).toHaveBeenCalledTimes(1);
});
it('refuses live bytes when parent registration fails or the library changes', async () => {
    const files = [new File(['x'], 'a.jpg')]; declareWaveSelection(files);
    const current = child(files, 'failed-live'); mocks.register.mockResolvedValue(null);
    await expect(liveWaveAdmission(current, current.files[0])).rejects.toThrow('admission is unavailable');
    mocks.register.mockResolvedValue({ manifestRevision: 'registered' });
    mocks.scope = 'other-library';
    await expect(liveWaveAdmission(current, current.files[0])).rejects.toThrow('admission is unavailable');
});

it('uses live finalize receipts without sending browser resolution or close requests', async () => {
    const files = [new File(['x'], 'a.jpg'), new File(['y'], 'b.jpg')];
    const parent = declareWaveSelection(files)!;
    const current = child(files, 'receipt-child');
    await ensureWaveShadowSessionOpened(current);
    for (let index = 0; index < current.files.length; index++) {
        const file = current.files[index];
        await acknowledgeWaveFinalizedFile(current, file, { sessionId: parent.id,
            itemId: `wire:${file.waveItemKey}`, manifestRevision: `resolved-${index}`,
            state: index === 1 ? 'closed' : 'open' });
    }
    await finalizeWaveShadowSession(current);
    expect(mocks.resolve).not.toHaveBeenCalled();
    expect(mocks.close).not.toHaveBeenCalled();
});

it('rejects a receipt for another item and resolves the outstanding file during cleanup', async () => {
    const files = [new File(['x'], 'a.jpg')];
    const parent = declareWaveSelection(files)!;
    const current = child(files, 'wrong-receipt');
    await acknowledgeWaveFinalizedFile(current, current.files[0], { sessionId: parent.id,
        itemId: 'wrong', manifestRevision: 'wrong', state: 'closed' });
    await finalizeWaveShadowSession(current);
    expect(mocks.resolve).toHaveBeenCalledTimes(1);
});
