import { getActiveLibraryFromToken } from './passwordAuthClient';
import type { FileSystemFileHandle } from './fileSystemAccess';
import { PHOTO_CACHE_STORAGE_KEY } from '../components/browserAiShared';

/**
 * IndexedDB-backed persistence for in-flight upload blobs/handles, extracted
 * from PhotoGallery.tsx (see runBrowserProcessing/runUploadSession, which are
 * the callers, reached indirectly by AppServicesProvider's
 * `withPhotoGalleryRuntime` lazy-import boundary -- PhotoGallery.tsx
 * re-exports idbPut/idbGet/idbDelete so that boundary keeps working).
 */
const UPLOAD_DB_NAME = 'photostore-upload-db';
const UPLOAD_DB_STORE = 'files';

const openUploadDb = (): Promise<IDBDatabase> => new Promise((resolve, reject) => {
    const request = indexedDB.open(UPLOAD_DB_NAME, 1);
    request.onupgradeneeded = () => {
        const db = request.result;
        if (!db.objectStoreNames.contains(UPLOAD_DB_STORE)) {
            db.createObjectStore(UPLOAD_DB_STORE);
        }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error('Failed to open upload database.'));
});

// Also accepts a FileSystemFileHandle (Chrome/Edge desktop's resume path --
// see fileSystemAccess.ts): IndexedDB's structured clone algorithm supports
// storing handles directly, and they're far cheaper to persist than the
// file's actual bytes.
export const idbPut = async (key: string, value: Blob | FileSystemFileHandle): Promise<void> => {
    const db = await openUploadDb();
    await new Promise<void>((resolve, reject) => {
        const tx = db.transaction(UPLOAD_DB_STORE, 'readwrite');
        tx.objectStore(UPLOAD_DB_STORE).put(value, key);
        tx.oncomplete = () => resolve();
        tx.onerror = () => reject(tx.error || new Error('Failed to persist upload blob.'));
    });
    db.close();
};

export const idbGet = async (key: string): Promise<Blob | FileSystemFileHandle | null> => {
    const db = await openUploadDb();
    const result = await new Promise<Blob | FileSystemFileHandle | null>((resolve, reject) => {
        const tx = db.transaction(UPLOAD_DB_STORE, 'readonly');
        const req = tx.objectStore(UPLOAD_DB_STORE).get(key);
        req.onsuccess = () => resolve((req.result as Blob | FileSystemFileHandle | undefined) || null);
        req.onerror = () => reject(req.error || new Error('Failed to load upload blob.'));
    });
    db.close();
    return result;
};

export const idbDelete = async (key: string): Promise<void> => {
    const db = await openUploadDb();
    await new Promise<void>((resolve, reject) => {
        const tx = db.transaction(UPLOAD_DB_STORE, 'readwrite');
        tx.objectStore(UPLOAD_DB_STORE).delete(key);
        tx.oncomplete = () => resolve();
        tx.onerror = () => reject(tx.error || new Error('Failed to delete upload blob.'));
    });
    db.close();
};

/**
 * localStorage-backed boot cache for the gallery's initial photo list, so a
 * reload can paint instantly from the last-known page instead of a blank
 * loading state while the first real fetch is in flight.
 */
export interface PersistedPhotoCache<TPhoto = unknown, TFilterOptions = unknown> {
    timestamp: number;
    photos: TPhoto[];
    totalAvailable: number;
    offset: number;
    hasMore: boolean;
    sortBy: string;
    searchQuery: string;
    filters: TFilterOptions;
    captureStartDate: string;
    captureEndDate: string;
}

const PHOTO_CACHE_MAX_AGE_MS = 1000 * 60 * 30;

// Scope the boot cache to the active library so switching libraries never
// replays the previous library's photos. Falls back to the base key when there
// is no session (local dev / unauthenticated).
const photoCacheKey = (): string => {
    const lib = getActiveLibraryFromToken();
    return lib ? `${PHOTO_CACHE_STORAGE_KEY}.${lib}` : PHOTO_CACHE_STORAGE_KEY;
};

export const loadPhotoCache = <TPhoto = unknown, TFilterOptions = unknown>(): PersistedPhotoCache<TPhoto, TFilterOptions> | null => {
    try {
        const raw = localStorage.getItem(photoCacheKey());
        if (!raw) {
            return null;
        }
        const parsed = JSON.parse(raw) as PersistedPhotoCache<TPhoto, TFilterOptions>;
        if (!parsed || !Array.isArray(parsed.photos) || typeof parsed.timestamp !== 'number') {
            return null;
        }
        if (Date.now() - parsed.timestamp > PHOTO_CACHE_MAX_AGE_MS) {
            return null;
        }
        return parsed;
    } catch {
        return null;
    }
};

export const writePhotoCache = <TPhoto = unknown, TFilterOptions = unknown>(cache: PersistedPhotoCache<TPhoto, TFilterOptions>) => {
    try {
        const key = photoCacheKey();
        // Purge the pre-scoping global cache entry (written before caches were
        // scoped per library) so it can never be replayed under another library.
        if (key !== PHOTO_CACHE_STORAGE_KEY) {
            localStorage.removeItem(PHOTO_CACHE_STORAGE_KEY);
        }
        localStorage.setItem(key, JSON.stringify(cache));
    } catch {
        // Ignore storage quota or serialization errors.
    }
};
