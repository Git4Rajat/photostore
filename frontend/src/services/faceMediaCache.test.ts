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

// resolveFaceCropUrl/resolveFaceFallbackUrl chain several .then()/await hops
// on top of the raw limiter (toDisplayableUrl, cache writes, in-flight
// cleanup), so a fixed count of `await Promise.resolve()` isn't reliably
// enough to flush everything through to the next queued task starting —
// crossing a real macrotask boundary drains the whole microtask queue first,
// regardless of how many hops are chained.
const flushAsync = () => new Promise<void>((resolve) => setTimeout(resolve, 0));

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

    it('caps concurrency so no more than 6 crop requests run at once', async () => {
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

        // Let the initially-permitted requests start.
        await flushAsync();
        expect(maxConcurrent).toBeLessThanOrEqual(6);
        expect(pending.length).toBeLessThanOrEqual(6);

        // Drain the queue in batches, re-checking the cap holds throughout.
        let guard = 0;
        while (pending.length > 0) {
            guard += 1;
            if (guard > 20) {
                throw new Error('drain loop did not converge — pending never reached 0');
            }
            const batch = pending.splice(0, pending.length);
            batch.forEach((p, i) => p.resolve({ url: `https://sas.example/${i}.jpg` }));
            await flushAsync();
            expect(maxConcurrent).toBeLessThanOrEqual(6);
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

    it('crop and fallback draw from independent pools — a burst of slow crops does not block fallback requests', async () => {
        // Regression test: crop and fallback used to share one small queue, so a
        // page full of slow (cache-miss) crop requests could starve every fast,
        // pre-generated fallback thumbnail behind them, stalling the whole grid.
        const stuckCrops: Array<{ resolve: (v: { url: string }) => void }> = [];
        getUploadJson.mockImplementation((url: string) => {
            if (url.includes('/api/faces/crop/')) {
                // Stays pending until this test explicitly resolves it below —
                // simulates a page full of slow (still in-progress) misses.
                return new Promise((resolve) => stuckCrops.push({ resolve }));
            }
            return Promise.resolve({ url: 'https://sas.example/thumb.jpg' });
        });

        // Saturate the crop pool exactly to its capacity (not beyond — a queued
        // excess would need draining too, which isn't what this test is about).
        const cropPromises = Array.from({ length: 6 }, (_, i) => mod.resolveFaceCropUrl(`face-v1-slow-${i}`));
        await flushAsync();
        expect(stuckCrops.length).toBe(6);

        // A fallback request for an unrelated photo should still complete promptly,
        // not queue behind the stuck (pool-saturating) crop requests.
        const fallbackUrl = await mod.resolveFaceFallbackUrl('other-photo.jpg', '', '/api/photos/thumbnail/other-photo.jpg');
        expect(fallbackUrl).toBe('https://sas.example/thumb.jpg');

        // Clean up the stuck crop requests so they don't leak into other tests.
        stuckCrops.forEach((c, i) => c.resolve({ url: `https://sas.example/crop-${i}.jpg` }));
        await Promise.all(cropPromises);
    });
});
