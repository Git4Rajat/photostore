import React from 'react';
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), albumsIndex: vi.fn(), confirm: vi.fn(), notify: vi.fn() }));
vi.mock('../../../services/apiClient', () => ({ get: mocks.get, post: mocks.post, postTools: vi.fn() }));
vi.mock('../../../services/localSortIndex', () => ({
    getLocalSortIndex: vi.fn().mockResolvedValue([]), invalidateLocalSortIndex: vi.fn(), isServerPagedLibrary: () => false,
    patchLocalSortIndexRow: vi.fn(), removeLocalSortIndexRows: vi.fn(), clearLocalSortIndexRemovals: vi.fn(),
}));
vi.mock('../../../services/mediaToken', () => ({
    getMediaToken: vi.fn().mockResolvedValue(null), getCachedMediaToken: () => null, thumbnailUrlForBlob: () => '',
}));
vi.mock('../../../services/backgroundRequestQueue', () => ({ enqueueBackgroundRequest: (task: () => Promise<unknown>) => task() }));
vi.mock('../../../services/indexBuilding', () => ({ reportIndexBuilding: vi.fn(), onIndexReady: () => () => {} }));
vi.mock('../../../services/faceService', () => ({ default: {} }));
vi.mock('../../../services/libraryClient', () => ({}));
vi.mock('../../../services/thumbnailAccessCache', () => ({ resolveThumbnailAccessUrls: vi.fn().mockResolvedValue(new Map()) }));
vi.mock('../../../services/localAlbumsIndex', () => ({ getLocalAlbumsIndex: mocks.albumsIndex, invalidateLocalAlbumsIndex: vi.fn() }));
vi.mock('../../../services/localPeopleIndex', () => ({ invalidateLocalPeopleIndex: vi.fn() }));
vi.mock('../../../services/jobNotifications', () => ({ requestJobPoll: vi.fn() }));
vi.mock('../../../services/imageClient', () => ({ useProtectedBlobUrls: () => ({}) }));
vi.mock('../../../services/notificationSink', () => ({ publishNotification: mocks.notify }));
vi.mock('../../../components/shared/dialogs', () => ({ confirmDialog: mocks.confirm, promptDialog: vi.fn() }));

import { AlbumsPage } from './AlbumsPage';
import { StoreProvider, useStore } from '../store';
import { AppSidebar } from '../components/AppSidebar';

let store: ReturnType<typeof useStore>;
function Probe() {
    store = useStore();
    return null;
}

const albumRows = (n: number) => Array.from({ length: n }, (_, i) => ({ albumId: `a${i}`, name: `Album ${i}`, photoCount: 1, coverFilename: '' }));

describe('albums page ways in', () => {
    beforeEach(() => {
        mocks.get.mockReset().mockResolvedValue({});
        mocks.post.mockReset().mockResolvedValue({});
        mocks.albumsIndex.mockReset();
        vi.spyOn(window, 'scrollTo').mockImplementation(() => {});
    });
    afterEach(() => { cleanup(); vi.restoreAllMocks(); });

    // Deleting the last album used to drop People and Places from the page with it.
    it('keeps People and places when there are no albums', async () => {
        mocks.albumsIndex.mockResolvedValue([]);
        render(<StoreProvider><Probe /><AlbumsPage /></StoreProvider>);
        await act(async () => { store.navigate('albums'); await store.fetchAlbums(true); });
        expect(await screen.findByText('No albums yet.')).toBeTruthy();
        expect(screen.getByRole('heading', { name: 'People and places' })).toBeTruthy();
        expect(screen.getByRole('button', { name: /People/ })).toBeTruthy();
        expect(screen.getByRole('button', { name: /Places and things/ })).toBeTruthy();
        expect(screen.getByRole('button', { name: /Videos/ })).toBeTruthy();
    });

    // The sidebar used to stop at eight albums with no way to reach the rest.
    it('lists every album in the sidebar, in its own scrolling list', async () => {
        mocks.albumsIndex.mockResolvedValue(albumRows(12));
        render(<StoreProvider><Probe /><AppSidebar /></StoreProvider>);
        await waitFor(() => expect(document.querySelectorAll('.pt-side-albums button').length).toBe(12));
        const names = Array.from(document.querySelectorAll('.pt-side-albums button span:not(.pt-side-thumb)'), (n) => n.textContent);
        expect(names).toEqual(albumRows(12).map((a) => a.name));
        expect(screen.getByRole('button', { name: /Recently deleted/ }).closest('.pt-side-albums')).toBeNull();
    });
});
