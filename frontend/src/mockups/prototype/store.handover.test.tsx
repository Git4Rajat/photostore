import React from 'react';
import { act, cleanup, render, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// Handover from the just-uploaded overlay to the index-backed gallery list.
// The sort index is the thing being emulated: tests publish it in stages
// (empty -> partial -> complete) the way the indexer does after an upload wave.

const env = vi.hoisted(() => ({
    get: vi.fn(), post: vi.fn(), index: vi.fn(), serverPaged: false, scope: 'lib-A',
}));
vi.mock('../../services/apiClient', () => ({ get: env.get, post: env.post, postTools: vi.fn() }));
vi.mock('../../services/localSortIndex', () => ({
    getLocalSortIndex: env.index, invalidateLocalSortIndex: vi.fn(), isServerPagedLibrary: () => env.serverPaged,
    patchLocalSortIndexRow: vi.fn(), removeLocalSortIndexRows: vi.fn(), clearLocalSortIndexRemovals: vi.fn(),
}));
vi.mock('../../services/sessionScope', () => ({ sessionScope: () => env.scope }));
vi.mock('../../services/mediaToken', () => ({
    getMediaToken: vi.fn().mockResolvedValue({ baseUrl: 'https://storage.invalid', sas: 'token' }), getCachedMediaToken: () => null,
    thumbnailUrlForBlob: (blob: string, token: unknown) => token ? `https://storage.invalid/${blob}` : '',
}));
vi.mock('../../services/backgroundRequestQueue', () => ({ enqueueBackgroundRequest: (task: () => Promise<unknown>) => task() }));
vi.mock('../../services/indexBuilding', () => ({ reportIndexBuilding: vi.fn(), onIndexReady: () => () => {} }));
vi.mock('../../services/faceService', () => ({ default: { labelPerson: vi.fn(), findPersonFaces: vi.fn() } }));
vi.mock('../../services/libraryClient', () => ({}));
vi.mock('../../services/thumbnailAccessCache', () => ({ resolveThumbnailAccessUrls: vi.fn() }));
vi.mock('../../services/localAlbumsIndex', () => ({ getLocalAlbumsIndex: vi.fn().mockResolvedValue([]), invalidateLocalAlbumsIndex: vi.fn() }));
vi.mock('../../services/localPeopleIndex', () => ({ invalidateLocalPeopleIndex: vi.fn() }));
vi.mock('../../services/jobNotifications', () => ({ requestJobPoll: vi.fn() }));

import { __resetPendingEditsForTests } from '../../services/pendingEdits';
import { __resetJustUploadedForTests, addJustUploaded, flushJustUploaded, listJustUploaded } from '../../services/justUploaded';
import { StoreProvider, useStore } from './store';

let store: ReturnType<typeof useStore>;
function Probe() {
    store = useStore();
    return null;
}

const NEW = 'IMG_new.jpg';
const row = (filename: string, captureDate: string) => ({
    filename, captureDate, uploadDate: null, rating: 0, likes: 0, thumb: `blob-${filename}`,
    gallery: { liked: false, personIds: [], tags: [], processing: { thumbnail: 'done' } },
});
// 300 library photos, newest first (2026-01-xx down to 2025).
const library = () => Array.from({ length: 300 }, (_, i) => row(`p${i}.jpg`, new Date(Date.UTC(2026, 0, 1) - i * 86_400_000).toISOString()));
const names = () => store.photos.map((p) => p.filename);
const mount = async () => {
    render(<StoreProvider><Probe /></StoreProvider>);
    await waitFor(() => expect(store.photosLoading).toBe(false));
};
// A null index makes the store retry with a 3 s backoff before giving up, hence the long wait.
const reload = async () => {
    await act(async () => { store.reloadPhotos(); });
    await waitFor(() => expect(store.photosLoading).toBe(false), { timeout: 12_000 });
};
const upload = (filename = NEW, captureDate: string | null = '2026-03-01T00:00:00.000Z') => act(() => { addJustUploaded({ filename, captureDate }); flushJustUploaded(); });

describe('upload overlay -> index handover', () => {
    beforeEach(() => {
        __resetPendingEditsForTests();
        __resetJustUploadedForTests();
        env.serverPaged = false;
        env.scope = 'lib-A';
        env.get.mockReset().mockResolvedValue({ years: {} });
        env.post.mockReset().mockImplementation(async (path: string, data: { filenames?: string[] }) => (
            path === '/photos/delete' ? { deleted: data.filenames || [] } : { photos: [] }
        ));
        env.index.mockReset().mockResolvedValue(library());
    });
    afterEach(() => { cleanup(); vi.restoreAllMocks(); });

    it('shows an uploaded photo first, before any index has it, and counts it', async () => {
        await mount();
        const total = store.totalPhotos;
        const before = store.photos.length;
        upload();
        expect(names()[0]).toBe(NEW);
        expect(store.photos).toHaveLength(before + 1);
        expect(store.photos[0]).toMatchObject({ pendingIndex: true, year: 2026, dateLabel: 'Mar 1, 2026' });
        expect(store.totalPhotos).toBe((total ?? 0) + 1);
    });

    it('keeps counting past the tile cap while only drawing the newest tiles', async () => {
        await mount();
        const total = store.totalPhotos ?? 0;
        const before = store.photos.length;
        act(() => { for (let i = 0; i < 700; i += 1) addJustUploaded({ filename: `burst${i}.jpg`, captureDate: null }); });
        expect(store.totalPhotos).toBe(total + 700);
        expect(store.photos).toHaveLength(before + 500);
    });

    it('EMPTY library: counts every upload past the tile cap too', async () => {
        env.index.mockResolvedValue([]);
        await mount();
        console.log('EMPTY totalPhotos before', store.totalPhotos);
        act(() => { for (let i = 0; i < 700; i += 1) addJustUploaded({ filename: `burst${i}.jpg`, captureDate: null }); });
        console.log('EMPTY totalPhotos after', store.totalPhotos, 'tiles', store.photos.length);
        expect(store.totalPhotos).toBe(700);
        expect(store.photos).toHaveLength(500);
    });

    it('works when the library is still empty/unindexed (first upload into a new library)', async () => {
        env.index.mockResolvedValue([]);
        await mount();
        upload();
        expect(names()).toEqual([NEW]);
    });

    it('hands over when the index lists the photo on the first page: no duplicate, overlay retired', async () => {
        await mount();
        upload();
        env.index.mockResolvedValue([row(NEW, '2026-03-01T00:00:00.000Z'), ...library()]);
        await reload();
        await waitFor(() => expect(listJustUploaded()).toHaveLength(0));
        expect(names().filter((n) => n === NEW)).toHaveLength(1);
        expect(store.photos.find((p) => p.filename === NEW)?.pendingIndex).toBeUndefined();
        expect(store.totalPhotos).toBe(301);
    });

    it('hands over when the photo sorts deep in the index (old capture date): no stray tile on top, not lost', async () => {
        await mount();
        upload(NEW, '2026-03-01T00:00:00.000Z'); // hint says newest; EXIF will say 2009
        env.index.mockResolvedValue([row(NEW, '2009-06-01T00:00:00.000Z'), ...library()]);
        await reload();
        await waitFor(() => expect(listJustUploaded()).toHaveLength(0));
        expect(names()).not.toContain(NEW);          // not on page one any more...
        expect(store.totalPhotos).toBe(301);          // ...but counted, and reachable by paging
        expect(env.index).toHaveBeenCalled();
    });

    it('keeps the overlay when the index is published but does not have the photo yet (partial index)', async () => {
        await mount();
        upload();
        await reload(); // index-ready fired, index still lacks the file
        expect(listJustUploaded()).toHaveLength(1);
        expect(names()[0]).toBe(NEW);
        env.index.mockResolvedValue([row(NEW, '2026-03-01T00:00:00.000Z'), ...library()]);
        await reload(); // next publication has it
        await waitFor(() => expect(listJustUploaded()).toHaveLength(0));
        expect(names().filter((n) => n === NEW)).toHaveLength(1);
    });

    it('keeps the overlay when the index cannot be read at all (download failure)', async () => {
        await mount();
        upload();
        env.index.mockResolvedValue(null);
        await reload();
        expect(listJustUploaded()).toHaveLength(1);
        expect(names()[0]).toBe(NEW);
    }, 20_000);

    it('keeps the overlay when the index read throws', async () => {
        await mount();
        upload();
        env.index.mockRejectedValue(new Error('gzip failed'));
        await reload();
        expect(listJustUploaded()).toHaveLength(1);
    });

    it('a burst of uploads hands over together; later ones stay until the index has them', async () => {
        await mount();
        upload('a.jpg'); upload('b.jpg'); upload('c.jpg');
        expect(names().slice(0, 3).sort()).toEqual(['a.jpg', 'b.jpg', 'c.jpg']);
        env.index.mockResolvedValue([row('a.jpg', '2026-02-01T00:00:00.000Z'), row('b.jpg', '2026-02-01T00:00:00.000Z'), ...library()]);
        await reload();
        await waitFor(() => expect(listJustUploaded().map((e) => e.filename)).toEqual(['c.jpg']));
        expect(names()[0]).toBe('c.jpg');
    });

    it('hides the overlay under a rating / liked / date filter and shows it again when cleared', async () => {
        await mount();
        upload();
        act(() => { store.setGalleryRating(3); });
        await waitFor(() => expect(store.photosLoading).toBe(false));
        expect(names()).not.toContain(NEW);
        expect(listJustUploaded()).toHaveLength(1);   // kept, only hidden
        act(() => { store.setGalleryRating(0); });
        await waitFor(() => expect(store.photosLoading).toBe(false));
        expect(names()[0]).toBe(NEW);
        act(() => { store.setCaptureRange({ label: '2025', start: '2025-01-01', end: '2025-12-31' }); });
        await waitFor(() => expect(store.photosLoading).toBe(false));
        expect(names()).not.toContain(NEW);
    });

    it('deleting an overlay photo removes it for good (does not reappear on the next render or reload)', async () => {
        await mount();
        upload();
        await act(async () => { store.deletePhotos([NEW]); });
        await waitFor(() => expect(names()).not.toContain(NEW));
        await reload();
        expect(names()).not.toContain(NEW);
        expect(listJustUploaded()).toHaveLength(0);
    });

    it('does not let overlay rows disturb pagination of the real list', async () => {
        await mount();
        const firstPage = store.photos.length;
        upload('a.jpg'); upload('b.jpg');
        expect(store.photos).toHaveLength(firstPage + 2);
        await act(async () => { store.loadMorePhotos(); });
        await waitFor(() => expect(store.photosLoading).toBe(false));
        const real = store.photos.filter((p) => !p.pendingIndex).map((p) => p.filename);
        expect(new Set(real).size).toBe(real.length);             // no repeats or gaps from an offset shift
        expect(real.slice(0, 3)).toEqual(['p0.jpg', 'p1.jpg', 'p2.jpg']);
    });

    it('another library never shows this library\'s uploads', async () => {
        await mount();
        upload();
        expect(names()[0]).toBe(NEW);
        env.scope = 'lib-B';
        act(() => { addJustUploaded({ filename: 'other.jpg', captureDate: null }); flushJustUploaded(); });
        expect(names()).toContain('other.jpg');
        expect(names()).not.toContain(NEW);
    });

    it('hides a clip whose still is already in the gallery (earlier batch), and shows an unrelated video', async () => {
        env.index.mockResolvedValue([row('IMG_9.HEIC', '2026-02-01T00:00:00.000Z'), ...library()]);
        await mount();
        upload('IMG_9.MOV');
        upload('trip.mp4');
        expect(names()).toContain('trip.mp4');
        expect(names()).not.toContain('IMG_9.MOV');
        expect(store.totalPhotos).toBe(302); // 300 + the HEIC + trip.mp4; the clip adds nothing
    });

    describe('server-paged library (no whole-library index to consult)', () => {
        // Like the real getLocalSortIndex: too big for a client index, so it answers null.
        beforeEach(() => { env.serverPaged = true; env.index.mockResolvedValue(null); });

        it('retires the overlay when the listing page contains the photo', async () => {
            env.get.mockResolvedValue({ photos: [], total: 0 });
            await mount();
            upload();
            env.get.mockImplementation(async (path: string) => (path.startsWith('/photos?')
                ? { photos: [{ filename: NEW, captureDate: '2026-03-01T00:00:00.000Z', thumbnailUrl: '/t' }], total: 1 } : { years: {} }));
            await reload();
            await waitFor(() => expect(listJustUploaded()).toHaveLength(0));
            expect(names().filter((n) => n === NEW)).toHaveLength(1);
        });

        it('keeps the overlay while the listing page does not contain the photo', async () => {
            env.get.mockResolvedValue({ photos: [], total: 0 });
            await mount();
            upload();
            await reload();
            expect(listJustUploaded()).toHaveLength(1);
            expect(names()).toEqual([NEW]);
        });
    });
});
