// Download library: copy every original straight from Blob Storage into a folder the user picked.
// See docs/library-download-dedicated.md. This file is the transfer itself and touches no browser
// API directly: the folder, the network, the hasher and the clock are handed in, so every rule here
// (resume, verify, retry, token renewal) is unit-tested against fakes.
//
// Rules:
// - A file is done when it exists in the folder with the manifest's size. Files are written
//   atomically (they appear on close), so existing means complete; nothing else records progress.
// - A file larger than SEGMENT_BYTES is fetched in ranges, each range its own atomic file under
//   STATE_DIR, then joined. A restart loses at most one segment per connection.
// - Bytes are hashed while they stream. A hash that differs from the manifest is fetched once more:
//   the same bytes twice means storage is consistent and the recorded hash is not of these bytes
//   (kept, counted as unverified); different bytes means a bad transfer (retried).

export const STATE_DIR = '.keepsake-download';
export const SEGMENT_BYTES = 64 * 1024 * 1024;
export const CONNECTIONS = 6;
const FILE_ATTEMPTS = 5;
const NETWORK_ATTEMPTS = 20;
const TOKEN_MARGIN_MS = 30 * 60 * 1000;
const RATE_WINDOW_MS = 60 * 1000;

export interface Entry {
    name: string;
    blob: string;
    path: string;
    size?: number;
    sha256?: string;
    livePhotoOf?: string;
    caption?: string;
    rating?: number;
    liked?: boolean;
    people?: string[];
}

export interface Token {
    baseUrl: string;
    sas: string;
    expiresAt: string;
}

export interface Sink {
    write(chunk: Uint8Array): Promise<void>;
    close(): Promise<void>;
    abort(): Promise<void>;
}

export interface Folder {
    /** Size of the file at `path`, or null when it does not exist. */
    size(path: string): Promise<number | null>;
    /** A new file that appears at `path` only when the sink is closed. */
    create(path: string): Promise<Sink>;
    /** Reads the file at `path` in chunks. */
    read(path: string): Promise<ByteSource>;
    /** Removes a file or a directory with everything in it; missing is fine. */
    remove(path: string): Promise<void>;
}

export interface ByteSource {
    /** The next chunk, or null at the end. */
    read(): Promise<Uint8Array | null>;
    cancel(): Promise<void>;
}

export interface FetchResult {
    status: number;
    body: ByteSource;
}

export interface Hasher {
    update(chunk: Uint8Array): void;
    digest(): string;
}

export interface Deps {
    folder: Folder;
    fetchBlob(url: string, range: [number, number] | null, signal: AbortSignal): Promise<FetchResult>;
    getToken(): Promise<Token>;
    newHasher(): Promise<Hasher>;
    now(): number;
    sleep(ms: number, signal: AbortSignal): Promise<void>;
    connections?: number;
    segmentBytes?: number;
}

export interface Failure {
    name: string;
    path: string;
    message: string;
}

export interface Progress {
    filesTotal: number;
    filesDone: number;
    bytesTotal: number;
    bytesDone: number;
    /** Files kept although their bytes do not match the hash recorded at upload. */
    unverified: number;
    failed: Failure[];
    /** Bytes per second over the last minute of actual transfer. */
    rate: number;
    etaSeconds: number | null;
}

export class HttpError extends Error {
    status: number;

    constructor(status: number) {
        super(status === 404 ? 'The original is missing from storage' : `Storage answered ${status}`);
        this.status = status;
    }
}

class VerifyError extends Error {
    actual: string;

    constructor(message: string, actual = '') {
        super(message);
        this.actual = actual;
    }
}

const isAbort = (error: unknown): boolean => (error as { name?: string } | null)?.name === 'AbortError';

/** Large files first (their tail would otherwise run on one connection), the rest in manifest order. */
export const order = (entries: Entry[], segmentBytes = SEGMENT_BYTES): Entry[] => {
    const large = entries.filter((e) => (e.size || 0) > segmentBytes).sort((a, b) => (b.size || 0) - (a.size || 0));
    return large.concat(entries.filter((e) => (e.size || 0) <= segmentBytes));
};

export const segments = (size: number, segmentBytes = SEGMENT_BYTES): Array<[number, number]> => {
    const out: Array<[number, number]> = [];
    for (let start = 0; start < size; start += segmentBytes) {
        out.push([start, Math.min(size, start + segmentBytes) - 1]);
    }
    return out;
};

