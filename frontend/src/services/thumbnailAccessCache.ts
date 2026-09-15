import { postUploadJson } from './apiClient';

// PhotoTile used to mint its own access token per tile (one GET per photo).
// Zooming the gallery to a denser grid multiplies the number of tiles
// mounted per page, which would multiply that request count too — the same
// failure mode documented in faceMediaCache.ts (a burst of per-avatar
// requests observed to spike the backend to 300+ req/min). Here every fetched
// page instead resolves all its filenames in one call to the existing
// /api/photos/access-batch endpoint, so request count scales with pages
// fetched, not with tiles rendered on screen.
//
// Only successful, directly-usable (absolute http) URLs are cached. A
// filename whose thumbnail isn't generated yet resolves to '' and is
// deliberately NOT cached, so a later re-resolution (e.g. scrolling back)
// can pick up a thumbnail that finished generating in the meantime.
const isHttpUrl = (value?: string) => Boolean(value && /^https?:\/\//i.test(value));

const CACHE_LIMIT = 2000;
const urlCache = new Map<string, string>();

const evictOldest = () => {
    if (urlCache.size <= CACHE_LIMIT) {
        return;
    }
    const oldestKey = urlCache.keys().next().value;
    if (oldestKey !== undefined) {
        urlCache.delete(oldestKey);
    }
};

export const resolveThumbnailAccessUrls = async (filenames: string[]): Promise<Map<string, string>> => {
    const result = new Map<string, string>();
    const toFetch: string[] = [];
    for (const filename of filenames) {
        const cached = urlCache.get(filename);
        if (cached !== undefined) {
            result.set(filename, cached);
        } else {
            toFetch.push(filename);
        }
    }
    if (toFetch.length === 0) {
        return result;
    }
    try {
        const response = await postUploadJson('/api/photos/access-batch', { kind: 'thumbnail', filenames: toFetch });
        const urls = (response && typeof response.urls === 'object' && response.urls) || {};
        for (const filename of toFetch) {
            const raw = urls[filename];
            const resolved = isHttpUrl(raw) ? raw : '';
            if (resolved) {
                urlCache.set(filename, resolved);
                evictOldest();
            }
            result.set(filename, resolved);
        }
    } catch {
        for (const filename of toFetch) {
            result.set(filename, '');
        }
    }
    return result;
};

export const __resetThumbnailAccessCacheForTests = () => {
    urlCache.clear();
};
