import { schedulePendingEditsReconciliation } from './reconcilePendingEdits';
import { get } from './apiClient';
import { perf, perfNow } from './perf';
import { getActiveLibraryFromToken } from './passwordAuthClient';
import { applyPendingEdits, beginPendingEdit, confirmPendingEdit, pendingEditScope, pendingEditReadTokens, observePhotoGeneration, pendingEntityIds } from './pendingEdits';
import type { Photo } from '../types/uiTypes';

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
    /** Absent only in snapshots built before the v4 schema migration. */
    gallery?: {
        liked: boolean;
        rotation: number;
        thumbnailRotation: number;
        personIds: string[];
        tags: string[];
        processing: Photo['processing'];
    };
}

/** True once the server said this library is too big for a client-side sort index (it pages instead). */
let serverPaged = false;
export const isServerPagedLibrary = (): boolean => serverPaged;

interface SortIndexResponse {
    reason?: string;
    rowCount?: number;
    available: boolean;
    indexUrl?: string;
    sourceVersion?: string;
    updatedAt?: string;
}

interface StoredSortIndex {
    payloadVersion?: string;
    sourceVersion: string;
    updatedAt: string;
    rows: SortIndexRow[];
    removedRows?: Record<string, string>;
}

const snapshotVersions = new WeakMap<object, string>();

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

