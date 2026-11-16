import React from 'react';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Photo } from '../types';

const mocks = vi.hoisted(() => ({
    setMediaFilter: vi.fn(), setGalleryLikedOnly: vi.fn(), setGalleryRating: vi.fn(),
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
        jumpToGalleryDate: vi.fn(async () => null), captureRange: null, setCaptureRange: vi.fn(), timeline: null,
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

    it('keeps every filter behind one button, which opens a sheet', () => {
        render(<GalleryPage />);
        expect(screen.queryByRole('button', { name: 'Videos' })).toBeNull();
        fireEvent.click(screen.getByRole('button', { name: 'Filter' }));
        const sheet = screen.getByRole('dialog', { name: 'Filter photos' });
        fireEvent.click(sheet.querySelectorAll('.pt-fs-seg button')[2]);
        expect(mocks.setMediaFilter).toHaveBeenCalledWith('video');
        fireEvent.click(screen.getByRole('switch', { name: 'Only liked' }));
        expect(mocks.setGalleryLikedOnly).toHaveBeenCalledWith(true);
    });

    it('shows what is on as chips that each remove one filter, and a count on the button', () => {
        Object.assign(mocks.state, { mediaFilter: 'photo', likedOnly: true, rating: 4 });
        render(<GalleryPage />);
        expect(screen.getByRole('button', { name: 'Filter, 3 on' })).toBeTruthy();
        const chips = Array.from(document.querySelectorAll('.pt-active-chip')).map((c) => c.textContent);
        expect(chips).toEqual(['Photos', 'Liked', '4 stars']);
        fireEvent.click(document.querySelectorAll('.pt-active-chip')[1]);
        expect(mocks.setGalleryLikedOnly).toHaveBeenCalledWith(false);
        expect(mocks.setMediaFilter).not.toHaveBeenCalled();
    });
});
