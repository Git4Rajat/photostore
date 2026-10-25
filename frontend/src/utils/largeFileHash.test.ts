// @vitest-environment node
import { createHash } from 'node:crypto';
import { describe, expect, it, vi } from 'vitest';
import { FILE_HASH_CHUNK_BYTES, hashLargeFile } from './largeFileHash';

describe('incremental upload SHA-256', () => {
    it('matches SHA-256 across chunk boundaries without reading the whole file', async () => {
        const bytes = new Uint8Array(FILE_HASH_CHUNK_BYTES + 137);
        for (let i = 0; i < bytes.length; i++) bytes[i] = (i * 31) % 251;
        const file = new Blob([bytes]);
        const wholeRead = vi.spyOn(file, 'arrayBuffer').mockRejectedValue(new DOMException('Too large', 'NotReadableError'));
        const slices = vi.spyOn(file, 'slice');
        expect(await hashLargeFile(file)).toBe(createHash('sha256').update(bytes).digest('hex'));
        expect(wholeRead).not.toHaveBeenCalled();
        expect(slices.mock.calls).toEqual([[0, FILE_HASH_CHUNK_BYTES], [FILE_HASH_CHUNK_BYTES, bytes.length]]);
    });

    it('hashes empty input correctly', async () => {
        expect(await hashLargeFile(new Blob([]))).toBe(createHash('sha256').digest('hex'));
    });

    it('stops before reading another chunk when aborted during a read', async () => {
        const controller = new AbortController();
        const file = new Blob([new Uint8Array(FILE_HASH_CHUNK_BYTES + 1)]);
        const slice = vi.spyOn(file, 'slice').mockImplementation(() => ({
            arrayBuffer: async () => { controller.abort(); return new ArrayBuffer(FILE_HASH_CHUNK_BYTES); },
        } as Blob));
        await expect(hashLargeFile(file, controller.signal)).rejects.toMatchObject({ name: 'AbortError' });
        expect(slice).toHaveBeenCalledTimes(1);
    });

    it('does not read an already aborted file', async () => {
        const controller = new AbortController();
        controller.abort();
        const file = new Blob(['abc']);
        const slice = vi.spyOn(file, 'slice');
        await expect(hashLargeFile(file, controller.signal)).rejects.toMatchObject({ name: 'AbortError' });
        expect(slice).not.toHaveBeenCalled();
    });

    it('preserves genuine file-read errors', async () => {
        const error = new DOMException('Access lost', 'NotReadableError');
        const file = new Blob(['abc']);
        const chunk = new Blob(['abc']);
        vi.spyOn(chunk, 'arrayBuffer').mockRejectedValue(error);
        vi.spyOn(file, 'slice').mockReturnValue(chunk);
        await expect(hashLargeFile(file)).rejects.toBe(error);
    });

    it('rejects short reads instead of hashing incomplete content', async () => {
        const file = new Blob(['abc']);
        vi.spyOn(file, 'slice').mockReturnValue(new Blob(['a']));
        await expect(hashLargeFile(file)).rejects.toThrow('could not be fully read');
    });
});
