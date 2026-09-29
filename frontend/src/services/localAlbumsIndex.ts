import { get } from './apiClient';
import { getActiveLibraryFromToken } from './passwordAuthClient';

/**
 * Client-side counterpart to the backend's albums index (see
 * get_user_albums_index in storage_utils.py): a whole-library
 * {albumId, name, photoCount, coverFilename, updatedAt, filenames} projection
 * downloaded once per session, cached in IndexedDB, and used to render the
 * Albums list and open an album without either of the two things this
 * replaces: list_albums's per-album cover scan (rides the whole-library
 * rating/likes/date sort), or get_album's sequential per-photo point-read
 * loop. Mirrors localSortIndex.ts's shape exactly.
 */
export interface AlbumIndexRow {
    albumId: string;
    name: string;
    photoCount: number;
    coverFilename: string;
    updatedAt: string;
    filenames: string[];
    isPublic: boolean;
    publicUrl: string;
    publicExpiresAt: string;
    hasAccessCode: boolean;
    isExpired: boolean;
}

interface AlbumsIndexResponse {
    available: boolean;
    indexUrl?: string;
    sourceVersion?: string;
    updatedAt?: string;
}

interface StoredAlbumsIndex {
    sourceVersion: string;
    updatedAt: string;
    rows: AlbumIndexRow[];
}

const DB_NAME = 'photostore-albums-index';
const DB_STORE = 'indexes';

const openAlbumsIndexDb = (): Promise<IDBDatabase> => new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, 1);
    request.onupgradeneeded = () => {
        const db = request.result;
        if (!db.objectStoreNames.contains(DB_STORE)) {
            db.createObjectStore(DB_STORE);
        }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error('Failed to open albums-index database.'));
});

const idbGetStored = async (key: string): Promise<StoredAlbumsIndex | null> => {
    const db = await openAlbumsIndexDb();
    const result = await new Promise<StoredAlbumsIndex | null>((resolve, reject) => {
        const tx = db.transaction(DB_STORE, 'readonly');
        const req = tx.objectStore(DB_STORE).get(key);
        req.onsuccess = () => resolve((req.result as StoredAlbumsIndex | undefined) || null);
        req.onerror = () => reject(req.error || new Error('Failed to load albums index.'));
    });
    db.close();
    return result;
};

const idbPutStored = async (key: string, value: StoredAlbumsIndex): Promise<void> => {
    const db = await openAlbumsIndexDb();
    await new Promise<void>((resolve, reject) => {
        const tx = db.transaction(DB_STORE, 'readwrite');
        tx.objectStore(DB_STORE).put(value, key);
        tx.oncomplete = () => resolve();
        tx.onerror = () => reject(tx.error || new Error('Failed to persist albums index.'));
    });
    db.close();
};

const decompressGzip = async (buffer: ArrayBuffer): Promise<string> => {
    const stream = new Response(buffer).body!.pipeThrough(new DecompressionStream('gzip'));
    return new Response(stream).text();
};

const normalizeRows = (raw: Record<string, unknown>[]): AlbumIndexRow[] => raw
    .map((row): AlbumIndexRow => ({
        albumId: String(row.albumId || ''),
        name: typeof row.name === 'string' ? row.name : '',
        photoCount: Number(row.photoCount) || 0,
        coverFilename: typeof row.coverFilename === 'string' ? row.coverFilename : '',
        updatedAt: typeof row.updatedAt === 'string' ? row.updatedAt : '',
        filenames: Array.isArray(row.filenames) ? row.filenames.filter((f): f is string => typeof f === 'string') : [],
        isPublic: Boolean(row.isPublic),
        publicUrl: typeof row.publicUrl === 'string' ? row.publicUrl : '',
        publicExpiresAt: typeof row.publicExpiresAt === 'string' ? row.publicExpiresAt : '',
        hasAccessCode: Boolean(row.hasAccessCode),
        isExpired: Boolean(row.isExpired),
    }))
    .filter((row) => row.albumId);

// A plain fetch() has no default timeout -- a stalled connection hangs this
// promise forever instead of rejecting, which (via getLocalAlbumsIndex's
// module-scoped inFlight dedup) would wedge every caller behind the same
// permanently-pending promise for the rest of the tab session. See
// localSearchIndex.ts's BLOB_FETCH_TIMEOUT_MS for the full writeup --
// confirmed live 2026-09-29 for that index's blob fetch; same latent gap
// here since the code shape is identical.
const BLOB_FETCH_TIMEOUT_MS = 120000;

const fetchWithTimeout = (url: string, timeoutMs: number): Promise<Response> => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    return fetch(url, { signal: controller.signal }).finally(() => clearTimeout(timer));
};

const downloadAlbumsIndexBlob = async (indexUrl: string): Promise<AlbumIndexRow[]> => {
    const response = await fetchWithTimeout(indexUrl, BLOB_FETCH_TIMEOUT_MS);
    if (!response.ok) {
        throw new Error(`Failed to download albums index (${response.status})`);
    }
    const buffer = await response.arrayBuffer();
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

const indexKey = (): string => getActiveLibraryFromToken() || '__default__';

let cachedKey: string | null = null;
let cachedIndex: AlbumIndexRow[] | null = null;
let inFlight: Promise<AlbumIndexRow[] | null> | null = null;

const fetchLocalAlbumsIndex = async (key: string): Promise<AlbumIndexRow[] | null> => {
    const response: AlbumsIndexResponse = await get('/api/albums/index');
    if (!response?.available || !response.indexUrl) {
        return null;
    }
    const sourceVersion = response.sourceVersion || '';
    const stored = await idbGetStored(key).catch(() => null);
    if (stored && sourceVersion && stored.sourceVersion === sourceVersion) {
        return stored.rows;
    }
    const rows = await downloadAlbumsIndexBlob(response.indexUrl);
    await idbPutStored(key, { sourceVersion, updatedAt: response.updatedAt || '', rows }).catch(() => {
        // Best-effort persistence -- an in-memory-only session still works.
    });
    return rows;
};

export const getLocalAlbumsIndex = async (): Promise<AlbumIndexRow[] | null> => {
    const key = indexKey();
    if (cachedIndex && cachedKey === key) {
        return cachedIndex;
    }
    if (inFlight && cachedKey === key) {
        return inFlight;
    }
    cachedKey = key;
    inFlight = fetchLocalAlbumsIndex(key)
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

// Called after any album mutation (create/rename/delete/add-photos/
// remove-photos/share/revoke/restore/purge) or a rating/like change (which
// can change an album's auto-picked cover) so the next read picks up fresh
// data instead of serving a stale in-memory copy for the rest of the tab
// session -- the backend dirty-marks its own persisted copy independently
// (see touch_user_albums_index_state), but this session's in-memory cache
// needs its own explicit invalidation the same way localSortIndex.ts does.
export const invalidateLocalAlbumsIndex = (): void => {
    cachedIndex = null;
    cachedKey = null;
};
