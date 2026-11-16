// The engine's Folder over the File System Access API (Chrome and Edge on desktop). A file handed
// out by createWritable() is written to a temporary file and swapped in on close(), which is what
// makes "exists in the folder" mean "complete" for the engine.
import type { ByteSource, Folder, Sink } from './engine';

// The subset of the API used here; the project's TypeScript (4.9) has no DOM types for it.
interface WritableStreamLike {
    write(data: Uint8Array): Promise<void>;
    close(): Promise<void>;
    abort(): Promise<void>;
}
interface FileHandle {
    getFile(): Promise<File>;
    createWritable(): Promise<WritableStreamLike>;
}
export interface DirectoryHandle {
    readonly name: string;
    getDirectoryHandle(name: string, options?: { create?: boolean }): Promise<DirectoryHandle>;
    getFileHandle(name: string, options?: { create?: boolean }): Promise<FileHandle>;
    removeEntry(name: string, options?: { recursive?: boolean }): Promise<void>;
    queryPermission(options: { mode: 'readwrite' }): Promise<PermissionState>;
    requestPermission(options: { mode: 'readwrite' }): Promise<PermissionState>;
}

type Picker = (options?: { mode?: 'readwrite'; id?: string }) => Promise<DirectoryHandle>;

const picker = (): Picker | undefined => (
    typeof window === 'undefined' ? undefined : (window as unknown as { showDirectoryPicker?: Picker }).showDirectoryPicker
);

export const isFolderDownloadSupported = (): boolean => typeof picker() === 'function';

/** Opens the folder picker. Resolves null when the user cancels. */
export const pickFolder = async (): Promise<DirectoryHandle | null> => {
    const show = picker();
    if (!show) return null;
    try {
        return await show.call(window, { mode: 'readwrite', id: 'keepsake-library-download' });
    } catch (error) {
        if ((error as { name?: string }).name === 'AbortError') return null;
        throw error;
    }
};

/** True when the page may write to the folder; asks the user when `ask` is set (needs a click). */
export const canWrite = async (handle: DirectoryHandle, ask: boolean): Promise<boolean> => {
    if ((await handle.queryPermission({ mode: 'readwrite' })) === 'granted') return true;
    return ask && (await handle.requestPermission({ mode: 'readwrite' })) === 'granted';
};

const missing = (error: unknown): boolean => {
    const name = (error as { name?: string } | null)?.name;
    return name === 'NotFoundError' || name === 'TypeMismatchError';
};

export const folderOver = (root: DirectoryHandle): Folder => {
    const directories = new Map<string, Promise<DirectoryHandle>>();

    const directory = (parts: string[], create: boolean): Promise<DirectoryHandle> => {
        if (!parts.length) return Promise.resolve(root);
        const key = parts.join('/');
        const cached = directories.get(key);
        if (cached) return cached;
        const found = directory(parts.slice(0, -1), create)
            .then((parent) => parent.getDirectoryHandle(parts[parts.length - 1], { create }));
        if (create) {
            directories.set(key, found);
            found.catch(() => directories.delete(key));
        }
        return found;
    };

    const split = (path: string): [string[], string] => {
        const parts = path.split('/').filter(Boolean);
        return [parts.slice(0, -1), parts[parts.length - 1]];
    };

    return {
        size: async (path) => {
            const [parents, name] = split(path);
            try {
                const parent = await directory(parents, false);
                return (await (await parent.getFileHandle(name)).getFile()).size;
            } catch (error) {
                if (missing(error)) return null;
                throw error;
            }
        },
        create: async (path): Promise<Sink> => {
            const [parents, name] = split(path);
            const parent = await directory(parents, true);
            const stream = await (await parent.getFileHandle(name, { create: true })).createWritable();
            return {
                write: (chunk) => stream.write(chunk),
                close: () => stream.close(),
                abort: () => stream.abort(),
            };
        },
        read: async (path): Promise<ByteSource> => {
            const [parents, name] = split(path);
            const file = await (await (await directory(parents, false)).getFileHandle(name)).getFile();
            const reader = file.stream().getReader();
            return {
                read: async () => {
                    const { done, value } = await reader.read();
                    return done ? null : (value as Uint8Array);
                },
                cancel: () => reader.cancel(),
            };
        },
        remove: async (path) => {
            const [parents, name] = split(path);
            try {
                await (await directory(parents, false)).removeEntry(name, { recursive: true });
            } catch (error) {
                if (!missing(error)) throw error;
            }
            Array.from(directories.keys()).forEach((key) => {
                if (key === path || key.startsWith(`${path}/`)) directories.delete(key);
            });
        },
    };
};
