import { getUploadJson, resolveApiUrl } from './apiClient';
import { isAuthEnabled } from './authClient';
import { fetchProtectedBlobUrl } from './imageClient';

// The People page can mount 30-200+ face avatars at once (a full cluster grid,
// or the unpaginated face-list view). Each one used to fire its own crop +
// thumbnail-fallback request the moment it entered the viewport, with no cap
// on how many ran in parallel — a burst that has been observed to spike the
// backend to 300+ requests/min against a single low-concurrency replica. This
// module gives every mounted avatar one shared, bounded queue plus a cache so
// repeat visits (or many faces from the same photo) don't repeat the work.
//
// Crop and fallback each get their OWN pool rather than sharing one. A crop
// cache-miss does real server-side work (image decode + re-encode) and can
// take seconds; a fallback thumbnail is pre-generated and cheap. Sharing one
// small queue meant a page full of first-time crop misses could starve every
// fallback fetch behind them too, stalling the whole grid.
const MAX_CONCURRENT_CROP_REQUESTS = 6;
const MAX_CONCURRENT_FALLBACK_REQUESTS = 6;

const createLimiter = (maxConcurrent: number) => {
    let active = 0;
    const queue: Array<() => void> = [];
    const runNext = () => {
        if (active >= maxConcurrent) {
            return;
        }
        const task = queue.shift();
        if (!task) {
            return;
        }
        active += 1;
        task();
    };
    return <T,>(fn: () => Promise<T>): Promise<T> =>
        new Promise<T>((resolve, reject) => {
            queue.push(() => {
                fn().then(resolve, reject).finally(() => {
                    active -= 1;
                    runNext();
                });
            });
            runNext();
        });
};

const limitCrop = createLimiter(MAX_CONCURRENT_CROP_REQUESTS);
const limitFallback = createLimiter(MAX_CONCURRENT_FALLBACK_REQUESTS);

// Mirrors FaceClusters.tsx's toDisplayableUrl: data:/absolute SAS URLs load
// directly, a relative auth-protected path needs a blob fetch.
const toDisplayableUrl = async (rawUrl: string): Promise<string> => {
    if (!rawUrl) {
        return '';
    }
    if (rawUrl.startsWith('data:') || /^https?:\/\//i.test(rawUrl)) {
        return rawUrl;
    }
    if (isAuthEnabled()) {
        return fetchProtectedBlobUrl(rawUrl);
    }
    return resolveApiUrl(rawUrl);
};

// face-v1-<hash> ids are content-addressed (see _deterministic_face_id in
// app.py) so a resolved crop is immutable for its lifetime — cached for the
// life of the tab. Bounded so a very long browsing session can't grow this
// without limit; eviction while an evicted URL is still on-screen just falls
// back to a re-fetch (rare in practice at this size), not a hard failure.
const CACHE_LIMIT = 600;

const evictOldest = (cache: Map<string, string>) => {
    if (cache.size <= CACHE_LIMIT) {
        return;
    }
    const oldestKey = cache.keys().next().value;
    if (oldestKey === undefined) {
        return;
    }
    const oldestUrl = cache.get(oldestKey);
    cache.delete(oldestKey);
    // Only non-SAS deployments (MEDIA_URL_MODE != 'sas') resolve to blob:
    // URLs here; SAS URLs are plain https links with nothing to revoke.
    if (oldestUrl && oldestUrl.startsWith('blob:')) {
        URL.revokeObjectURL(oldestUrl);
    }
};

const cropUrlCache = new Map<string, string>();
const cropUrlInFlight = new Map<string, Promise<string>>();

export const resolveFaceCropUrl = (faceId: string): Promise<string> => {
    const cached = cropUrlCache.get(faceId);
    if (cached !== undefined) {
        return Promise.resolve(cached);
    }
    const inFlight = cropUrlInFlight.get(faceId);
    if (inFlight) {
        return inFlight;
    }
    const promise = limitCrop(async () => {
        const result = await getUploadJson(`/api/faces/crop/${encodeURIComponent(faceId)}`);
        if (typeof result?.url !== 'string' || !result.url) {
            throw new Error('No crop url');
        }
        return toDisplayableUrl(result.url);
    })
        .then((url) => {
            cropUrlCache.set(faceId, url);
            evictOldest(cropUrlCache);
            return url;
        })
        .finally(() => {
            cropUrlInFlight.delete(faceId);
        });
    cropUrlInFlight.set(faceId, promise);
    return promise;
};

// Fallback thumbnails are keyed by filename, not face id — every face on the
// same photo shares one, so this also dedupes across faces from one photo.
const fallbackUrlCache = new Map<string, string>();
const fallbackUrlInFlight = new Map<string, Promise<string>>();

export const resolveFaceFallbackUrl = (
    filename: string,
    inlineThumb: string,
    proxyFallbackPath: string,
): Promise<string> => {
    // Listing already gave us a usable absolute URL — no network call at all.
    if (inlineThumb) {
        return Promise.resolve(inlineThumb);
    }
    const cached = fallbackUrlCache.get(filename);
    if (cached !== undefined) {
        return Promise.resolve(cached);
    }
    const inFlight = fallbackUrlInFlight.get(filename);
    if (inFlight) {
        return inFlight;
    }
    const promise = limitFallback(async () => {
        try {
            const result = await getUploadJson(`/api/photos/access/thumbnail/${encodeURIComponent(filename)}`);
            const rawFallback = typeof result?.url === 'string' && result.url ? result.url : proxyFallbackPath;
            return rawFallback ? await toDisplayableUrl(rawFallback) : '';
        } catch {
            return proxyFallbackPath ? toDisplayableUrl(proxyFallbackPath) : '';
        }
    })
        .then((url) => {
            fallbackUrlCache.set(filename, url);
            evictOldest(fallbackUrlCache);
            return url;
        })
        .finally(() => {
            fallbackUrlInFlight.delete(filename);
        });
    fallbackUrlInFlight.set(filename, promise);
    return promise;
};

export const __resetFaceMediaCacheForTests = () => {
    cropUrlCache.clear();
    cropUrlInFlight.clear();
    fallbackUrlCache.clear();
    fallbackUrlInFlight.clear();
};
