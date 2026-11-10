import { createBrowserDb, requestResult } from './browserStore';
import { getActiveLibraryFromToken } from './passwordAuthClient';

// Persistent thumbnail bytes. thumbnailAccessCache already remembers the signed
// URL per filename and the HTTP cache covers repeat loads of one URL, but SAS
// URLs rotate (and the HTTP cache can be evicted or partitioned), so a fresh
// tab or a new day re-downloads thumbnails the browser had already seen. This
// keeps the bytes in IndexedDB keyed by filename so they paint with no network.
//
// Best-effort end to end: the fetch needs CORS on the storage account, so any
// failure just leaves the tile on its normal remote <img> path.
const THUMBNAIL_CACHE_DB = 'photostore-thumb-cache';
const STORE = 'thumbs';
const MAX_TOTAL_BYTES = 100 * 1024 * 1024;
const EVICT_TO_BYTES = 80 * 1024 * 1024;
const MAX_ENTRY_BYTES = 2 * 1024 * 1024;
// Thumbnails are regenerated in place by a forced re-run, so bytes are only
// trusted for a bounded time (and only while the blob path is unchanged).
const MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000;
const MAX_CONCURRENT_STORES = 3;
const MAX_OBJECT_URLS = 500;
const BREAKER_THRESHOLD = 5;

interface StoredThumbnail {
    key: string;
    src: string;
    blob: Blob;
    size: number;
    storedAt: number;
    lastAccess: number;
}

const db = createBrowserDb(THUMBNAIL_CACHE_DB, 1, [
    { name: STORE, keyPath: 'key', indexes: [{ name: 'lastAccess', keyPath: 'lastAccess' }] },
]);

const keyFor = (filename: string) => `${getActiveLibraryFromToken() || '_'}|${filename}`;

// Signed URLs differ only by query string, so the blob path identifies content.
const sourceIdentity = (url: string): string => {
    try {
        const parsed = new URL(url);
        return `${parsed.origin}${parsed.pathname}`;
    } catch {
        return '';
    }
};

const isCacheableSource = (url: string) => /^https?:\/\//i.test(url) && sourceIdentity(url) !== '';

const objectUrls = new Map<string, { src: string; url: string }>();
const inFlight = new Set<string>();
const stored = new Set<string>();
let consecutiveFailures = 0;
let active = 0;
const waiting: Array<() => void> = [];
let approxTotalBytes: number | null = null;

const setObjectUrl = (key: string, src: string, blob: Blob): string => {
    const previous = objectUrls.get(key);
    if (previous) {
        URL.revokeObjectURL(previous.url);
        objectUrls.delete(key);
    }
    const url = URL.createObjectURL(blob);
    objectUrls.set(key, { src, url });
    if (objectUrls.size > MAX_OBJECT_URLS) {
        const oldest = objectUrls.keys().next().value;
        if (oldest !== undefined) {
            const entry = objectUrls.get(oldest);
            if (entry) {
                URL.revokeObjectURL(entry.url);
            }
            objectUrls.delete(oldest);
        }
    }
    return url;
};

// Synchronous hit for tiles remounting during scroll (no IndexedDB round trip).
export const peekThumbnailObjectUrl = (filename: string, srcUrl: string): string | null => {
    const entry = objectUrls.get(keyFor(filename));
    return entry && entry.src === sourceIdentity(srcUrl) ? entry.url : null;
};

export const loadThumbnailObjectUrl = async (filename: string, srcUrl: string): Promise<string | null> => {
    if (!isCacheableSource(srcUrl)) {
        return null;
    }
    const src = sourceIdentity(srcUrl);
    const key = keyFor(filename);
    const memory = objectUrls.get(key);
    if (memory && memory.src === src) {
        return memory.url;
    }
    const now = Date.now();
    const row = await db.run<StoredThumbnail | null>(STORE, 'readwrite', async (store) => {
        const found = await requestResult(store.get(key) as IDBRequest<StoredThumbnail | undefined>);
        if (!found) {
            return null;
        }
        if (found.src !== src || now - found.storedAt > MAX_AGE_MS) {
            store.delete(key);
            return null;
        }
        store.put({ ...found, lastAccess: now });
        return found;
    }, null);
    if (!row) {
        return null;
    }
    stored.add(key);
    return setObjectUrl(key, src, row.blob);
};

const acquire = (): Promise<void> => {
    if (active < MAX_CONCURRENT_STORES) {
        active += 1;
        return Promise.resolve();
    }
    return new Promise((resolve) => waiting.push(() => { active += 1; resolve(); }));
};

const release = () => {
    active -= 1;
    waiting.shift()?.();
};

const evictIfNeeded = async () => {
    if (approxTotalBytes !== null && approxTotalBytes <= MAX_TOTAL_BYTES) {
        return;
    }
    approxTotalBytes = await db.run<number>(STORE, 'readwrite', async (store) => {
        const rows = await requestResult(store.getAll() as IDBRequest<StoredThumbnail[]>);
        let total = rows.reduce((sum, row) => sum + row.size, 0);
        if (total <= MAX_TOTAL_BYTES) {
            return total;
        }
        rows.sort((a, b) => a.lastAccess - b.lastAccess);
        for (const row of rows) {
            if (total <= EVICT_TO_BYTES) {
                break;
            }
            store.delete(row.key);
            stored.delete(row.key);
            total -= row.size;
        }
        return total;
    }, 0);
};

// Fire-and-forget: call after the tile painted from the network so the bytes
// are likely already in the HTTP cache and this costs no extra transfer.
export const storeThumbnail = (filename: string, srcUrl: string): void => {
    if (consecutiveFailures >= BREAKER_THRESHOLD || !isCacheableSource(srcUrl)) {
        return;
    }
    const connection = (navigator as Navigator & { connection?: { saveData?: boolean } }).connection;
    if (connection?.saveData) {
        return;
    }
    const key = keyFor(filename);
    if (stored.has(key) || inFlight.has(key)) {
        return;
    }
    inFlight.add(key);
    void (async () => {
        await acquire();
        try {
            const response = await fetch(srcUrl, { mode: 'cors', credentials: 'omit' });
            if (!response.ok) {
                throw new Error(`thumbnail fetch ${response.status}`);
            }
            const blob = await response.blob();
            if (blob.size === 0 || blob.size > MAX_ENTRY_BYTES || (blob.type && !blob.type.startsWith('image/'))) {
                return;
            }
            const now = Date.now();
            const row: StoredThumbnail = {
                key, src: sourceIdentity(srcUrl), blob, size: blob.size, storedAt: now, lastAccess: now,
            };
            await db.run<void>(STORE, 'readwrite', (store) => { store.put(row); }, undefined);
            stored.add(key);
            consecutiveFailures = 0;
            // null = total not measured yet this session; evictIfNeeded sums it.
            if (approxTotalBytes !== null) {
                approxTotalBytes += blob.size;
            }
            await evictIfNeeded();
        } catch {
            consecutiveFailures += 1;
        } finally {
            inFlight.delete(key);
            release();
        }
    })();
};

export const __resetThumbnailBlobCacheForTests = () => {
    objectUrls.forEach((entry) => URL.revokeObjectURL(entry.url));
    objectUrls.clear();
    inFlight.clear();
    stored.clear();
    waiting.length = 0;
    active = 0;
    consecutiveFailures = 0;
    approxTotalBytes = null;
};
