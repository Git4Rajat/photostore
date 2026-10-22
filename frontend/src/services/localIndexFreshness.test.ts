import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ get: vi.fn(), library: 'A' }));
vi.mock('./apiClient', () => ({ get: mocks.get }));
vi.mock('./passwordAuthClient', () => ({ getActiveLibraryFromToken: () => mocks.library }));
vi.mock('./perf', () => ({ perf: { recordSpan: vi.fn() }, perfNow: () => 0 }));

const manifest = (version: string) => ({ available: true, sourceVersion: version, indexUrl: `https://storage.test/${version}` });
const response = (name: string) => new Response(JSON.stringify({ rows: [{ RowKey: name, personId: name, name, isNamed: true }] }));
const cache = async (kind: string) => {
    if (kind === 'people') {
        const module = await import('./localPeopleIndex');
        return { get: module.getLocalPeopleIndex, invalidate: module.invalidateLocalPeopleIndex };
    }
    const module = await import('./localSortIndex');
    return { get: module.getLocalSortIndex, invalidate: module.invalidateLocalSortIndex };
};

describe.each([['people', 'name'], ['sort', 'filename']])('%s index freshness', (kind, field) => {
    beforeEach(() => {
        vi.resetModules();
        mocks.library = 'A';
        mocks.get.mockReset().mockResolvedValue(manifest('new'));
        vi.stubGlobal('indexedDB', undefined);
        vi.stubGlobal('fetch', vi.fn(async (url) => response(String(url).split('/').pop()!)));
    });
    afterEach(() => vi.unstubAllGlobals());

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
