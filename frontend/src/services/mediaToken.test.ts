import { beforeEach, describe, expect, it, vi } from 'vitest';

const getMock = vi.fn();
vi.mock('./apiClient', () => ({ get: (...args: unknown[]) => getMock(...args) }));
vi.mock('./passwordAuthClient', () => ({ getActiveLibraryFromToken: () => 'lib-1' }));

import { getCachedMediaToken, getMediaToken, imageUrlForBlob, invalidateMediaToken, previewUrlForBlob, thumbnailUrlForBlob } from './mediaToken';

const future = (hours: number) => new Date(Date.now() + hours * 3600_000).toISOString();
const tokenResponse = (hours = 30) => ({
    available: true, baseUrl: 'https://acct.blob.core.windows.net/thumbnails', sas: 'sp=r&sr=c&sig=abc', expiresAt: future(hours), previewPrefix: 'preview/',
    image: { baseUrl: 'https://acct.blob.core.windows.net/images', sas: 'sp=r&sr=c&sig=def' },
});

describe('mediaToken', () => {
    beforeEach(() => {
        getMock.mockReset();
        invalidateMediaToken();
        window.localStorage.clear();
    });

    it('fetches once, caches, and builds direct thumbnail/preview URLs without further requests', async () => {
        getMock.mockResolvedValue(tokenResponse());
        const token = await getMediaToken();
        expect(token).not.toBeNull();
        await getMediaToken();
        expect(getMock).toHaveBeenCalledTimes(1);
        expect(getCachedMediaToken()).toEqual(token);
        expect(thumbnailUrlForBlob('1ed9-uuid')).toBe('https://acct.blob.core.windows.net/thumbnails/1ed9-uuid?sp=r&sr=c&sig=abc');
        expect(previewUrlForBlob('1ed9-uuid')).toBe('https://acct.blob.core.windows.net/thumbnails/preview/1ed9-uuid.jpg?sp=r&sr=c&sig=abc');
        expect(imageUrlForBlob('1ed9-uuid')).toBe('https://acct.blob.core.windows.net/images/1ed9-uuid?sp=r&sr=c&sig=def');
    });

    it('shares one in-flight request between concurrent callers', async () => {
        getMock.mockResolvedValue(tokenResponse());
        await Promise.all([getMediaToken(), getMediaToken(), getMediaToken()]);
        expect(getMock).toHaveBeenCalledTimes(1);
    });

    it('reuses a stored token across reloads and refetches one that is about to expire', async () => {
        getMock.mockResolvedValue(tokenResponse());
        await getMediaToken();
        invalidateMediaToken(); // simulate a page reload: memory gone, localStorage kept
        await getMediaToken();
        expect(getMock).toHaveBeenCalledTimes(1);

        window.localStorage.clear();
        invalidateMediaToken();
        getMock.mockResolvedValue(tokenResponse(0.2)); // < 30 min left counts as stale
        await getMediaToken();
        invalidateMediaToken();
        await getMediaToken();
        expect(getMock).toHaveBeenCalledTimes(3);
    });

    it('returns null and builds no URLs when the backend has no token (proxy mode)', async () => {
        getMock.mockResolvedValue({ available: false });
        expect(await getMediaToken()).toBeNull();
        expect(thumbnailUrlForBlob('x')).toBe('');
    });

    it('url-encodes blob names per path segment', () => {
        const token = { baseUrl: 'https://a/b', sas: 's=1', expiresAt: future(30), previewPrefix: 'preview/' };
        expect(thumbnailUrlForBlob('my photo#1.jpg', token)).toBe('https://a/b/my%20photo%231.jpg?s=1');
    });
});
