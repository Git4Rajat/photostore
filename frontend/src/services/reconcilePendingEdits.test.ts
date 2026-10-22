import { afterEach, beforeEach, expect, it, vi } from 'vitest';
const mocks = vi.hoisted(() => ({ get: vi.fn(), postTools: vi.fn(), library: 'A' }));
vi.mock('./apiClient', () => ({ get: mocks.get, postTools: mocks.postTools }));
vi.mock('./passwordAuthClient', () => ({ getActiveLibraryFromToken: () => mocks.library, getSubjectFromToken: () => 'owner' }));
vi.mock('./perf', () => ({ perf: { recordSpan: vi.fn() }, perfNow: () => 0 }));
beforeEach(() => {
    vi.resetModules(); mocks.get.mockReset(); mocks.postTools.mockReset(); mocks.library = 'A';
    const values = new Map<string, string>();
    vi.stubGlobal('localStorage', { get length() { return values.size; }, key: (i: number) => Array.from(values.keys())[i] ?? null,
        getItem: (k: string) => values.get(k) ?? null, setItem: (k: string, v: string) => values.set(k, v), removeItem: (k: string) => values.delete(k) });
    vi.stubGlobal('indexedDB', undefined);
});
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

it('acknowledges saved photos in a server-paged library and overlays later authoritative changes', async () => {
    const j = await import('./pendingEdits');
    j.confirmPendingEdit(j.beginPendingEdit('photo', 'one.jpg', { rating: 5 }));
    mocks.get.mockResolvedValue({ available: false, reason: 'library_too_large' });
    mocks.postTools.mockResolvedValue({ libraryId: 'A', rows: [{ kind: 'photo', id: 'one.jpg', values: { rating: 5 } }] });
    const index = await import('./localSortIndex');
    expect(await index.getLocalSortIndex()).toBeNull();
    expect(index.isServerPagedLibrary()).toBe(true);
    await vi.waitFor(() => expect(j.hasPendingEdits()).toBe(false));
    expect(j.applyPendingEdits('photo', 'one.jpg', { rating: 1 }).rating).toBe(5);
    mocks.postTools.mockResolvedValue({ libraryId: 'A', rows: [{ kind: 'photo', id: 'one.jpg', values: { rating: 2 } }] });
    // A server page containing a new conflicting value demands a targeted source check.
    j.applyPendingEdits('photo', 'one.jpg', { rating: 2 }, undefined, j.pendingEditScope(), j.pendingEditReadTokens());
    await (await import('./reconcilePendingEdits')).reconcilePendingEdits();
    expect(j.applyPendingEdits('photo', 'one.jpg', { rating: 1 }).rating).toBe(2);
});

it('keeps saved edits pending if source reconciliation fails', async () => {
    const j = await import('./pendingEdits');
    j.confirmPendingEdit(j.beginPendingEdit('person', 'p1', { name: 'Alice' }));
    mocks.postTools.mockRejectedValue(new Error('offline'));
    await (await import('./reconcilePendingEdits')).reconcilePendingEdits();
    expect(j.hasPendingEdits()).toBe(true);
    expect(j.applyPendingEdits('person', 'p1', { name: 'Old' }).name).toBe('Alice');
});

it('ignores a response from an old library', async () => {
    const j = await import('./pendingEdits');
    j.confirmPendingEdit(j.beginPendingEdit('person', 'p1', { name: 'Alice' }));
    let finish!: (value: unknown) => void;
    mocks.postTools.mockImplementation(() => new Promise(resolve => { finish = resolve; }));
    const pending = (await import('./reconcilePendingEdits')).reconcilePendingEdits();
    await vi.waitFor(() => expect(mocks.postTools).toHaveBeenCalledTimes(1));
    mocks.library = 'B'; finish({ libraryId: 'A', rows: [{ kind: 'person', id: 'p1', values: { name: 'Bob' } }] });
    await pending; mocks.library = 'A';
    expect(j.applyPendingEdits('person', 'p1', { name: 'Old' }).name).toBe('Alice');
    expect(j.hasPendingEdits()).toBe(true);
});

it('a new conflicting projection rejects an older in-flight source check and schedules a follow-up', async () => {
    const j = await import('./pendingEdits');
    j.confirmPendingEdit(j.beginPendingEdit('person', 'p1', { name: 'Alice' }));
    let finish!: (value: unknown) => void;
    mocks.postTools.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }))
        .mockResolvedValue({ libraryId: 'A', rows: [{ kind: 'person', id: 'p1', values: { name: 'Bob' } }] });
    const { reconcilePendingEdits } = await import('./reconcilePendingEdits');
    const old = reconcilePendingEdits();
    await vi.waitFor(() => expect(mocks.postTools).toHaveBeenCalledTimes(1));
    j.applyPendingEdits('person', 'p1', { name: 'Bob' }, 'v2', j.pendingEditScope(), j.pendingEditReadTokens());
    const current = reconcilePendingEdits(true);
    finish({ libraryId: 'A', rows: [{ kind: 'person', id: 'p1', values: { name: 'Alice' } }] });
    await old; await current;
    await vi.waitFor(() => expect(mocks.postTools).toHaveBeenCalledTimes(2));
    expect(j.applyPendingEdits('person', 'p1', { name: 'Old' }).name).toBe('Bob');
});

