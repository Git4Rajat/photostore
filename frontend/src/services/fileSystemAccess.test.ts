import { afterEach, describe, expect, it, vi } from 'vitest';
import {
    ensureDirectoryReadPermission,
    isFileSystemDirectoryAccessSupported,
    pickUploadFolderViaFileSystemAccess,
    resolveFileFromDirectoryEntry,
    type FileSystemDirectoryHandle,
    type FileSystemFileHandle,
} from './fileSystemAccess';

// Defaults content to the name itself so distinct filenames are trivially
// distinguishable; callers verifying disambiguation between two files that
// SHARE a name (in different subfolders) pass distinct `content` explicitly.
function fakeFile(name: string, content: string = name): FileSystemFileHandle {
    return {
        kind: 'file',
        name,
        getFile: async () => new File([content], name),
        queryPermission: vi.fn(),
        requestPermission: vi.fn(),
    };
}

function fakeDir(
    name: string,
    entries: Array<FileSystemFileHandle | FileSystemDirectoryHandle>,
    overrides: Partial<FileSystemDirectoryHandle> = {},
): FileSystemDirectoryHandle {
    const children = new Map(entries.map((entry) => [entry.name, entry]));
    return {
        kind: 'directory',
        name,
        queryPermission: vi.fn().mockResolvedValue('granted'),
        requestPermission: vi.fn().mockResolvedValue('granted'),
        getFileHandle: async (childName: string) => {
            const child = children.get(childName);
            if (!child || child.kind !== 'file') throw new Error(`not a file: ${childName}`);
            return child;
        },
        getDirectoryHandle: async (childName: string) => {
            const child = children.get(childName);
            if (!child || child.kind !== 'directory') throw new Error(`not a directory: ${childName}`);
            return child;
        },
        values: async function* () {
            for (const entry of entries) yield entry;
        },
        ...overrides,
    };
}

afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
});

describe('isFileSystemDirectoryAccessSupported', () => {
    it('is false without window.showDirectoryPicker and true with it', () => {
        expect(isFileSystemDirectoryAccessSupported()).toBe(false);
        vi.stubGlobal('window', { ...window, showDirectoryPicker: vi.fn() });
        expect(isFileSystemDirectoryAccessSupported()).toBe(true);
    });
});

