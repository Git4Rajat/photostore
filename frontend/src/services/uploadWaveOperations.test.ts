import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import type { PersistedUploadSession } from '../types/browserProcessing';
const mocks = vi.hoisted(() => ({ scope: 'library-a', renew: vi.fn(), discard: vi.fn(), open: vi.fn(), register: vi.fn(), resolve: vi.fn(), close: vi.fn() }));
vi.mock('./sessionScope', () => ({ sessionScope: () => mocks.scope }));
vi.mock('./uploadWaveClient', () => ({ waveShadowEnabled: () => true, waveLiveEnabled: () => true,
    discardWaveSession:mocks.discard,
    waveProtocol: async () => 'live', renewUploadWaveLease: mocks.renew,
    wireItemId: async (key: string) => `wire:${key}`, WAVE_SHADOW_MAX_PAGE: 2000, WAVE_RESOLVE_MAX_BATCH: 100,
    openWaveShadowSession: mocks.open, registerWaveShadowItems: mocks.register,
    resolveWaveShadowItems: async (id: string, items: any[]) => {
        const results = await Promise.all(items.map(item => mocks.resolve(id, item.itemId, item.outcome, item.filename)));
        return results.every(Boolean) ? results[results.length - 1] : null;
    }, closeWaveShadowSession: mocks.close }));
import { bindWaveChild, clearWaveOperationCachesForTests, declareWaveSelection, loadWaveOperation,
    retryWaveOperation, sliceWaveSelection, waveParentResolved } from './uploadWaveOperations';
import { __resetWaveShadowStateForTests, ensureWaveShadowSessionOpened, finalizeWaveShadowSession,
    discardWaveShadowSession, cancelQueuedWaveSelection, liveWaveAdmission, heartbeatWaveSession, acknowledgeWaveFinalizedFile } from './uploadWaveSync';
