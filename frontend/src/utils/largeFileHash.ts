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
