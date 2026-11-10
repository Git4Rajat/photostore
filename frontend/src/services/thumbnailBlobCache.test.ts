import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('./passwordAuthClient', () => ({ getActiveLibraryFromToken: () => 'lib' }));

import {
    __resetThumbnailBlobCacheForTests,
    loadThumbnailObjectUrl,
    peekThumbnailObjectUrl,
    storeThumbnail,
} from './thumbnailBlobCache';

const SRC = 'https://acct.blob.core.windows.net/thumbs/a.jpg?se=2030-01-01T00%3A00%3A00Z&sig=x';

afterEach(() => {
    __resetThumbnailBlobCacheForTests();
    vi.unstubAllGlobals();
});

describe('thumbnailBlobCache', () => {
    it('misses without throwing when IndexedDB is unavailable', async () => {
        expect(peekThumbnailObjectUrl('a.jpg', SRC)).toBeNull();
        await expect(loadThumbnailObjectUrl('a.jpg', SRC)).resolves.toBeNull();
    });

    it('ignores non-http sources', async () => {
        const fetchMock = vi.fn();
        vi.stubGlobal('fetch', fetchMock);
        storeThumbnail('a.jpg', 'data:image/png;base64,AAAA');
        storeThumbnail('a.jpg', '/api/photos/thumb/a.jpg');
        await expect(loadThumbnailObjectUrl('a.jpg', 'blob:abc')).resolves.toBeNull();
        expect(fetchMock).not.toHaveBeenCalled();
    });

    it('stops fetching after repeated failures (e.g. no CORS on the storage account)', async () => {
        const fetchMock = vi.fn().mockRejectedValue(new TypeError('Failed to fetch'));
        vi.stubGlobal('fetch', fetchMock);
        for (let i = 0; i < 8; i += 1) {
            storeThumbnail(`p${i}.jpg`, SRC.replace('a.jpg', `p${i}.jpg`));
            await vi.waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThanOrEqual(Math.min(i + 1, 5)));
            await new Promise((resolve) => setTimeout(resolve, 0));
        }
        expect(fetchMock).toHaveBeenCalledTimes(5);
    });
});
