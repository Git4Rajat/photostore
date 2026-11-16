// Download library, browser side: picks (or reopens) the destination folder, fetches the manifest
// once, and runs the engine. One download at a time, owned by this module rather than a page, so it
// keeps running while the user moves around the app. See docs/library-download-dedicated.md.
import * as library from '../libraryClient';
import { createBrowserDb } from '../browserStore';
import { getActiveLibraryFromToken } from '../passwordAuthClient';
import {
    ByteSource, Entry, Folder, HttpError, Progress, STATE_DIR, run,
} from './engine';
import {
    DirectoryHandle, canWrite, folderOver, isFolderDownloadSupported, pickFolder,
} from './folder';

export { isFolderDownloadSupported };
export type { Progress };

export type Status = 'idle' | 'starting' | 'running' | 'stopped' | 'done' | 'error';

export interface State {
    status: Status;
    folderName: string;
    progress: Progress | null;
    /** Why it stopped, when status is 'error'. */
    message: string;
}

const db = createBrowserDb('keepsake-library-download', 1, [{ name: 'folders' }]);

const savedHandle = (libraryId: string): Promise<DirectoryHandle | null> => db.run<DirectoryHandle | null>(
    'folders', 'readonly',
    (store) => new Promise((resolve, reject) => {
        const request = store.get(libraryId);
        request.onsuccess = () => resolve((request.result as DirectoryHandle | undefined) || null);
        request.onerror = () => reject(request.error);
    }),
    null,
);

const saveHandle = (libraryId: string, handle: DirectoryHandle): Promise<void> => db.run<void>(
    'folders', 'readwrite', (store) => { store.put(handle, libraryId); }, undefined,
);

let state: State = { status: 'idle', folderName: '', progress: null, message: '' };
let controller: AbortController | null = null;
const listeners = new Set<(state: State) => void>();

const set = (patch: Partial<State>): void => {
    state = { ...state, ...patch };
    listeners.forEach((listener) => listener(state));
};

export const getState = (): State => state;

export const subscribe = (listener: (state: State) => void): (() => void) => {
    listeners.add(listener);
    return () => { listeners.delete(listener); };
};

/** The folder an earlier download of this library went to, if the browser still remembers it. */
export const rememberedFolderName = async (): Promise<string> => {
    const libraryId = getActiveLibraryFromToken();
    const handle = libraryId ? await savedHandle(libraryId) : null;
    return handle ? handle.name : '';
};

const reading = (stream: ReadableStream<Uint8Array> | null): ByteSource => {
    const reader = stream ? stream.getReader() : null;
    return {
        read: async () => {
            if (!reader) return null;
            const { done, value } = await reader.read();
            return done ? null : (value as Uint8Array);
        },
        cancel: async () => { if (reader) await reader.cancel(); },
    };
};

export const parseManifest = async (source: ByteSource): Promise<{ entries: Entry[]; complete: boolean }> => {
    const decoder = new TextDecoder();
    const entries: Entry[] = [];
    let complete = false;
    let pending = '';
    const take = (line: string): void => {
        if (!line.trim()) return;
        const item = JSON.parse(line);
        if (item.end) complete = true; else entries.push(item as Entry);
    };
    for (;;) {
        const chunk = await source.read();
        pending += chunk ? decoder.decode(chunk, { stream: true }) : decoder.decode();
        const lines = pending.split('\n');
        pending = chunk ? (lines.pop() as string) : '';
        lines.forEach(take);
        if (!chunk) break;
    }
    return { entries, complete };
};

const writeJson = async (folder: Folder, path: string, value: unknown): Promise<void> => {
    const sink = await folder.create(path);
    try {
        await sink.write(new TextEncoder().encode(JSON.stringify(value, null, 1)));
        await sink.close();
    } catch (error) {
        await sink.abort().catch(() => undefined);
        throw error;
    }
};

const sleep = (ms: number, signal: AbortSignal): Promise<void> => new Promise((resolve, reject) => {
    if (signal.aborted) {
        reject(new DOMException('Aborted', 'AbortError'));
        return;
    }
    const timer = setTimeout(() => { signal.removeEventListener('abort', onAbort); resolve(); }, ms);
    const onAbort = () => { clearTimeout(timer); reject(new DOMException('Aborted', 'AbortError')); };
    signal.addEventListener('abort', onAbort);
});

// Keeps the screen (and so the machine) awake during a transfer that can run for a day.
const keepAwake = (): (() => void) => {
    const wakeLock = (navigator as unknown as { wakeLock?: { request(type: 'screen'): Promise<{ release(): Promise<void> }> } }).wakeLock;
    let lock: { release(): Promise<void> } | null = null;
    let released = false;
    const acquire = (): void => {
        if (!wakeLock || released || document.visibilityState !== 'visible') return;
        wakeLock.request('screen').then((held) => {
            if (released) void held.release(); else lock = held;
        }).catch(() => undefined);
    };
    acquire();
    document.addEventListener('visibilitychange', acquire); // the browser drops the lock when hidden
    return () => {
        released = true;
        document.removeEventListener('visibilitychange', acquire);
        if (lock) void lock.release().catch(() => undefined);
    };
};

