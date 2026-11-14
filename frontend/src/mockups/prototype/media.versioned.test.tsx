import { cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
    previews: new Map<string, string>(),
    originals: new Map<string, string>(),
    fetchPreview: vi.fn(),
    fetchOriginal: vi.fn(),
    resolveAccess: vi.fn(),
    NotFound: class extends Error { status = 404; },
}));
const TOKEN = {
    baseUrl: 'https://store.invalid/thumbnails', sas: 'sig=t', expiresAt: '2999-01-01T00:00:00Z', previewPrefix: 'preview/',
    image: { baseUrl: 'https://store.invalid/images', sas: 'sig=i' },
};
const THUMB = 'derived/v1/scope/key/thumbnail/t.jpg';
const PREVIEW = 'derived/v1/scope/key/preview/p.jpg';

vi.mock('../../services/localSortIndex', () => ({
    getCachedSortGrid: () => '',
    getCachedSortThumb: () => THUMB,
    getCachedSortPreview: (filename: string) => mocks.previews.get(filename) || '',
    getCachedSortOriginal: (filename: string) => mocks.originals.get(filename) || '',
}));
vi.mock('../../services/mediaToken', async (importOriginal) => ({
    ...(await importOriginal<typeof import('../../services/mediaToken')>()),
    getCachedMediaToken: () => TOKEN,
    getMediaToken: vi.fn(async () => TOKEN),
}));
vi.mock('../../services/mediaAccessCache', () => ({ resolveMediaAccessUrls: mocks.resolveAccess }));
vi.mock('../../services/imageClient', () => ({
    fetchProtectedBlobUrl: mocks.fetchPreview,
    fetchProtectedBlobUrlWithProgress: mocks.fetchOriginal,
    ProtectedFetchError: mocks.NotFound,
}));

import { preloadMediaAccessUrls, useMainMedia } from './media';
import type { Photo } from './types';

const photo = (filename: string): Photo => ({
    id: filename, filename, swatch: 's1', dateLabel: '', year: 2026, rating: 0,
    liked: false, placeId: null, personIds: [], tags: [],
});

describe('a photo whose thumbnail is a versioned derivative', () => {
    beforeEach(() => {
        mocks.previews.clear();
        mocks.previews.set('IMG_2.jpg', PREVIEW);
        mocks.originals.clear();
        mocks.originals.set('IMG_2.jpg', 'uuid-2');
        mocks.fetchPreview.mockReset().mockResolvedValue('blob:preview');
        mocks.fetchOriginal.mockReset().mockResolvedValue('blob:original');
        mocks.resolveAccess.mockReset().mockResolvedValue(new Map());
        URL.revokeObjectURL = vi.fn();
    });
    afterEach(cleanup);

    it('shows its preview straight from the media token, with no backend call', async () => {
        const { result } = renderHook(() => useMainMedia(photo('IMG_2.jpg')));
        await waitFor(() => expect(result.current.url).toBe('blob:preview'));
        expect(mocks.fetchPreview).toHaveBeenCalledWith(`${TOKEN.baseUrl}/${PREVIEW}?sig=t`);
        preloadMediaAccessUrls(['IMG_2.jpg']);
        expect(mocks.resolveAccess).not.toHaveBeenCalled();
    });

    it('loads full resolution from the original by its physical name', async () => {
        const { result } = renderHook(() => useMainMedia(photo('IMG_2.jpg'), true));
        await waitFor(() => expect(result.current.url).toBe('blob:original'));
        expect(mocks.fetchOriginal).toHaveBeenCalledWith('https://store.invalid/images/uuid-2?sig=i', expect.anything());
        expect(mocks.resolveAccess).not.toHaveBeenCalled();
    });

    it('asks the backend when a stale index names a preview that is gone, before trying the original', async () => {
        mocks.fetchPreview.mockReset()
            .mockRejectedValueOnce(new mocks.NotFound())
            .mockResolvedValueOnce('blob:current');
        mocks.resolveAccess.mockResolvedValue(new Map([['IMG_2.jpg', 'https://store.invalid/signed-preview']]));
        const { result } = renderHook(() => useMainMedia(photo('IMG_2.jpg')));
        await waitFor(() => expect(result.current.url).toBe('blob:current'));
        expect(mocks.resolveAccess).toHaveBeenCalledWith('preview', ['IMG_2.jpg']);
        expect(mocks.fetchPreview.mock.calls.map(([url]) => url)).toEqual([
            `${TOKEN.baseUrl}/${PREVIEW}?sig=t`, 'https://store.invalid/signed-preview',
        ]);
    });

    it('still asks the backend while its preview has no name yet', async () => {
        mocks.previews.clear();
        mocks.resolveAccess.mockResolvedValue(new Map([['IMG_2.jpg', 'https://store.invalid/proxy-preview']]));
        const { result } = renderHook(() => useMainMedia(photo('IMG_2.jpg')));
        await waitFor(() => expect(result.current.url).toBe('blob:preview'));
        expect(mocks.resolveAccess).toHaveBeenCalledWith('preview', ['IMG_2.jpg']);
        expect(mocks.fetchPreview).toHaveBeenCalledWith('https://store.invalid/proxy-preview');
    });
});
