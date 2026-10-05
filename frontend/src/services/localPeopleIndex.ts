import { getExtras } from './apiClient';
import { perf, perfNow } from './perf';
import { getActiveLibraryFromToken } from './passwordAuthClient';

/**
 * Client-side counterpart to the backend's people index (see
 * get_user_people_index in storage_utils.py): a whole-library
 * {personId, name, isNamed, faceCount, coverFaceId, coverFilename,
 * coverBbox, updatedAt} projection downloaded once per session, used to
 * render the People grid without routes/people.py:list_persons's hardcoded
 * single page (the live frontend calls listPersons(undefined, 0, 200) --
 * anyone with 201+ clusters silently never sees the rest). Mirrors
 * localAlbumsIndex.ts's shape exactly.
 *
 * Uses getExtras, not the default get -- /api/persons/index (like the rest
 * of routes/people.py) is only registered on the 'extras' role, a separate
 * container app from 'backend' (which serves /photos, /albums). See
 * app.py's blueprint-registration block (APP_ROLE == 'extras').
 */
export interface PersonIndexRow {
    personId: string;
    name: string;
    isNamed: boolean;
    faceCount: number;
    coverFaceId: string;
    coverFilename: string;
    updatedAt: string;
}

interface PeopleIndexResponse {
    available: boolean;
    indexUrl?: string;
    sourceVersion?: string;
    updatedAt?: string;
}

interface StoredPeopleIndex {
    sourceVersion: string;
    updatedAt: string;
    rows: PersonIndexRow[];
}

const DB_NAME = 'photostore-people-index';
const DB_STORE = 'indexes';

const openPeopleIndexDb = (): Promise<IDBDatabase> => new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, 1);
    request.onupgradeneeded = () => {
        const db = request.result;
        if (!db.objectStoreNames.contains(DB_STORE)) {
            db.createObjectStore(DB_STORE);
        }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error('Failed to open people-index database.'));
});

const idbGetStored = async (key: string): Promise<StoredPeopleIndex | null> => {
    const db = await openPeopleIndexDb();
    const result = await new Promise<StoredPeopleIndex | null>((resolve, reject) => {
        const tx = db.transaction(DB_STORE, 'readonly');
        const req = tx.objectStore(DB_STORE).get(key);
        req.onsuccess = () => resolve((req.result as StoredPeopleIndex | undefined) || null);
        req.onerror = () => reject(req.error || new Error('Failed to load people index.'));
    });
    db.close();
    return result;
};

const idbPutStored = async (key: string, value: StoredPeopleIndex): Promise<void> => {
    const db = await openPeopleIndexDb();
    await new Promise<void>((resolve, reject) => {
        const tx = db.transaction(DB_STORE, 'readwrite');
        tx.objectStore(DB_STORE).put(value, key);
        tx.oncomplete = () => resolve();
        tx.onerror = () => reject(tx.error || new Error('Failed to persist people index.'));
    });
    db.close();
};

const decompressGzip = async (buffer: ArrayBuffer): Promise<string> => {
    const stream = new Response(buffer).body!.pipeThrough(new DecompressionStream('gzip'));
    return new Response(stream).text();
};

const normalizeRows = (raw: Record<string, unknown>[]): PersonIndexRow[] => raw
    .map((row): PersonIndexRow => ({
        personId: String(row.personId || ''),
        name: typeof row.name === 'string' ? row.name : '',
        isNamed: Boolean(row.isNamed),
        faceCount: Number(row.faceCount) || 0,
        coverFaceId: typeof row.coverFaceId === 'string' ? row.coverFaceId : '',
        coverFilename: typeof row.coverFilename === 'string' ? row.coverFilename : '',
        updatedAt: typeof row.updatedAt === 'string' ? row.updatedAt : '',
    }))
    .filter((row) => row.personId);

// A plain fetch() has no default timeout -- a stalled connection hangs this
// promise forever instead of rejecting, which (via getLocalPeopleIndex's
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

const downloadPeopleIndexBlob = async (indexUrl: string): Promise<PersonIndexRow[]> => {
    const downloadStarted = perfNow();
    const response = await fetchWithTimeout(indexUrl, BLOB_FETCH_TIMEOUT_MS);
    if (!response.ok) {
        throw new Error(`Failed to download people index (${response.status})`);
    }
    const buffer = await response.arrayBuffer();
    perf.recordSpan('index.people.blob_download', perfNow() - downloadStarted, { bytes: buffer.byteLength });
    const parseStarted = perfNow();
    try {
        const text = new TextDecoder().decode(buffer);
        const parsed = JSON.parse(text);
        if (parsed && Array.isArray(parsed.rows)) {
            perf.recordSpan('index.people.parse', perfNow() - parseStarted, { rows: parsed.rows.length });
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
let cachedIndex: PersonIndexRow[] | null = null;
let inFlight: Promise<PersonIndexRow[] | null> | null = null;

const fetchLocalPeopleIndex = async (key: string): Promise<PersonIndexRow[] | null> => {
    const totalStarted = perfNow();
    const manifestStarted = perfNow();
    const response: PeopleIndexResponse = await getExtras('/api/persons/index');
    perf.recordSpan('index.people.manifest', perfNow() - manifestStarted);
    if (!response?.available || !response.indexUrl) {
        return null;
    }
    const sourceVersion = response.sourceVersion || '';
    const idbStarted = perfNow();
    const stored = await idbGetStored(key).catch(() => null);
    perf.recordSpan('index.people.idb_read', perfNow() - idbStarted, { cached: Boolean(stored && sourceVersion && stored.sourceVersion === sourceVersion) });
    if (stored && sourceVersion && stored.sourceVersion === sourceVersion) {
        perf.recordSpan('index.people.total', perfNow() - totalStarted, { cached: true, rows: stored.rows.length });
        return stored.rows;
    }
    const rows = await downloadPeopleIndexBlob(response.indexUrl);
    await idbPutStored(key, { sourceVersion, updatedAt: response.updatedAt || '', rows }).catch(() => {
        // Best-effort persistence -- an in-memory-only session still works.
    });
    return rows;
};

export const getLocalPeopleIndex = async (): Promise<PersonIndexRow[] | null> => {
    const key = indexKey();
    if (cachedIndex && cachedKey === key) {
        return cachedIndex;
    }
    if (inFlight && cachedKey === key) {
        return inFlight;
    }
    cachedKey = key;
    inFlight = fetchLocalPeopleIndex(key)
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

// Called after any person mutation (rename/merge/delete) so the next read
// picks up fresh data instead of serving a stale in-memory copy for the rest
// of the tab session -- the backend dirty-marks its own persisted copy
// independently (via _invalidate_people_scan_cache), but this session's
// in-memory cache needs its own explicit invalidation the same way
// localAlbumsIndex.ts does.
export const invalidateLocalPeopleIndex = (): void => {
    cachedIndex = null;
    cachedKey = null;
};
