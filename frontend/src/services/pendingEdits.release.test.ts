import { afterEach, beforeEach, expect, it, vi } from 'vitest';
const identity = vi.hoisted(() => ({ library: 'A', user: 'owner' }));
vi.mock('./passwordAuthClient', () => ({ getActiveLibraryFromToken: () => identity.library, getSubjectFromToken: () => identity.user }));
vi.mock('./authClient', () => ({ isAuthEnabled: () => false, getAccessToken: async () => null }));
beforeEach(() => {
    const values = new Map<string, string>();
    vi.stubGlobal('localStorage', { get length() { return values.size; }, key: (i: number) => Array.from(values.keys())[i] ?? null,
        getItem: (key: string) => values.get(key) ?? null,
        setItem: (key: string, value: string) => values.set(key, value), removeItem: (key: string) => values.delete(key) });
    identity.library = 'A'; identity.user = 'owner'; vi.resetModules();
});
afterEach(() => vi.unstubAllGlobals());

it('cancels an old-library queued mutation before dispatch', async () => {
    const j = await import('./pendingEdits');
    let release!: () => void;
    const first = j.savePendingEdits([j.beginPendingEdit('photo', 'same.jpg', { rating: 1 })],
        () => new Promise<void>(resolve => { release = resolve; }));
    const send = vi.fn(async () => {});
    const queued = j.savePendingEdits([j.beginPendingEdit('photo', 'same.jpg', { rating: 5 })], send);
    await Promise.resolve(); await Promise.resolve();
    identity.library = 'B';
    const rejected = expect(queued).rejects.toThrow('active account or library changed');
    release(); await first; await rejected;
    expect(send).not.toHaveBeenCalled();
});

it('HTTP retries cannot attach another library token', async () => {
    const { createHttpClient, requestJson } = await import('./httpClient');
    const client = createHttpClient('https://example.invalid');
    const sent: string[] = [];
    client.defaults.adapter = async config => {
        sent.push(String(config.headers.Authorization));
        return { data: {}, status: 200, statusText: 'OK', headers: {}, config };
    };
    localStorage.setItem('photostore.passwordAuthToken', 'library-B-token');
    identity.library = 'B';
    await expect(requestJson(client, 'post', '/photos/rate-multiple', {}, {
        expectedSessionScope: JSON.stringify(['owner', 'A']),
    })).rejects.toBeDefined();
    expect(sent).toEqual([]);
});

it('preserves an in-flight local field when another tab updates the same field', async () => {
    const j = await import('./pendingEdits');
    const edit = j.beginPendingEdit('photo', 'one.jpg', { rating: 5 });
    const key = 'photostore.pending-edits.v2:' + JSON.stringify([j.pendingEditScope(), JSON.stringify(['photo', 'one.jpg']), 'rating']);
    const value = JSON.stringify({ value: 3, saved: true, token: 'other-tab' });
    localStorage.setItem(key, value);
    window.dispatchEvent(new StorageEvent('storage', { key, newValue: value }));
    expect(j.applyPendingEdits('photo', 'one.jpg', { rating: 1 }).rating).toBe(5);
    j.confirmPendingEdit(edit);
    vi.resetModules();
    const reloaded = await import('./pendingEdits');
    expect(reloaded.applyPendingEdits('photo', 'one.jpg', { rating: 1 }).rating).toBe(5);
});

it('rollback restores the latest cross-tab receipt', async () => {
    const j = await import('./pendingEdits');
    const edit = j.beginPendingEdit('photo', 'one.jpg', { rating: 5 });
    const key = 'photostore.pending-edits.v2:' + JSON.stringify([j.pendingEditScope(), edit.key, 'rating']);
    const value = JSON.stringify({ value: 3, saved: true, token: 'other-tab' });
    localStorage.setItem(key, value); window.dispatchEvent(new StorageEvent('storage', { key, newValue: value }));
    j.rollbackPendingEdit(edit);
    expect(j.applyPendingEdits('photo', 'one.jpg', { rating: 1 }).rating).toBe(3);
});

it('keeps both tabs confirmed edits without depending on storage event delivery', async () => {
    const firstTab = await import('./pendingEdits');
    const first = firstTab.beginPendingEdit('photo', 'one.jpg', { rating: 5 });
    vi.resetModules();
    const secondTab = await import('./pendingEdits');
    const second = secondTab.beginPendingEdit('photo', 'two.jpg', { rating: 4 });
    firstTab.confirmPendingEdit(first); secondTab.confirmPendingEdit(second);
    vi.resetModules();
    const j = await import('./pendingEdits');
    expect(j.applyPendingEdits('photo', 'one.jpg', { rating: 1 }).rating).toBe(5);
    expect(j.applyPendingEdits('photo', 'two.jpg', { rating: 1 }).rating).toBe(4);
});

