import { readBlobArrayBuffer } from './blobIo';

export const LARGE_FILE_HASH_THRESHOLD = 64 * 1024 * 1024;
export const FILE_HASH_CHUNK_BYTES = 8 * 1024 * 1024;

// WebCrypto requires a contiguous input buffer. Large local files can fail
// that read with NotReadableError even though each smaller slice is readable.
// Feed the original bytes, in order, to one incremental SHA-256 state.
export const hashLargeFile = async (file: Blob, signal?: AbortSignal): Promise<string> => {
    const checkAbort = () => {
        if (signal?.aborted) throw new DOMException('Upload hashing stopped.', 'AbortError');
    };
    checkAbort();
    const { createSHA256 } = await import('hash-wasm');
    const hasher = await createSHA256();
    hasher.init();
    for (let offset = 0; offset < file.size; offset += FILE_HASH_CHUNK_BYTES) {
        checkAbort();
        const end = Math.min(offset + FILE_HASH_CHUNK_BYTES, file.size);
        const bytes = await readBlobArrayBuffer(file.slice(offset, end));
        checkAbort();
        if (bytes.byteLength !== end - offset) throw new Error('File changed or could not be fully read. Please reselect it.');
        hasher.update(new Uint8Array(bytes));
    }
    checkAbort();
    return hasher.digest('hex');
};

// The duplicate check hashes a whole selection before any upload session
// exists. A read that never resolves (an iCloud "Optimize Mac Storage"
// placeholder, a sleeping network or external drive) used to hold its hashing
// worker forever; four of them froze a 2,092-file check on Chrome for macOS
// with nothing reaching the server. Each file now gets a limit that grows with
// its size, generous enough for a slow disk (10 MB/s) on top of a fixed base.
export const FILE_HASH_TIMEOUT_BASE_MS = 60_000;
export const FILE_HASH_TIMEOUT_MS_PER_MB = 100;

export const fileHashTimeoutMs = (sizeBytes: number): number => (
    FILE_HASH_TIMEOUT_BASE_MS + Math.ceil(Math.max(0, sizeBytes) / (1024 * 1024)) * FILE_HASH_TIMEOUT_MS_PER_MB
);

export class FileHashTimeoutError extends Error {
    constructor(timeoutMs: number) {
        super(`File took longer than ${Math.round(timeoutMs / 1000)}s to read.`);
        this.name = 'FileHashTimeoutError';
    }
}

// The stalled read itself can't be cancelled; it is abandoned so the caller's
// worker can move on to the next file.
export const withFileHashTimeout = <T>(work: Promise<T>, timeoutMs: number): Promise<T> => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    const timeout = new Promise<never>((_, reject) => {
        timer = setTimeout(() => reject(new FileHashTimeoutError(timeoutMs)), timeoutMs);
    });
    return Promise.race([work, timeout]).finally(() => clearTimeout(timer));
};
