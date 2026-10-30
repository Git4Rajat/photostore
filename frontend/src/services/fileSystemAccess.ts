// File System Access API support (Chrome/Edge desktop only -- unsupported in
// Safari and Firefox, and not exposed on any mobile browser). Where available,
// it replaces the plain <input type="file"> picker for the initial upload
// selection so a handle can be kept for the selection: handles are cheap to
// persist (a few bytes, not the file's actual bytes) and can be turned back
// into a live File on demand, which is what lets an interrupted upload resume
// after a page reload without the user reselecting anything -- something the
// blob-based IndexedDB cache (the fallback for Safari/Firefox/mobile, see
// cacheUploadFilesForResume in AppServicesProvider.tsx) can't do at any real
// scale for large batches without risking the same memory pressure it's
// designed to avoid.
//
// Folder picking (showDirectoryPicker/pickUploadFolderViaFileSystemAccess) is
// the only picker used for new selections -- see AppServicesProvider.tsx's
// requestUpload. A folder handle has exactly one permission grant for the
// whole subtree; re-deriving a child file handle from an already-granted
// folder handle (getFileHandle/getDirectoryHandle) never needs its own
// separate permission check at all. This replaced an earlier design that
// picked individual files (showOpenFilePicker), which handed back N
// independent file handles each with its own grant that could lapse
// independently -- resuming hundreds of them after a reload meant the
// browser's native "Allow this site to read <filename>" dialog firing once
// per file, back to back. isFileSystemFileHandle and resolveFileFromHandle
// below are kept only to resume sessions that were already persisted under
// that old per-file-handle shape before this changed; nothing creates a new
// one of those handles anymore.

// Minimal ambient typing for the subset of the API used below -- this
// project's pinned TypeScript version (4.9) doesn't ship these DOM types, and
// pulling in a whole @types/wicg-file-system-access dependency for a handful
// of methods isn't worth it.
interface FileSystemFileHandle {
    readonly kind: 'file';
    readonly name: string;
    getFile(): Promise<File>;
    queryPermission(options?: { mode?: 'read' | 'readwrite' }): Promise<PermissionState>;
    requestPermission(options?: { mode?: 'read' | 'readwrite' }): Promise<PermissionState>;
}

interface FileSystemDirectoryHandle {
    readonly kind: 'directory';
    readonly name: string;
    queryPermission(options?: { mode?: 'read' | 'readwrite' }): Promise<PermissionState>;
    requestPermission(options?: { mode?: 'read' | 'readwrite' }): Promise<PermissionState>;
    getFileHandle(name: string): Promise<FileSystemFileHandle>;
    getDirectoryHandle(name: string): Promise<FileSystemDirectoryHandle>;
    values(): AsyncIterableIterator<FileSystemFileHandle | FileSystemDirectoryHandle>;
}

type DirectoryPickerOptions = {
    mode?: 'read' | 'readwrite';
};

declare global {
    interface Window {
        showDirectoryPicker?: (options?: DirectoryPickerOptions) => Promise<FileSystemDirectoryHandle>;
    }
}

export type { FileSystemFileHandle, FileSystemDirectoryHandle };

export const isFileSystemDirectoryAccessSupported = (): boolean => (
    typeof window !== 'undefined' && typeof window.showDirectoryPicker === 'function'
);

// Duck-typed rather than an instanceof check -- there's no constructor to
// check against in browsers/TS versions that lack the type, and a handle
// round-tripped through IndexedDB's structured clone keeps its methods
// regardless.
export const isFileSystemFileHandle = (value: unknown): value is FileSystemFileHandle => (
    typeof value === 'object' && value !== null
    && typeof (value as FileSystemFileHandle).getFile === 'function'
    && typeof (value as FileSystemFileHandle).queryPermission === 'function'
);

export const isFileSystemDirectoryHandle = (value: unknown): value is FileSystemDirectoryHandle => (
    typeof value === 'object' && value !== null
    && typeof (value as FileSystemDirectoryHandle).getFileHandle === 'function'
    && typeof (value as FileSystemDirectoryHandle).queryPermission === 'function'
);

export interface PickedUploadFolderFile {
    file: File;
    // Slash-separated path of this file relative to the picked folder (e.g.
    // "2026-10-29/IMG_0001.jpg"), so a resumed session can re-derive the same
    // file's handle from the persisted folder handle via getDirectoryHandle/
    // getFileHandle without needing its own independently-persisted handle.
    relativePath: string;
}

export interface PickedUploadFolder {
    directoryHandle: FileSystemDirectoryHandle;
    files: PickedUploadFolderFile[];
}