const child = (files: File[], id: string): PersistedUploadSession => {
    const session: PersistedUploadSession = { id, createdAt: 1, files: files.map((file, index) => ({
        key: `${id}:${index}`, name: file.name, size: file.size, type: file.type, lastModified: file.lastModified,
        uploadedBytes: file.size, skippedDuplicate: false, status: 'done' })) };
    bindWaveChild(files, session);
    return session;
};
afterEach(() => { vi.useRealTimers(); });
beforeEach(() => {
    const storage = new Map<string, string>();
    vi.stubGlobal("localStorage", { getItem: (key: string) => storage.get(key) ?? null,
        setItem: (key: string, value: string) => storage.set(key, value), clear: () => storage.clear(),
        removeItem: (key: string) => storage.delete(key), key: (index: number) => Array.from(storage.keys())[index] ?? null,
        get length() { return storage.size; } });
    clearWaveOperationCachesForTests(); __resetWaveShadowStateForTests(); vi.clearAllMocks();
    mocks.scope = 'library-a';
    mocks.discard.mockReset().mockResolvedValue({state:'absent',manifestRevision:''});
    mocks.renew.mockResolvedValue({leaseActive:true,leaseExpiresAt:1300,serverNow:1000});
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

it('preserves an admission error after Stop and permits registration to recover', async () => {
    const stopped = [new File(['old'], 'stopped.webp')];
    declareWaveSelection(stopped);
    await cancelQueuedWaveSelection(stopped);
    const files = [new File(['new'], '000201.webp')];
    declareWaveSelection(files);
    const current = child(files, 'after-stop');
    mocks.register.mockRejectedValueOnce(new Error('Upload admission failed: Library cleanup is in progress'));
    await expect(liveWaveAdmission(current, current.files[0])).rejects.toThrow('Library cleanup is in progress');
    await expect(liveWaveAdmission(current, current.files[0])).resolves.toHaveProperty('waveSessionId');
});

it('recovers quota space by compacting legacy manifests without removing unfinished identities or receipts', async () => {
    const prefix = 'photostore.upload.wave-operation.v1:';
    const oldKey = prefix + JSON.stringify([mocks.scope, 'old']);
    const file = { key: 'old-0', name: 'unfinished.webp', previewDataUrl: 'x'.repeat(12000),
        size: 500000, type: 'image/webp', status: 'pending' };
    localStorage.setItem(oldKey, JSON.stringify({ id: 'old', scope: mocks.scope, createdAt: 1, files: [file] }));
    localStorage.setItem(oldKey + ':old-0', 'resolved');
    localStorage.setItem('photostore.upload.session', 'keep checkpoint');
    const save = localStorage.setItem.bind(localStorage);
    vi.spyOn(localStorage, 'setItem').mockImplementation((storageKey, value) => {
        const total = Array.from({ length: localStorage.length }, (_, index) => localStorage.key(index)!)
            .filter(key => key !== storageKey).reduce((sum, key) => sum + localStorage.getItem(key)!.length, value.length);
        if (total > 12500) throw new DOMException('Quota exceeded', 'QuotaExceededError');
        save(storageKey, value);
    });
    const files = Array.from({ length: 20 }, (_, i) => new File(['x'], `${i}.webp`));
    expect(declareWaveSelection(files)).not.toBeNull();
    const restored = loadWaveOperation('old', mocks.scope)!;
    expect(restored.files[0]).toMatchObject({ key: 'old-0', name: 'unfinished.webp' });
    expect(localStorage.getItem(oldKey)!.length).toBeLessThan(300);
    expect(localStorage.getItem(oldKey + ':old-0')).toBe('resolved');
    expect(localStorage.getItem('photostore.upload.session')).toBe('keep checkpoint');
});

it('retires only confirmed closed parents and preserves retry linkage after reload', async () => {
    const files = [new File(['x'], 'retry.webp')];
    const parent = declareWaveSelection(files)!;
    const current = child(files, 'closed-child');
    current.files[0].status = 'failed';
    mocks.close.mockResolvedValueOnce(null);
    await finalizeWaveShadowSession(current);
    expect(loadWaveOperation(parent.id, mocks.scope)!.files).toHaveLength(1);
    await finalizeWaveShadowSession(current);
    expect(localStorage.length).toBe(1);
    clearWaveOperationCachesForTests(); __resetWaveShadowStateForTests();
    expect(loadWaveOperation(parent.id, mocks.scope)).toMatchObject({ closed: true, files: [] });
    expect(waveParentResolved(current)).toBe(true);
    const retry = retryWaveOperation(current);
    mocks.open.mockClear();
    await ensureWaveShadowSessionOpened(retry);
    expect(mocks.open).toHaveBeenCalledTimes(1);
    expect(mocks.open).toHaveBeenCalledWith(retry.waveOperationId, 1, parent.id);
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

it('repeated lease expiry resumes only unfinished files with a fresh linked parent', async () => {
    const files=[new File(['a'],'a.jpg'),new File(['b'],'b.jpg')];
    declareWaveSelection(files);
    const current=child(files,'resume');
    current.files[1].status='pending';
    vi.useFakeTimers({toFake:['Date']});
    mocks.open.mockResolvedValueOnce({manifestRevision:'open',leaseActive:true,leaseExpiresAt:1000,serverNow:1000})
        .mockResolvedValue({manifestRevision:'new',leaseActive:true,leaseExpiresAt:1300,serverNow:1000});
    // First admission observes the timed-out server lease before any bytes.
    mocks.renew.mockResolvedValue({leaseActive:false});
    const ids=[current.waveOperationId];
    for (let i=0;i<3;i+=1) {
        if (i) vi.setSystemTime(Date.now()+301000);
        const previous=current.waveOperationId;
        const rebind=vi.fn();
        const [a,b]=await Promise.all([
            liveWaveAdmission(current,current.files[1],rebind),
            liveWaveAdmission(current,current.files[1],rebind),
        ]);
        expect(a).toEqual(b);
        expect(current.waveOperationId).not.toBe(previous);
        expect(rebind).toHaveBeenCalledTimes(1);
        expect(mocks.open).toHaveBeenLastCalledWith(current.waveOperationId,1,previous);
        expect(loadWaveOperation(current.waveOperationId!,mocks.scope)?.files.map(file => file.name)).toEqual(['b.jpg']);
        expect(current.files[0].waveItemKey).toContain(ids[0]);
        ids.push(current.waveOperationId);
    }
    expect(new Set(ids).size).toBe(4);
});

it('restore detects server-closed intake without replaying its old manifest', async () => {
    const files=[new File(['a'],'a.jpg')];declareWaveSelection(files);
    const current=child(files,'restored');current.files[0].status='pending';
    const previous=current.waveOperationId;
    mocks.open.mockResolvedValueOnce({manifestRevision:'expired',state:'abandoned',leaseActive:false})
        .mockResolvedValue({manifestRevision:'new',leaseActive:true,leaseExpiresAt:1300,serverNow:1000});
    await liveWaveAdmission(current,current.files[0]);
    expect(mocks.register).toHaveBeenCalledTimes(1);
    expect(mocks.register.mock.calls[0][0]).not.toBe(previous);
});


it('rebinds a progress-store copy used by automatic retries after expiry', async () => {
    const files=[new File(['a'],'a.jpg')];declareWaveSelection(files);
    const current=child(files,'retry-copy');current.files[0].status='failed';
    const retryCopy={...current.files[0]};
    mocks.open.mockResolvedValueOnce({manifestRevision:'expired',leaseActive:false})
        .mockResolvedValue({manifestRevision:'new',leaseActive:true,leaseExpiresAt:1300,serverNow:1000});
    const binding=await liveWaveAdmission(current,retryCopy);
    expect(retryCopy.waveItemKey).toBe(current.files[0].waveItemKey);
    expect(binding.waveItemId).toBe(`wire:${current.files[0].waveItemKey}`);
});


it('coalesces concurrent heartbeat renewals for the same active parent', async () => {
    const files=[new File(['a'],'a.jpg')];declareWaveSelection(files);
    const current=child(files,'heartbeat');current.files[0].status='pending';
    await liveWaveAdmission(current,current.files[0]);
    let complete!: (value: any) => void;
    mocks.renew.mockImplementationOnce(() => new Promise(resolve => {complete=resolve;}));
    const first=heartbeatWaveSession(current), second=heartbeatWaveSession(current);
    for (let i=0;i<10 && !complete;i++) await Promise.resolve();
    expect(mocks.renew).toHaveBeenCalledTimes(1);
    complete({leaseActive:true,leaseExpiresAt:1300,serverNow:1000});
    await Promise.all([first,second]);
});


it('keeps the retry checkpoint until its unleased earlier parent has also closed', async () => {
    const files=[new File(['a'],'a.jpg')];declareWaveSelection(files);
    const original=child(files,'original');original.files[0].status='failed';
    const retry=retryWaveOperation(original);
    const root=retry.waveOperationId!;
    mocks.discard.mockImplementation(async (id:string) => {
        if (id===root) return {state:'absent',manifestRevision:'',resumedFromSessionId:original.waveOperationId};
        throw new Error('Earlier parent finalization still active');
    });
    await expect(discardWaveShadowSession(retry)).rejects.toThrow('Earlier parent finalization still active');
    expect(loadWaveOperation(root,mocks.scope)?.closed).not.toBe(true);
    clearWaveOperationCachesForTests();__resetWaveShadowStateForTests();
    mocks.discard.mockImplementation(async (id:string) => ({state:id===root ? 'absent' : 'abandoned',manifestRevision:'closed',
        resumedFromSessionId:id===root ? original.waveOperationId : ''}));
    await discardWaveShadowSession(retry);
    expect(loadWaveOperation(root,mocks.scope)?.closed).toBe(true);
    expect(loadWaveOperation(original.waveOperationId!,mocks.scope)?.closed).toBe(true);
});

it('follows server lineage when the older browser manifest has been lost', async () => {
    const files=[new File(['a'],'a.jpg')];declareWaveSelection(files);
    const current=child(files,'server-lineage');
    mocks.discard.mockImplementation(async (id:string) => ({state:'abandoned',manifestRevision:'closed',
        resumedFromSessionId:id===current.waveOperationId ? 'server-only-ancestor' : ''}));
    await discardWaveShadowSession(current);
    expect(mocks.discard).toHaveBeenCalledWith('server-only-ancestor',undefined);
});

it('closes every linked retry after multiple interruptions before retiring the root', async () => {
    const files=[new File(['a'],'a.jpg')];declareWaveSelection(files);
    let current=child(files,'multiple-retries');current.files[0].status='failed';
    const ids=[current.waveOperationId!];
    for (let i=0;i<3;i++) { current=retryWaveOperation(current);ids.push(current.waveOperationId!); }
    const calls:string[]=[];
    mocks.discard.mockImplementation(async (id:string,hint?:string) => {
        calls.push(id);
        expect(loadWaveOperation(ids[3],mocks.scope)?.closed).not.toBe(true);
        return {state:'abandoned',manifestRevision:'closed',resumedFromSessionId:hint};
    });
    await discardWaveShadowSession(current);
    expect(calls).toEqual([...ids].reverse());
    for (const id of ids) expect(loadWaveOperation(id,mocks.scope)?.closed).toBe(true);
});
