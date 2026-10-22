import React from 'react';
import { act, cleanup, render, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
    get: vi.fn(), post: vi.fn(), index: vi.fn(), token: vi.fn(),
    albumsIndex: vi.fn(),
    labelPerson: vi.fn(), poll: vi.fn(), postTools: vi.fn(),
}));
vi.mock('../../services/apiClient', () => ({ get: mocks.get, post: mocks.post, postTools: mocks.postTools }));
vi.mock('../../services/localSortIndex', () => ({
    getLocalSortIndex: mocks.index, invalidateLocalSortIndex: vi.fn(), isServerPagedLibrary: () => false,
    patchLocalSortIndexRow: vi.fn(), removeLocalSortIndexRows: vi.fn(), clearLocalSortIndexRemovals: vi.fn(),
}));
vi.mock('../../services/mediaToken', () => ({
    getMediaToken: mocks.token, getCachedMediaToken: () => null,
    thumbnailUrlForBlob: (blob: string, token: unknown) => token ? `https://storage.invalid/${blob}` : '',
}));
vi.mock('../../services/backgroundRequestQueue', () => ({ enqueueBackgroundRequest: (task: () => Promise<unknown>) => task() }));
vi.mock('../../services/indexBuilding', () => ({ reportIndexBuilding: vi.fn(), onIndexReady: () => () => {} }));
vi.mock('../../services/faceService', () => ({ default: { labelPerson: mocks.labelPerson } }));
vi.mock('../../services/libraryClient', () => ({}));
vi.mock('../../services/thumbnailAccessCache', () => ({ resolveThumbnailAccessUrls: vi.fn() }));
vi.mock('../../services/localAlbumsIndex', () => ({ getLocalAlbumsIndex: mocks.albumsIndex, invalidateLocalAlbumsIndex: vi.fn() }));
vi.mock('../../services/localPeopleIndex', () => ({ invalidateLocalPeopleIndex: vi.fn() }));
vi.mock('../../services/jobNotifications', () => ({ requestJobPoll: mocks.poll }));

import { __resetPendingEditsForTests, beginPendingEdit, confirmPendingEdit, pendingEditScope } from '../../services/pendingEdits';
import { galleryIndexPhoto, StoreProvider, useStore } from './store';

let store: ReturnType<typeof useStore>;
function Probe() {
    store = useStore();
    return null;
}
const rows = () => Array.from({ length: 800 }, (_, i) => ({
    filename: `p${i}.jpg`, captureDate: '2026-01-01T00:00:00Z', uploadDate: null,
    rating: 3, likes: 1, thumb: `blob-${i}`,
    gallery: { liked: true, rotation: 90, thumbnailRotation: 0, personIds: ['p1'], tags: ['beach'], processing: { thumbnail: 'done' } },
}));

