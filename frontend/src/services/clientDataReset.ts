// Wipes every client-side store that holds account- or library-scoped data, so
// a logout or an account switch can't replay the previous account's photos
// under the new session. Without this, only the auth token was swapped on
// login while the old library's IndexedDB rows, media/access caches and the
// pre-filled email hint survived -- which is why the gallery would briefly load
// the real library, then "disappear" into whichever account the stale state
// (or the stale email hint) pointed at. Incognito never hit this because it
// starts with empty storage. See the 2026-10-17 account-switch blank-gallery
// investigation (HAR: login rajatverma57 -> 401 gap -> re-login cgc.beee).
//
// UI-only preferences (tile sizes, zoom level, the browser-AI model manifest,
// perf buffer) are intentionally NOT cleared -- they carry no library data and
// losing them on every logout is pointless churn.

// IndexedDB databases that cache a single library's photos/people/albums/upload
// state. Each is internally keyed by the active library, but a full delete is
// the simplest guarantee that nothing from a previous account can be read back.
const LIBRARY_IDB_DATABASES = [
    'photostore-sort-index',
    'photostore-people-index',
    'photostore-albums-index',
    'photostore-upload-db',
    'photostore-library-export',
];

// Exact localStorage keys holding account/library-scoped data.
const LIBRARY_LOCAL_STORAGE_KEYS = [
    'photostore.mediaAccessCache',
    'photostore.thumbnailAccessCache',
    'photostore.photo.cache.v1',
    'photostore.library-change.latest',
    'photostore.library-changes.v1',
    'photostore.jobNotifier.seen',
    'photostore.upload.active',
    'photostore.upload.session.v1',
];

// localStorage key prefixes whose every entry is per-library.
const LIBRARY_LOCAL_STORAGE_PREFIXES = [
    'photostore-media-token:',          // signed media tokens, keyed by library
    'photostore.dismissedSuggestion.',  // per-library dismissed suggestions
];

export const clearLibraryClientData = (): void => {
    try {
        for (const name of LIBRARY_IDB_DATABASES) {
            try { indexedDB.deleteDatabase(name); } catch { /* ignore per-db failure */ }
        }
    } catch { /* indexedDB unavailable */ }

    try {
        for (const key of LIBRARY_LOCAL_STORAGE_KEYS) {
            window.localStorage.removeItem(key);
        }
        const doomed: string[] = [];
        for (let i = 0; i < window.localStorage.length; i += 1) {
            const key = window.localStorage.key(i);
            if (key && LIBRARY_LOCAL_STORAGE_PREFIXES.some((prefix) => key.startsWith(prefix))) {
                doomed.push(key);
            }
        }
        doomed.forEach((key) => window.localStorage.removeItem(key));
    } catch { /* localStorage unavailable */ }
};
