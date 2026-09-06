// Client-orchestrated "download entire library" -- the browser fetches every
// original file directly from blob storage and saves it via the browser's
// own download mechanism, instead of waiting on a server-built zip. No
// folder picker: every file lands wherever the browser already saves
// downloads. See opfsDownloadStaging.ts for how a file survives a closed tab
// or a restart mid-download, and serviceWorkerDownload.ts for how the final
// save avoids both a real Safari blob: URL bug and cross-origin filename
// restrictions.
//
// Progress and per-file status live in a dedicated IndexedDB database (not
// localStorage -- this needs structured per-file records, not one blob) so a
// reload resumes exactly where it left off: already-'saved' files are
// skipped, and a file that was fully staged but never handed off (tab closed
// between the two steps) finalizes without re-fetching anything.
//
// Deliberately NOT persisting a manifest resume cursor: an earlier version
// did, advancing it as soon as a page was *fetched* into the in-memory queue
// rather than once its files were actually *saved* -- a reload while some of
// that page's entries were still sitting unprocessed in memory would silently
// drop them forever, since the next run's listing would resume past them.
// Every run re-pages the whole manifest from the start instead; already-
// 'saved' files are skipped fast (a local IndexedDB read, no network), and
// correctness matters far more here than the resume-speed optimization did.
import * as library from './libraryClient';
import { stagedByteCount, writeResponseToStaging, readStagedFile, removeStagedFile } from './opfsDownloadStaging';
import { prepareServiceWorkerDownload, triggerPreparedDownload } from './serviceWorkerDownload';

const DB_NAME = 'photostore-library-export';
const FILES_STORE = 'files';

type FileStatus = 'staged' | 'saved';

const openExportDb = (): Promise<IDBDatabase> => new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, 1);
    request.onupgradeneeded = () => {
        const db = request.result;
        if (!db.objectStoreNames.contains(FILES_STORE)) db.createObjectStore(FILES_STORE);
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error('Failed to open export database.'));
});

const fileKey = (libraryId: string, filename: string): string => `${libraryId}::${filename}`;

const getFileStatus = async (libraryId: string, filename: string): Promise<FileStatus | null> => {
    const db = await openExportDb();
    const result = await new Promise<FileStatus | null>((resolve, reject) => {
        const tx = db.transaction(FILES_STORE, 'readonly');
        const req = tx.objectStore(FILES_STORE).get(fileKey(libraryId, filename));
        req.onsuccess = () => resolve((req.result as FileStatus | undefined) || null);
        req.onerror = () => reject(req.error || new Error('Failed to read export status.'));
    });
    db.close();
    return result;
};

const setFileStatus = async (libraryId: string, filename: string, status: FileStatus): Promise<void> => {
    const db = await openExportDb();
    await new Promise<void>((resolve, reject) => {
        const tx = db.transaction(FILES_STORE, 'readwrite');
        tx.objectStore(FILES_STORE).put(status, fileKey(libraryId, filename));
        tx.oncomplete = () => resolve();
        tx.onerror = () => reject(tx.error || new Error('Failed to persist export status.'));
    });
    db.close();
};

// Wipes this library's 'saved'/'staged' records so a deliberate re-export
// (LibraryPage's "Download again", after a run already finished) actually
// re-downloads everything instead of silently no-op'ing -- confirmed live
// 2026-09-06 that without this, stageAndSaveOne sees every file already
// 'saved' from the prior run, skips both the fetch and the actual browser
// download trigger for all of them, yet still counts them into filesSaved
// and reports "Done — N file(s) saved" as if it had just done real work.
// Auto-resume (reloading mid-run) must NOT go through this path -- that
// case relies on exactly the records this clears to avoid re-downloading
// files a previous, interrupted run already finished.
const clearLibraryExportStatus = async (libraryId: string): Promise<void> => {
    const db = await openExportDb();
    await new Promise<void>((resolve, reject) => {
        const tx = db.transaction(FILES_STORE, 'readwrite');
        // Keys are `${libraryId}::${filename}` -- this range visits only this
        // library's own entries, not every library ever exported on this
        // device.
        const range = IDBKeyRange.bound(`${libraryId}::`, `${libraryId}::￿`);
        const request = tx.objectStore(FILES_STORE).openCursor(range);
        request.onsuccess = () => {
            const cursor = request.result;
            if (cursor) {
                cursor.delete();
                cursor.continue();
            }
        };
        tx.oncomplete = () => resolve();
        tx.onerror = () => reject(tx.error || new Error('Failed to clear export status.'));
    });
    db.close();
};