describe('index-backed gallery', () => {
    beforeEach(() => {
        __resetPendingEditsForTests();
        mocks.get.mockReset().mockResolvedValue({ years: {} });
        mocks.post.mockReset().mockResolvedValue({ photos: [] });
        mocks.index.mockReset().mockResolvedValue(rows());
        mocks.token.mockReset().mockResolvedValue({ baseUrl: 'https://storage.invalid', sas: 'token' });
        mocks.albumsIndex.mockReset().mockResolvedValue([]);
        mocks.labelPerson.mockReset().mockResolvedValue({ success: true });
        mocks.poll.mockReset();
        mocks.postTools.mockReset().mockRejectedValue(new Error('Source unavailable'));
    });
    afterEach(() => { cleanup(); vi.restoreAllMocks(); });

    it.each(['Remote', 'Unnamed 1'])('keeps reconciled person name %s after a later rename fails', async (remoteName) => {
        const actual = await vi.importActual<typeof import('../../services/faceService')>('../../services/faceService');
        mocks.labelPerson.mockImplementation(actual.default.labelPerson);
        mocks.get.mockResolvedValue({ available: true, rows: [
            { personId: 'p1', name: 'Original', isNamed: true, faceCount: 1 },
        ] });
        render(<StoreProvider><Probe /></StoreProvider>);
        await act(async () => { await store.fetchPeople(); await store.openPerson('p1'); });
        await act(async () => { store.renamePerson('p1', 'First'); });
        await waitFor(() => expect(store.people[0]?.name).toBe('First'));
        mocks.postTools.mockResolvedValue({ libraryId: JSON.parse(pendingEditScope())[1],
            rows: [{ kind: 'person', id: 'p1', values: { name: remoteName } }] });
        await act(async () => {
            await (await import('../../services/reconcilePendingEdits')).reconcilePendingEdits();
        });
        const expectedName = remoteName === 'Remote' ? remoteName : null;
        expect(store.people[0]?.name).toBe(expectedName);
        mocks.post.mockRejectedValue(new Error('offline'));
        await act(async () => { store.renamePerson('p1', 'Attempt'); });
        await waitFor(() => expect(store.people[0]?.name).not.toBe('Attempt'));
        expect(store.personById('p1')?.name).toBe(expectedName);
        expect(store.people[0]?.name).toBe(expectedName);
        // Remove the paged row so personById falls back to extraPeople.
        mocks.get.mockResolvedValue({ available: true, rows: [], hasMore: false });
        await act(async () => { await store.fetchPeople(); });
        expect(store.people).toEqual([]);
        expect(store.personById('p1')?.name).toBe(expectedName);
    });

    it('shows a later authoritative name even if the optimistic name was never indexed', async () => {
        mocks.get.mockResolvedValue({ available: true, rows: [{ personId: 'p1', name: 'Alice', isNamed: true, faceCount: 1 }] });
        render(<StoreProvider><Probe /></StoreProvider>);
        await act(async () => { store.reloadPeople(); });
        await waitFor(() => expect(store.people[0]?.name).toBe('Alice'));
        await act(async () => { store.renamePerson('p1', 'Beatrice'); });
        // labelPerson is mocked above; model the receipt normally created by that service.
        confirmPendingEdit(beginPendingEdit('person', 'p1', { name: 'Beatrice' }));
        mocks.postTools.mockResolvedValue({ libraryId: JSON.parse(pendingEditScope())[1],
            rows: [{ kind: 'person', id: 'p1', values: { name: 'Bob' } }] });
        await act(async () => { store.reloadPeople(); });
        await waitFor(() => expect(store.people[0]?.name).toBe('Bob'));
        await act(async () => { store.reloadPeople(); });
        await waitFor(() => expect(store.people[0]?.name).toBe('Bob'));
    });

    it('loads and scrolls complete tiles without metadata requests', async () => {
        render(<StoreProvider><Probe /></StoreProvider>);
        await waitFor(() => expect(store.photosLoading).toBe(false));
        const firstPage = store.photos.length;
        expect(firstPage).toBeGreaterThan(0);
        expect(store.photos[0]).toMatchObject({ liked: true, rotation: 90, personIds: ['p1'], tags: ['beach'] });
        act(() => store.loadMorePhotos());
        await waitFor(() => expect(store.photos.length).toBeGreaterThan(firstPage));
        expect(mocks.post).not.toHaveBeenCalled();
    });

    it('keeps an edited name visible until the People index catches up', async () => {
        let indexedName = 'Alice';
        mocks.get.mockImplementation(async () => ({ available: true, rows: [
            { personId: 'p1', name: indexedName, isNamed: true, faceCount: 1 },
        ] }));
        render(<StoreProvider><Probe /></StoreProvider>);
        await act(async () => { await store.fetchPeople(); });
        await act(async () => { store.renamePerson('p1', 'Beatrice'); });
        expect(store.people[0].name).toBe('Beatrice');
        expect(mocks.poll).toHaveBeenCalledTimes(1);
        await act(async () => { await store.fetchPeople(); });
        expect(store.people[0].name).toBe('Beatrice');
        indexedName = 'Beatrice';
        await act(async () => { await store.fetchPeople(); });
        indexedName = 'Carol'; // A subsequent authoritative edit is not masked forever.
        await act(async () => { await store.fetchPeople(); });
        expect(store.people[0].name).toBe('Carol');
    });

    it('does not let an older People response overwrite a newer refresh', async () => {
        let finish!: (value: unknown) => void;
        const page = (name: string) => ({ available: true, rows: [{ personId: 'p1', name, isNamed: true, faceCount: 1 }] });
        render(<StoreProvider><Probe /></StoreProvider>);
        await waitFor(() => expect(store.photosLoading).toBe(false));
        mocks.get.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }))
            .mockResolvedValue(page('Latest'));
        let old!: Promise<void>;
        act(() => { old = store.fetchPeople(); });
        await act(async () => { await store.fetchPeople(); });
        await act(async () => { finish(page('Old')); await old; });
        expect(store.people[0].name).toBe('Latest');
    });

    it('keeps the edited person visible if a stale first page no longer includes it', async () => {
        mocks.get.mockResolvedValue({ available: true, rows: [{ personId: 'p1', name: 'Alice', isNamed: true, faceCount: 1 }] });
        render(<StoreProvider><Probe /></StoreProvider>);
        await act(async () => { await store.fetchPeople(); });
        await act(async () => { store.renamePerson('p1', 'Beatrice'); });
        mocks.get.mockResolvedValue({ available: true, rows: [], total: 1, hasMore: true });
        await act(async () => { await store.fetchPeople(); });
        expect(store.people).toEqual([expect.objectContaining({ id: 'p1', name: 'Beatrice' })]);
    });

    it('rolls back failed naming but an older failure cannot undo a newer edit', async () => {
        mocks.get.mockResolvedValue({ available: true, rows: [{ personId: 'p1', name: 'Alice', isNamed: true, faceCount: 1 }] });
        render(<StoreProvider><Probe /></StoreProvider>);
        await act(async () => { await store.fetchPeople(); });
        mocks.labelPerson.mockRejectedValueOnce(new Error('offline'));
        await act(async () => { store.renamePerson('p1', 'Failed'); });
        expect(store.people[0].name).toBe('Alice');
        let fail!: (error: Error) => void;
        mocks.labelPerson.mockImplementationOnce(() => new Promise((_resolve, reject) => { fail = reject; }));
        act(() => { store.renamePerson('p1', 'Older'); });
        await act(async () => { store.renamePerson('p1', 'Latest'); });
        await act(async () => { fail(new Error('older save failed')); });
        expect(store.people[0].name).toBe('Latest');
    });

    it('wakes completion polling after rating and like saves', async () => {
        render(<StoreProvider><Probe /></StoreProvider>);
        await waitFor(() => expect(store.photosLoading).toBe(false));
        await act(async () => { store.ratePhotos(['p0.jpg'], 5); });
        expect(mocks.poll).toHaveBeenCalledTimes(1);
        await act(async () => { store.toggleLike('p0.jpg'); });
        expect(mocks.poll).toHaveBeenCalledTimes(2);
    });

    it('date jumps and rating filters stay index-only', async () => {
        render(<StoreProvider><Probe /></StoreProvider>);
        await waitFor(() => expect(store.photosLoading).toBe(false));
        await act(async () => { await store.jumpToGalleryDate('2026-01-01'); });
        act(() => store.setGalleryRating(3));
        await waitFor(() => expect(store.photosLoading).toBe(false));
        expect(store.photos.length).toBeGreaterThan(0);
        expect(mocks.post).not.toHaveBeenCalled();
        expect(mocks.get.mock.calls.map(([path]) => path)).not.toContain('/photos');
    });

    it('keeps proxy thumbnails working without fetching tile metadata', async () => {
        mocks.token.mockResolvedValue(null);
        render(<StoreProvider><Probe /></StoreProvider>);
        await waitFor(() => expect(store.photosLoading).toBe(false));
        expect(store.photos[0].thumbnailUrl).toMatch(/^\/api\/photos\/thumbnail\//);
        expect(mocks.post).not.toHaveBeenCalled();
    });

    it('uses enrichment for older snapshots during schema migration', async () => {
        mocks.index.mockResolvedValue(rows().slice(0, 1).map(({ gallery, ...row }) => row));
        render(<StoreProvider><Probe /></StoreProvider>);
        await waitFor(() => expect(store.photosLoading).toBe(false));
        expect(mocks.post).toHaveBeenCalledWith('/api/photos/lookup-batch', expect.objectContaining({ directMedia: true }));
    });

    it('keeps a saved rating through navigation-style refetch and provider remount before the index rebuilds', async () => {
        const mounted = render(<StoreProvider><Probe /></StoreProvider>);
        await waitFor(() => expect(store.photosLoading).toBe(false));
        await act(async () => { store.ratePhotos(['p0.jpg'], 5); });
        await act(async () => { store.reloadPhotos(); });
        await waitFor(() => expect(store.photosLoading).toBe(false));
        expect(store.photoById('p0.jpg')?.rating).toBe(5);
        mounted.unmount();
        render(<StoreProvider><Probe /></StoreProvider>);
        await waitFor(() => expect(store.photosLoading).toBe(false));
        expect(store.photos.find(photo => photo.id === 'p0.jpg')?.rating).toBe(5);
    });

    it('shows a pending name from beyond the first indexed page without skipping the next page', async () => {
        confirmPendingEdit(beginPendingEdit('person', 'late-person', { name: 'New name' }));
        const page = Array.from({ length: 120 }, (_, n) => ({ personId: `person-${n}`, name: 'Known', isNamed: true, faceCount: 1 }));
        const late = { personId: 'late-person', name: 'Unnamed 121', isNamed: false, faceCount: 1 };
        mocks.get.mockImplementation(async (url: string) => {
            if (!url.startsWith('/api/persons/page')) return { years: {} };
            if (url.includes('ids=')) return { available: true, sourceVersion: 'v1', rows: [late] };
            return { available: true, sourceVersion: 'v1', rows: url.includes('offset=120') ? [late] : page, total: 121, hasMore: !url.includes('offset=120') };
        });
        render(<StoreProvider><Probe /></StoreProvider>);
        await act(async () => { await store.fetchPeople(); });
        expect(store.people.find(person => person.id === 'late-person')?.name).toBe('New name');
        await act(async () => { store.loadMorePeople(); });
        expect(mocks.get.mock.calls.some(([url]) => String(url).includes('offset=120'))).toBe(true);
        expect(store.people.filter(person => person.id === 'late-person')).toHaveLength(1);
    });

    it('maps all tile fields directly from the index', () => {
        expect(galleryIndexPhoto(rows()[0], null)).toMatchObject({
            liked: true, rotation: 90, thumbnailRotation: 0, tags: ['beach'],
            personIds: ['p1'], processing: { thumbnail: 'done' },
        });
    });

    it('reuses loaded albums on repeat visits, including an empty list', async () => {
        render(<StoreProvider><Probe /></StoreProvider>);
        await waitFor(() => expect(store.photosLoading).toBe(false));
        await act(async () => { await store.fetchAlbums(); });
        expect(store.albumsLoading).toBe(false);
        await act(async () => { await store.fetchAlbums(); });
        expect(mocks.albumsIndex).toHaveBeenCalledTimes(1);
        expect(mocks.get.mock.calls.some(([path]) => path === '/albums')).toBe(false);
    });

    it('shares overlapping album loads and keeps loaded albums visible during forced refresh', async () => {
        mocks.albumsIndex.mockResolvedValue([{ albumId: 'a1', name: 'Trip', photoCount: 2 }]);
        render(<StoreProvider><Probe /></StoreProvider>);
        await waitFor(() => expect(store.photosLoading).toBe(false));
        await act(async () => { await store.fetchAlbums(); });
        let resolve!: (value: unknown[]) => void;
        mocks.albumsIndex.mockImplementation(() => new Promise(r => { resolve = r; }));
        let first!: Promise<void>;
        let second!: Promise<void>;
        act(() => { first = store.fetchAlbums(true); second = store.fetchAlbums(); });
        expect(first).toBe(second);
        expect(store.albumsLoading).toBe(false);
        expect(store.albums[0].name).toBe('Trip');
        await act(async () => { resolve([{ albumId: 'a1', name: 'Renamed', photoCount: 2 }]); await first; });
        expect(store.albums[0].name).toBe('Renamed');
        expect(mocks.albumsIndex).toHaveBeenCalledTimes(2);
    });

    it('refreshes again if invalidation arrives during an in-flight read', async () => {
        let resolve!: (value: unknown[]) => void;
        mocks.albumsIndex.mockImplementationOnce(() => new Promise(r => { resolve = r; }))
            .mockResolvedValue([{ albumId: 'a1', name: 'Latest', photoCount: 1 }]);
        render(<StoreProvider><Probe /></StoreProvider>);
        await waitFor(() => expect(store.photosLoading).toBe(false));
        let request!: Promise<void>;
        act(() => { request = store.fetchAlbums(); store.reloadAlbums(); });
        await act(async () => { resolve([]); await request; });
        expect(mocks.albumsIndex).toHaveBeenCalledTimes(2);
        expect(store.albums[0].name).toBe('Latest');
    });

    it('uses legacy data without repeating cold-index waits on warm refreshes', async () => {
        mocks.albumsIndex.mockResolvedValue([{ albumId: 'a1', name: 'Trip', photoCount: 2 }]);
        render(<StoreProvider><Probe /></StoreProvider>);
        await waitFor(() => expect(store.photosLoading).toBe(false));
        await act(async () => { await store.fetchAlbums(); });
        mocks.albumsIndex.mockClear().mockResolvedValue(null);
        mocks.get.mockResolvedValue({ albums: [{ id: 'a1', name: 'Legacy', photoCount: 2 }] });
        await act(async () => { await store.fetchAlbums(true); });
        expect(mocks.albumsIndex).toHaveBeenCalledTimes(1);
        expect(store.albums[0].name).toBe('Legacy');
        await act(async () => { await store.fetchAlbums(); });
        expect(mocks.albumsIndex).toHaveBeenCalledTimes(1);
    });

    it('retains cached albums on refresh failure and allows an explicit retry', async () => {
        mocks.albumsIndex.mockResolvedValue([{ albumId: 'a1', name: 'Trip', photoCount: 2 }]);
        render(<StoreProvider><Probe /></StoreProvider>);
        await waitFor(() => expect(store.photosLoading).toBe(false));
        await act(async () => { await store.fetchAlbums(); });
        mocks.albumsIndex.mockResolvedValue(null);
        mocks.get.mockRejectedValue(new Error('offline'));
        await act(async () => { await store.fetchAlbums(true); });
        expect(store.albums[0].name).toBe('Trip');
        expect(store.albumsLoading).toBe(false);
        mocks.albumsIndex.mockResolvedValue([]);
        await act(async () => { await store.fetchAlbums(true); });
        expect(store.albums).toEqual([]);
    });

    it('revalidates an older list in the background after the navigation cache expires', async () => {
        const now = Date.now();
        const clock = vi.spyOn(Date, 'now').mockReturnValue(now);
        render(<StoreProvider><Probe /></StoreProvider>);
        await waitFor(() => expect(store.photosLoading).toBe(false));
        await act(async () => { await store.fetchAlbums(); });
        clock.mockReturnValue(now + 60001);
        await act(async () => { await store.fetchAlbums(); });
        expect(mocks.albumsIndex).toHaveBeenCalledTimes(2);
        expect(store.albumsLoading).toBe(false);
    });
});
// Append this test to a copy of store.gallery.test.tsx to reuse its controlled service fixtures.
it('opening an older album request cannot undo a successful rename', async () => {
    mocks.albumsIndex.mockResolvedValue([{ albumId: 'a1', name: 'Old', photoCount: 0, coverFilename: '' }]);
    mocks.index.mockResolvedValue(rows());
    mocks.token.mockResolvedValue({ baseUrl: 'https://storage.invalid', sas: 'token' });
    mocks.post.mockResolvedValue({ success: true });
    render(<StoreProvider><Probe /></StoreProvider>);
    await act(async () => { store.reloadAlbums(); });
    await waitFor(() => expect(store.albumById('a1')?.name).toBe('Old'));
    let finish!: (value: unknown) => void;
    mocks.get.mockImplementation((url: string) => url.startsWith('/albums/a1?')
        ? new Promise(resolve => { finish = resolve; }) : Promise.resolve({ years: {} }));
    act(() => { store.openAlbum('a1'); });
    await waitFor(() => expect(finish).toBeDefined());
    await act(async () => { store.renameAlbum('a1', 'New'); });
    await waitFor(() => expect(store.albumById('a1')?.name).toBe('New'));
    await act(async () => { finish({ album: { id: 'a1', name: 'Old', photoCount: 0 }, photos: [] }); });
    expect(store.albumById('a1')?.name).toBe('New');
    cleanup();
});
// Append to a temporary copy of store.gallery.test.tsx in the same directory to reuse its fixtures.
it('finds a pending rename beyond the enrichment budget when the search matches only that name', async () => {
    __resetPendingEditsForTests();
    mocks.postTools.mockReset().mockRejectedValue(new Error('Source temporarily unavailable'));
    mocks.index.mockResolvedValue(rows()); mocks.albumsIndex.mockResolvedValue([]);
    mocks.token.mockResolvedValue({ baseUrl: 'https://storage.invalid', sas: 'token' });
    for (let n = 0; n < 101; n++) confirmPendingEdit(beginPendingEdit('person', 'pending-' + n, { name: 'New Name ' + n }));
    mocks.get.mockImplementation(async (url: string) => {
        const ids = new URL(url, 'https://test.invalid').searchParams.get('ids');
        if (ids) return { available: true, sourceVersion: 'v1', rows: ids.split(',').map(personId => ({
            personId, name: 'Unnamed 1', isNamed: false, faceCount: 1,
        })) };
        return { available: true, sourceVersion: 'v1', rows: [], total: 101 };
    });
    render(<StoreProvider><Probe /></StoreProvider>);
    let result: Awaited<ReturnType<typeof store.searchPeople>> = [];
    await act(async () => { result = await store.searchPeople('New Name 100'); });
    expect(result.map(person => person.id)).toContain('pending-100');
    cleanup();
});

