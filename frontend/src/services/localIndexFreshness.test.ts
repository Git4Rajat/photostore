import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ get: vi.fn(), library: 'A' }));
vi.mock('./apiClient', () => ({ get: mocks.get }));
vi.mock('./passwordAuthClient', () => ({ getActiveLibraryFromToken: () => mocks.library }));
vi.mock('./perf', () => ({ perf: { recordSpan: vi.fn() }, perfNow: () => 0 }));

const manifest = (version: string) => ({ available: true, sourceVersion: version, indexUrl: `https://storage.test/${version}` });
const response = (name: string, sourceVersion?: string) => new Response(JSON.stringify({ sourceVersion,
    rows: [{ RowKey: name, personId: name, albumId: name, name, isNamed: true }] }));
const cache = async (kind: string) => {
    if (kind === 'albums') {
        const module = await import('./localAlbumsIndex');
        return { get: module.getLocalAlbumsIndex, invalidate: module.invalidateLocalAlbumsIndex };
    }
    if (kind === 'people') {
        const module = await import('./localPeopleIndex');
        return { get: module.getLocalPeopleIndex, invalidate: module.invalidateLocalPeopleIndex };
    }
    const module = await import('./localSortIndex');
    return { get: module.getLocalSortIndex, invalidate: module.invalidateLocalSortIndex };
};

// Seed a cache written by an older client that labeled an HTTP-cached payload
// with the freshly published manifest version.
const memoryDatabase = (initial: unknown) => {
    let value = initial;
    const database = {
        close: vi.fn(),
        transaction: () => {
            const tx: { oncomplete?: () => void; objectStore: () => unknown } = {
                objectStore: () => ({
                    get: () => {
                        const request: { result?: unknown; onsuccess?: () => void } = {};
                        queueMicrotask(() => { request.result = structuredClone(value); request.onsuccess?.(); });
                        return request;
                    },
                    put: (next: unknown) => {
                        value = structuredClone(next);
                        queueMicrotask(() => tx.oncomplete?.());
                    },
                }),
            };
            return tx;
        },
    };
    return { open: () => {
        const request: { result: typeof database; onsuccess?: () => void } = { result: database };
        queueMicrotask(() => request.onsuccess?.());
        return request;
    } };
};

describe.each([['people', 'name'], ['sort', 'filename'], ['albums', 'albumId']])('%s index freshness', (kind, field) => {
    beforeEach(() => {
        vi.resetModules();
        mocks.library = 'A';
        mocks.get.mockReset().mockResolvedValue(manifest('new'));
        vi.stubGlobal('indexedDB', undefined);
        vi.stubGlobal('fetch', vi.fn(async (url) => response(String(url).split('/').pop()!)));
    });
    afterEach(() => vi.unstubAllGlobals());

    it('repairs a cached old payload under a newer manifest and reuses the repaired version', async () => {
        const publishedVersion = '2026-10-28';
        const previousVersion = '2026-10-27';
        mocks.get.mockResolvedValue({ ...manifest(publishedVersion), indexUrl: 'https://storage.test/stable-catalog' });
        vi.stubGlobal('indexedDB', memoryDatabase({ sourceVersion: publishedVersion, payloadVersion: previousVersion,
            updatedAt: publishedVersion, rows: [{ filename: 'old', personId: 'old', albumId: 'old', name: 'old' }] }));
        // Model a browser HTTP cache containing the previous stable-URL body.
        const download = vi.fn(async (_url: string, options?: RequestInit) => options?.cache === 'no-store'
            ? response('new', publishedVersion) : response('old', previousVersion));
        vi.stubGlobal('fetch', download);
        const index = await cache(kind);
        expect((await index.get())?.[0]).toMatchObject({ [field]: 'new' });
        expect(download).toHaveBeenCalledWith('https://storage.test/stable-catalog',
            expect.objectContaining({ cache: 'no-store', signal: expect.any(AbortSignal) }));
        index.invalidate();
        expect((await index.get())?.[0]).toMatchObject({ [field]: 'new' });
        expect(download).toHaveBeenCalledTimes(1);
    });

    it('does not let a pre-invalidation result overwrite the refreshed cache', async () => {
        const index = await cache(kind);
        let finish!: (value: ReturnType<typeof manifest>) => void;
        mocks.get.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
        const old = index.get();
        index.invalidate();
        expect((await index.get())?.[0]).toMatchObject({ [field]: 'new' });
        finish(manifest('old'));
        await old;
        expect((await index.get())?.[0]).toMatchObject({ [field]: 'new' });
        expect(mocks.get).toHaveBeenCalledTimes(2);
    });

    it('does not clear the newer in-flight request when the old one finishes', async () => {
        const index = await cache(kind);
        let finishOld!: (value: ReturnType<typeof manifest>) => void;
        let finishNew!: (value: ReturnType<typeof manifest>) => void;
        mocks.get.mockImplementationOnce(() => new Promise(resolve => { finishOld = resolve; }))
            .mockImplementationOnce(() => new Promise(resolve => { finishNew = resolve; }));
        const old = index.get();
        index.invalidate();
        const current = index.get();
        finishOld(manifest('old'));
        await old;
        const shared = index.get();
        expect(mocks.get).toHaveBeenCalledTimes(2);
        finishNew(manifest('new'));
        expect((await current)?.[0]).toMatchObject({ [field]: 'new' });
        expect((await shared)?.[0]).toMatchObject({ [field]: 'new' });
    });

    it('does not show the previous library while a new library loads', async () => {
        const index = await cache(kind);
        await index.get();
        mocks.library = 'B';
        let finish!: (value: ReturnType<typeof manifest>) => void;
        mocks.get.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
        const first = index.get();
        const second = index.get();
        finish(manifest('library-B'));
        expect((await first)?.[0]).toMatchObject({ [field]: 'library-B' });
        expect((await second)?.[0]).toMatchObject({ [field]: 'library-B' });
    });
});
