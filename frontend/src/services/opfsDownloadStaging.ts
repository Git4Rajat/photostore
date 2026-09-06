// Per-file staging for the client-orchestrated library export
// (libraryExportDownloader.ts). Each file is fetched with an HTTP Range
// resume and written into a private Origin Private File System (OPFS) copy
// before ever being handed to the browser's own save mechanism -- OPFS is
// disk-backed (not an in-memory buffer) and durable across a closed tab or a
// full browser restart, which is what lets a large-file download resume from
// its last byte instead of from zero. Unlike the File System Access picker
// (showDirectoryPicker), OPFS needs no user permission and is supported in
// every evergreen browser (Chrome, Edge, Firefox, Safari) -- see the design
// discussion this module implements for why no folder picker is used at all.
//
// This project's pinned TypeScript version (4.9) ships FileSystemFileHandle/
// FileSystemDirectoryHandle/StorageManager.getDirectory() already (unlike the
// picker methods augmented in fileSystemAccess.ts), but not the writable-
// stream side of the API -- createWritable() and FileSystemWritableFileStream
// don't exist in its lib.dom.d.ts at all. Augmenting the real global
// interfaces (rather than shadowing them locally, as fileSystemAccess.ts does
// for its own picker-obtained handles) is required here because these
// handles come from a real lib.dom-typed call (navigator.storage.
// getDirectory()) -- a locally shadowed interface wouldn't apply to a value
// typed via the global one.
declare global {
    interface FileSystemFileHandle {
        createWritable(options?: { keepExistingData?: boolean }): Promise<FileSystemWritableFileStream>;
    }

    interface FileSystemWritableFileStream extends WritableStream {
        write(data: BufferSource | Blob | string): Promise<void>;
        seek(position: number): Promise<void>;
        truncate(size: number): Promise<void>;
    }
}

// Shape check only -- true wherever the API exists at all, even if calling it
// would actually fail (see verifyOpfsUsable below for the real answer).
export const isOpfsSupported = (): boolean => (
    typeof navigator !== 'undefined'
    && typeof navigator.storage !== 'undefined'
    && typeof navigator.storage.getDirectory === 'function'
);

const STAGING_DIR_NAME = 'library-export-staging';

let stagingDirPromise: Promise<FileSystemDirectoryHandle> | null = null;

const getStagingDirectory = (): Promise<FileSystemDirectoryHandle> => {
    if (!stagingDirPromise) {
        stagingDirPromise = navigator.storage.getDirectory()
            .then((root) => root.getDirectoryHandle(STAGING_DIR_NAME, { create: true }));
    }
    return stagingDirPromise;
};

// isOpfsSupported() only checks that the API shape exists -- confirmed live
// 2026-09-06 that WebKit can have getDirectory() *reject* outright (observed:
// "The operation failed for an unknown transient reason (e.g. out of
// memory)", consistently, not actually transient) while still exposing the
// function -- Safari's storage backend refuses OPFS in some real conditions
// (Private Browsing disables persistent storage entirely; there are other
// known WebKit configurations with the same effect). Without this check,
// every file in the export fails individually with that cryptic message
// while the UI still ends on "Done", which is exactly as unhelpful as it
// sounds -- this actually calls getDirectory() once so LibraryPage can catch
// it before burning through an entire manifest one silent per-file error at
// a time. Memoized onto the same stagingDirPromise real usage reads from, so
// a successful probe isn't wasted work.
export const verifyOpfsUsable = async (): Promise<boolean> => {
    if (!isOpfsSupported()) return false;
    try {
        await getStagingDirectory();
        return true;
    } catch {
        return false;
    }
};

// OPFS entry names are flat (no path separators) -- encode so any filename
// safely round-trips as a single entry regardless of what characters the
// original photo/video filename contains. Scoped by libraryId (matching
// libraryExportDownloader.ts's IndexedDB fileKey) because filenames are not
// unique across libraries -- confirmed live 2026-09-06 this was NOT scoped:
// a user in two shared libraries that both happen to have an "IMG_1234.jpg"
// (an extremely common camera default, not a rare collision) could interrupt
// library A's export partway through that file, then export library B --
// stagedByteCount would report A's partial byte count as if it were B's, a
// Range request would be issued from that offset against B's file, and the
// response would be written on top of A's leftover bytes at that offset,
// producing a corrupted spliced file saved to the user's downloads under
// library B's export.
const stagingKey = (libraryId: string, filename: string): string => `${encodeURIComponent(libraryId)}::${encodeURIComponent(filename)}`;

// Current size of a file's staging copy, or 0 if none exists yet -- this IS
// the resume offset (no separate byte counter kept in IndexedDB, so there's
// nothing that can drift out of sync with what's actually on disk).
export const stagedByteCount = async (libraryId: string, filename: string): Promise<number> => {
    try {
        const dir = await getStagingDirectory();
        const handle = await dir.getFileHandle(stagingKey(libraryId, filename));
        const file = await handle.getFile();
        return file.size;
    } catch {
        return 0;
    }
};

// Streams a fetch Response's body into the staging copy starting at
// `resumeOffset` (0 for a fresh download, stagedByteCount()'s result when
// resuming a Range request). keepExistingData preserves bytes already on
// disk; seek() positions the write cursor since a freshly created writable
// stream always starts at 0 regardless of keepExistingData.
export const writeResponseToStaging = async (
    libraryId: string,
    filename: string,
    response: Response,
    resumeOffset: number,
): Promise<void> => {
    if (!response.body) {
        throw new Error('Response has no readable body.');
    }
    const dir = await getStagingDirectory();
    const handle = await dir.getFileHandle(stagingKey(libraryId, filename), { create: true });
    const writable = await handle.createWritable({ keepExistingData: true });
    try {
        if (resumeOffset > 0) {
            await writable.seek(resumeOffset);
        }
        const reader = response.body.getReader();
        // eslint-disable-next-line no-constant-condition
        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            if (value) await writable.write(value);
        }
    } finally {
        await writable.close();
    }
};

// Reads the fully-staged file back out for the final hand-off to the
// browser's own save mechanism (a plain <a download> click in
// libraryExportDownloader.ts).
export const readStagedFile = async (libraryId: string, filename: string): Promise<File> => {
    const dir = await getStagingDirectory();
    const handle = await dir.getFileHandle(stagingKey(libraryId, filename));
    return handle.getFile();
};

// Frees the staging copy once it's been handed off -- otherwise every
// exported file's bytes would sit in OPFS twice (staged + saved) for the
// life of the browser profile.
export const removeStagedFile = async (libraryId: string, filename: string): Promise<void> => {
    try {
        const dir = await getStagingDirectory();
        await dir.removeEntry(stagingKey(libraryId, filename));
    } catch {
        // Already gone, or never staged -- nothing to clean up.
    }
};
