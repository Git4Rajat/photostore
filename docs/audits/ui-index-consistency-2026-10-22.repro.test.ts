// Audit reproductions: assertions express required behavior and currently fail.
import { beforeEach, expect, it, vi } from 'vitest';
const identity = vi.hoisted(() => ({ library: 'A' }));
vi.mock('./passwordAuthClient', () => ({ getActiveLibraryFromToken: () => identity.library, getSubjectFromToken: () => 'owner' }));
vi.mock('./authClient', () => ({ isAuthEnabled: () => false, getAccessToken: async () => null }));
beforeEach(() => {
    const values = new Map<string, string>();
    const storage = { getItem: (key: string) => values.get(key) ?? null,
        setItem: (key: string, value: string) => values.set(key, value),
        removeItem: (key: string) => values.delete(key) };
    vi.stubGlobal('localStorage', storage);
    identity.library = 'A'; vi.resetModules();
});

it('binds a queued mutation to its original authentication scope', async () => {
    const j = await import('./pendingEdits');
    const { createHttpClient } = await import('./httpClient');
    const client = createHttpClient('https://example.invalid');
    const sent: string[] = [];
    client.defaults.adapter = async config => {
        sent.push(String(config.headers.Authorization));
        return { data: {}, status: 200, statusText: 'OK', headers: {}, config };
    };
    localStorage.setItem('photostore.passwordAuthToken', 'library-A-token');
    let release!: () => void;
    const first = j.savePendingEdits([j.beginPendingEdit('photo', 'same.jpg', { rating: 1 })],
        () => new Promise<void>(resolve => { release = resolve; }));
    const queued = j.savePendingEdits([j.beginPendingEdit('photo', 'same.jpg', { rating: 5 })],
        () => client.post('/photos/rate-multiple', { filenames: ['same.jpg'], rating: 5 }));
    await Promise.resolve(); await Promise.resolve();
    identity.library = 'B'; localStorage.setItem('photostore.passwordAuthToken', 'library-B-token');
    release(); await first; await queued;
    expect(sent).toEqual(['Bearer library-A-token']);
});

it('keeps an in-flight local edit through a storage event and later confirmation', async () => {
    const j = await import('./pendingEdits');
    const edit = j.beginPendingEdit('photo', 'one.jpg', { rating: 5 });
    const key = 'photostore.pending-edits.v1:' + j.pendingEditScope();
    localStorage.setItem(key, '{}');
    window.dispatchEvent(new StorageEvent('storage', { key, newValue: '{}' }));
    j.confirmPendingEdit(edit);
    expect(j.applyPendingEdits('photo', 'one.jpg', { rating: 1 }).rating).toBe(5);
});

it('accepts a later server edit when the intermediate value was never indexed', async () => {
    const j = await import('./pendingEdits');
    j.confirmPendingEdit(j.beginPendingEdit('person', 'p1', { name: 'Alice' }));
    const tokens = j.pendingEditReadTokens();
    j.applyPendingEdits('person', 'p1', { name: 'Bob' }, 'v2', undefined, tokens);
    expect(j.applyPendingEdits('person', 'p1', { name: 'Bob' }, 'v3', undefined, tokens).name).toBe('Bob');
});

it('merges confirmed edits from two tabs before storage events are delivered', async () => {
    const firstTab = await import('./pendingEdits');
    const first = firstTab.beginPendingEdit('photo', 'one.jpg', { rating: 5 });
    vi.resetModules();
    const secondTab = await import('./pendingEdits');
    const second = secondTab.beginPendingEdit('photo', 'two.jpg', { rating: 4 });
    firstTab.confirmPendingEdit(first);
    secondTab.confirmPendingEdit(second);
    vi.resetModules();
    const reloaded = await import('./pendingEdits');
    expect(reloaded.applyPendingEdits('photo', 'one.jpg', { rating: 1 }).rating).toBe(5);
});