// `generation` below is per-tab -- another tab downloading the same library
// concurrently has its own counter that's always "valid" from its own point
// of view, so it can't fence a write against a newer snapshot a *different*
// tab just persisted. `updatedAt` is the manifest's publish timestamp, not
// the payload's: a dirty index rebuilding back to a clean state can republish
// the same manifest timestamp/sourceVersion label over genuinely different
// content, so two different payloads can carry an identical `updatedAt` --
// comparing it can't tell stale and rebuilt bodies apart. `payloadVersion`
// (embedded in the downloaded body itself, the same version the in-memory
// `snapshotVersions`/pending-edits overlay compare against) is the one value
// that actually identifies *which* content this is, so compare that instead,
// inside a single transaction to avoid racing another tab's read-then-write.
const idbPutStoredIfNewer = async (key: string, value: StoredSortIndex): Promise<void> => {
    const db = await openSortIndexDb();
    await new Promise<void>((resolve, reject) => {
        const tx = db.transaction(DB_STORE, 'readwrite');
        const store = tx.objectStore(DB_STORE);
        const getReq = store.get(key);
        getReq.onsuccess = () => {
            const current = getReq.result as StoredSortIndex | undefined;
            if (!current?.payloadVersion || !value.payloadVersion || current.payloadVersion <= value.payloadVersion) {
                store.put(value, key);
            }
        };
        getReq.onerror = () => reject(getReq.error || new Error('Failed to read sort index before persisting.'));
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
        ...(row.gallery && typeof row.gallery === 'object'
            ? { gallery: row.gallery as SortIndexRow['gallery'] } : {}),
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

const fetchWithTimeout = async (url: string, timeoutMs: number): Promise<ArrayBuffer> => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
        const response = await fetch(url, { signal: controller.signal });
        if (!response.ok) throw new Error(`Failed to download sort index (${response.status})`);
        return await response.arrayBuffer();
    } finally {
        clearTimeout(timer);
    }
};

const downloadSortIndexBlob = async (indexUrl: string, scope: string, current: () => boolean, readTokens: ReadonlySet<string>): Promise<SortIndexRow[]> => {
    const downloadStarted = perfNow();
    const buffer = await fetchWithTimeout(indexUrl, BLOB_FETCH_TIMEOUT_MS);
    perf.recordSpan('index.sort.blob_download', perfNow() - downloadStarted, { bytes: buffer.byteLength });
    const parseStarted = perfNow();
    // The blob is stored with Content-Encoding: gzip -- most browsers
    // transparently decompress it before we ever see the bytes, so try
    // parsing directly first and only fall back to manual decompression if
    // the fetch handed back the raw compressed bytes instead (mirrors
    // the former browser search index's downloadIndexBlob).
    try {
        const text = new TextDecoder().decode(buffer);
        const parsed = JSON.parse(text);
        if (parsed && Array.isArray(parsed.rows)) {
            perf.recordSpan('index.sort.parse', perfNow() - parseStarted, { rows: parsed.rows.length });
            const rows = normalizeRows(parsed.rows);
            if (typeof parsed.sourceVersion === 'string') snapshotVersions.set(rows, parsed.sourceVersion);
            if (current()) {
                rows.forEach(row => observePhotoGeneration(row.filename, row.thumb, scope, parsed.sourceVersion));
                applyRowPatches(rows, parsed.sourceVersion, scope, readTokens);
            }
            return rows;
        }
    } catch {
        // fall through to manual gunzip
    }
    const text = await decompressGzip(buffer);
    const parsed = JSON.parse(text);
    const rows = normalizeRows(Array.isArray(parsed?.rows) ? parsed.rows : []);
    if (typeof parsed.sourceVersion === 'string') snapshotVersions.set(rows, parsed.sourceVersion);
    if (current()) {
        rows.forEach(row => observePhotoGeneration(row.filename, row.thumb, scope, parsed.sourceVersion));
        applyRowPatches(rows, parsed.sourceVersion, scope, readTokens);
    }
    return rows;
};

// Keyed by active library, same reasoning as the former browser search index's indexKey/
// photoCache.ts's photoCacheKey -- switching libraries can't serve one
// library's rows while browsing another.
const indexKey = (): string => getActiveLibraryFromToken() || '__default__';

let cachedKey: string | null = null;
let cachedIndex: SortIndexRow[] | null = null;
let inFlight: Promise<SortIndexRow[] | null> | null = null;
let generation = 0;
type RowPatch = { rating?: number; likes?: number; liked?: boolean; rotation?: number };
const applyRowPatches = (rows: SortIndexRow[], version?: string, scope = pendingEditScope(), readTokens?: ReadonlySet<string>): SortIndexRow[] => {
    const edited = new Set(pendingEntityIds('photo', scope));
    if (!edited.size) return rows;
    let changed = false;
    const merged = rows.map(row => {
        if (!edited.has(row.filename)) return row;
        const original = { rating: row.rating, likes: row.likes, liked: row.gallery?.liked, rotation: row.gallery?.rotation };
        const tile = applyPendingEdits('photo', row.filename, original, version, scope, readTokens);
        if (tile === original) return row;
        changed = true;
        return { ...row, rating: tile.rating, likes: tile.likes,
            ...(row.gallery ? { gallery: { ...row.gallery, liked: tile.liked ?? row.gallery.liked,
                rotation: tile.rotation ?? row.gallery.rotation } } : {}),
        };
    });
    return changed ? merged : rows;
};
// Keep authoritative local deletes hidden while the server snapshot catches up.
// The blob name identifies a generation, so a re-upload can reuse its filename.
const removedRows = new Map<string, Map<string, string>>();
const restoredRows = new Map<string, Set<string>>();
const removalsFor = (key: string): Map<string, string> => {
    let removed = removedRows.get(key);
    if (!removed) { removed = new Map(); removedRows.set(key, removed); }
    return removed;
};
const readStoredRemovals = (key: string, stored: StoredSortIndex | null): void => {
    const removed = removalsFor(key);
    for (const [name, blob] of Object.entries(stored?.removedRows ?? {})) {
        if (!removed.has(name) && !restoredRows.get(key)?.has(name)) removed.set(name, blob);
    }
};
const filterRemovedRows = (key: string, rows: SortIndexRow[], freshDownload = false): SortIndexRow[] => {
    const removed = removalsFor(key);
    if (freshDownload && removed.size) {
        const byName = new Map(rows.map((row) => [row.filename, row]));
        removed.forEach((blob, name) => {
            const row = byName.get(name);
            if (!row || (blob && row.thumb && row.thumb !== blob)) removed.delete(name);
        });
    }
    return removed.size ? rows.filter((row) => !removed.has(row.filename)) : rows;
};

const fetchLocalSortIndex = async (key: string, requestGeneration: number, editScope: string, readTokens: ReadonlySet<string>): Promise<SortIndexRow[] | null> => {
    const totalStarted = perfNow();
    const manifestStarted = perfNow();
    const response: SortIndexResponse = await get('/api/photos/sort-index');
    perf.recordSpan('index.sort.manifest', perfNow() - manifestStarted);
    if (response?.reason === 'library_too_large') {
        if (generation === requestGeneration) serverPaged = true;
        return null;
    }
    if (!response?.available || !response.indexUrl) {
        return null;
    }
    const sourceVersion = response.sourceVersion || '';
    const idbStarted = perfNow();
    const stored = await idbGetStored(key).catch(() => null);
    readStoredRemovals(key, stored);
    perf.recordSpan('index.sort.idb_read', perfNow() - idbStarted, { cached: Boolean(stored && sourceVersion && stored.sourceVersion === sourceVersion) });
    if (stored && sourceVersion && stored.sourceVersion === sourceVersion) {
        // Unchanged since the last download for this library -- skip the
        // blob re-fetch entirely, this is the whole point of persisting it.
        perf.recordSpan('index.sort.total', perfNow() - totalStarted, { cached: true, rows: stored.rows.length });
        if (stored.payloadVersion) snapshotVersions.set(stored.rows, stored.payloadVersion);
        if (generation === requestGeneration) stored.rows.forEach(row =>
            observePhotoGeneration(row.filename, row.thumb, editScope, stored.payloadVersion));
        const visible = filterRemovedRows(key, stored.rows);
        if (stored.payloadVersion) snapshotVersions.set(visible, stored.payloadVersion);
        return visible;
    }
    const rows = await downloadSortIndexBlob(response.indexUrl, editScope, () => generation === requestGeneration, readTokens);
    filterRemovedRows(key, rows, generation === requestGeneration);
    // An invalidation (bumping `generation`) during this download can let a
    // newer request's write land in IndexedDB first, then have this now-stale
    // download finish writing last and overwrite it with older data. The
    // in-memory cache is already fenced by `generation` (see getLocalSortIndex);
    // the persistent write needs the same fence, checked as late as possible.
    // `generation` is only per-tab though -- idbPutStoredIfNewer additionally
    // guards against a *different* tab's concurrent, independently-fenced
    // write regressing this one (or vice versa).
    if (generation === requestGeneration) {
        const writeStarted = perfNow();
        await idbPutStoredIfNewer(key, { sourceVersion, payloadVersion: snapshotVersions.get(rows), updatedAt: response.updatedAt || '', rows,
            removedRows: Object.fromEntries(removalsFor(key)) }).catch(() => {
            // Best-effort persistence -- an in-memory-only session still works,
            // it just re-downloads next reload instead of skipping the fetch.
        });
        perf.recordSpan('index.sort.idb_write', perfNow() - writeStarted, { rows: rows.length });
    }
    perf.recordSpan('index.sort.total', perfNow() - totalStarted, { cached: false, rows: rows.length });
    const visible = filterRemovedRows(key, rows);
    const version = snapshotVersions.get(rows);
    if (version) snapshotVersions.set(visible, version);
    return visible;
};

export const getLocalSortIndex = async (): Promise<SortIndexRow[] | null> => {
    const key = indexKey();
    if (cachedIndex && cachedKey === key) {
        const snapshot = cachedIndex;
        const scope = pendingEditScope();
        applyRowPatches(snapshot, snapshotVersions.get(snapshot), scope, pendingEditReadTokens(scope, false));
        schedulePendingEditsReconciliation();
        return scope === pendingEditScope() ? applyRowPatches(snapshot, undefined, scope) : null;
    }
    if (inFlight && cachedKey === key) {
        return inFlight;
    }
    cachedIndex = null;
    cachedKey = key;
    serverPaged = false;
    const requestGeneration = ++generation;
    const editScope = pendingEditScope();
    const readTokens = pendingEditReadTokens(editScope);
    const request = fetchLocalSortIndex(key, requestGeneration, editScope, readTokens)
        .then((result) => {
            if (editScope !== pendingEditScope()) return null;
            if (result) applyRowPatches(result, snapshotVersions.get(result), editScope, readTokens);
            schedulePendingEditsReconciliation();
            if (editScope !== pendingEditScope()) return null;
            if (generation === requestGeneration) cachedIndex = result;
            return result ? applyRowPatches(result, undefined, editScope) : result;
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

// Called after an upload batch completes, same trigger as
// invalidateLocalSearchIndex, so the next gallery load picks up newly-added
// photos instead of serving a stale in-memory copy for the rest of the tab
// session.
// Filename -> thumbnail blob name for the loaded sort index, so any grid can build the same
// container-token URL the gallery uses (one browser-cache entry per thumbnail, whichever page
// shows it). Empty until the sort index is loaded.
let thumbMap: Map<string, string> | null = null;
let thumbMapSource: SortIndexRow[] | null = null;
export const getCachedSortThumb = (filename: string): string => {
    if (!cachedIndex) return '';
    if (thumbMapSource !== cachedIndex || !thumbMap) {
        thumbMap = new Map();
        for (const row of cachedIndex) {
            if (row.thumb) thumbMap.set(row.filename, row.thumb);
        }
        thumbMapSource = cachedIndex;
    }
    return thumbMap.get(filename) || '';
};

export const invalidateLocalSortIndex = (): void => {
    generation += 1;
    cachedIndex = null;
    cachedKey = null;
    inFlight = null;
};

// Optimistic local remove after a delete/purge. The backend dirty-marks and
// rebuilds its own sort index, but the already-open tab should not keep paging
// through filenames it just removed.
export const removeLocalSortIndexRows = (filenames: string[]): void => {
    if (!filenames.length) return;
    const doomed = new Set(filenames);
    const key = indexKey();
    const removed = removalsFor(key);
    const thumbnails = new Map(cachedKey === key ? cachedIndex?.map((row) => [row.filename, row.thumb ?? '']) : []);
    filenames.forEach((name) => {
        restoredRows.get(key)?.delete(name);
        removed.set(name, thumbnails.get(name) || removed.get(name) || '');
    });
    if (cachedKey === key && cachedIndex) cachedIndex = filterRemovedRows(key, cachedIndex);
    thumbMap = null;
    thumbMapSource = null;
    if (key) {
        idbGetStored(key)
            .then((stored) => {
                if (!stored) return;
                readStoredRemovals(key, stored);
                for (const row of stored.rows) {
                    if (doomed.has(row.filename) && !removed.get(row.filename)) removed.set(row.filename, row.thumb ?? '');
                }
                return idbPutStored(key, { ...stored, removedRows: Object.fromEntries(removed) });
            })
            .catch(() => {
                // Best-effort -- the in-memory removal is what fixes the
                // current tab; IndexedDB just prevents stale reloads.
            });
    }
};

export const clearLocalSortIndexRemovals = (filenames: string[]): void => {
    const key = indexKey();
    const removed = removalsFor(key);
    const restored = restoredRows.get(key) ?? new Set<string>();
    restoredRows.set(key, restored);
    filenames.forEach((name) => { removed.delete(name); restored.add(name); });
    void idbGetStored(key).then((stored) => {
        if (!stored) return;
        const remaining = { ...stored.removedRows };
        filenames.forEach((name) => delete remaining[name]);
        return idbPutStored(key, { ...stored, removedRows: remaining });
    }).catch(() => {});
};

// Optimistic local patch after a rating/like edit -- store.tsx already
// patches its own `photos` page-state array immediately on edit; this keeps
// the whole-library sort-index in step too, so a re-sort/re-scroll within
// the same session reflects the change without waiting on the backend's
// independent sort-index manifest to catch up (see touch_user_sort_index_dirty
// in storage_utils.py, which is near-instant but still a round trip).
export const patchLocalSortIndexRow = (filename: string, patch: RowPatch): void => {
    confirmPendingEdit(beginPendingEdit('photo', filename, patch));
};
