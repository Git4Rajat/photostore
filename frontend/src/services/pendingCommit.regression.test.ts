// Regression coverage for the pending-commit audit findings.
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
afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers(); vi.unstubAllGlobals(); });

it('preserves a successful predecessor when a storage event arrives during two local saves', async () => {
    const j = await import('./pendingEdits');
    const first = j.beginPendingEdit('photo', 'one.jpg', { rating: 3 });
    const second = j.beginPendingEdit('photo', 'one.jpg', { rating: 5 });
    const key = 'photostore.pending-edits.v2:' + JSON.stringify([j.pendingEditScope(), first.key, 'rating']);
    const value = JSON.stringify({ value: 1, token: 'other-tab', saved: true });
    localStorage.setItem(key, value);
    window.dispatchEvent(new StorageEvent('storage', { key, newValue: value }));
    j.confirmPendingEdit(first);
    j.rollbackPendingEdit(second);
    expect(j.applyPendingEdits('photo', 'one.jpg', { rating: 0 }).rating).toBe(3);
});

it.each([80, 1000])('persists a bulk edit of %i photos with linear serialization work', async count => {
    const j = await import('./pendingEdits');
    const edits = Array.from({ length: count }, (_, n) => j.beginPendingEdit('photo', 'p' + n, { rating: 5 }));
    const serialize = vi.spyOn(JSON, 'stringify');
    edits.forEach(edit => j.confirmPendingEdit(edit));
    const calls = serialize.mock.calls.length;
    serialize.mockRestore();
    expect(calls).toBeLessThanOrEqual(count * 8);
});

it('does not point-read every historical acknowledged receipt on every index refresh', async () => {
    const j = await import('./pendingEdits');
    for (let n = 0; n < 51; n++) j.confirmPendingEdit(j.beginPendingEdit('photo', 'p' + n, { rating: 5 }));
    mocks.postTools.mockImplementation(async (_url, body) => ({ libraryId: 'A', rows: body.edits.map((edit: any) => ({
        kind: edit.kind, id: edit.id, values: { rating: 5 },
    })) }));
    const { reconcilePendingEdits } = await import('./reconcilePendingEdits');
    await reconcilePendingEdits(true);
    for (let n = 0; n < 51; n++) j.applyPendingEdits('photo', 'p' + n, { rating: 5 }, 'v2', undefined, j.pendingEditReadTokens());
    expect(j.hasPendingEdits()).toBe(false);
    mocks.postTools.mockClear();
    await reconcilePendingEdits(true);
    expect(mocks.postTools.mock.calls.length).toBe(0);
});

it('can serve a warm cached index when source verification stalls', async () => {
    const j = await import('./pendingEdits');
    mocks.get.mockResolvedValue({ available: true, indexUrl: 'https://storage/index', sourceVersion: 'v1' });
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ sourceVersion: 'v1', rows: [{ RowKey: 'one.jpg', rating: 1 }] }))));
    const index = await import('./localSortIndex');
    await index.getLocalSortIndex();
    j.confirmPendingEdit(j.beginPendingEdit('photo', 'one.jpg', { rating: 5 }));
    mocks.postTools.mockImplementation(() => new Promise(() => {}));
    vi.useFakeTimers();
    let finished = false;
    void index.getLocalSortIndex().then(() => { finished = true; });
    await vi.advanceTimersByTimeAsync(5000);
    expect(finished).toBe(true);
});

it('updating one field does not serialize its historical siblings', async () => {
    const j = await import('./pendingEdits');
    for (let n = 0; n < 1000; n++) j.confirmPendingEdit(j.beginPendingEdit('photo', 'p' + n, { rating: 5 }));
    const edit = j.beginPendingEdit('photo', 'one-new.jpg', { rating: 4 });
    const serialize = vi.spyOn(JSON, 'stringify');
    j.confirmPendingEdit(edit);
    const calls = serialize.mock.calls.length;
    serialize.mockRestore();
    expect(calls).toBeLessThanOrEqual(8);
});