it('source revalidation accepts a later value even if indexing skipped the intermediate value', async () => {
    const j = await import('./pendingEdits');
    j.confirmPendingEdit(j.beginPendingEdit('person', 'p1', { name: 'Alice' }));
    const read = j.sourceEditRequests();
    j.acceptSourceEdits(read, [{ kind: 'person', id: 'p1', values: { name: 'Bob' } }], j.pendingEditScope());
    expect(j.hasPendingEdits()).toBe(false);
    // Even an old projection cannot overwrite the authoritative source receipt.
    expect(j.applyPendingEdits('person', 'p1', { name: 'Old' }, 'v1', undefined, j.pendingEditReadTokens()).name).toBe('Bob');
    vi.resetModules();
    expect((await import('./pendingEdits')).applyPendingEdits('person', 'p1', { name: 'Alice' }).name).toBe('Bob');
});

it('an old source response cannot acknowledge a newer local mutation', async () => {
    const j = await import('./pendingEdits');
    j.confirmPendingEdit(j.beginPendingEdit('photo', 'one.jpg', { rating: 3 }));
    const read = j.sourceEditRequests();
    j.confirmPendingEdit(j.beginPendingEdit('photo', 'one.jpg', { rating: 5 }));
    j.acceptSourceEdits(read, [{ kind: 'photo', id: 'one.jpg', values: { rating: 3 } }], j.pendingEditScope());
    expect(j.hasPendingEdits()).toBe(true);
    expect(j.applyPendingEdits('photo', 'one.jpg', { rating: 1 }).rating).toBe(5);
});

it('an unrelated save from a stale tab cannot rewrite a cached receipt', async () => {
    const firstTab = await import('./pendingEdits');
    firstTab.confirmPendingEdit(firstTab.beginPendingEdit('photo', 'one.jpg', { rating: 3 }));
    vi.resetModules();
    const secondTab = await import('./pendingEdits');
    const second = secondTab.beginPendingEdit('photo', 'two.jpg', { rating: 4 });
    firstTab.confirmPendingEdit(firstTab.beginPendingEdit('photo', 'one.jpg', { rating: 5 }));
    secondTab.confirmPendingEdit(second);
    vi.resetModules();
    expect((await import('./pendingEdits')).applyPendingEdits('photo', 'one.jpg', { rating: 0 }).rating).toBe(5);
});

it('unsaved fields do not erase another tab receipt when an unrelated save completes', async () => {
    const firstTab = await import('./pendingEdits');
    firstTab.beginPendingEdit('photo', 'one.jpg', { rating: 3 });
    vi.resetModules();
    const secondTab = await import('./pendingEdits');
    secondTab.confirmPendingEdit(secondTab.beginPendingEdit('photo', 'one.jpg', { rating: 5 }));
    firstTab.confirmPendingEdit(firstTab.beginPendingEdit('photo', 'two.jpg', { rating: 4 }));
    vi.resetModules();
    expect((await import('./pendingEdits')).applyPendingEdits('photo', 'one.jpg', { rating: 0 }).rating).toBe(5);
});

it('migrates legacy confirmed receipts without resurrecting a deleted field', async () => {
    const scope = JSON.stringify(['owner', 'A']);
    const key = JSON.stringify(['photo', 'one.jpg']);
    localStorage.setItem('photostore.pending-edits.v1:' + scope,
        JSON.stringify({ [key]: { rating: { value: 5, saved: true, token: 'legacy' } } }));
    let j = await import('./pendingEdits');
    expect(j.applyPendingEdits('photo', 'one.jpg', { rating: 0 }).rating).toBe(5);
    j.acceptSourceEdits(j.sourceEditRequests(), [{ kind: 'photo', id: 'one.jpg', values: {}, deleted: true }], scope);
    vi.resetModules(); j = await import('./pendingEdits');
    expect(j.applyPendingEdits('photo', 'one.jpg', { rating: 0 }).rating).toBe(0);
});

it('cancels a cold-start retry after the active library changes', async () => {
    const { createHttpClient, requestJson } = await import('./httpClient');
    const { AxiosError } = await import('axios');
    const client = createHttpClient('https://example.invalid');
    const sent: string[] = [];
    client.defaults.adapter = async config => {
        sent.push(String(config.headers.Authorization));
        identity.library = 'B';
        localStorage.setItem('photostore.passwordAuthToken', 'library-B-token');
        throw new AxiosError('warming up', 'ERR_BAD_RESPONSE', config, {}, {
            data: {}, status: 503, statusText: 'Unavailable', headers: {}, config,
        });
    };
    localStorage.setItem('photostore.passwordAuthToken', 'library-A-token');
    vi.useFakeTimers();
    try {
        const result = requestJson(client, 'post', '/photos/rate-multiple', {}).catch(error => error);
        await vi.advanceTimersByTimeAsync(1000);
        expect((await result).kind).toBe('canceled');
        expect(sent).toEqual(['Bearer library-A-token']);
    } finally { vi.useRealTimers(); }
});
