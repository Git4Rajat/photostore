import React from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), albumsIndex: vi.fn(), confirm: vi.fn() }));
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
vi.mock('../../../services/thumbnailAccessCache', () => ({ resolveThumbnailAccessUrls: vi.fn() }));
vi.mock('../../../services/localAlbumsIndex', () => ({ getLocalAlbumsIndex: mocks.albumsIndex, invalidateLocalAlbumsIndex: vi.fn() }));
vi.mock('../../../services/localPeopleIndex', () => ({ invalidateLocalPeopleIndex: vi.fn() }));
vi.mock('../../../services/jobNotifications', () => ({ requestJobPoll: vi.fn() }));
vi.mock('../../../services/imageClient', () => ({ useProtectedBlobUrls: () => ({}) }));
vi.mock('../../../components/shared/dialogs', () => ({ confirmDialog: mocks.confirm, promptDialog: vi.fn() }));
vi.mock('../media', () => ({
    setPhotoRotation: vi.fn(), preloadMediaAccessUrls: vi.fn(), fetchPhotoMetadata: vi.fn().mockResolvedValue(null), downloadPhoto: vi.fn(),
    useMainMedia: () => ({ url: '', loading: false, progress: 0, status: '' }),
    useVideoSource: () => ({ src: undefined, failed: false, poster: undefined, reportError: vi.fn() }),
    useLiveClipSource: () => undefined,
}));
// The real shell is all stage and gestures; only the action bar matters here.
vi.mock('./ViewerShell', () => ({ default: ({ bar }: { bar: React.ReactNode }) => <div>{bar}</div> }));

import CommandBar from './CommandBar';
import PhotoViewer from './PhotoViewer';
import { mapPhoto, StoreProvider, useStore } from '../store';
import type { Photo as BackendPhoto } from '../../../types/uiTypes';

let store: ReturnType<typeof useStore>;
function Probe() {
    store = useStore();
    return null;
}

const photo = (filename: string) => mapPhoto({ filename, captureDate: '2026-01-01T00:00:00Z' } as BackendPhoto, null);
const deleteCalls = () => mocks.post.mock.calls.filter(([path]) => path === '/photos/delete');
const expectedDialog = (count: number) => expect.objectContaining({
    title: `Delete ${count} ${count === 1 ? 'item' : 'items'}?`,
    message: count === 1
        ? 'It moves to Recently Deleted, where you can restore it for 30 days.'
        : 'They move to Recently Deleted, where you can restore them for 30 days.',
    // The button names what it does, as in the redesign: "Delete 3 items".
    confirmLabel: `Delete ${count} ${count === 1 ? 'item' : 'items'}`,
    danger: true,
});

describe('delete confirmation', () => {
    beforeEach(() => {
        mocks.get.mockReset().mockResolvedValue({});
        mocks.post.mockReset().mockResolvedValue({});
        mocks.albumsIndex.mockReset().mockResolvedValue([]);
        mocks.confirm.mockReset();
    });
    afterEach(() => { cleanup(); vi.restoreAllMocks(); });

    const renderWith = async (ui: React.ReactNode) => {
        render(<StoreProvider><Probe />{ui}</StoreProvider>);
        await act(async () => { store.registerPhotos([photo('a.jpg'), photo('b.mp4'), photo('c.jpg')]); });
    };

    it('the viewer’s Delete asks first and does nothing when cancelled', async () => {
        mocks.confirm.mockResolvedValue(false);
        await renderWith(<PhotoViewer />);
        await act(async () => { store.openViewer(['b.mp4'], 0); });
        await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Delete' })); });
        expect(mocks.confirm).toHaveBeenCalledWith(expectedDialog(1));
        expect(deleteCalls()).toHaveLength(0);
        expect(store.photoById('b.mp4')).toBeTruthy();
    });

    it('the viewer deletes once confirmed', async () => {
        mocks.confirm.mockResolvedValue(true);
        await renderWith(<PhotoViewer />);
        await act(async () => { store.openViewer(['b.mp4'], 0); });
        await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Delete' })); });
        await waitFor(() => expect(mocks.post).toHaveBeenCalledWith('/photos/delete', { filenames: ['b.mp4'] }));
    });

    it('the selection bar asks before deleting everything after Select all', async () => {
        mocks.confirm.mockResolvedValue(false);
        await renderWith(<CommandBar />);
        await act(async () => { store.selectMany(['a.jpg', 'b.mp4', 'c.jpg']); });
        const trash = screen.getByRole('button', { name: 'Delete' });
        await act(async () => { fireEvent.click(trash); });
        expect(mocks.confirm).toHaveBeenCalledWith(expectedDialog(3));
        expect(deleteCalls()).toHaveLength(0);
        expect(store.selection).toHaveLength(3);

        mocks.confirm.mockResolvedValue(true);
        await act(async () => { fireEvent.click(trash); });
        await waitFor(() => expect(mocks.post).toHaveBeenCalledWith('/photos/delete', { filenames: ['a.jpg', 'b.mp4', 'c.jpg'] }));
    });
});