export const blobUrl = (token: Token, blob: string): string => (
    `${token.baseUrl.replace(/\/$/, '')}/${encodeURIComponent(blob)}?${token.sas}`
);

const isDone = (entry: Entry, existing: number | null): boolean => (
    existing !== null && (entry.size === undefined ? existing > 0 : existing === entry.size)
);

export const run = async (
    entries: Entry[], deps: Deps, signal: AbortSignal, onProgress: (progress: Progress) => void,
): Promise<Progress> => {
    const segmentBytes = deps.segmentBytes || SEGMENT_BYTES;
    const queue = order(entries, segmentBytes);
    const progress: Progress = {
        filesTotal: entries.length, filesDone: 0, bytesDone: 0, unverified: 0, failed: [], rate: 0, etaSeconds: null,
        bytesTotal: entries.reduce((sum, e) => sum + (e.size || 0), 0),
    };
    const samples: Array<[number, number]> = [];
    let transferred = 0;
    let lastEmit = 0;

    const emit = (force = false): void => {
        const now = deps.now();
        if (!force && now - lastEmit < 500) return;
        lastEmit = now;
        while (samples.length > 1 && now - samples[0][0] > RATE_WINDOW_MS) samples.shift();
        const span = samples.length > 1 ? (now - samples[0][0]) / 1000 : 0;
        progress.rate = span > 0 ? (transferred - samples[0][1]) / span : 0;
        const left = progress.bytesTotal - progress.bytesDone;
        progress.etaSeconds = progress.rate > 0 && left > 0 ? Math.round(left / progress.rate) : null;
        onProgress({ ...progress, failed: progress.failed.slice() });
    };

    const count = (bytes: number, fromNetwork: boolean): void => {
        progress.bytesDone += bytes;
        if (fromNetwork) {
            transferred += bytes;
            samples.push([deps.now(), transferred]);
        }
        emit();
    };

    let token: Token | null = null;
    let tokenRequest: Promise<Token> | null = null;
    const currentToken = async (force = false): Promise<Token> => {
        const stale = !token || Date.parse(token.expiresAt) - deps.now() < TOKEN_MARGIN_MS;
        if (!force && !stale && token) return token;
        if (!tokenRequest) {
            tokenRequest = deps.getToken().then((fresh) => {
                token = fresh;
                tokenRequest = null;
                return fresh;
            }, (error) => {
                tokenRequest = null;
                throw error;
            });
        }
        return tokenRequest;
    };

    /** Streams one response into a sink. Returns the byte count; the sink is left open. */
    const pump = async (
        entry: Entry, range: [number, number] | null, sink: Sink, hasher: Hasher | null, counted: { bytes: number },
    ): Promise<number> => {
        const response = await deps.fetchBlob(blobUrl(await currentToken(), entry.blob), range, signal);
        if (response.status !== 200 && response.status !== 206) {
            await response.body.cancel().catch(() => undefined);
            throw new HttpError(response.status);
        }
        if (range && response.status !== 206) {
            await response.body.cancel().catch(() => undefined);
            throw new VerifyError('Storage ignored the byte range');
        }
        let bytes = 0;
        for (;;) {
            const chunk = await response.body.read();
            if (!chunk) break;
            if (hasher) hasher.update(chunk);
            await sink.write(chunk);
            bytes += chunk.byteLength;
            counted.bytes += chunk.byteLength;
            count(chunk.byteLength, true);
        }
        return bytes;
    };

    const checkHash = (entry: Entry, hasher: Hasher | null, mismatches: Map<string, string>): boolean => {
        if (!hasher || !entry.sha256) return true;
        const actual = hasher.digest();
        if (actual === entry.sha256) return true;
        if (mismatches.get(entry.blob) === actual) return false; // same bytes twice: keep, unverified
        mismatches.set(entry.blob, actual);
        throw new VerifyError('Downloaded bytes do not match the recorded hash', actual);
    };

    const mismatches = new Map<string, string>();

    const whole = async (entry: Entry, counted: { bytes: number }): Promise<boolean> => {
        const hasher = entry.sha256 ? await deps.newHasher() : null;
        const sink = await deps.folder.create(entry.path);
        try {
            const bytes = await pump(entry, null, sink, hasher, counted);
            if (entry.size !== undefined && bytes !== entry.size) {
                throw new VerifyError(`Got ${bytes} bytes, expected ${entry.size}`);
            }
            const verified = checkHash(entry, hasher, mismatches);
            await sink.close();
            return verified;
        } catch (error) {
            await sink.abort().catch(() => undefined);
            throw error;
        }
    };

    const segmented = async (entry: Entry, counted: { bytes: number }): Promise<boolean> => {
        const size = entry.size as number;
        const dir = `${STATE_DIR}/parts/${entry.blob}`;
        const ranges = segments(size, segmentBytes);
        for (let i = 0; i < ranges.length; i += 1) {
            const [start, end] = ranges[i];
            const part = `${dir}/${i}`;
            if ((await deps.folder.size(part)) === end - start + 1) {
                counted.bytes += end - start + 1;
                count(end - start + 1, false); // kept from an earlier run
                continue;
            }
            const sink = await deps.folder.create(part);
            try {
                const bytes = await pump(entry, [start, end], sink, null, counted);
                if (bytes !== end - start + 1) throw new VerifyError(`Segment ${i} was ${bytes} bytes`);
                await sink.close();
            } catch (error) {
                await sink.abort().catch(() => undefined);
                throw error;
            }
        }
        const hasher = entry.sha256 ? await deps.newHasher() : null;
        const sink = await deps.folder.create(entry.path);
        try {
            for (let i = 0; i < ranges.length; i += 1) {
                const source = await deps.folder.read(`${dir}/${i}`);
                for (;;) {
                    const chunk = await source.read();
                    if (!chunk) break;
                    if (hasher) hasher.update(chunk);
                    await sink.write(chunk);
                }
            }
            let verified = true;
            try {
                verified = checkHash(entry, hasher, mismatches);
            } catch (error) {
                await deps.folder.remove(dir); // the segments are what was wrong: fetch them again
                throw error;
            }
            await sink.close();
            await deps.folder.remove(dir);
            return verified;
        } catch (error) {
            await sink.abort().catch(() => undefined);
            throw error;
        }
    };

    const one = async (entry: Entry): Promise<void> => {
        const existing = await deps.folder.size(entry.path);
        if (isDone(entry, existing)) {
            progress.filesDone += 1;
            count(entry.size === undefined ? 0 : entry.size, false);
            return;
        }
        let failures = 0;
        let networkFailures = 0;
        for (;;) {
            const counted = { bytes: 0 };
            try {
                const useSegments = entry.size !== undefined && entry.size > segmentBytes;
                const verified = await (useSegments ? segmented(entry, counted) : whole(entry, counted));
                if (!verified) progress.unverified += 1;
                progress.filesDone += 1;
                emit();
                return;
            } catch (error) {
                if (signal.aborted || isAbort(error)) throw error;
                progress.bytesDone -= counted.bytes; // this attempt's bytes are fetched again
                const status = error instanceof HttpError ? error.status : 0;
                if (status === 403) {
                    await currentToken(true); // the token expired mid-run
                } else if (status === 404) {
                    throw error; // retrying cannot bring it back
                }
                const network = !(error instanceof HttpError) && !(error instanceof VerifyError);
                if (network) networkFailures += 1; else failures += 1;
                if (failures >= FILE_ATTEMPTS || networkFailures >= NETWORK_ATTEMPTS) throw error;
                const attempt = network ? networkFailures : failures;
                await deps.sleep(Math.min(30000, 1000 * 2 ** (attempt - 1)), signal);
            }
        }
    };

    let next = 0;
    const worker = async (): Promise<void> => {
        while (!signal.aborted) {
            const entry = queue[next];
            next += 1;
            if (!entry) return;
            try {
                await one(entry);
            } catch (error) {
                if (signal.aborted || isAbort(error)) return;
                progress.failed.push({
                    name: entry.name, path: entry.path,
                    message: error instanceof Error ? error.message : String(error),
                });
                emit(true);
            }
        }
    };

    const workers: Array<Promise<void>> = [];
    for (let i = 0; i < Math.min(deps.connections || CONNECTIONS, Math.max(queue.length, 1)); i += 1) {
        workers.push(worker());
    }
    await Promise.all(workers);
    emit(true);
    return { ...progress, failed: progress.failed.slice() };
};
