import { afterEach, beforeEach, expect, it, vi } from 'vitest';
const mocks = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock('./apiClient', () => ({ get: mocks.get }));
vi.mock('./passwordAuthClient', () => ({ getActiveLibraryFromToken: () => 'A', getSubjectFromToken: () => 'u1' }));
vi.mock('./perf', () => ({ perf: { recordSpan: vi.fn() }, perfNow: () => 0 }));
beforeEach(() => {
    vi.resetModules();
    vi.stubGlobal('indexedDB', undefined);
    mocks.get.mockResolvedValue({ available: true, indexUrl: 'https://storage/index', sourceVersion: 'dirty-manifest-version' });
});
afterEach(() => vi.unstubAllGlobals());

it.each(['people', 'albums'])('keeps a saved %s name until actual downloaded index content confirms it', async kind => {
    const j = await import('./pendingEdits');
    const entity = kind === 'people' ? 'person' : 'album';
    j.confirmPendingEdit(j.beginPendingEdit(entity, 'id1', { name: 'New name' }));
    const row = kind === 'people' ? { personId: 'id1', name: 'Old', isNamed: true } : { albumId: 'id1', name: 'Old' };
    let payload = { sourceVersion: 'v1', rows: [row] };
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(payload))));
    const index = kind === 'people' ? await import('./localPeopleIndex') : await import('./localAlbumsIndex');
    const get = 'getLocalPeopleIndex' in index ? index.getLocalPeopleIndex : index.getLocalAlbumsIndex;
    const invalidate = 'invalidateLocalPeopleIndex' in index ? index.invalidateLocalPeopleIndex : index.invalidateLocalAlbumsIndex;
    expect((await get())?.[0].name).toBe('New name');
    expect(j.hasPendingEdits()).toBe(true);
    payload = { sourceVersion: 'v2', rows: [{ ...row, name: 'New name' }] };
    invalidate();
    expect((await get())?.[0].name).toBe('New name');
    expect(j.hasPendingEdits()).toBe(false);
    payload = { sourceVersion: 'v1', rows: [row] };
    invalidate();
    expect((await get())?.[0].name).toBe('New name');
});