export interface ExportError {
    filename: string;
    message: string;
}

export interface ExportProgress {
    filesSaved: number;
    // Grows as manifest pages are consumed; only a firm total once
    // listingComplete is true (before that, show it as a lower bound).
    filesSeen: number;
    listingComplete: boolean;
    currentFilename: string | null;
    errors: ExportError[];
    // Set if paging the manifest itself failed (e.g. a session expiring
    // mid-export) -- distinct from a per-file error, which is skipped and
    // recorded in `errors` without stopping the rest of the export. Calling
    // startLibraryExport again resumes from the last successfully-listed
    // page's cursor.
    fatalError: string | null;
}

export interface ExportController {
    cancel: () => void;
}

// A manifest entry with its actual URL already built from that page's shared
// baseUrl/sas (see libraryClient.buildExportFileUrl) -- computed once when a
// page is listed rather than carrying the page's token around separately.
interface QueuedFile {
    filename: string;
    url: string;
}

const EXPORT_CONCURRENCY = 4;
// Rapid, fully automatic downloads without further user interaction trip
// most browsers' "this site is downloading many files" heuristics -- pacing
// the final save step (not the fetch/stage step, which stays fully
// concurrent) keeps well under that without slowing the actual transfer.
// There's no code-level fix for a save a browser silently blocks under this
// heuristic (page JS gets no signal either way) -- pacing only reduces how
// often it happens; LibraryPage's copy tells the user to look for and
// approve a "multiple downloads" prompt near the address bar if files seem
// to be failing.
const MIN_SAVE_INTERVAL_MS = 500;
// Bounds how many {filename, url} entries sit in memory waiting for a free
// worker -- large enough that listing rarely blocks a worker, small enough
// that a 500k-file library never holds more than a sliver of its manifest.
const QUEUE_HIGH_WATER = 2000;

const wait = (ms: number): Promise<void> => new Promise((resolve) => { setTimeout(resolve, ms); });

