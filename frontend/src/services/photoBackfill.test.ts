import { beforeEach, describe, expect, it, vi } from 'vitest';
import { postAdmin } from './apiClient';
import { queuePhotoBackfill } from './photoBackfill';
import { ApiError } from './apiError';

vi.mock('./apiClient', () => ({ postAdmin: vi.fn() }));

describe('queuePhotoBackfill', () => {
    beforeEach(() => vi.resetAllMocks());

    it('queues sequential pages and accumulates progress, including failures', async () => {
        vi.mocked(postAdmin)
            .mockResolvedValueOnce({ queued: 8, skipped: 1, failed: 1, complete: false, continuation: 'a.jpg' })
            .mockResolvedValueOnce({ queued: 3, skipped: 0, failed: 0, complete: true, continuation: null, processingMode: 'backend' });
        const progress = vi.fn();
        expect(await queuePhotoBackfill(['ocr'], progress)).toEqual({ queued: 11, skipped: 1, failed: 1, processingMode: 'backend' });
        expect(postAdmin).toHaveBeenNthCalledWith(2, '/api/admin/backfill/photos', {
            repair: true, confirm: 'BACKFILL_ALL_PHOTOS', steps: ['ocr'], continuation: 'a.jpg',
        });
        expect(progress).toHaveBeenNthCalledWith(1, { queued: 8, skipped: 1, failed: 1 });
    });

    it('does not repeat completed pages when a subsequent request fails', async () => {
        vi.mocked(postAdmin)
            .mockResolvedValueOnce({ queued: 10, complete: false, continuation: 'a.jpg' })
            .mockRejectedValueOnce(new Error('offline'));
        await expect(queuePhotoBackfill(undefined, vi.fn())).rejects.toThrow('10 photos queued');
        expect(postAdmin).toHaveBeenCalledTimes(2);
    });

    it('stops if the cursor does not advance', async () => {
        vi.mocked(postAdmin).mockResolvedValue({ queued: 1, complete: false, continuation: 'same.jpg' });
        await expect(queuePhotoBackfill(undefined, vi.fn())).rejects.toThrow('advancing continuation');
        expect(postAdmin).toHaveBeenCalledTimes(2);
    });

    it('includes partial queueing reported by a failed storage page', async () => {
        vi.mocked(postAdmin).mockRejectedValue(new ApiError({
            kind: 'server', status: 503, message: 'Storage unavailable', rawMessage: 'Storage unavailable',
            retriable: true, requestId: 'test', responseData: { queued: 2, skipped: 1, failed: 1 },
        }));
        await expect(queuePhotoBackfill(undefined, vi.fn())).rejects.toThrow('2 photos queued, 1 skipped, 1 failed');
    });

    it('supports the older single-response API', async () => {
        vi.mocked(postAdmin).mockResolvedValue({ queued: 20, skipped: 2 });
        expect(await queuePhotoBackfill(undefined, vi.fn())).toMatchObject({ queued: 20, skipped: 2, failed: 0 });
        expect(postAdmin).toHaveBeenCalledTimes(1);
    });
});