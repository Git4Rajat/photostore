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
