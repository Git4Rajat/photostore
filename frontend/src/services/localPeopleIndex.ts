import { schedulePendingEditsReconciliation } from './reconcilePendingEdits';
import { applyPendingEdits, pendingEditScope, pendingEditReadTokens } from './pendingEdits';
import { get } from './apiClient';
import { perf, perfNow } from './perf';
import { getActiveLibraryFromToken } from './passwordAuthClient';

/**
 * Client-side counterpart to the backend's people index (see
 * get_user_people_index in storage_utils.py): a whole-library
 * {personId, name, isNamed, faceCount, coverFaceId, coverFilename,
 * coverBbox, updatedAt} projection downloaded once per session, used to
 * render the People grid without a full account-wide face-table scan.
 * The live frontend now uses /api/persons/page, which reads this same server
 * index a page at a time; this browser-side loader is kept for views that want
 * a local full-list copy. Mirrors localAlbumsIndex.ts's shape exactly.
 *
 * Uses the plain get (API_BASE_URL) -- /api/persons/index (like the rest of
 * routes/people.py) moved back onto the 'backend' role 2026-10-08, the same
 * container app that serves /photos, /albums. See app.py's
 * blueprint-registration block.
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
    payloadVersion?: string;
    sourceVersion: string;
    updatedAt: string;
    rows: PersonIndexRow[];
}

const snapshotVersions = new WeakMap<object, string>();

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

// `generation` is per-tab, so it can't fence a write against a newer
// snapshot a *different* tab just persisted -- see localSortIndex.ts's
// idbPutStoredIfNewer for the full reasoning. `updatedAt` is the manifest's
// publish timestamp, not the payload's, and a dirty index rebuilding back to
// a clean state can republish the same timestamp/sourceVersion label over
// genuinely different content -- compare `payloadVersion` (embedded in the
// downloaded body itself) instead, since that's what actually identifies
// which content this is.
const idbPutStoredIfNewer = async (key: string, value: StoredPeopleIndex): Promise<void> => {
    const db = await openPeopleIndexDb();
    await new Promise<void>((resolve, reject) => {
        const tx = db.transaction(DB_STORE, 'readwrite');
        const store = tx.objectStore(DB_STORE);
        const getReq = store.get(key);
        getReq.onsuccess = () => {
            const current = getReq.result as StoredPeopleIndex | undefined;
            if (!current?.payloadVersion || !value.payloadVersion || current.payloadVersion <= value.payloadVersion) {
                store.put(value, key);
            }
        };
        getReq.onerror = () => reject(getReq.error || new Error('Failed to read people index before persisting.'));
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
    // Stable catalog URLs are overwritten; reuse is controlled by versioned IndexedDB.
    return fetch(url, { signal: controller.signal, cache: 'no-store' }).finally(() => clearTimeout(timer));
};

const mergeRows = (rows: PersonIndexRow[], version?: string, scope = pendingEditScope(), tokens?: ReadonlySet<string>): PersonIndexRow[] => rows.map(row => {
    const merged = applyPendingEdits('person', row.personId, row, version, scope, tokens);
    return { ...merged, isNamed: Boolean(merged.name.trim()) && !/^unnamed\s*\d*$/i.test(merged.name.trim()) };
});

const downloadPeopleIndexBlob = async (indexUrl: string, scope: string, tokens: ReadonlySet<string>, current: () => boolean): Promise<PersonIndexRow[]> => {
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
            const rows = normalizeRows(parsed.rows);
            if (typeof parsed.sourceVersion === 'string') snapshotVersions.set(rows, parsed.sourceVersion);
            if (current()) mergeRows(rows, parsed.sourceVersion, scope, tokens);
            return rows;
        }
    } catch {
        // fall through to manual gunzip
    }
    const text = await decompressGzip(buffer);
    const parsed = JSON.parse(text);
    const rows = normalizeRows(Array.isArray(parsed?.rows) ? parsed.rows : []);
    if (typeof parsed.sourceVersion === 'string') snapshotVersions.set(rows, parsed.sourceVersion);
    if (current()) mergeRows(rows, parsed.sourceVersion, scope, tokens);
    return rows;
};

const indexKey = (): string => getActiveLibraryFromToken() || '__default__';

let cachedKey: string | null = null;
let cachedIndex: PersonIndexRow[] | null = null;
let inFlight: Promise<PersonIndexRow[] | null> | null = null;
let generation = 0;

const fetchLocalPeopleIndex = async (key: string, scope: string, requestGeneration: number, tokens: ReadonlySet<string>): Promise<PersonIndexRow[] | null> => {
    const totalStarted = perfNow();
    const manifestStarted = perfNow();
    const response: PeopleIndexResponse = await get('/api/persons/index');
    perf.recordSpan('index.people.manifest', perfNow() - manifestStarted);
    if (!response?.available || !response.indexUrl) {
        return null;
    }
    const sourceVersion = response.sourceVersion || '';
    const idbStarted = perfNow();
    const stored = await idbGetStored(key).catch(() => null);
    const storedMatches = stored && sourceVersion && stored.sourceVersion === sourceVersion
        && (!stored.payloadVersion || stored.payloadVersion === sourceVersion);
    perf.recordSpan('index.people.idb_read', perfNow() - idbStarted, { cached: Boolean(storedMatches) });
    if (storedMatches) {
        perf.recordSpan('index.people.total', perfNow() - totalStarted, { cached: true, rows: stored.rows.length });
        if (stored.payloadVersion) snapshotVersions.set(stored.rows, stored.payloadVersion);
        return stored.rows;
    }
    const rows = await downloadPeopleIndexBlob(response.indexUrl, scope, tokens, () => generation === requestGeneration);
    // An invalidation during this download can let a newer request's write
    // land first, then have this now-stale download overwrite it when it
    // finishes last -- same race as localSortIndex.ts's fetchLocalSortIndex.
    // idbPutStoredIfNewer additionally guards against a different tab's
    // concurrent, independently-fenced write regressing this one.
    if (generation === requestGeneration) {
        await idbPutStoredIfNewer(key, { sourceVersion, payloadVersion: snapshotVersions.get(rows), updatedAt: response.updatedAt || '', rows }).catch(() => {
            // Best-effort persistence -- an in-memory-only session still works.
        });
    }
    return rows;
};

export const getLocalPeopleIndex = async (): Promise<PersonIndexRow[] | null> => {
    const key = indexKey();
    if (cachedIndex && cachedKey === key) {
        const snapshot = cachedIndex;
        const scope = pendingEditScope();
        mergeRows(snapshot, snapshotVersions.get(snapshot), scope, pendingEditReadTokens(scope, false));
        schedulePendingEditsReconciliation();
        return scope === pendingEditScope() ? mergeRows(snapshot, undefined, scope) : null;
    }
    if (inFlight && cachedKey === key) {
        return inFlight;
    }
    cachedIndex = null;
    cachedKey = key;
    const requestGeneration = ++generation;
    const scope = pendingEditScope();
    const readTokens = pendingEditReadTokens(scope);
    const request = fetchLocalPeopleIndex(key, scope, requestGeneration, readTokens)
        .then((result) => {
            if (scope !== pendingEditScope()) return null;
            if (result) mergeRows(result, snapshotVersions.get(result), scope, readTokens);
            schedulePendingEditsReconciliation();
            if (scope !== pendingEditScope()) return null;
            if (generation === requestGeneration) cachedIndex = result;
            return result ? mergeRows(result, undefined, scope) : result;
        })
        .catch(() => {
            if (generation === requestGeneration) cachedIndex = null;
            return null;
        })
        .finally(() => {
            if (inFlight === request) inFlight = null;
        });
    inFlight = request;
    return inFlight;
};

// Called after any person mutation (rename/merge/delete) so the next read
// picks up fresh data instead of serving a stale in-memory copy for the rest
// of the tab session -- the backend dirty-marks its own persisted copy
// independently (via _invalidate_people_scan_cache), but this session's
// in-memory cache needs its own explicit invalidation the same way
// localAlbumsIndex.ts does.
export const invalidateLocalPeopleIndex = (): void => {
    generation += 1;
    cachedIndex = null;
    cachedKey = null;
    inFlight = null;
};
