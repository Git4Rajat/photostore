// Adversarial audit assertions for the current uncommitted tree.
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
const mocks = vi.hoisted(() => ({ get: vi.fn(), postTools: vi.fn() }));
vi.mock('./passwordAuthClient', () => ({ getActiveLibraryFromToken: () => 'A', getSubjectFromToken: () => 'owner' }));
vi.mock('./apiClient', () => ({ get: mocks.get, postTools: mocks.postTools }));
vi.mock('./perf', () => ({ perf: { recordSpan: vi.fn() }, perfNow: () => 0 }));
beforeEach(() => {
    const values = new Map<string, string>();
    vi.stubGlobal('localStorage', { get length() { return values.size; }, key: (i: number) => Array.from(values.keys())[i] ?? null,
        getItem: (k: string) => values.get(k) ?? null, setItem: (k: string, v: string) => values.set(k, v), removeItem: (k: string) => values.delete(k) });
    vi.stubGlobal('indexedDB', undefined);
    mocks.get.mockReset(); mocks.postTools.mockReset(); vi.resetModules();
});
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it('does not treat an older thumbnail as a replacement upload and erase a saved rating', async () => {
    const j = await import('./pendingEdits');
    mocks.get.mockResolvedValue({ available: true, indexUrl: 'https://storage/index', sourceVersion: 'manifest' });
    mocks.postTools.mockRejectedValue(new Error('Source temporarily unavailable'));
    let payload = { sourceVersion: 'v2', rows: [{ RowKey: 'one.jpg', rating: 1, thumb: 'new-upload' }] };
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(payload))));
    const index = await import('./localSortIndex');
    await index.getLocalSortIndex();
    j.confirmPendingEdit(j.beginPendingEdit('photo', 'one.jpg', { rating: 5 }));
    payload = { sourceVersion: 'v1', rows: [{ RowKey: 'one.jpg', rating: 1, thumb: 'old-upload' }] };
    index.invalidateLocalSortIndex();
    expect((await index.getLocalSortIndex())?.[0].rating).toBe(5);
});

it('sees a later authoritative change back to the same previously stale unversioned value', async () => {
    const j = await import('./pendingEdits');
    j.confirmPendingEdit(j.beginPendingEdit('photo', 'one.jpg', { rating: 5 }));
    // A server page is still stale at 2, but the source already contains our saved 5.
    j.applyPendingEdits('photo', 'one.jpg', { rating: 2 }, undefined, j.pendingEditScope(), j.pendingEditReadTokens());
    mocks.postTools.mockResolvedValue({ libraryId: 'A', rows: [{ kind: 'photo', id: 'one.jpg', values: { rating: 5 } }] });
    const { reconcilePendingEdits } = await import('./reconcilePendingEdits');
    await reconcilePendingEdits();
    expect(j.hasPendingEdits()).toBe(false);
    // Another client now intentionally changes 5 back to 2; the newly fetched page is authoritative at 2.
    mocks.postTools.mockResolvedValue({ libraryId: 'A', rows: [{ kind: 'photo', id: 'one.jpg', values: { rating: 2 } }] });
    vi.spyOn(Date, 'now').mockReturnValue(Date.now() + 10 * 60 * 1000);
    vi.resetModules();
    const reloaded = await import('./pendingEdits');
    reloaded.applyPendingEdits('photo', 'one.jpg', { rating: 2 }, undefined, reloaded.pendingEditScope(), reloaded.pendingEditReadTokens());
    await (await import('./reconcilePendingEdits')).reconcilePendingEdits(true);
    expect(reloaded.applyPendingEdits('photo', 'one.jpg', { rating: 2 }).rating).toBe(2);
});

it('a matching old read cannot cancel reconciliation of a newer conflicting publication', async () => {
    const j = await import('./pendingEdits');
    j.confirmPendingEdit(j.beginPendingEdit('person', 'p1', { name: 'Alice' }));
    const tokens = j.pendingEditReadTokens();
    j.applyPendingEdits('person', 'p1', { name: 'Alice' }, 'v2', j.pendingEditScope(), tokens);
    j.applyPendingEdits('person', 'p1', { name: 'Bob' }, 'v3', j.pendingEditScope(), tokens);
    const freshRequest = j.sourceEditRequests();
    expect(freshRequest.length).toBe(1);
    // A search/page request issued earlier returns from an older replica.
    j.applyPendingEdits('person', 'p1', { name: 'Alice' }, 'v1', j.pendingEditScope(), tokens);
    j.acceptSourceEdits(freshRequest, [{ kind: 'person', id: 'p1', values: { name: 'Bob' } }], j.pendingEditScope());
    expect(j.applyPendingEdits('person', 'p1', { name: 'Old' }).name).toBe('Bob');
});

it('revalidates a repeated unversioned value on a fresh request, but not on cached reads', async () => {
    const j = await import('./pendingEdits');
    const scope = j.pendingEditScope();
    j.confirmPendingEdit(j.beginPendingEdit('photo', 'one.jpg', { rating: 5 }));
    const older = j.pendingEditReadTokens();
    j.applyPendingEdits('photo', 'one.jpg', { rating: 2 }, undefined, scope, j.pendingEditReadTokens());
    j.acceptSourceEdits(j.sourceEditRequests(), [{ kind: 'photo', id: 'one.jpg', values: { rating: 5 } }], scope);
    for (let n = 0; n < 3; n++) {
        j.applyPendingEdits('photo', 'one.jpg', { rating: 2 }, undefined, scope, j.pendingEditReadTokens(scope, false));
        expect(j.sourceEditRequests()).toEqual([]);
    }
    j.applyPendingEdits('photo', 'one.jpg', { rating: 2 }, undefined, scope, j.pendingEditReadTokens());
    const current = j.sourceEditRequests();
    j.applyPendingEdits('photo', 'one.jpg', { rating: 1 }, undefined, scope, older);
    expect(j.sourceEditRequests()).toEqual(current);
    j.acceptSourceEdits(current, [{ kind: 'photo', id: 'one.jpg', values: { rating: 2 } }], scope);
    expect(j.applyPendingEdits('photo', 'one.jpg', { rating: 0 }).rating).toBe(2);
});

