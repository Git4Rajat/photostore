import React from 'react';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Photo } from '../types';

const mocks = vi.hoisted(() => ({
    setMediaFilter: vi.fn(), setGalleryLikedOnly: vi.fn(), setGalleryRating: vi.fn(), setCaptureRange: vi.fn(),
    state: { mediaFilter: 'all', likedOnly: false, rating: 0 },
}));
const photo: Photo = { id: 'a.jpg', filename: 'a.jpg', swatch: 's1', dateLabel: '', year: 2024, rating: 0, liked: false, placeId: null, personIds: [], tags: [] };

vi.mock('../store', () => ({
    isVideoFilename: () => false,
    useStore: () => ({
        photos: [photo], navigate: vi.fn(), selectMany: vi.fn(), photosLoading: false, hasMorePhotos: false,
        loadMorePhotos: vi.fn(), reloadPhotos: vi.fn(), totalPhotos: 1,
        mediaFilter: mocks.state.mediaFilter, setMediaFilter: mocks.setMediaFilter,
        galleryFilters: { rating: mocks.state.rating, likedOnly: mocks.state.likedOnly },
        setGalleryRating: mocks.setGalleryRating, setGalleryLikedOnly: mocks.setGalleryLikedOnly,
        captureRange: null, setCaptureRange: mocks.setCaptureRange, timeline: null,
        route: { page: 'gallery', params: {} }, focusPhoto: vi.fn(), selectMode: false, setSelectMode: vi.fn(),
        selection: [], clearSelection: vi.fn(),
    }),
}));
vi.mock('../../../components/AppServicesProvider', () => ({
    useAppServices: () => ({ requestUpload: vi.fn(), startUpload: vi.fn(), uploadActive: false, registerUploadCompletionHandler: () => () => {} }),
}));
vi.mock('../../../components/UploadDock', () => ({ UploadDock: () => null }));
vi.mock('../components/PhotoGrid', () => ({ default: () => <div data-testid="grid" /> }));

import GalleryPage from './GalleryPage';

describe('Photos filter (one Filter button)', () => {
    beforeEach(() => {
        Object.assign(mocks.state, { mediaFilter: 'all', likedOnly: false, rating: 0 });
        vi.stubGlobal('IntersectionObserver', class { observe() {} disconnect() {} });
    });
    afterEach(() => { cleanup(); vi.clearAllMocks(); vi.unstubAllGlobals(); });

    it('keeps every filter behind one button, which opens a panel', () => {
        render(<GalleryPage />);
        expect(screen.queryByRole('button', { name: 'Videos' })).toBeNull();
        fireEvent.click(screen.getByRole('button', { name: 'Filter' }));
        const panel = screen.getByRole('dialog', { name: 'Filter photos' });
        fireEvent.click(within(panel).getByRole('button', { name: 'Videos' }));
        expect(mocks.setMediaFilter).toHaveBeenCalledWith('video');
        fireEvent.click(within(panel).getByRole('button', { name: 'Live' }));
        expect(mocks.setMediaFilter).toHaveBeenCalledWith('live');
        fireEvent.click(within(panel).getByRole('switch', { name: 'Liked only' }));
        expect(mocks.setGalleryLikedOnly).toHaveBeenCalledWith(true);
        fireEvent.click(within(panel).getByRole('button', { name: /4\+/ }));
        expect(mocks.setGalleryRating).toHaveBeenCalledWith(4);
        fireEvent.click(within(panel).getByRole('button', { name: 'This year' }));
        expect(mocks.setCaptureRange).toHaveBeenCalledWith(expect.objectContaining({ label: 'This year' }));
    });

    it('counts what is on and says it in one sentence with a Clear beside it', () => {
        Object.assign(mocks.state, { mediaFilter: 'video', likedOnly: true, rating: 4 });
        render(<GalleryPage />);
        expect(screen.getByRole('button', { name: 'Filter, 3 on' })).toBeTruthy();
        const sentence = document.querySelector('.pt-fsum');
        expect(sentence?.textContent).toContain('Videos you liked rated 4+ stars');
        fireEvent.click(screen.getByRole('button', { name: 'Clear' }));
        expect(mocks.setMediaFilter).toHaveBeenCalledWith('all');
        expect(mocks.setGalleryLikedOnly).toHaveBeenCalledWith(false);
        expect(mocks.setGalleryRating).toHaveBeenCalledWith(0);
        expect(mocks.setCaptureRange).toHaveBeenCalledWith(null);
    });
});
