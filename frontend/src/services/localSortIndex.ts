import { get } from './apiClient';
import { getActiveLibraryFromToken } from './passwordAuthClient';

/**
 * Client-side counterpart to the backend's sort index (see
 * get_user_sort_index in storage_utils.py): a whole-library {filename,
 * captureDate, rating, likes, uploadDate} projection, small enough to
 * download once per session and sort/paginate locally instead of every
 * gallery page request materializing and sorting the whole library
 * server-side. Combines the former browser search index's fetch/gunzip/cache shape with
 * photoCache.ts's raw-IndexedDB idiom, persisted (not just in-memory) so a
 * reload within the manifest's still-fresh window skips the blob re-download
 * entirely.
 */
export interface SortIndexRow {
    filename: string;
    captureDate: string | null;
    rating: number;
    likes: number;
    uploadDate: string | null;
    /** Physical thumbnail blob name once the thumbnail exists; with the media
     * token this yields a direct URL with no backend call. */
    thumb?: string;
}

interface SortIndexResponse {
    available: boolean;
    indexUrl?: string;
    sourceVersion?: string;
    updatedAt?: string;
}

interface StoredSortIndex {
    sourceVersion: string;
    updatedAt: string;
    rows: SortIndexRow[];
}

const DB_NAME = 'photostore-sort-index';
const DB_STORE = 'indexes';

const openSortIndexDb = (): Promise<IDBDatabase> => new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, 1);
    request.onupgradeneeded = () => {
        const db = request.result;
        if (!db.objectStoreNames.contains(DB_STORE)) {
            db.createObjectStore(DB_STORE);
        }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error('Failed to open sort-index database.'));
});

const idbGetStored = async (key: string): Promise<StoredSortIndex | null> => {
    const db = await openSortIndexDb();
    const result = await new Promise<StoredSortIndex | null>((resolve, reject) => {
        const tx = db.transaction(DB_STORE, 'readonly');
        const req = tx.objectStore(DB_STORE).get(key);
        req.onsuccess = () => resolve((req.result as StoredSortIndex | undefined) || null);
        req.onerror = () => reject(req.error || new Error('Failed to load sort index.'));
    });
    db.close();
    return result;
};

const idbPutStored = async (key: string, value: StoredSortIndex): Promise<void> => {
    const db = await openSortIndexDb();
    await new Promise<void>((resolve, reject) => {
        const tx = db.transaction(DB_STORE, 'readwrite');
        tx.objectStore(DB_STORE).put(value, key);
        tx.oncomplete = () => resolve();
        tx.onerror = () => reject(tx.error || new Error('Failed to persist sort index.'));
    });
    db.close();
};

const decompressGzip = async (buffer: ArrayBuffer): Promise<string> => {
    // Same approach as the former browser search index -- DecompressionStream is the
    // standard, dependency-free way to gunzip in a browser.
    const stream = new Response(buffer).body!.pipeThrough(new DecompressionStream('gzip'));
    return new Response(stream).text();
};

const normalizeRows = (raw: Record<string, unknown>[]): SortIndexRow[] => raw
    .map((row): SortIndexRow => ({
        filename: String(row.RowKey || ''),
        captureDate: typeof row.captureDate === 'string' ? row.captureDate : null,
        rating: Number(row.rating) || 0,
        likes: Number(row.likes) || 0,
        uploadDate: typeof row.uploadDate === 'string' ? row.uploadDate : null,
        ...(typeof row.thumb === 'string' && row.thumb ? { thumb: row.thumb } : {}),
    }))
    .filter((row) => row.filename);

// A plain fetch() has no default timeout -- a stalled connection hangs this
// promise forever instead of rejecting, which (via getLocalSortIndex's
// module-scoped inFlight dedup) would wedge every caller behind the same
// permanently-pending promise for the rest of the tab session. See
// the former browser search index's BLOB_FETCH_TIMEOUT_MS for the full writeup --
// confirmed live 2026-09-29 for that index's blob fetch; same latent gap
// here since the code shape is identical.
const BLOB_FETCH_TIMEOUT_MS = 120000;

const fetchWithTimeout = (url: string, timeoutMs: number): Promise<Response> => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    return fetch(url, { signal: controller.signal }).finally(() => clearTimeout(timer));
};

