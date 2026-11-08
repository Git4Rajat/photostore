import { getActiveLibraryFromToken } from './passwordAuthClient';
import type { Photo } from '../mockups/prototype/types';

/**
 * A device-local copy of the top of the gallery (photo list + thumbnail bytes) so the home page can
 * show real photos at once on a return visit, while the backend is still waking. It never leaves the
 * browser, is keyed per library, and is wiped on logout (see clientDataReset). Thumbnail *bytes* are
 * stored because the signed thumbnail URLs expire within minutes, so the HTTP cache can't replay them.
 */
export const HOME_SNAPSHOT_LIMIT = 240;
const MAX_AGE_MS = 30 * 24 * 60 * 60 * 1000;
const MAX_THUMB_BYTES = 256 * 1024;
const FETCH_CONCURRENCY = 6;

export interface SnapshotPhoto {
    filename: string;
    dateLabel: string;
    year: number;
    rotation: number;
    thumbnailRotation: number;
}

export interface HomeSnapshot {
    savedAt: number;
    photos: SnapshotPhoto[];
    thumbs: Map<string, Blob>;
}

interface StoredMeta { savedAt: number; photos: SnapshotPhoto[] }

/** Minimal storage surface, so the logic is testable without a real IndexedDB. */
export interface SnapshotBackend {
    read(library: string): Promise<{ meta: StoredMeta; thumbs: Map<string, Blob> } | null>;
    write(library: string, meta: StoredMeta, thumbs: Map<string, Blob>): Promise<void>;
    clear(): Promise<void>;
}

const DB_NAME = 'photostore-home-snapshot';

const openDb = (): Promise<IDBDatabase> => new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, 1);
    request.onupgradeneeded = () => {
        request.result.createObjectStore('meta');
        request.result.createObjectStore('thumbs');
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
});

const done = (tx: IDBTransaction): Promise<void> => new Promise((resolve, reject) => {
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
    tx.onabort = () => reject(tx.error);
});

const request = <T>(req: IDBRequest<T>): Promise<T> => new Promise((resolve, reject) => {
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
});

const thumbKey = (library: string, filename: string) => `${library}\u0000${filename}`;

const idbBackend: SnapshotBackend = {
    async read(library) {
        const db = await openDb();
        try {
            const tx = db.transaction(['meta', 'thumbs'], 'readonly');
            const meta = await request<StoredMeta | undefined>(tx.objectStore('meta').get(library));
            if (!meta) return null;
            const thumbs = new Map<string, Blob>();
            await Promise.all(meta.photos.map(async (photo) => {
                const blob = await request<Blob | undefined>(tx.objectStore('thumbs').get(thumbKey(library, photo.filename)));
                if (blob) thumbs.set(photo.filename, blob);
            }));
            return { meta, thumbs };
        } finally {
            db.close();
        }
    },
    async write(library, meta, thumbs) {
        const db = await openDb();
        try {
            const tx = db.transaction(['meta', 'thumbs'], 'readwrite');
            const store = tx.objectStore('thumbs');
            // Replace the previous snapshot of this library; other libraries' entries stay.
            const prefix = `${library}\u0000`;
            const range = IDBKeyRange.bound(prefix, `${prefix}￿`);
            store.delete(range);
            thumbs.forEach((blob, filename) => store.put(blob, thumbKey(library, filename)));
            tx.objectStore('meta').put(meta, library);
            await done(tx);
        } finally {
            db.close();
        }
    },
    async clear() {
        await new Promise<void>((resolve) => {
            const req = indexedDB.deleteDatabase(DB_NAME);
            req.onsuccess = () => resolve();
            req.onerror = () => resolve();
            req.onblocked = () => resolve();
        });
    },
};

const activeLibrary = () => getActiveLibraryFromToken() || '__default__';

export const createHomeSnapshotStore = (backend: SnapshotBackend = idbBackend, now: () => number = Date.now) => ({
    async load(library: string = activeLibrary()): Promise<HomeSnapshot | null> {
        try {
            const stored = await backend.read(library);
            if (!stored || now() - stored.meta.savedAt > MAX_AGE_MS) return null;
            const photos = stored.meta.photos.filter((p) => stored.thumbs.has(p.filename));
            if (!photos.length) return null;
            return { savedAt: stored.meta.savedAt, photos, thumbs: stored.thumbs };
        } catch {
            return null;
        }
    },

    /**
     * Saves the first HOME_SNAPSHOT_LIMIT photos that have a loadable thumbnail. `urls` maps filename to the
     * thumbnail URL the gallery is already showing, so the bytes normally come straight from the HTTP cache.
     */
    async save(
        photos: Photo[],
        urls: Record<string, string>,
        library: string = activeLibrary(),
        fetcher: typeof fetch = (...args) => fetch(...args),
    ): Promise<number> {
        const wanted = photos.filter((p) => urls[p.filename]).slice(0, HOME_SNAPSHOT_LIMIT);
        const blobs = new Map<string, Blob>();
        let next = 0;
        const worker = async () => {
            while (next < wanted.length) {
                const photo = wanted[next++];
                try {
                    const response = await fetcher(urls[photo.filename], { credentials: 'omit' });
                    if (!response.ok) continue;
                    const blob = await response.blob();
                    if (blob.size > 0 && blob.size <= MAX_THUMB_BYTES && (!blob.type || blob.type.startsWith('image/'))) {
                        blobs.set(photo.filename, blob);
                    }
                } catch { /* a missing thumbnail just isn't cached */ }
            }
        };
        await Promise.all(Array.from({ length: FETCH_CONCURRENCY }, worker));
        const kept = wanted.filter((p) => blobs.has(p.filename));
        // Never replace a good snapshot with a mostly-empty one (e.g. fetched while offline).
        if (kept.length < Math.min(wanted.length, 12)) return 0;
        try {
            await backend.write(library, {
                savedAt: now(),
                photos: kept.map((p) => ({
                    filename: p.filename,
                    dateLabel: p.dateLabel,
                    year: p.year,
                    rotation: p.rotation ?? 0,
                    thumbnailRotation: p.thumbnailRotation ?? 0,
                })),
            }, blobs);
            return kept.length;
        } catch {
            return 0;
        }
    },

    clear: () => backend.clear(),
});

export const homeSnapshot = createHomeSnapshotStore();