it('fences a source response superseded by another fresh unversioned read', async () => {
    const j = await import('./pendingEdits');
    const scope = j.pendingEditScope();
    j.confirmPendingEdit(j.beginPendingEdit('photo', 'one.jpg', { rating: 5 }));
    j.applyPendingEdits('photo', 'one.jpg', { rating: 2 }, undefined, scope, j.pendingEditReadTokens());
    const oldCheck = j.sourceEditRequests();
    j.applyPendingEdits('photo', 'one.jpg', { rating: 2 }, undefined, scope, j.pendingEditReadTokens());
    j.acceptSourceEdits(oldCheck, [{ kind: 'photo', id: 'one.jpg', values: { rating: 1 } }], scope);
    expect(j.applyPendingEdits('photo', 'one.jpg', { rating: 0 }).rating).toBe(5);
    j.acceptSourceEdits(j.sourceEditRequests(), [{ kind: 'photo', id: 'one.jpg', values: { rating: 2 } }], scope);
    expect(j.applyPendingEdits('photo', 'one.jpg', { rating: 0 }).rating).toBe(2);
});

it('persists generation ordering across reload and releases only a newer replacement', async () => {
    let j = await import('./pendingEdits');
    j.observePhotoGeneration('one.jpg', 'new-upload', undefined, 'v2');
    j.confirmPendingEdit(j.beginPendingEdit('photo', 'one.jpg', { rating: 5 }));
    vi.resetModules(); j = await import('./pendingEdits');
    for (const version of ['v1', 'v2', undefined]) {
        j.observePhotoGeneration('one.jpg', 'old-upload', undefined, version);
        expect(j.applyPendingEdits('photo', 'one.jpg', { rating: 1 }).rating).toBe(5);
    }
    j.confirmPendingEdit(j.beginPendingEdit('photo', 'one.jpg', { rating: 4 }));
    j.observePhotoGeneration('one.jpg', 'replacement-upload', undefined, 'v3');
    expect(j.applyPendingEdits('photo', 'one.jpg', { rating: 1 }).rating).toBe(1);
});

it('keeps a newer conflict across older conflicts, old matches, and reload', async () => {
    let j = await import('./pendingEdits');
    const scope = j.pendingEditScope();
    j.confirmPendingEdit(j.beginPendingEdit('person', 'p1', { name: 'Alice' }));
    j.applyPendingEdits('person', 'p1', { name: 'Alice' }, 'v2', scope, j.pendingEditReadTokens());
    j.applyPendingEdits('person', 'p1', { name: 'Bob' }, 'v4', scope, j.pendingEditReadTokens());
    const current = j.sourceEditRequests();
    j.applyPendingEdits('person', 'p1', { name: 'Carol' }, 'v3', scope, j.pendingEditReadTokens());
    expect(j.sourceEditRequests()).toEqual(current);
    vi.resetModules(); j = await import('./pendingEdits');
    j.applyPendingEdits('person', 'p1', { name: 'Alice' }, 'v2', scope, j.pendingEditReadTokens());
    expect(j.sourceEditRequests()).toEqual(current);
    j.acceptSourceEdits(current, [{ kind: 'person', id: 'p1', values: { name: 'Bob' } }], scope);
    expect(j.applyPendingEdits('person', 'p1', { name: 'Old' }).name).toBe('Bob');
});

it('a matching newer publication invalidates an earlier source read', async () => {
    const j = await import('./pendingEdits');
    j.confirmPendingEdit(j.beginPendingEdit('person', 'p1', { name: 'Alice' }));
    const oldCheck = j.sourceEditRequests();
    j.applyPendingEdits('person', 'p1', { name: 'Alice' }, 'v3', undefined, j.pendingEditReadTokens());
    j.acceptSourceEdits(oldCheck, [{ kind: 'person', id: 'p1', values: { name: 'Old' } }], j.pendingEditScope());
    expect(j.applyPendingEdits('person', 'p1', { name: 'Old' }).name).toBe('Alice');
});

it('filters pending names before budgeting and rotates broad enrichment fairly', async () => {
    const j = await import('./pendingEdits');
    for (let n = 0; n < 101; n++) j.confirmPendingEdit(j.beginPendingEdit('person', `p${n}`, { name: `New Name ${n}` }));
    expect(j.pendingPeopleEnrichmentIds(new Set(), 'new name 100')).toEqual(['p100']);
    const first = j.pendingPeopleEnrichmentIds(new Set(), '');
    const second = j.pendingPeopleEnrichmentIds(new Set(), '');
    expect(first).toHaveLength(100); expect(second).toHaveLength(100);
    expect(new Set([...first, ...second]).size).toBe(101);
    expect(j.pendingPeopleEnrichmentIds(new Set(['p100']), 'New Name 100')).toEqual([]);
});
