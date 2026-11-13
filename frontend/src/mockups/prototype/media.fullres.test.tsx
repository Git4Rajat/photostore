import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ fetchPreview: vi.fn(), fetchOriginal: vi.fn(), revoke: vi.fn() }));
vi.mock('../../services/localSortIndex', () => ({
    getCachedSortGrid: () => '',
    getCachedSortThumb: () => 'abc',
}));
vi.mock('../../services/mediaToken', () => ({
    getCachedMediaToken: () => ({ sas: 'sig=1' }),
    getMediaToken: vi.fn(),
    thumbnailUrlForBlob: () => 'https://store.invalid/thumbnails/abc',
    previewUrlForBlob: () => 'https://store.invalid/previews/abc',
    imageUrlForBlob: () => 'https://store.invalid/images/abc',
}));
vi.mock('../../services/imageClient', () => ({
    fetchProtectedBlobUrl: mocks.fetchPreview,
    fetchProtectedBlobUrlWithProgress: mocks.fetchOriginal,
    ProtectedFetchError: class extends Error { status = 0; },
}));

import { useMainMedia } from './media';
import type { Photo } from './types';

const photo = (filename: string): Photo => ({
    id: filename, filename, swatch: 's1', dateLabel: '', year: 2026, rating: 0,
    liked: false, placeId: null, personIds: [], tags: [],
});

describe('useMainMedia full resolution', () => {
    beforeEach(() => {
        mocks.fetchPreview.mockReset().mockResolvedValue('blob:preview');
        mocks.fetchOriginal.mockReset();
        mocks.revoke.mockReset();
        URL.revokeObjectURL = mocks.revoke;
    });
    afterEach(cleanup);

    it('keeps the preview on screen while the original downloads', async () => {
        let finish: (url: string) => void = () => {};
        mocks.fetchOriginal.mockImplementation(() => new Promise<string>((resolve) => { finish = resolve; }));
        const { result, rerender } = renderHook(({ fullRes }) => useMainMedia(photo('IMG_1.HEIC'), fullRes), { initialProps: { fullRes: false } });
        await waitFor(() => expect(result.current.url).toBe('blob:preview'));

        rerender({ fullRes: true });
        expect(result.current.url).toBe('blob:preview');
        expect(result.current.loading).toBe(true);
        expect(mocks.revoke).not.toHaveBeenCalled();

        await waitFor(() => expect(mocks.fetchOriginal).toHaveBeenCalled());
        await act(async () => { finish('blob:original'); });
        expect(result.current.url).toBe('blob:original');
        expect(result.current.loading).toBe(false);
        expect(mocks.revoke).toHaveBeenCalledWith('blob:preview');
    });

    it('keeps the preview when the original cannot be fetched', async () => {
        mocks.fetchOriginal.mockRejectedValue(new Error('network'));
        const { result, rerender } = renderHook(({ fullRes }) => useMainMedia(photo('IMG_1.HEIC'), fullRes), { initialProps: { fullRes: false } });
        await waitFor(() => expect(result.current.url).toBe('blob:preview'));
        rerender({ fullRes: true });
        await waitFor(() => expect(result.current.loading).toBe(false));
        expect(result.current.url).toBe('blob:preview');
        expect(result.current.status).toBeUndefined();
    });

    it('clears the stage when the photo changes', async () => {
        let finish: (url: string) => void = () => {};
        const { result, rerender } = renderHook(({ name }) => useMainMedia(photo(name), false), { initialProps: { name: 'IMG_1.HEIC' } });
        await waitFor(() => expect(result.current.url).toBe('blob:preview'));
        mocks.fetchPreview.mockImplementation(() => new Promise<string>((resolve) => { finish = resolve; }));
        rerender({ name: 'IMG_2.HEIC' });
        expect(result.current.url).toBeUndefined();
        expect(mocks.revoke).toHaveBeenCalledWith('blob:preview');
        await waitFor(() => expect(mocks.fetchPreview).toHaveBeenCalledTimes(2));
        await act(async () => { finish('blob:preview-2'); });
        expect(result.current.url).toBe('blob:preview-2');
    });
});
