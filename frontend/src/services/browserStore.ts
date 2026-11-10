// Tiny promise wrapper over IndexedDB for the two browser caches
// (thumbnailBlobCache, notificationHistory). Both are best-effort: every entry
// point resolves to a fallback value instead of rejecting, and an open that
// never completes (IndexedDB can hang in private windows / corrupted profiles,
// the same failure mode browserProcessingRuntime documents for CacheStorage)
// times out so a caller is never left awaiting a dead store.
const OPEN_TIMEOUT_MS = 3000;

export interface StoreSpec {
    name: string;
    keyPath?: string;
    indexes?: Array<{ name: string; keyPath: string }>;
}

const openDb = (dbName: string, version: number, stores: StoreSpec[]): Promise<IDBDatabase> => (
    new Promise((resolve, reject) => {
        if (typeof indexedDB === 'undefined') {
            reject(new Error('IndexedDB unavailable'));
            return;
        }
        const timer = setTimeout(() => reject(new Error('IndexedDB open timed out')), OPEN_TIMEOUT_MS);
        const request = indexedDB.open(dbName, version);
        request.onupgradeneeded = () => {
            const db = request.result;
            for (const spec of stores) {
                if (db.objectStoreNames.contains(spec.name)) {
                    continue;
                }
                const store = spec.keyPath
                    ? db.createObjectStore(spec.name, { keyPath: spec.keyPath })
                    : db.createObjectStore(spec.name);
                spec.indexes?.forEach((index) => store.createIndex(index.name, index.keyPath));
            }
        };
        request.onsuccess = () => {
            clearTimeout(timer);
            const db = request.result;
            // A deleteDatabase (logout -> clearLibraryClientData) blocks until
            // open connections close; yield so it can't be held hostage.
            db.onversionchange = () => db.close();
            resolve(db);
        };
        request.onerror = () => {
            clearTimeout(timer);
            reject(request.error || new Error('IndexedDB open failed'));
        };
    })
);

export interface BrowserDb {
    // Runs `work` inside one transaction; resolves with its result once the
    // transaction commits. Resolves `fallback` on any failure.
    run: <T>(
        storeName: string,
        mode: IDBTransactionMode,
        work: (store: IDBObjectStore, tx: IDBTransaction) => Promise<T> | T,
        fallback: T,
    ) => Promise<T>;
}

export const requestResult = <T,>(request: IDBRequest<T>): Promise<T> => (
    new Promise((resolve, reject) => {
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => reject(request.error);
    })
);

export const createBrowserDb = (dbName: string, version: number, stores: StoreSpec[]): BrowserDb => {
    let opened: Promise<IDBDatabase> | null = null;
    const getDb = () => {
        if (!opened) {
            opened = openDb(dbName, version, stores).then((db) => {
                // Drop the cached handle if the browser closes it under us
                // (versionchange above), so the next call reopens.
                db.onclose = () => { opened = null; };
                const previous = db.onversionchange;
                db.onversionchange = (event) => {
                    opened = null;
                    previous?.call(db, event);
                };
                return db;
            });
            opened.catch(() => { opened = null; });
        }
        return opened;
    };
    return {
        run: async (storeName, mode, work, fallback) => {
            try {
                const db = await getDb();
                const tx = db.transaction(storeName, mode);
                const done = new Promise<void>((resolve, reject) => {
                    tx.oncomplete = () => resolve();
                    tx.onerror = () => reject(tx.error);
                    tx.onabort = () => reject(tx.error);
                });
                const result = await work(tx.objectStore(storeName), tx);
                await done;
                return result;
            } catch {
                return fallback;
            }
        },
    };
};