it('batches point reads at fifty entities', async () => {
    const j = await import('./pendingEdits');
    for (let i = 0; i < 51; i++) j.confirmPendingEdit(j.beginPendingEdit('person', 'p' + i, { name: 'Alice' }));
    mocks.postTools.mockResolvedValue({ libraryId: 'A', rows: [] });
    await (await import('./reconcilePendingEdits')).reconcilePendingEdits();
    expect(mocks.postTools.mock.calls.map(call => call[1].edits.length)).toEqual([50, 1]);
});

it('serves cache immediately while ignoring background responses from an old library', async () => {
    const j = await import('./pendingEdits');
    mocks.get.mockResolvedValue({ available: true, indexUrl: 'https://storage/index', sourceVersion: 'v1' });
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ sourceVersion: 'v1', rows: [{ RowKey: 'one.jpg', rating: 1 }] }))));
    const index = await import('./localSortIndex');
    await index.getLocalSortIndex();
    j.confirmPendingEdit(j.beginPendingEdit('photo', 'one.jpg', { rating: 5 }));
    let finish!: (value: unknown) => void;
    mocks.postTools.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    const cached = await index.getLocalSortIndex();
    expect(cached?.[0].rating).toBe(5);
    expect(mocks.postTools).toHaveBeenCalledTimes(1);
    mocks.library = 'B'; finish({ libraryId: 'A', rows: [{ kind: 'photo', id: 'one.jpg', values: { rating: 2 } }] });
    expect((await index.getLocalSortIndex())?.[0].rating).toBe(1);
    mocks.library = 'A';
    expect(j.applyPendingEdits('photo', 'one.jpg', { rating: 0 }).rating).toBe(5);
});

it('aborts a stalled source check within its own deadline and ignores late responses', async () => {
    const j = await import('./pendingEdits');
    j.confirmPendingEdit(j.beginPendingEdit('photo', 'one.jpg', { rating: 5 }));
    let finish!: (value: unknown) => void;
    mocks.postTools.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    const { reconcilePendingEdits, SOURCE_CHECK_TIMEOUT_MS } = await import('./reconcilePendingEdits');
    vi.useFakeTimers();
    const request = reconcilePendingEdits();
    expect(mocks.postTools).toHaveBeenCalledTimes(1);
    const options = mocks.postTools.mock.calls[0][2];
    expect(options.singleAttempt).toBe(true);
    await vi.advanceTimersByTimeAsync(SOURCE_CHECK_TIMEOUT_MS);
    await request;
    expect(options.signal.aborted).toBe(true);
    expect(j.hasPendingEdits()).toBe(true);
    finish({ libraryId: 'A', rows: [{ kind: 'photo', id: 'one.jpg', values: { rating: 1 } }] });
    await Promise.resolve(); await Promise.resolve();
    expect(j.applyPendingEdits('photo', 'one.jpg', { rating: 0 }).rating).toBe(5);
});

it('limits each background pass to one hundred entities and rotates failed work', async () => {
    const j = await import('./pendingEdits');
    for (let n = 0; n < 150; n++) j.confirmPendingEdit(j.beginPendingEdit('photo', 'p' + n, { rating: 5 }));
    mocks.postTools.mockResolvedValue({ libraryId: 'A', rows: [] });
    const { reconcilePendingEdits } = await import('./reconcilePendingEdits');
    await reconcilePendingEdits();
    expect(mocks.postTools.mock.calls.map(call => call[1].edits.length)).toEqual([50, 50]);
    vi.useFakeTimers(); vi.setSystemTime(Date.now() + 1100);
    mocks.postTools.mockClear();
    await reconcilePendingEdits();
    expect(mocks.postTools.mock.calls[0][1].edits[0].id).toBe('p100');
    expect(mocks.postTools.mock.calls.map(call => call[1].edits.length)).toEqual([50, 50]);
});

it('only a newer conflicting payload activates an acknowledged receipt', async () => {
    const j = await import('./pendingEdits');
    j.confirmPendingEdit(j.beginPendingEdit('photo', 'one.jpg', { rating: 5 }));
    mocks.get.mockResolvedValue({ available: true, indexUrl: 'https://storage/index', sourceVersion: 'manifest-1' });
    let payload = { sourceVersion: 'v2', rows: [{ RowKey: 'one.jpg', rating: 5 }] };
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(payload))));
    const index = await import('./localSortIndex');
    expect((await index.getLocalSortIndex())?.[0].rating).toBe(5);
    expect(j.hasPendingEdits()).toBe(false);
    mocks.postTools.mockResolvedValue({ libraryId: 'A', rows: [{ kind: 'photo', id: 'one.jpg', values: { rating: 2 } }] });
    payload = { sourceVersion: 'v1', rows: [{ RowKey: 'one.jpg', rating: 1 }] };
    index.invalidateLocalSortIndex();
    expect((await index.getLocalSortIndex())?.[0].rating).toBe(5);
    expect((await index.getLocalSortIndex())?.[0].rating).toBe(5);
    expect(mocks.postTools).not.toHaveBeenCalled();
    payload = { sourceVersion: 'v3', rows: [{ RowKey: 'one.jpg', rating: 2 }] };
    index.invalidateLocalSortIndex();
    await index.getLocalSortIndex();
    await (await import('./reconcilePendingEdits')).reconcilePendingEdits();
    expect(mocks.postTools).toHaveBeenCalledTimes(1);
    expect((await index.getLocalSortIndex())?.[0].rating).toBe(2);
});