// Dotfiles (.DS_Store, Thumbs.db-style OS metadata, hidden sync-tool state)
// are routine clutter in a real folder that a conscious multi-file selection
// would never include in the first place -- skip them during a recursive
// folder walk rather than uploading them as if the user had picked them.
const isHiddenEntryName = (name: string): boolean => name.startsWith('.');

async function* walkDirectory(
    handle: FileSystemDirectoryHandle, prefix: string,
): AsyncGenerator<{ file: File; relativePath: string }> {
    for await (const entry of handle.values()) {
        if (isHiddenEntryName(entry.name)) {
            continue;
        }
        const relativePath = prefix ? `${prefix}/${entry.name}` : entry.name;
        if (entry.kind === 'directory') {
            yield* walkDirectory(entry, relativePath);
        } else {
            yield { file: await entry.getFile(), relativePath };
        }
    }
}

// Returns null both when the user cancels (AbortError) and when the picker
// throws for any other reason -- callers already have the classic
// <input type="file"> as a fallback UI, so there's no separate error state to
// surface here.
export const pickUploadFolderViaFileSystemAccess = async (): Promise<PickedUploadFolder | null> => {
    if (!isFileSystemDirectoryAccessSupported()) {
        return null;
    }
    try {
        const directoryHandle = await window.showDirectoryPicker!({ mode: 'read' });
        const files: PickedUploadFolderFile[] = [];
        for await (const entry of walkDirectory(directoryHandle, '')) {
            files.push(entry);
        }
        return { directoryHandle, files };
    } catch {
        return null;
    }
};

// Re-derives a live File from a persisted handle, re-requesting read
// permission first if it's lapsed. Returns null (rather than throwing) when
// permission can't be silently regained -- e.g. requestPermission needing a
// user gesture that isn't available from an automatic resume-on-load effect
// -- so callers can fall back to the existing "please reselect this file"
// path exactly as if the handle were missing.
//
// Kept only for resuming sessions persisted under the old per-file-handle
// shape -- see this file's header comment and resolveFileFromDirectoryEntry.
export const resolveFileFromHandle = async (handle: FileSystemFileHandle): Promise<File | null> => {
    try {
        let permission = await handle.queryPermission({ mode: 'read' });
        if (permission !== 'granted') {
            permission = await handle.requestPermission({ mode: 'read' });
        }
        if (permission !== 'granted') {
            return null;
        }
        return await handle.getFile();
    } catch {
        return null;
    }
};

// Ensures read permission on a folder handle, requesting it if needed.
// Deliberately NOT memoized here -- concurrent callers awaiting this
// directly (e.g. several upload worker lanes resolving different files from
// the same folder at once) could each see the handle not-yet-granted before
// any of their requestPermission calls resolve, and each independently call
// requestPermission, showing the browser's native dialog more than once.
// Callers that may run concurrently against the same handle (see
// AppServicesProvider.tsx's runUploadSession) must memoize their own call to
// this into a single shared in-flight promise; resolveFileFromDirectoryEntry
// below is only safe to call directly when nothing else can race it.
export const ensureDirectoryReadPermission = async (directoryHandle: FileSystemDirectoryHandle): Promise<boolean> => {
    let permission = await directoryHandle.queryPermission({ mode: 'read' });
    if (permission !== 'granted') {
        permission = await directoryHandle.requestPermission({ mode: 'read' });
    }
    return permission === 'granted';
};

// Re-derives a live File from a persisted folder handle plus a file's
// relative path within it. Child file/directory handles obtained from an
// already-granted folder handle never need (and never get) their own
// separate permission check -- that's the whole benefit of a folder-level
// grant over the old per-file-handle model.
//
// Does NOT check permission itself -- unlike resolveFileFromHandle, this is
// expected to be called concurrently (once per file, across several upload
// worker lanes) against the SAME shared `directoryHandle`, and
// ensureDirectoryReadPermission is explicitly unsafe to call unmemoized from
// concurrent callers (see its own comment). The caller must have already
// confirmed permission via its own memoized call to that function first.
export const resolveFileFromDirectoryEntry = async (
    directoryHandle: FileSystemDirectoryHandle, relativePath: string,
): Promise<File | null> => {
    try {
        const segments = relativePath.split('/');
        let dir = directoryHandle;
        for (let i = 0; i < segments.length - 1; i += 1) {
            dir = await dir.getDirectoryHandle(segments[i]);
        }
        const fileHandle = await dir.getFileHandle(segments[segments.length - 1]);
        return await fileHandle.getFile();
    } catch {
        return null;
    }
};
