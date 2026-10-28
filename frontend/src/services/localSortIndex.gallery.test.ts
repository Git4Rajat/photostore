import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
    library: 'library-A',
    postTools: vi.fn(),
    get: vi.fn(async () => ({ available: true, indexUrl: 'https://storage.test/sort', sourceVersion: '2026-02-01' })),
}));
vi.mock('./apiClient', () => ({ get: mocks.get, postTools: mocks.postTools }));
vi.mock('./passwordAuthClient', () => ({ getActiveLibraryFromToken: () => mocks.library }));
vi.mock('./perf', () => ({ perf: { recordSpan: vi.fn() }, perfNow: () => 0 }));

const gallery = {
    liked: true, rotation: 90, thumbnailRotation: 180, personIds: ['person-1'], tags: ['beach'],
    processing: { thumbnail: 'done', face: 'pending' },
};
const snapshot = (thumb = 'generation-one') => ({ sourceVersion: thumb === 'generation-one' ? '2026-02-01' : '2026-02-04', rows: [
    { RowKey: 'a.jpg', rating: 4, likes: 3, thumb, gallery },
    { RowKey: 'b.jpg', rating: 0, likes: 0, thumb: 'other', gallery },
] });

// Minimal asynchronous IndexedDB surface used by the index cache. Clone on
// reads/writes so tests cannot accidentally pass by mutating persisted objects.
const memoryDatabase = () => {
    const values = new Map<string, unknown>();
    const database = {
        close: vi.fn(),
        transaction: () => {
            const tx: { oncomplete?: () => void; objectStore: () => unknown } = {
                objectStore: () => ({
                    get: (key: string) => {
                        const request: { result?: unknown; onsuccess?: () => void } = {};
                        queueMicrotask(() => { request.result = structuredClone(values.get(key)); request.onsuccess?.(); });
                        return request;
                    },
                    put: (value: unknown, key: string) => {
                        values.set(key, structuredClone(value));
                        queueMicrotask(() => tx.oncomplete?.());
                    },
                }),
            };
            return tx;
        },
    };
    return {
        open: () => {
            const request: { result: typeof database; onsuccess?: () => void } = { result: database };
            queueMicrotask(() => request.onsuccess?.());
            return request;
        },
    };
};

