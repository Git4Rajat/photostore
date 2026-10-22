import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ get: vi.fn(), library: 'library-a' }));
vi.mock('./apiClient', () => ({ get: mocks.get }));
vi.mock('./passwordAuthClient', () => ({ getActiveLibraryFromToken: () => mocks.library }));
vi.mock('./perf', () => ({ perf: { recordSpan: vi.fn() }, perfNow: () => 0 }));

import { __resetPendingEditsForTests, beginPendingEdit } from './pendingEdits';
import { getLocalAlbumsIndex, invalidateLocalAlbumsIndex } from './localAlbumsIndex';

const manifest = (version: string) => ({ available: true, indexUrl: `https://storage.test/${version}`, sourceVersion: version });
const response = (albumId: string) => ({
    ok: true,
    arrayBuffer: async () => new TextEncoder().encode(JSON.stringify({ rows: [{ albumId, name: albumId }] })).buffer,
});

describe('Albums index session cache', () => {
    beforeEach(() => {
        invalidateLocalAlbumsIndex();
        __resetPendingEditsForTests();
        mocks.library = 'library-a';
        mocks.get.mockReset().mockResolvedValue(manifest('v1'));
        // Exercise the supported memory-only fallback without browser persistence.
        vi.stubGlobal('indexedDB', undefined);
        vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response('a1')));
    });
    afterEach(() => { vi.unstubAllGlobals(); });

    it('loads once and reuses the index on repeat visits', async () => {
        expect((await getLocalAlbumsIndex())?.[0].albumId).toBe('a1');
        expect((await getLocalAlbumsIndex())?.[0].albumId).toBe('a1');
        expect(mocks.get).toHaveBeenCalledTimes(1);
        expect(fetch).toHaveBeenCalledTimes(1);
    });

    it('shares concurrent reads', async () => {
        await Promise.all([getLocalAlbumsIndex(), getLocalAlbumsIndex()]);
        expect(mocks.get).toHaveBeenCalledTimes(1);
        expect(fetch).toHaveBeenCalledTimes(1);
    });

    it.each([true, false])('keeps source fields separate from optimistic fields for concurrent readers (raw first: %s)', async (rawFirst) => {
        beginPendingEdit('album', 'a1', {name: 'Optimistic'});
        const first = getLocalAlbumsIndex({raw: rawFirst});
        const second = getLocalAlbumsIndex({raw: !rawFirst});
        const [one, two] = await Promise.all([first, second]);
        expect(one?.[0].name).toBe(rawFirst ? 'a1' : 'Optimistic');
        expect(two?.[0].name).toBe(rawFirst ? 'Optimistic' : 'a1');
        expect((await getLocalAlbumsIndex({raw: true}))?.[0].name).toBe('a1');
        expect((await getLocalAlbumsIndex())?.[0].name).toBe('Optimistic');
        expect(mocks.get).toHaveBeenCalledTimes(1);
    });

    it('does not let a pre-invalidation response overwrite a newer cache', async () => {
        let finishOld!: (value: ReturnType<typeof manifest>) => void;
        mocks.get.mockImplementationOnce(() => new Promise(resolve => { finishOld = resolve; }))
            .mockResolvedValue(manifest('v2'));
        vi.mocked(fetch).mockImplementation(async url => response(String(url).endsWith('v2') ? 'new' : 'old') as Response);
        const old = getLocalAlbumsIndex();
        invalidateLocalAlbumsIndex();
        expect((await getLocalAlbumsIndex())?.[0].albumId).toBe('new');
        finishOld(manifest('v1'));
        await old;
        expect((await getLocalAlbumsIndex())?.[0].albumId).toBe('new');
        expect(mocks.get).toHaveBeenCalledTimes(2);
    });

    it('does not expose the previous library cache while the next library loads', async () => {
        await getLocalAlbumsIndex();
        let finishNew!: (value: ReturnType<typeof manifest>) => void;
        mocks.library = 'library-b';
        mocks.get.mockImplementationOnce(() => new Promise(resolve => { finishNew = resolve; }));
        vi.mocked(fetch).mockResolvedValue(response('library-b-album') as Response);
        const first = getLocalAlbumsIndex();
        const overlapping = getLocalAlbumsIndex();
        finishNew(manifest('v2'));
        expect((await first)?.[0].albumId).toBe('library-b-album');
        expect((await overlapping)?.[0].albumId).toBe('library-b-album');
        expect(mocks.get).toHaveBeenCalledTimes(2);
    });

    it('keeps a different library isolated from a prior in-flight read', async () => {
        let finishOld!: (value: ReturnType<typeof manifest>) => void;
        mocks.get.mockImplementationOnce(() => new Promise(resolve => { finishOld = resolve; }))
            .mockResolvedValue(manifest('v2'));
        vi.mocked(fetch).mockImplementation(async url => response(String(url).endsWith('v2') ? 'library-b-album' : 'library-a-album') as Response);
        const old = getLocalAlbumsIndex();
        mocks.library = 'library-b';
        await getLocalAlbumsIndex();
        finishOld(manifest('v1'));
        await old;
        expect((await getLocalAlbumsIndex())?.[0].albumId).toBe('library-b-album');
    });
});