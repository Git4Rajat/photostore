import React from 'react';
import { act, cleanup, render, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('../../services/apiClient', () => ({ get: mocks.get, post: mocks.post, postTools: vi.fn() }));
vi.mock('../../services/localSortIndex', () => ({
    getLocalSortIndex: vi.fn().mockResolvedValue([]), invalidateLocalSortIndex: vi.fn(), isServerPagedLibrary: () => false,
    patchLocalSortIndexRow: vi.fn(), removeLocalSortIndexRows: vi.fn(), clearLocalSortIndexRemovals: vi.fn(),
}));
vi.mock('../../services/mediaToken', () => ({
    getMediaToken: vi.fn().mockResolvedValue(null), getCachedMediaToken: () => null, thumbnailUrlForBlob: () => '',
}));
vi.mock('../../services/backgroundRequestQueue', () => ({ enqueueBackgroundRequest: (task: () => Promise<unknown>) => task() }));
vi.mock('../../services/indexBuilding', () => ({ reportIndexBuilding: vi.fn(), onIndexReady: () => () => {} }));
vi.mock('../../services/faceService', () => ({ default: {} }));
vi.mock('../../services/libraryClient', () => ({}));
vi.mock('../../services/thumbnailAccessCache', () => ({ resolveThumbnailAccessUrls: vi.fn() }));
vi.mock('../../services/localAlbumsIndex', () => ({ getLocalAlbumsIndex: vi.fn().mockResolvedValue([]), invalidateLocalAlbumsIndex: vi.fn() }));
vi.mock('../../services/localPeopleIndex', () => ({ invalidateLocalPeopleIndex: vi.fn() }));
vi.mock('../../services/jobNotifications', () => ({ requestJobPoll: vi.fn() }));

import { mapPhoto, StoreProvider, useStore } from './store';
import type { Photo as BackendPhoto } from '../../types/uiTypes';

let store: ReturnType<typeof useStore>;
function Probe() {
    store = useStore();
    return null;
}

const clip = () => mapPhoto({ filename: 'clip.mp4', captureDate: '2026-01-01T00:00:00Z' } as BackendPhoto, null);
const trashCalls = () => mocks.post.mock.calls
    .map(([path]) => String(path))
    .filter((path) => path === '/photos/delete' || path === '/photos/trash/restore');

describe('deleting and restoring photos', () => {
    beforeEach(() => {
        mocks.get.mockReset().mockResolvedValue({});
        mocks.post.mockReset().mockResolvedValue({});
    });
    afterEach(() => { cleanup(); vi.restoreAllMocks(); });

    it('Undo on the delete toast puts the deleted video back and takes it out of Recently Deleted', async () => {
        render(<StoreProvider><Probe /></StoreProvider>);
        await act(async () => { store.registerPhotos([clip()]); });
        await act(async () => { store.deletePhotos(['clip.mp4']); });
        expect(store.photoById('clip.mp4')).toBeUndefined();
        expect(store.trash.map((t) => t.photo.id)).toEqual(['clip.mp4']);

        const undo = store.toasts.find((t) => t.actionLabel === 'Undo');
        expect(undo?.message).toBe('Deleted 1 item');
        await act(async () => { undo!.onAction!(); });

        await waitFor(() => expect(mocks.post).toHaveBeenCalledWith('/photos/trash/restore', { filenames: ['clip.mp4'] }));
        expect(store.trash).toEqual([]);
        expect(store.photoById('clip.mp4')?.filename).toBe('clip.mp4');
    });

    it('a restore sent while the delete is still in flight waits for it, so the delete cannot land last', async () => {
        let finishDelete!: (value: unknown) => void;
        mocks.post.mockImplementation((path: string) => (path === '/photos/delete'
            ? new Promise((resolve) => { finishDelete = resolve; })
            : Promise.resolve({ restored: ['clip.mp4'], success: true })));
        render(<StoreProvider><Probe /></StoreProvider>);
        await act(async () => { store.registerPhotos([clip()]); });
        await act(async () => { store.deletePhotos(['clip.mp4']); });
        // Recently Deleted lists the photo straight away, so it can be restored
        // before the server has finished deleting it.
        await act(async () => { store.restorePhotos(['clip.mp4']); });
        expect(trashCalls()).toEqual(['/photos/delete']);
        expect(store.photoById('clip.mp4')).toBeTruthy();

        await act(async () => { finishDelete({ deleted: ['clip.mp4'], success: true }); });
        await waitFor(() => expect(trashCalls()).toEqual(['/photos/delete', '/photos/trash/restore']));
        expect(store.trash).toEqual([]);
    });

    it('a restore racing a failed delete does not list the photo twice', async () => {
        let failDelete!: (reason: unknown) => void;
        mocks.post.mockImplementation((path: string) => (path === '/photos/delete'
            ? new Promise((_resolve, reject) => { failDelete = reject; })
            : Promise.resolve({ restored: ['clip.mp4'], success: true })));
        render(<StoreProvider><Probe /></StoreProvider>);
        await act(async () => { store.registerPhotos([clip()]); });
        await act(async () => { store.deletePhotos(['clip.mp4']); });
        await act(async () => { store.restorePhotos(['clip.mp4']); });
        await act(async () => { failDelete(new Error('offline')); });
        await waitFor(() => expect(trashCalls()).toEqual(['/photos/delete', '/photos/trash/restore']));
        expect(store.photos.filter((p) => p.id === 'clip.mp4')).toHaveLength(1);
    });
});