const downloadSortIndexBlob = async (indexUrl: string): Promise<SortIndexRow[]> => {
    const response = await fetchWithTimeout(indexUrl, BLOB_FETCH_TIMEOUT_MS);
    if (!response.ok) {
        throw new Error(`Failed to download sort index (${response.status})`);
    }
    const buffer = await response.arrayBuffer();
    // The blob is stored with Content-Encoding: gzip -- most browsers
    // transparently decompress it before we ever see the bytes, so try
    // parsing directly first and only fall back to manual decompression if
    // the fetch handed back the raw compressed bytes instead (mirrors
    // the former browser search index's downloadIndexBlob).
    try {
        const text = new TextDecoder().decode(buffer);
        const parsed = JSON.parse(text);
        if (parsed && Array.isArray(parsed.rows)) {
            return normalizeRows(parsed.rows);
        }
    } catch {
        // fall through to manual gunzip
    }
    const text = await decompressGzip(buffer);
    const parsed = JSON.parse(text);
    return normalizeRows(Array.isArray(parsed?.rows) ? parsed.rows : []);
};

// Keyed by active library, same reasoning as the former browser search index's indexKey/
// photoCache.ts's photoCacheKey -- switching libraries can't serve one
// library's rows while browsing another.
const indexKey = (): string => getActiveLibraryFromToken() || '__default__';

let cachedKey: string | null = null;
let cachedIndex: SortIndexRow[] | null = null;
let inFlight: Promise<SortIndexRow[] | null> | null = null;

const fetchLocalSortIndex = async (key: string): Promise<SortIndexRow[] | null> => {
    const response: SortIndexResponse = await get('/api/photos/sort-index');
    if (!response?.available || !response.indexUrl) {
        return null;
    }
    const sourceVersion = response.sourceVersion || '';
    const stored = await idbGetStored(key).catch(() => null);
    if (stored && sourceVersion && stored.sourceVersion === sourceVersion) {
        // Unchanged since the last download for this library -- skip the
        // blob re-fetch entirely, this is the whole point of persisting it.
        return stored.rows;
    }
    const rows = await downloadSortIndexBlob(response.indexUrl);
    await idbPutStored(key, { sourceVersion, updatedAt: response.updatedAt || '', rows }).catch(() => {
        // Best-effort persistence -- an in-memory-only session still works,
        // it just re-downloads next reload instead of skipping the fetch.
    });
    return rows;
};

export const getLocalSortIndex = async (): Promise<SortIndexRow[] | null> => {
    const key = indexKey();
    if (cachedIndex && cachedKey === key) {
        return cachedIndex;
    }
    if (inFlight && cachedKey === key) {
        return inFlight;
    }
    cachedKey = key;
    inFlight = fetchLocalSortIndex(key)
        .then((result) => {
            cachedIndex = result;
            return result;
        })
        .catch(() => {
            cachedIndex = null;
            return null;
        })
        .finally(() => {
            inFlight = null;
        });
    return inFlight;
};

// Called after an upload batch completes, same trigger as
// invalidateLocalSearchIndex, so the next gallery load picks up newly-added
// photos instead of serving a stale in-memory copy for the rest of the tab
// session.
export const invalidateLocalSortIndex = (): void => {
    cachedIndex = null;
    cachedKey = null;
};

// Optimistic local patch after a rating/like edit -- store.tsx already
// patches its own `photos` page-state array immediately on edit; this keeps
// the whole-library sort-index in step too, so a re-sort/re-scroll within
// the same session reflects the change without waiting on the backend's
// independent sort-index manifest to catch up (see touch_user_sort_index_dirty
// in storage_utils.py, which is near-instant but still a round trip).
export const patchLocalSortIndexRow = (filename: string, patch: { rating?: number; likes?: number }): void => {
    if (!cachedIndex) return;
    const key = cachedKey;
    cachedIndex = cachedIndex.map((row) => (row.filename === filename
        ? { ...row, rating: patch.rating ?? row.rating, likes: patch.likes ?? row.likes }
        : row));
    if (key) {
        idbGetStored(key)
            .then((stored) => {
                if (!stored) return;
                const rows = stored.rows.map((row) => (row.filename === filename
                    ? { ...row, rating: patch.rating ?? row.rating, likes: patch.likes ?? row.likes }
                    : row));
                return idbPutStored(key, { ...stored, rows });
            })
            .catch(() => {
                // Best-effort -- the in-memory patch above is what matters
                // for this session; IndexedDB just avoids one stale reload.
            });
    }
};