describe('index-backed gallery cache', () => {
    beforeEach(() => {
        globalThis.localStorage?.clear();
        vi.resetModules();
        mocks.library = 'library-A';
        mocks.postTools.mockReset().mockRejectedValue(new Error('Source unavailable'));
        mocks.get.mockReset().mockResolvedValue({ available: true, indexUrl: 'https://storage.test/sort', sourceVersion: '2026-02-01' });
        vi.stubGlobal('indexedDB', memoryDatabase());
        vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(snapshot()))));
    });
    afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

    it('retains complete gallery metadata and patches likes, rating and rotation', async () => {
        const index = await import('./localSortIndex');
        expect((await index.getLocalSortIndex())?.[0].gallery).toEqual(gallery);
        index.patchLocalSortIndexRow('a.jpg', { liked: false, likes: 2, rating: 5, rotation: 270 });
        expect((await index.getLocalSortIndex())?.[0]).toMatchObject({
            likes: 2, rating: 5, gallery: { ...gallery, liked: false, rotation: 270 },
        });
    });


    it('keeps rating and like edits when invalidation downloads a still-stale snapshot', async () => {
        const index = await import('./localSortIndex');
        await index.getLocalSortIndex();
        index.patchLocalSortIndexRow('a.jpg', { rating: 5, likes: 4, liked: false });
        vi.stubGlobal('indexedDB', undefined);
        index.invalidateLocalSortIndex();
        expect((await index.getLocalSortIndex())?.[0]).toMatchObject({ rating: 5, likes: 4, gallery: { liked: false } });
        vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ sourceVersion: '2026-02-02', rows: [
            { ...snapshot().rows[0], rating: 5, likes: 4, gallery: { ...gallery, liked: false } },
        ] }))));
        index.invalidateLocalSortIndex();
        await index.getLocalSortIndex(); // Authoritative snapshot confirms the edits.
        vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ ...snapshot(), sourceVersion: '2026-02-03' }))));
        index.invalidateLocalSortIndex();
        // New index timestamps alone cannot supersede a saved receipt.
        expect((await index.getLocalSortIndex())?.[0].rating).toBe(5);
        mocks.postTools.mockResolvedValue({ libraryId: mocks.library,
            rows: [{ kind: 'photo', id: 'a.jpg', values: { rating: 4, likes: 3, liked: true } }] });
        vi.useFakeTimers(); vi.setSystemTime(Date.now() + 1100);
        index.invalidateLocalSortIndex();
        await index.getLocalSortIndex();
        await (await import('./reconcilePendingEdits')).reconcilePendingEdits();
        expect((await index.getLocalSortIndex())?.[0].rating).toBe(4);
    });

    it('retains an edit made while the first snapshot is still loading', async () => {
        const index = await import('./localSortIndex');
        let finish!: (value: Response) => void;
        vi.stubGlobal('fetch', vi.fn(() => new Promise(resolve => { finish = resolve; })));
        const loading = index.getLocalSortIndex();
        await vi.waitFor(() => expect(fetch).toHaveBeenCalled());
        index.patchLocalSortIndexRow('a.jpg', { rating: 5 });
        finish(new Response(JSON.stringify(snapshot())));
        expect((await loading)?.[0].rating).toBe(5);
    });

    it('does not apply optimistic ratings to a different library or a new upload generation', async () => {
        const index = await import('./localSortIndex');
        await index.getLocalSortIndex();
        index.patchLocalSortIndexRow('a.jpg', { rating: 5 });
        mocks.library = 'library-B';
        expect((await index.getLocalSortIndex())?.[0].rating).toBe(4);
        mocks.library = 'library-A';
        vi.stubGlobal('indexedDB', undefined);
        vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(snapshot('new-upload')))));
        index.invalidateLocalSortIndex();
        expect((await index.getLocalSortIndex())?.[0].rating).toBe(4);
    });

    it('keeps deletes hidden after invalidation and page reload, then permits restore', async () => {
        let index = await import('./localSortIndex');
        await index.getLocalSortIndex();
        index.removeLocalSortIndexRows(['a.jpg']);
        index.removeLocalSortIndexRows(['a.jpg']); // repeated cross-tab notification
        index.invalidateLocalSortIndex();
        expect((await index.getLocalSortIndex())?.map(row => row.filename)).toEqual(['b.jpg']);
        vi.resetModules();
        index = await import('./localSortIndex');
        expect((await index.getLocalSortIndex())?.map(row => row.filename)).toEqual(['b.jpg']);
        index.clearLocalSortIndexRemovals(['a.jpg']);
        index.invalidateLocalSortIndex();
        expect((await index.getLocalSortIndex())?.map(row => row.filename)).toEqual(['a.jpg', 'b.jpg']);
        expect(fetch).toHaveBeenCalledTimes(1); // restore can reuse the persisted snapshot
    });

    it('filters deleted rows even when a stale snapshot is downloaded again', async () => {
        const index = await import('./localSortIndex');
        await index.getLocalSortIndex();
        index.removeLocalSortIndexRows(['a.jpg']);
        vi.stubGlobal('indexedDB', undefined);
        index.invalidateLocalSortIndex();
        expect((await index.getLocalSortIndex())?.map(row => row.filename)).toEqual(['b.jpg']);
    });

    it('allows a new upload with the same filename and a new thumbnail generation', async () => {
        const index = await import('./localSortIndex');
        await index.getLocalSortIndex();
        index.removeLocalSortIndexRows(['a.jpg']);
        vi.stubGlobal('indexedDB', undefined);
        vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(snapshot('generation-two')))));
        index.invalidateLocalSortIndex();
        expect((await index.getLocalSortIndex())?.[0].thumb).toBe('generation-two');
    });

    it('does not apply one library\'s deletes to another library', async () => {
        const index = await import('./localSortIndex');
        await index.getLocalSortIndex();
        index.removeLocalSortIndexRows(['a.jpg']);
        mocks.library = 'library-B';
        expect((await index.getLocalSortIndex())?.map(row => row.filename)).toEqual(['a.jpg', 'b.jpg']);
        mocks.library = 'library-A';
        mocks.postTools.mockReset().mockRejectedValue(new Error('Source unavailable'));
        expect((await index.getLocalSortIndex())?.map(row => row.filename)).toEqual(['b.jpg']);
    });

    it('does not let an older library response switch a fresh cache into server-paged mode', async () => {
        const index = await import('./localSortIndex');
        let finish!: (value: { available: boolean; indexUrl: string; sourceVersion: string; reason?: string }) => void;
        mocks.get.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }))
            .mockResolvedValue({ available: true, indexUrl: 'https://storage.test/sort', sourceVersion: 'new' });
        const old = index.getLocalSortIndex();
        index.invalidateLocalSortIndex();
        await index.getLocalSortIndex();
        finish({ available: false, indexUrl: '', sourceVersion: '', reason: 'library_too_large' });
        await old;
        expect(index.isServerPagedLibrary()).toBe(false);
    });

    it('aborts a stalled response body and releases the shared request for a retry', async () => {
        vi.useFakeTimers();
        vi.stubGlobal('indexedDB', undefined);
        const fetcher = vi.fn(async (_url: string, options: RequestInit) => ({
            ok: true,
            arrayBuffer: () => new Promise<ArrayBuffer>((_resolve, reject) => {
                options.signal?.addEventListener('abort', () => reject(new Error('aborted')));
            }),
        }));
        vi.stubGlobal('fetch', fetcher);
        const index = await import('./localSortIndex');
        const pending = index.getLocalSortIndex();
        await vi.advanceTimersByTimeAsync(120_000);
        expect(await pending).toBeNull();
        expect(fetcher.mock.calls[0][1].signal?.aborted).toBe(true);
        vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(snapshot()))));
        expect(await index.getLocalSortIndex()).toHaveLength(2);
        expect(vi.getTimerCount()).toBe(0);
    });

    it('captures the ordered upload generation when saving after an IndexedDB reload', async () => {
        let index = await import('./localSortIndex');
        await index.getLocalSortIndex();
        vi.resetModules(); index = await import('./localSortIndex');
        await index.getLocalSortIndex();
        const j = await import('./pendingEdits');
        j.confirmPendingEdit(j.beginPendingEdit('photo', 'a.jpg', { rating: 5 }));
        vi.stubGlobal('indexedDB', undefined);
        vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(snapshot('replacement')))));
        index.invalidateLocalSortIndex();
        expect((await index.getLocalSortIndex())?.[0].rating).toBe(4);
    });

});
