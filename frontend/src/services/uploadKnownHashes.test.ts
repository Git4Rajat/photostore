import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), span: vi.fn() }));
vi.mock('./apiClient', () => ({ getUpload: mocks.get, postUpload: mocks.post }));
vi.mock('./perf', () => ({ perf: { recordSpan: mocks.span } }));

import { fetchKnownUploadHashes, requestUploadHashRefresh, UPLOAD_HASH_DOWNLOAD_TIMEOUT_MS, UPLOAD_HASH_DOWNLOAD_ATTEMPTS } from './uploadKnownHashes';

beforeEach(() => {
    vi.useFakeTimers();
    vi.clearAllMocks();
});
afterEach(() => vi.useRealTimers());

describe('upload-end hash refresh', () => {
    it.each(['completed', 'stopped', 'failed', 'interrupted', 'discarded'] as const)('requests a background build for %s', async (reason) => {
        mocks.post.mockResolvedValue({ status: 'queued' });
        await requestUploadHashRefresh(reason);
        expect(mocks.post).toHaveBeenCalledWith('/upload/known-hashes/refresh', { reason }, { timeout: 15000 });
    });

    it('does not turn refresh failure into upload failure', async () => {
        mocks.post.mockRejectedValue(new Error('offline'));
        await expect(requestUploadHashRefresh('completed')).resolves.toBeUndefined();
    });
});

describe('background upload hash download', () => {
    it('returns hashes quickly, with no unnecessary retries or leftover timer', async () => {
        mocks.get.mockResolvedValue({ hashes: { abc: 'photo.heic' } });
        expect(await fetchKnownUploadHashes()).toEqual({ abc: 'photo.heic' });
        expect(mocks.get).toHaveBeenCalledWith('/upload/known-hashes', expect.objectContaining({
            singleAttempt: true, timeout: UPLOAD_HASH_DOWNLOAD_TIMEOUT_MS,
        }));
        expect(vi.getTimerCount()).toBe(0);
    });

    it('bounds each hung attempt and stops after a few retries', async () => {
        mocks.get.mockImplementation(() => new Promise(() => {}));
        const promise = fetchKnownUploadHashes();
        await vi.advanceTimersByTimeAsync(UPLOAD_HASH_DOWNLOAD_TIMEOUT_MS * UPLOAD_HASH_DOWNLOAD_ATTEMPTS + 7000);
        expect(await promise).toEqual({});
        expect(mocks.get).toHaveBeenCalledTimes(UPLOAD_HASH_DOWNLOAD_ATTEMPTS);
        expect(mocks.get.mock.calls.every(([, config]) => config.signal.aborted)).toBe(true);
        expect(mocks.span).toHaveBeenCalledWith('upload.known_hashes.timeout', expect.any(Number), {
            rows: 0, cached: false, attempt: 0,
        });
        expect(vi.getTimerCount()).toBe(0);
    });

    it('accepts a download taking longer than three seconds', async () => {
        let resolve!: (value: unknown) => void;
        mocks.get.mockImplementation(() => new Promise((done) => { resolve = done; }));
        const promise = fetchKnownUploadHashes();
        await vi.advanceTimersByTimeAsync(10000);
        expect(mocks.get.mock.calls[0][1].signal.aborted).toBe(false);
        resolve({ hashes: { late: 'already-stored.heic' } });
        expect(await promise).toEqual({ late: 'already-stored.heic' });
        expect(mocks.span).toHaveBeenCalledTimes(1);
    });

    it('stops after bounded retries on network failure', async () => {
        mocks.get.mockRejectedValue(new Error('offline'));
        const promise = fetchKnownUploadHashes();
        await vi.advanceTimersByTimeAsync(7000);
        expect(await promise).toEqual({});
        expect(mocks.get).toHaveBeenCalledTimes(UPLOAD_HASH_DOWNLOAD_ATTEMPTS);
        expect(vi.getTimerCount()).toBe(0);
    });

    it('retries an unavailable snapshot and uses it once built', async () => {
        mocks.get.mockResolvedValueOnce({ hashes: {}, available: false })
            .mockResolvedValueOnce({ hashes: { ready: 'photo.heic' } });
        const promise = fetchKnownUploadHashes();
        await vi.advanceTimersByTimeAsync(2000);
        expect(await promise).toEqual({ ready: 'photo.heic' });
        expect(mocks.get).toHaveBeenCalledTimes(2);
        expect(mocks.span).toHaveBeenCalledWith('upload.known_hashes', expect.any(Number), {
            rows: 0, cached: false, attempt: 0,
        });
    });

    it('cancels a hung transport without retrying or accepting its late result', async () => {
        let resolve!: (value: unknown) => void;
        mocks.get.mockImplementation(() => new Promise((done) => { resolve = done; }));
        const controller = new AbortController();
        const promise = fetchKnownUploadHashes(controller.signal);
        controller.abort();
        expect(await promise).toEqual({});
        resolve({ hashes: { late: 'deleted.heic' } });
        await Promise.resolve();
        expect(mocks.get).toHaveBeenCalledTimes(1);
        expect(vi.getTimerCount()).toBe(0);
    });

    it('cancels during retry backoff without making another request', async () => {
        mocks.get.mockRejectedValue(new Error('offline'));
        const controller = new AbortController();
        const promise = fetchKnownUploadHashes(controller.signal);
        await vi.advanceTimersByTimeAsync(100);
        controller.abort();
        expect(await promise).toEqual({});
        expect(mocks.get).toHaveBeenCalledTimes(1);
        expect(vi.getTimerCount()).toBe(0);
    });

    it('does not start a request when already cancelled', async () => {
        const controller = new AbortController();
        controller.abort();
        expect(await fetchKnownUploadHashes(controller.signal)).toEqual({});
        expect(mocks.get).not.toHaveBeenCalled();
    });

    it('ignores an expired attempt arriving after a retry succeeds', async () => {
        let resolveOld!: (value: unknown) => void;
        mocks.get.mockImplementationOnce(() => new Promise((resolve) => { resolveOld = resolve; }))
            .mockResolvedValueOnce({ hashes: { fresh: 'fresh.heic' } });
        const promise = fetchKnownUploadHashes();
        await vi.advanceTimersByTimeAsync(UPLOAD_HASH_DOWNLOAD_TIMEOUT_MS + 2000);
        expect(await promise).toEqual({ fresh: 'fresh.heic' });
        resolveOld({ hashes: { stale: 'deleted.heic' } });
        await Promise.resolve();
        expect(mocks.span).toHaveBeenCalledTimes(2);
        expect(vi.getTimerCount()).toBe(0);
    });

    it.each([{}, { hashes: [] }, { hashes: 'invalid' }])('ignores malformed responses %j', async (response) => {
        mocks.get.mockResolvedValue(response);
        expect(await fetchKnownUploadHashes()).toEqual({});
    });
});
