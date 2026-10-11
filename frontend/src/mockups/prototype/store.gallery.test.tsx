import React from 'react';
import { act, cleanup, render, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
    get: vi.fn(), post: vi.fn(), index: vi.fn(), token: vi.fn(),
}));
vi.mock('../../services/apiClient', () => ({ get: mocks.get, post: mocks.post }));
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
vi.mock('../../services/faceService', () => ({ default: {} }));
vi.mock('../../services/libraryClient', () => ({}));
vi.mock('../../services/thumbnailAccessCache', () => ({ resolveThumbnailAccessUrls: vi.fn() }));
vi.mock('../../services/localAlbumsIndex', () => ({ getLocalAlbumsIndex: vi.fn(), invalidateLocalAlbumsIndex: vi.fn() }));
vi.mock('../../services/localPeopleIndex', () => ({ invalidateLocalPeopleIndex: vi.fn() }));
vi.mock('../../services/jobNotifications', () => ({ requestJobPoll: vi.fn() }));

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
        mocks.get.mockReset().mockResolvedValue({ years: {} });
        mocks.post.mockReset().mockResolvedValue({ photos: [] });
        mocks.index.mockReset().mockResolvedValue(rows());
        mocks.token.mockReset().mockResolvedValue({ baseUrl: 'https://storage.invalid', sas: 'token' });
    });
    afterEach(cleanup);

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

    it('maps all tile fields directly from the index', () => {
        expect(galleryIndexPhoto(rows()[0], null)).toMatchObject({
            liked: true, rotation: 90, thumbnailRotation: 0, tags: ['beach'],
            personIds: ['p1'], processing: { thumbnail: 'done' },
        });
    });
});
