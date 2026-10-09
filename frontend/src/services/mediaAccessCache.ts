import { post } from './apiClient';
import { getActiveLibraryFromToken } from './passwordAuthClient';

// Generalizes thumbnailAccessCache.ts's batched-resolve + cache pattern to
// the viewer's preview/full-image access kinds. Kept as its own module
// (rather than a kind parameter bolted onto thumbnailAccessCache.ts) so the
// existing, widely-used thumbnail cache's storage format and call sites stay
// completely untouched -- same "mirror, don't modify" precedent as
// localSortIndex.ts mirroring the former browser search index rather than parametrizing
// it in place.
//
// Fixes: the viewer (mockups/prototype/media.ts's useMainMedia) used to mint
// its own preview/image access URL per photo via the single-item
// GET /api/photos/access/<kind>/<filename> route -- a guaranteed Table
// Storage point-read (_get_metadata_entity) on every photo view/navigation.
// /api/photos/access-batch resolves metadata from the warm
// _cached_metadata_list_rows_for_user scan instead (already populated
// moments earlier when the gallery/album loaded this photo's tile), so even
// a "batch of one" call through this cache is materially cheaper -- and
// repeat views within a session (navigating back to an already-opened photo,
// or a neighbor PhotoViewer.tsx pre-warmed) skip the backend call entirely.
export type MediaAccessKind = 'preview' | 'image';

const isHttpUrl = (value?: string) => Boolean(value && /^https?:\/\//i.test(value));

const CACHE_LIMIT = 2000;
const urlCache = new Map<string, string>(); // keyed by `${kind}:${filename}`

const STORAGE_KEY_BASE = 'photostore.mediaAccessCache';
let hydrated = false;

const storageKey = (): string => {
    const lib = getActiveLibraryFromToken();
    return lib ? `${STORAGE_KEY_BASE}.${lib}` : STORAGE_KEY_BASE;
};

const cacheKey = (kind: MediaAccessKind, filename: string) => `${kind}:${filename}`;

// Mirrors thumbnailAccessCache.ts's isUrlFresh exactly -- SAS URLs carry
// their own expiry as the `se` query param.
const isUrlFresh = (url: string): boolean => {
    try {
        const se = new URL(url).searchParams.get('se');
        if (!se) {
            return true;
        }
        const expiry = Date.parse(se);
        if (Number.isNaN(expiry)) {
            return true;
        }
        return expiry - Date.now() > 5 * 60 * 1000;
    } catch {
        return true;
    }
};

const hydrateFromStorage = () => {
    if (hydrated) {
        return;
    }
    hydrated = true;
    try {
        const raw = localStorage.getItem(storageKey());
        if (!raw) {
            return;
        }
        const parsed = JSON.parse(raw) as Record<string, string>;
        if (!parsed || typeof parsed !== 'object') {
            return;
        }
        for (const [key, url] of Object.entries(parsed)) {
            if (isHttpUrl(url) && isUrlFresh(url)) {
                urlCache.set(key, url);
            }
        }
    } catch {
        // Ignore corrupt/inaccessible storage; falls back to a cold cache.
    }
};

const persistToStorage = () => {
    try {
        localStorage.setItem(storageKey(), JSON.stringify(Object.fromEntries(urlCache)));
    } catch {
        // Ignore quota/serialization errors -- the in-memory cache still works.
    }
};

const evictOldest = () => {
    if (urlCache.size <= CACHE_LIMIT) {
        return;
    }
    const oldestKey = urlCache.keys().next().value;
    if (oldestKey !== undefined) {
        urlCache.delete(oldestKey);
    }
};

// Concurrent callers resolving the same not-yet-cached (kind, filename) --
// e.g. the viewer's active photo and a neighbor-preload racing each other --
// share one network request/signed URL instead of each minting their own.
// Two different SAS signatures for the same blob otherwise show up as a
// false "blob-multi-url" duplicate-transfer report (see perf.ts). Mirrors
// thumbnailAccessCache.ts's pending-request map.
const pending = new Map<string, Promise<string>>();

const fetchBatch = async (kind: MediaAccessKind, filenames: string[]): Promise<Map<string, string>> => {
    const out = new Map<string, string>();
    try {
        const response = await post('/api/photos/access-batch', { kind, filenames });
        const urls = (response && typeof response.urls === 'object' && response.urls) || {};
        let gotNewUrl = false;
        for (const filename of filenames) {
            const raw = urls[filename];
            const resolved = isHttpUrl(raw) ? raw : '';
            if (resolved) {
                urlCache.set(cacheKey(kind, filename), resolved);
                evictOldest();
                gotNewUrl = true;
            }
            out.set(filename, resolved);
        }
        if (gotNewUrl) {
            persistToStorage();
        }
    } catch {
        for (const filename of filenames) {
            out.set(filename, '');
        }
    }
    return out;
};

/**
 * Resolves preview/image access URLs for a set of filenames, one kind at a
 * time, via a single batched /api/photos/access-batch call for whatever
 * isn't already cached. Callers pass a single filename (viewer's active
 * photo) or several (neighbor preload) -- either way, request count scales
 * with distinct cache misses, not with how many times this is called.
 */
export const resolveMediaAccessUrls = async (kind: MediaAccessKind, filenames: string[]): Promise<Map<string, string>> => {
    hydrateFromStorage();
    const result = new Map<string, string>();
    const toFetch: string[] = [];
    for (const filename of filenames) {
        const cached = urlCache.get(cacheKey(kind, filename));
        if (cached !== undefined) {
            result.set(filename, cached);
        } else {
            toFetch.push(filename);
        }
    }
    if (toFetch.length === 0) {
        return result;
    }

    const alreadyPending = new Map<string, Promise<string>>();
    const fresh: string[] = [];
    for (const filename of toFetch) {
        const existing = pending.get(cacheKey(kind, filename));
        if (existing) {
            alreadyPending.set(filename, existing);
        } else {
            fresh.push(filename);
        }
    }

    let freshPromise: Promise<Map<string, string>> | undefined;
    if (fresh.length > 0) {
        freshPromise = fetchBatch(kind, fresh);
        const settled = freshPromise;
        fresh.forEach((filename) => {
            pending.set(cacheKey(kind, filename), settled.then((map) => map.get(filename) || ''));
        });
        void settled.finally(() => {
            fresh.forEach((filename) => pending.delete(cacheKey(kind, filename)));
        });
    }

    await Promise.all(Array.from(alreadyPending, ([filename, promise]) => promise.then((url) => {
        result.set(filename, url);
    })));
    if (freshPromise) {
        const freshResult = await freshPromise;
        freshResult.forEach((url, filename) => result.set(filename, url));
    }
    return result;
};

export const __resetMediaAccessCacheForTests = () => {
    urlCache.clear();
    hydrated = false;
    try {
        localStorage.removeItem(storageKey());
    } catch {
        // Ignore storage access failures in non-browser test environments.
    }
};
