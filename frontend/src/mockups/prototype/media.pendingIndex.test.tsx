import { renderHook } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ resolve: vi.fn().mockResolvedValue(new Map()) }));
vi.mock('../../services/localSortIndex', () => ({
    getCachedSortGrid: () => '', getCachedSortOriginal: () => '', getCachedSortPreview: () => '', getCachedSortThumb: () => '',
}));
vi.mock('../../services/mediaToken', () => ({
    getCachedMediaToken: () => null, getMediaToken: vi.fn(), thumbnailUrlForBlob: () => '', previewUrlForBlob: () => '', imageUrlForBlob: () => '',
}));
vi.mock('../../services/thumbnailAccessCache', () => ({ resolveThumbnailAccessUrls: mocks.resolve }));
vi.mock('../../services/authClient', () => ({ isAuthEnabled: () => true }));

import { usePhotoThumbnails } from './media';
import type { Photo } from './types';

const pending = (filename: string, thumbnailUrl = ''): Photo => ({
    id: filename, filename, swatch: 's1', dateLabel: '', year: 2026, rating: 0, liked: false, placeId: null,
    personIds: [], tags: [], thumbnailUrl, pendingIndex: true,
});

describe('tiles for photos that are not indexed yet', () => {
    afterEach(() => mocks.resolve.mockClear());

    it('uses the browser-drawn blob thumbnail as is, with no backend thumbnail lookup', () => {
        const { result } = renderHook(() => usePhotoThumbnails([pending('a.jpg', 'blob:http://localhost/abc')]));
        expect(result.current['a.jpg']).toBe('blob:http://localhost/abc');
        expect(mocks.resolve).not.toHaveBeenCalled();
    });

    it('shows no image (swatch) and makes no backend call for HEIC/RAW/video the browser cannot draw', () => {
        const { result } = renderHook(() => usePhotoThumbnails([pending('a.heic'), pending('b.cr3'), pending('c.mov')]));
        expect(result.current).toEqual({});
        expect(mocks.resolve).not.toHaveBeenCalled();
    });
});