describe('pickUploadFolderViaFileSystemAccess', () => {
    it('returns null when the folder picker is unsupported', async () => {
        expect(await pickUploadFolderViaFileSystemAccess()).toBeNull();
    });

    it('walks nested subfolders and builds slash-joined relativePaths', async () => {
        const root = fakeDir('root', [
            fakeFile('IMG_0001.jpg'),
            fakeDir('2026-10-29', [fakeFile('IMG_0002.jpg'), fakeDir('raw', [fakeFile('IMG_0002.cr3')])]),
        ]);
        vi.stubGlobal('window', { ...window, showDirectoryPicker: vi.fn().mockResolvedValue(root) });
        const picked = await pickUploadFolderViaFileSystemAccess();
        expect(picked).not.toBeNull();
        expect(picked!.directoryHandle).toBe(root);
        expect(new Set(picked!.files.map((entry) => entry.relativePath))).toEqual(new Set([
            'IMG_0001.jpg', '2026-10-29/IMG_0002.jpg', '2026-10-29/raw/IMG_0002.cr3',
        ]));
    });

    it('disambiguates two files sharing a leaf name in different subfolders -- each relativePath round-trips to the right one', async () => {
        const root = fakeDir('root', [
            fakeFile('IMG_0001.jpg', 'root-copy'),
            fakeDir('album-a', [fakeFile('IMG_0001.jpg', 'album-a-copy')]),
            fakeDir('album-b', [fakeFile('IMG_0001.jpg', 'album-b-copy')]),
        ]);
        vi.stubGlobal('window', { ...window, showDirectoryPicker: vi.fn().mockResolvedValue(root) });
        const picked = await pickUploadFolderViaFileSystemAccess();
        expect(picked!.files).toHaveLength(3);
        const contentByPath = new Map(await Promise.all(
            picked!.files.map(async (entry) => [entry.relativePath, await entry.file.text()] as const),
        ));
        expect(contentByPath).toEqual(new Map([
            ['IMG_0001.jpg', 'root-copy'],
            ['album-a/IMG_0001.jpg', 'album-a-copy'],
            ['album-b/IMG_0001.jpg', 'album-b-copy'],
        ]));
        // The actual guarantee that matters for resume: re-deriving each file
        // from the shared directory handle via its own relativePath must
        // fetch the SAME distinguishing content, not whichever same-named
        // file a basename-only lookup happened to find first.
        for (const [relativePath, expectedContent] of Array.from(contentByPath.entries())) {
            const resolved = await resolveFileFromDirectoryEntry(root, relativePath);
            expect(await resolved?.text()).toBe(expectedContent);
        }
    });

    it('skips dotfile-named files and entire dotfile-named subdirectories', async () => {
        const root = fakeDir('root', [
            fakeFile('.DS_Store'),
            fakeFile('IMG_0001.jpg'),
            fakeDir('.Trashes', [fakeFile('deleted.jpg')]),
        ]);
        vi.stubGlobal('window', { ...window, showDirectoryPicker: vi.fn().mockResolvedValue(root) });
        const picked = await pickUploadFolderViaFileSystemAccess();
        expect(picked!.files.map((entry) => entry.relativePath)).toEqual(['IMG_0001.jpg']);
    });

    it('returns null (not a partial result) when the user cancels or the walk throws partway through', async () => {
        const abort = new DOMException('cancelled', 'AbortError');
        vi.stubGlobal('window', { ...window, showDirectoryPicker: vi.fn().mockRejectedValue(abort) });
        expect(await pickUploadFolderViaFileSystemAccess()).toBeNull();

        const root = fakeDir('root', [
            fakeFile('IMG_0001.jpg'),
            { ...fakeFile('IMG_0002.jpg'), getFile: async () => { throw new Error('mid-walk failure'); } },
        ]);
        vi.stubGlobal('window', { ...window, showDirectoryPicker: vi.fn().mockResolvedValue(root) });
        expect(await pickUploadFolderViaFileSystemAccess()).toBeNull();
    });
});

describe('ensureDirectoryReadPermission', () => {
    it('skips requestPermission when already granted', async () => {
        const dir = fakeDir('root', []);
        expect(await ensureDirectoryReadPermission(dir)).toBe(true);
        expect(dir.requestPermission).not.toHaveBeenCalled();
    });

    it('requests permission when not yet granted, and reflects the result', async () => {
        const dir = fakeDir('root', [], {
            queryPermission: vi.fn().mockResolvedValue('prompt'),
            requestPermission: vi.fn().mockResolvedValue('denied'),
        });
        expect(await ensureDirectoryReadPermission(dir)).toBe(false);
        expect(dir.requestPermission).toHaveBeenCalledTimes(1);
    });
});

describe('resolveFileFromDirectoryEntry', () => {
    it('walks nested segments via getDirectoryHandle then getFileHandle on the last one', async () => {
        const target = fakeFile('IMG_0002.cr3');
        const root = fakeDir('root', [fakeDir('2026-10-29', [fakeDir('raw', [target])])]);
        const file = await resolveFileFromDirectoryEntry(root, '2026-10-29/raw/IMG_0002.cr3');
        expect(file?.name).toBe('IMG_0002.cr3');
    });

    it('resolves a root-level file directly on the given handle, with no getDirectoryHandle calls', async () => {
        const target = fakeFile('IMG_0001.jpg');
        const root = fakeDir('root', [target]);
        const file = await resolveFileFromDirectoryEntry(root, 'IMG_0001.jpg');
        expect(file?.name).toBe('IMG_0001.jpg');
    });

    it('returns null instead of throwing when a path segment cannot be found', async () => {
        const root = fakeDir('root', []);
        expect(await resolveFileFromDirectoryEntry(root, 'missing/IMG_0001.jpg')).toBeNull();
    });
});
