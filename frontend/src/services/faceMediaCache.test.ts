import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const getUploadJson = vi.fn();
const resolveApiUrl = vi.fn((url: string) => `resolved:${url}`);
const isAuthEnabled = vi.fn(() => false);
const fetchProtectedBlobUrl = vi.fn(async (path: string) => `blob:${path}`);

vi.mock('./apiClient', () => ({ getUploadJson, resolveApiUrl }));
vi.mock('./authClient', () => ({ isAuthEnabled }));
vi.mock('./imageClient', () => ({ fetchProtectedBlobUrl }));

type FaceMediaCacheModule = typeof import('./faceMediaCache');

// Deferred promise helper so tests can control exactly when a "network" call
// resolves, to observe how many are in flight at once.
const deferred = <T>() => {
    let resolve!: (value: T) => void;
    let reject!: (reason?: unknown) => void;
    const promise = new Promise<T>((res, rej) => {
        resolve = res;
        reject = rej;
    });
    return { promise, resolve, reject };
};

describe('faceMediaCache', () => {
    let mod: FaceMediaCacheModule;

    beforeEach(async () => {
        vi.resetModules();
        getUploadJson.mockReset();
        resolveApiUrl.mockReset().mockImplementation((url: string) => `resolved:${url}`);
        isAuthEnabled.mockReset().mockReturnValue(false);
        fetchProtectedBlobUrl.mockReset().mockImplementation(async (path: string) => `blob:${path}`);
        mod = await import('./faceMediaCache');
    });

    afterEach(() => {
        vi.restoreAllMocks();
    });

    it('resolves a crop URL from the backend response', async () => {
        getUploadJson.mockResolvedValue({ url: 'https://sas.example/cover.jpg' });
        const url = await mod.resolveFaceCropUrl('face-v1-abc');
        expect(url).toBe('https://sas.example/cover.jpg');
        expect(getUploadJson).toHaveBeenCalledWith('/api/faces/crop/face-v1-abc');
    });

    it('caches by faceId so a second call does not hit the network again', async () => {
        getUploadJson.mockResolvedValue({ url: 'https://sas.example/cover.jpg' });
        await mod.resolveFaceCropUrl('face-v1-abc');
        await mod.resolveFaceCropUrl('face-v1-abc');
        expect(getUploadJson).toHaveBeenCalledTimes(1);
    });

    it('dedupes concurrent in-flight requests for the same faceId', async () => {
        const d = deferred<{ url: string }>();
        getUploadJson.mockReturnValue(d.promise);

        const first = mod.resolveFaceCropUrl('face-v1-abc');
        const second = mod.resolveFaceCropUrl('face-v1-abc');
        d.resolve({ url: 'https://sas.example/cover.jpg' });

        await expect(first).resolves.toBe('https://sas.example/cover.jpg');
        await expect(second).resolves.toBe('https://sas.example/cover.jpg');
        expect(getUploadJson).toHaveBeenCalledTimes(1);
    });

    it('caps concurrency so no more than 4 crop requests run at once', async () => {
        const pending: Array<{ resolve: (v: { url: string }) => void }> = [];
        let concurrent = 0;
        let maxConcurrent = 0;
        getUploadJson.mockImplementation(
            () =>
                new Promise<{ url: string }>((resolve) => {
                    concurrent += 1;
                    maxConcurrent = Math.max(maxConcurrent, concurrent);
                    pending.push({
                        resolve: (v) => {
                            concurrent -= 1;
                            resolve(v);
                        },
                    });
                }),
        );

        const faceIds = Array.from({ length: 10 }, (_, i) => `face-v1-${i}`);
        const results = Promise.all(faceIds.map((id) => mod.resolveFaceCropUrl(id)));

        // Let microtasks flush so all initially-permitted requests start.
        await Promise.resolve();
        await Promise.resolve();
        expect(maxConcurrent).toBeLessThanOrEqual(4);
        expect(pending.length).toBeLessThanOrEqual(4);

        // Drain the queue in batches, re-checking the cap holds throughout.
        while (pending.length > 0) {
            const batch = pending.splice(0, pending.length);
            batch.forEach((p, i) => p.resolve({ url: `https://sas.example/${i}.jpg` }));
            await Promise.resolve();
            await Promise.resolve();
            expect(maxConcurrent).toBeLessThanOrEqual(4);
        }

        await results;
    });

    it('uses the inline thumbnail URL without any network call', async () => {
        const url = await mod.resolveFaceFallbackUrl('photo.jpg', 'https://sas.example/thumb.jpg', '/api/photos/thumbnail/photo.jpg');
        expect(url).toBe('https://sas.example/thumb.jpg');
        expect(getUploadJson).not.toHaveBeenCalled();
    });

    it('caches the fallback by filename, shared across faces from the same photo', async () => {
        getUploadJson.mockResolvedValue({ url: 'https://sas.example/thumb.jpg' });
        await mod.resolveFaceFallbackUrl('photo.jpg', '', '/api/photos/thumbnail/photo.jpg');
        await mod.resolveFaceFallbackUrl('photo.jpg', '', '/api/photos/thumbnail/photo.jpg');
        expect(getUploadJson).toHaveBeenCalledTimes(1);
    });

    it('falls back to the proxy path when the thumbnail lookup fails', async () => {
        getUploadJson.mockRejectedValue(new Error('network error'));
        const url = await mod.resolveFaceFallbackUrl('photo.jpg', '', '/api/photos/thumbnail/photo.jpg');
        expect(url).toBe('resolved:/api/photos/thumbnail/photo.jpg');
    });
});