it('filters renamed-away People search rows and respects the requested result limit', async () => {
    __resetPendingEditsForTests();
    mocks.postTools.mockReset().mockRejectedValue(new Error('Source temporarily unavailable'));
    mocks.index.mockResolvedValue(rows()); mocks.albumsIndex.mockResolvedValue([]);
    mocks.token.mockResolvedValue({ baseUrl: 'https://storage.invalid', sas: 'token' });
    confirmPendingEdit(beginPendingEdit('person', 'renamed-away', { name: 'Other' }));
    for (let n = 0; n < 101; n++) confirmPendingEdit(beginPendingEdit('person', `pending-${n}`, { name: `Alice ${n}` }));
    const batches: string[][] = [];
    mocks.get.mockImplementation(async (url: string) => {
        const ids = new URL(url, 'https://test.invalid').searchParams.get('ids');
        if (ids) {
            batches.push(ids.split(','));
            return { available: true, sourceVersion: 'v1', rows: ids.split(',').map(personId => ({
                personId, name: 'Unnamed 1', isNamed: false, faceCount: 1,
            })) };
        }
        return { available: true, sourceVersion: 'v1', rows: [{ personId: 'renamed-away', name: 'Alice', isNamed: true, faceCount: 1 }] };
    });
    render(<StoreProvider><Probe /></StoreProvider>);
    let result: Awaited<ReturnType<typeof store.searchPeople>> = [];
    await act(async () => { result = await store.searchPeople('Alice', 1); });
    expect(result).toHaveLength(1);
    expect(result[0].id).not.toBe('renamed-away');
    expect(result[0].name).toContain('Alice');
    expect(batches).toHaveLength(2);
    expect(batches.every(batch => batch.length <= 50)).toBe(true);
    expect(batches.flat()).toHaveLength(100);
    cleanup();
});
