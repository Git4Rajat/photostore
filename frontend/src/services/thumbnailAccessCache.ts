import { post } from './apiClient';
import { getActiveLibraryFromToken } from './passwordAuthClient';

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

// Persisted to localStorage (not IndexedDB — these are short strings, not
// blobs, so this mirrors the same PersistedPhotoCache pattern photoCache.ts
// already uses for the gallery boot cache) so a fresh tab doesn't re-hit
// access-batch for every filename it already resolved last session, even
// though the day-stable SAS URLs backing them stay valid for ~48h. Scoped
// per active library, same reasoning as photoCacheKey there.
const STORAGE_KEY_BASE = 'photostore.thumbnailAccessCache';
let hydrated = false;

const storageKey = (): string => {
    const lib = getActiveLibraryFromToken();
    return lib ? `${STORAGE_KEY_BASE}.${lib}` : STORAGE_KEY_BASE;
};

// SAS URLs carry their own expiry as the `se` query param. Without parsing
// it we'd risk rehydrating an expired URL and serving a 403 straight from
// blob storage — anything without a parseable `se` (e.g. a non-SAS proxy
// URL) has no known expiry and is kept as-is.
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
        for (const [filename, url] of Object.entries(parsed)) {
            if (isHttpUrl(url) && isUrlFresh(url)) {
                urlCache.set(filename, url);
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
        // Ignore quota/serialization errors — the in-memory cache still works.
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

// Concurrent callers resolving the same not-yet-cached filename (e.g. several
// virtualized tiles mounting for the same photo at once, see Gallery
// windowing) share one network request/signed URL instead of each minting
// their own -- two different SAS signatures for the same blob otherwise show
// up as a false "blob-multi-url" duplicate-transfer report (see perf.ts).
const pending = new Map<string, Promise<string>>();

const fetchBatch = async (filenames: string[]): Promise<Map<string, string>> => {
    const out = new Map<string, string>();
    try {
        const response = await post('/api/photos/access-batch', { kind: 'thumbnail', filenames });
        const urls = (response && typeof response.urls === 'object' && response.urls) || {};
        let gotNewUrl = false;
        for (const filename of filenames) {
            const raw = urls[filename];
            const resolved = isHttpUrl(raw) ? raw : '';
            if (resolved) {
                urlCache.set(filename, resolved);
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

export const resolveThumbnailAccessUrls = async (filenames: string[]): Promise<Map<string, string>> => {
    hydrateFromStorage();
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

    const alreadyPending = new Map<string, Promise<string>>();
    const fresh: string[] = [];
    for (const filename of toFetch) {
        const existing = pending.get(filename);
        if (existing) {
            alreadyPending.set(filename, existing);
        } else {
            fresh.push(filename);
        }
    }

    let freshPromise: Promise<Map<string, string>> | undefined;
    if (fresh.length > 0) {
        freshPromise = fetchBatch(fresh);
        const settled = freshPromise;
        fresh.forEach((filename) => {
            pending.set(filename, settled.then((map) => map.get(filename) || ''));
        });
        void settled.finally(() => {
            fresh.forEach((filename) => pending.delete(filename));
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

export const __resetThumbnailAccessCacheForTests = () => {
    urlCache.clear();
    hydrated = false;
    try {
        localStorage.removeItem(storageKey());
    } catch {
        // Ignore storage access failures in non-browser test environments.
    }
};
