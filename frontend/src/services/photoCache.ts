import type { FileSystemFileHandle } from './fileSystemAccess';

/**
 * IndexedDB-backed persistence for in-flight upload blobs/handles, extracted
 * from browserProcessingRuntime.tsx (see runBrowserProcessing/runUploadSession, which are
 * the callers, reached indirectly by AppServicesProvider's
 * `withBrowserProcessingRuntime` lazy-import boundary -- browserProcessingRuntime.tsx
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

// Deletes every cached upload blob whose key isn't claimed by `keep`. The
// per-file deletes in the upload flow (releaseCachedUploadFile,
// clearPersistedSession) are best-effort and only know about keys still in the
// persisted session record, so blobs left by a closed/crashed tab, a skipped
// checkpoint, or a swallowed delete error would otherwise sit in this origin's
// storage forever, shrinking the quota available to later uploads.
export const idbSweepOrphans = async (keep: (key: string) => boolean): Promise<number> => {
    const db = await openUploadDb();
    try {
        const keys = await new Promise<IDBValidKey[]>((resolve, reject) => {
            const req = db.transaction(UPLOAD_DB_STORE, 'readonly').objectStore(UPLOAD_DB_STORE).getAllKeys();
            req.onsuccess = () => resolve(req.result);
            req.onerror = () => reject(req.error || new Error('Failed to list upload blobs.'));
        });
        const orphans = keys.filter((key) => typeof key !== 'string' || !keep(key));
        if (orphans.length === 0) return 0;
        await new Promise<void>((resolve, reject) => {
            const tx = db.transaction(UPLOAD_DB_STORE, 'readwrite');
            const store = tx.objectStore(UPLOAD_DB_STORE);
            orphans.forEach((key) => store.delete(key));
            tx.oncomplete = () => resolve();
            tx.onerror = () => reject(tx.error || new Error('Failed to sweep upload blobs.'));
        });
        return orphans.length;
    } finally {
        db.close();
    }
};
