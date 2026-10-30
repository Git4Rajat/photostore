// File System Access API support (Chrome/Edge desktop only -- unsupported in
// Safari and Firefox, and not exposed on any mobile browser). Where available,
// it replaces the plain <input type="file"> picker for the initial upload
// selection so a FileSystemFileHandle can be kept for each file: handles are
// cheap to persist (a few bytes, not the file's actual bytes) and can be
// turned back into a live File on demand, which is what lets an interrupted
// upload resume after a page reload without the user reselecting anything --
// something the blob-based IndexedDB cache (the fallback for Safari/Firefox/
// mobile, see cacheUploadFilesForResume in AppServicesProvider.tsx) can't do
// at any real scale for large batches without risking the same memory
// pressure it's designed to avoid.

// Minimal ambient typing for the subset of the API used below -- this
// project's pinned TypeScript version (4.9) doesn't ship these DOM types, and
// pulling in a whole @types/wicg-file-system-access dependency for four
// methods isn't worth it.
interface FileSystemFileHandle {
    readonly kind: 'file';
    readonly name: string;
    getFile(): Promise<File>;
    queryPermission(options?: { mode?: 'read' | 'readwrite' }): Promise<PermissionState>;
    requestPermission(options?: { mode?: 'read' | 'readwrite' }): Promise<PermissionState>;
}

type OpenFilePickerOptions = {
    multiple?: boolean;
    excludeAcceptAllOption?: boolean;
};

declare global {
    interface Window {
        showOpenFilePicker?: (options?: OpenFilePickerOptions) => Promise<FileSystemFileHandle[]>;
    }
}

export type { FileSystemFileHandle };

export const isFileSystemAccessSupported = (): boolean => (
    typeof window !== 'undefined' && typeof window.showOpenFilePicker === 'function'
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

export interface PickedUploadFile {
    file: File;
    handle: FileSystemFileHandle;
}

// Returns [] both when the user cancels (AbortError) and when the picker
// throws for any other reason -- callers already have the classic
// <input type="file"> as a fallback UI, so there's no separate error state to
// surface here. No `types` filter is passed: many RAW extensions (.cr3, .nef,
// ...) have no well-known MIME type to key an extension filter off of, and
// duplicating photoDisplay.ts's extension lists here isn't worth it just to
// pre-select a dropdown filter that defaults to "All Files" either way.
export const pickUploadFilesViaFileSystemAccess = async (): Promise<PickedUploadFile[]> => {
    if (!isFileSystemAccessSupported()) {
        return [];
    }
    try {
        const handles = await window.showOpenFilePicker!({
            multiple: true,
            excludeAcceptAllOption: false,
        });
        return await Promise.all(handles.map(async (handle) => ({
            file: await handle.getFile(),
            handle,
        })));
    } catch {
        return [];
    }
};

// Re-derives a live File from a persisted handle, re-requesting read
// permission first if it's lapsed. Returns null (rather than throwing) when
// permission can't be silently regained -- e.g. requestPermission needing a
// user gesture that isn't available from an automatic resume-on-load effect
// -- so callers can fall back to the existing "please reselect this file"
// path exactly as if the handle were missing.
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