// library.json: what a folder of files cannot say by itself.
const libraryJson = async (entries: Entry[]): Promise<unknown> => {
    const info = await library.getLibraryDownloadInfo();
    const people = new Map(info.people.map((p) => [p.id, p.name] as [string, string]));
    const paths = new Map(entries.map((e) => [e.name, e.path] as [string, string]));
    return {
        library: info.libraryName,
        exportedAt: new Date().toISOString(),
        albums: info.albums.map((album) => ({
            name: album.name,
            photos: album.photos.map((name) => paths.get(name)).filter(Boolean),
        })),
        photos: entries.filter((e) => e.caption || e.rating || e.liked || e.people || e.livePhotoOf).map((e) => ({
            path: e.path,
            ...(e.caption ? { caption: e.caption } : {}),
            ...(e.rating ? { rating: e.rating } : {}),
            ...(e.liked ? { favourite: true } : {}),
            ...(e.people ? { people: e.people.map((id) => people.get(id)).filter(Boolean) } : {}),
            ...(e.livePhotoOf ? { livePhotoOf: paths.get(e.livePhotoOf) || e.livePhotoOf } : {}),
        })),
    };
};

const transfer = async (handle: DirectoryHandle, signal: AbortSignal): Promise<void> => {
    const folder = folderOver(handle);
    const manifestPath = `${STATE_DIR}/manifest.json`;
    let entries: Entry[];
    try {
        const response = await library.getLibraryDownloadManifest(signal);
        const parsed = await parseManifest(reading(response.body));
        if (!parsed.complete) throw new Error('The library listing was cut short.');
        entries = parsed.entries;
        await writeJson(folder, manifestPath, entries);
    } catch (error) {
        if (signal.aborted) throw error;
        // The app may be unreachable while storage is not: carry on from the listing saved last time.
        if ((await folder.size(manifestPath)) === null) throw error;
        const saved = await folder.read(manifestPath);
        const chunks: Uint8Array[] = [];
        for (let chunk = await saved.read(); chunk; chunk = await saved.read()) chunks.push(chunk);
        entries = JSON.parse(await new Blob(chunks).text());
    }
    libraryJson(entries).then((value) => writeJson(folder, 'library.json', value)).catch(() => undefined);

    set({ status: 'running' });
    const { createSHA256 } = await import('hash-wasm');
    const result = await run(entries, {
        folder,
        getToken: library.getLibraryDownloadToken,
        fetchBlob: async (url, range, fetchSignal) => {
            const response = await fetch(url, {
                signal: fetchSignal, cache: 'no-store', credentials: 'omit',
                headers: range ? { Range: `bytes=${range[0]}-${range[1]}` } : undefined,
            });
            return { status: response.status, body: reading(response.body) };
        },
        newHasher: async () => {
            const hasher = await createSHA256();
            hasher.init();
            return { update: (chunk) => { hasher.update(chunk); }, digest: () => hasher.digest('hex') };
        },
        now: () => Date.now(),
        sleep,
    }, signal, (progress) => set({ progress }));

    if (result.failed.length) {
        await writeJson(folder, `${STATE_DIR}/failed.json`, result.failed);
    } else if (!signal.aborted) {
        await folder.remove(`${STATE_DIR}/failed.json`);
    }
    if (signal.aborted) {
        set({ status: 'stopped', progress: result });
    } else if (result.failed.length) {
        set({
            status: 'error', progress: result,
            message: `${result.failed.length} file${result.failed.length === 1 ? '' : 's'} could not be downloaded. Start again to retry them.`,
        });
    } else {
        set({ status: 'done', progress: result });
    }
};

/**
 * Starts or resumes the download. Must be called from a click: it may open the folder picker or ask
 * for permission to write. `chooseNew` ignores the remembered folder.
 */
export const start = async (chooseNew = false): Promise<void> => {
    if (state.status === 'starting' || state.status === 'running') return;
    const libraryId = getActiveLibraryFromToken();
    set({ status: 'starting', message: '', progress: null });
    let release: (() => void) | null = null;
    try {
        let handle = chooseNew || !libraryId ? null : await savedHandle(libraryId);
        if (handle && !(await canWrite(handle, true))) handle = null;
        if (!handle) handle = await pickFolder();
        if (!handle) {
            set({ status: 'idle' });
            return;
        }
        if (!(await canWrite(handle, true))) throw new Error('The browser was not allowed to write to that folder.');
        if (libraryId) await saveHandle(libraryId, handle);
        set({ folderName: handle.name });
        controller = new AbortController();
        release = keepAwake();
        await transfer(handle, controller.signal);
    } catch (error) {
        if (controller?.signal.aborted) {
            set({ status: 'stopped' });
        } else {
            const status = error instanceof HttpError ? ` (${error.status})` : '';
            set({ status: 'error', message: `${error instanceof Error ? error.message : String(error)}${status}` });
        }
    } finally {
        if (release) release();
        controller = null;
    }
};

/** Stops the transfer. Everything already saved stays; start() carries on from there. */
export const stop = (): void => {
    controller?.abort();
};
