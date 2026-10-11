import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ get: vi.fn(), span: vi.fn() }));
vi.mock('./apiClient', () => ({ getUpload: mocks.get }));
vi.mock('./perf', () => ({ perf: { recordSpan: mocks.span } }));

import { fetchKnownUploadHashes, UPLOAD_HASH_PREFETCH_TIMEOUT_MS } from './uploadKnownHashes';

beforeEach(() => {
    vi.useFakeTimers();
    vi.clearAllMocks();
});
afterEach(() => vi.useRealTimers());

describe('optional upload hash prefetch', () => {
    it('returns hashes quickly, with no retries and no leftover deadline timer', async () => {
        mocks.get.mockResolvedValue({ hashes: { abc: 'photo.heic' } });
        expect(await fetchKnownUploadHashes()).toEqual({ abc: 'photo.heic' });
        expect(mocks.get).toHaveBeenCalledWith('/upload/known-hashes', expect.objectContaining({
            singleAttempt: true, timeout: UPLOAD_HASH_PREFETCH_TIMEOUT_MS,
        }));
        expect(vi.getTimerCount()).toBe(0);
    });

    it('bounds a never-settling request even if it ignores abort', async () => {
        mocks.get.mockImplementation(() => new Promise(() => {}));
        const promise = fetchKnownUploadHashes();
        await vi.advanceTimersByTimeAsync(UPLOAD_HASH_PREFETCH_TIMEOUT_MS);
        expect(await promise).toEqual({});
        expect(mocks.get.mock.calls[0][1].signal.aborted).toBe(true);
        expect(mocks.span).toHaveBeenCalledWith('upload.known_hashes.timeout', expect.any(Number), {
            rows: 0, cached: false,
        });
    });

    it('ignores late results after timeout', async () => {
        let resolve!: (value: unknown) => void;
        mocks.get.mockImplementation(() => new Promise((done) => { resolve = done; }));
        const promise = fetchKnownUploadHashes();
        await vi.advanceTimersByTimeAsync(UPLOAD_HASH_PREFETCH_TIMEOUT_MS);
        const result = await promise;
        resolve({ hashes: { late: 'not-yet-stored.heic' } });
        await Promise.resolve();
        expect(result).toEqual({});
        expect(mocks.span).toHaveBeenCalledTimes(1);
    });

    it('continues without early skipping on network failure', async () => {
        mocks.get.mockRejectedValue(new Error('offline'));
        expect(await fetchKnownUploadHashes()).toEqual({});
        expect(mocks.get).toHaveBeenCalledTimes(1);
        expect(vi.getTimerCount()).toBe(0);
    });

    it.each([{}, { hashes: [] }, { hashes: 'invalid' }])('ignores malformed responses %j', async (response) => {
        mocks.get.mockResolvedValue(response);
        expect(await fetchKnownUploadHashes()).toEqual({});
    });
});
