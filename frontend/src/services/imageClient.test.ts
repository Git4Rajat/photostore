import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const { resolveApiUrl, isAuthEnabled, getAccessToken } = vi.hoisted(() => ({
    resolveApiUrl: vi.fn((url: string) => url),
    isAuthEnabled: vi.fn(() => false),
    getAccessToken: vi.fn(async () => null),
}));

vi.mock('./apiClient', () => ({ resolveApiUrl }));
vi.mock('./authClient', () => ({ isAuthEnabled, getAccessToken }));

import { fetchProtectedBlobUrl } from './imageClient';

describe('fetchProtectedBlobUrl', () => {
    beforeEach(() => {
        resolveApiUrl.mockReset().mockImplementation((url: string) => url);
        isAuthEnabled.mockReset().mockReturnValue(false);
        vi.stubGlobal('URL', { ...URL, createObjectURL: vi.fn(() => 'blob:mock-url') });
    });

    afterEach(() => {
        vi.restoreAllMocks();
        vi.unstubAllGlobals();
    });

    // /api/faces/crop/<faceId> (and similar backend-relative endpoints) return a
    // JSON envelope like {"url": "..."} rather than raw image bytes. Before this
    // fix, response.blob() was called on the JSON body itself -- createObjectURL
    // still "succeeded" and produced a blob: URL, but an <img> pointed at it
    // couldn't decode it and rendered as a broken image.
    it('unwraps a JSON {url} envelope instead of blobbing the JSON body', async () => {
        const dataUrl = 'data:image/jpeg;base64,AAAA';
        vi.stubGlobal(
            'fetch',
            vi.fn(async () => ({
                ok: true,
                headers: { get: () => 'application/json' },
                json: async () => ({ url: dataUrl }),
            })),
        );

        const result = await fetchProtectedBlobUrl('/api/faces/crop/face-1');

        expect(result).toBe(dataUrl);
        expect(URL.createObjectURL).not.toHaveBeenCalled();
    });

    it('recursively resolves a JSON envelope pointing at another backend-relative path', async () => {
        const fetchMock = vi
            .fn()
            .mockResolvedValueOnce({
                ok: true,
                headers: { get: () => 'application/json' },
                json: async () => ({ url: '/api/faces/crop/face-1/resolved' }),
            })
            .mockResolvedValueOnce({
                ok: true,
                headers: { get: () => 'image/jpeg' },
                blob: async () => new Blob(['fake-bytes']),
            });
        vi.stubGlobal('fetch', fetchMock);

        const result = await fetchProtectedBlobUrl('/api/faces/crop/face-1');

        expect(result).toBe('blob:mock-url');
        expect(fetchMock).toHaveBeenCalledTimes(2);
    });

    it('still creates an object URL directly for a raw image response', async () => {
        vi.stubGlobal(
            'fetch',
            vi.fn(async () => ({
                ok: true,
                headers: { get: () => 'image/jpeg' },
                blob: async () => new Blob(['fake-bytes']),
            })),
        );

        const result = await fetchProtectedBlobUrl('/api/photos/thumbnail/foo.jpg');

        expect(result).toBe('blob:mock-url');
        expect(URL.createObjectURL).toHaveBeenCalledTimes(1);
    });
});