export const startLibraryExport = (
    libraryId: string,
    onProgress: (progress: ExportProgress) => void,
    onDone: () => void,
    options?: { forceRestart?: boolean },
): ExportController => {
    let cancelled = false;
    const queue: QueuedFile[] = [];
    let listingComplete = false;
    let listingError: string | null = null;
    let filesSeen = 0;
    let filesSaved = 0;
    const errors: ExportError[] = [];
    let lastSaveAt = 0;
    // Strictly serializes the actual save trigger across all EXPORT_CONCURRENCY
    // workers -- confirmed live 2026-09-06 that two triggers landing close
    // together are two overlapping main-frame navigations, and the browser
    // silently cancels one with no signal to page JS. Pacing `lastSaveAt`
    // alone wasn't enough: two workers can each pass the pacing check, then
    // spend a different amount of time in prepareServiceWorkerDownload's own
    // awaits, and still end up triggering within milliseconds of each other.
    // Chaining onto this promise makes each trigger wait for the previous
    // one's full completion (pacing included) no matter how the preparation
    // work interleaves.
    let clickChain: Promise<void> = Promise.resolve();

    const emitProgress = (currentFilename: string | null) => {
        onProgress({ filesSaved, filesSeen, listingComplete, currentFilename, errors: [...errors], fatalError: listingError });
    };

    const scheduleTrigger = (url: string): Promise<void> => {
        const next = clickChain.then(async () => {
            const waitFor = Math.max(0, MIN_SAVE_INTERVAL_MS - (Date.now() - lastSaveAt));
            if (waitFor > 0) await wait(waitFor);
            triggerPreparedDownload(url);
            lastSaveAt = Date.now();
        });
        // Keep the chain alive even if this trigger's step throws, so one
        // bad file can't wedge every save behind it forever.
        clickChain = next.catch(() => {});
        return next;
    };

    // A 500k-file library pages through this ~1000 times over a run that can
    // span hours -- a single transient blip (one dropped connection, one
    // backend 503 under load) shouldn't end the whole unattended export, so
    // retry a few times with backoff before treating a page as a fatal
    // listing error.
    const MANIFEST_PAGE_RETRIES = 4;
    const fetchManifestPageWithRetry = async (cursor: string | null): Promise<library.ExportManifestPage> => {
        let lastError: unknown;
        for (let attempt = 0; attempt < MANIFEST_PAGE_RETRIES; attempt += 1) {
            if (attempt > 0) await wait(1000 * 2 ** (attempt - 1));
            try {
                return await library.getLibraryExportManifestPage(cursor);
            } catch (e) {
                lastError = e;
            }
        }
        throw lastError instanceof Error ? lastError : new Error('Could not list the library for export.');
    };

    const listManifest = async () => {
        let cursor: string | null = null;
        while (!cancelled) {
            if (queue.length >= QUEUE_HIGH_WATER) {
                await wait(200);
                continue;
            }
            let page: library.ExportManifestPage;
            try {
                page = await fetchManifestPageWithRetry(cursor);
            } catch (e) {
                listingError = e instanceof Error ? e.message : 'Could not list the library for export.';
                return;
            }
            queue.push(...page.files.map((f) => ({ filename: f.filename, url: library.buildExportFileUrl(page, f.blobName) })));
            filesSeen += page.files.length;
            cursor = page.nextCursor;
            emitProgress(null);
            if (!cursor) {
                listingComplete = true;
                return;
            }
        }
    };

    const stageAndSaveOne = async (entry: QueuedFile): Promise<void> => {
        const { filename, url } = entry;
        const existing = await getFileStatus(libraryId, filename);
        if (existing !== 'saved') {
            if (existing !== 'staged') {
                const offset = await stagedByteCount(libraryId, filename);
                const headers: Record<string, string> = offset > 0 ? { Range: `bytes=${offset}-` } : {};
                const response = await fetch(url, { headers });
                // 416 means the offset already covers the whole file -- the
                // OPFS write finished in an earlier run but the 'staged'
                // status write never landed (tab closed/reloaded between the
                // two, which the hard-refresh recovery reload above can
                // trigger more often than a normal close would). Without
                // this, every retry re-requests the same past-the-end range,
                // gets 416 again, and this file can never succeed.
                if (response.status === 416) {
                    await setFileStatus(libraryId, filename, 'staged');
                } else {
                    if (!response.ok && response.status !== 206) {
                        throw new Error(`Download failed (${response.status}).`);
                    }
                    // A plain 200 with a nonzero offset means the server didn't
                    // honor the Range request -- the body is the whole file, so
                    // the partial staging copy must be discarded, not appended to.
                    const actualOffset = response.status === 200 ? 0 : offset;
                    await writeResponseToStaging(libraryId, filename, response, actualOffset);
                    await setFileStatus(libraryId, filename, 'staged');
                }
            }
        }
        if (existing !== 'saved') {
            const file = await readStagedFile(libraryId, filename);
            const downloadUrl = await prepareServiceWorkerDownload(filename, file);
            await scheduleTrigger(downloadUrl);
            await setFileStatus(libraryId, filename, 'saved');
            await removeStagedFile(libraryId, filename);
        }
        filesSaved += 1;
    };

    const runWorker = async () => {
        while (!cancelled) {
            const entry = queue.shift();
            if (!entry) {
                if (listingComplete || listingError) return;
                await wait(100);
                continue;
            }
            emitProgress(entry.filename);
            try {
                await stageAndSaveOne(entry);
            } catch (e) {
                errors.push({ filename: entry.filename, message: e instanceof Error ? e.message : 'Download failed.' });
            }
            emitProgress(null);
        }
    };

    (async () => {
        if (options?.forceRestart) {
            await clearLibraryExportStatus(libraryId);
        }
        if (cancelled) return;
        const listing = listManifest();
        const workers = Array.from({ length: EXPORT_CONCURRENCY }, () => runWorker());
        await Promise.all([listing, ...workers]);
        if (!cancelled) {
            emitProgress(null);
            onDone();
        }
    })();

    return {
        cancel: () => { cancelled = true; },
    };
};