describe('selection menu inside an album', () => {
    beforeEach(() => {
        mocks.get.mockReset().mockResolvedValue({});
        mocks.post.mockReset().mockResolvedValue({});
        mocks.albumsIndex.mockReset().mockResolvedValue([
            { albumId: 'a1', name: 'Trip', photoCount: 2, coverFilename: '' },
            { albumId: 'a2', name: 'Home', photoCount: 1, coverFilename: '' },
        ]);
        vi.spyOn(window, 'scrollTo').mockImplementation(() => {});
    });
    afterEach(() => { cleanup(); vi.restoreAllMocks(); });

    it('puts Remove first in the bar for the open album, and it keeps photos in the library', async () => {
        render(<StoreProvider><Probe /><CommandBar /></StoreProvider>);
        await act(async () => { await store.fetchAlbums(true); });
        await act(async () => { store.navigate('albums', { albumId: 'a2' }); });
        await act(async () => { store.selectMany(['a.jpg']); });
        const buttons = Array.from(document.querySelectorAll('.pt-action-bar > button'));
        const remove = buttons.find((b) => b.textContent?.includes('Remove'));
        expect(remove).toBeTruthy();
        expect(buttons.indexOf(remove!)).toBeLessThan(buttons.findIndex((b) => b.textContent?.includes('Add to album')));
        await act(async () => { fireEvent.click(remove!); });
        expect(mocks.post).toHaveBeenCalledWith('/albums/a2/photos/remove', { filenames: ['a.jpg'] });
        expect(mocks.post).not.toHaveBeenCalledWith('/photos/delete', expect.anything());
    });

    it('says the photos are still in Photos, and Undo puts them back', async () => {
        render(<StoreProvider><Probe /><CommandBar /></StoreProvider>);
        await act(async () => { await store.fetchAlbums(true); });
        await act(async () => { store.navigate('albums', { albumId: 'a1' }); });
        await act(async () => { store.removePhotosFromAlbum('a1', ['a.jpg', 'c.jpg']); });
        await waitFor(() => expect(store.toasts.some((t) => t.message === 'Removed 2 items from “Trip”. They’re still in Photos.')).toBe(true));
        const toast = store.toasts.find((t) => t.actionLabel === 'Undo');
        await act(async () => { toast?.onAction?.(); });
        expect(mocks.post).toHaveBeenCalledWith('/albums/a1/photos/add', { filenames: ['a.jpg', 'c.jpg'] });
    });

    // The bare Albums tab shows album cards; no album is open to remove from.
    it('is not offered on the bare Albums tab', async () => {
        render(<StoreProvider><Probe /><CommandBar /></StoreProvider>);
        await act(async () => { await store.fetchAlbums(true); });
        await act(async () => { store.navigate('albums', {}); });
        await act(async () => { store.selectMany(['a.jpg']); });
        expect(screen.queryByRole('button', { name: /Remove/ })).toBeNull();
    });

    it('is not offered outside the Albums page', async () => {
        render(<StoreProvider><Probe /><CommandBar /></StoreProvider>);
        await act(async () => { await store.fetchAlbums(true); });
        await act(async () => { store.navigate('gallery', {}); });
        await act(async () => { store.selectMany(['a.jpg']); });
        expect(screen.queryByRole('button', { name: /Remove/ })).toBeNull();
    });
});
