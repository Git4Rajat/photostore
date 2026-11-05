import { describe, expect, it, vi } from 'vitest';
import { ApiError } from '../../services/apiError';
import {
    BACKFILL_MAX_BATCHES, describeQueueResult, describeStatusResult, failureReason, runBackfillBatches,
    type BackfillBatch,
} from './toolsActions';

const apiError = (kind: 'client' | 'server' | 'unreachable', message: string, status?: number) =>
    new ApiError({ kind, message, rawMessage: message, status, retriable: kind !== 'client', requestId: 'r1' });

describe('failureReason', () => {
    it('shows the backend message for a 4xx so the user learns why', () => {
        expect(failureReason(apiError('client', 'This library processes photos through upload waves', 409), 'Couldn’t start backfill'))
            .toBe('Couldn’t start backfill: This library processes photos through upload waves');
    });

    it('keeps the generic text for server and network failures', () => {
        expect(failureReason(apiError('server', 'Something went wrong on our side'), 'Couldn’t start backfill')).toBe('Couldn’t start backfill');
        expect(failureReason(apiError('unreachable', 'offline'), 'Couldn’t start backfill')).toBe('Couldn’t start backfill');
    });
});

describe('describeQueueResult', () => {
    it('is a success when the job was queued, or when the body says nothing', () => {
        expect(describeQueueResult('Face dedupe', { success: true, queued: true, status: 'queued' })).toEqual({ outcome: 'success', message: 'Face dedupe queued' });
        expect(describeQueueResult('Face dedupe', undefined).outcome).toBe('success');
    });

    it('reports a failure when a 200 response says nothing was queued', () => {
        const result = describeQueueResult('Face dedupe', { success: false, queued: false, status: 'unavailable' });
        expect(result).toEqual({ outcome: 'failed', message: 'Face dedupe wasn’t queued (unavailable)', detail: 'unavailable' });
        expect(describeQueueResult('Vector index rebuild', { queued: false }).outcome).toBe('failed');
    });
});

describe('describeStatusResult', () => {
    const messages = { queued: 'Sent pending photos to server processing', waveOwned: 'Wave-owned message' };

    it('only claims work was sent when the status is queued', () => {
        expect(describeStatusResult('queued', messages)).toEqual({ outcome: 'success', message: messages.queued });
        expect(describeStatusResult(undefined, messages).outcome).toBe('success');
        expect(describeStatusResult('unavailable', messages)).toMatchObject({ outcome: 'failed', message: 'Nothing was sent (unavailable)' });
    });

    it('explains a wave-owned library instead of claiming photos were sent', () => {
        expect(describeStatusResult('wave_owned', messages)).toEqual({ outcome: 'success', message: 'Wave-owned message', detail: 'wave_owned' });
        expect(describeStatusResult('wave_owned', { queued: 'x' }).outcome).toBe('failed');
    });
});

describe('runBackfillBatches', () => {
    const reason = (error: unknown) => `reason: ${(error as Error).message}`;
    const pages = (...batches: BackfillBatch[]) => {
        const post = vi.fn();
        batches.forEach((b) => post.mockResolvedValueOnce(b));
        return post;
    };

    it('follows the continuation cursor until the backend says complete', async () => {
        const post = pages(
            { queued: 10, skipped: 0, failed: 0, complete: false, continuation: 'p09.jpg' },
            { queued: 7, skipped: 2, failed: 0, complete: false, continuation: 'p19.jpg' },
            { queued: 3, skipped: 0, failed: 0, complete: true, continuation: null },
        );
        const progress = vi.fn();
        const result = await runBackfillBatches(post, progress, reason);
        expect(result).toEqual({ outcome: 'success', message: 'Backfill: 20 queued, 2 skipped, 0 failed', detail: 'Backfill: 20 queued, 2 skipped, 0 failed' });
        expect(post.mock.calls.map(([body]) => body.continuation)).toEqual([undefined, 'p09.jpg', 'p19.jpg']);
        expect(post.mock.calls[0][0]).toMatchObject({ repair: true, confirm: 'BACKFILL_ALL_PHOTOS' });
        expect(progress).toHaveBeenCalledTimes(3);
        expect(progress).toHaveBeenLastCalledWith({ queued: 20, skipped: 2, failed: 0, batches: 3 });
    });

    it('is partial when some photos failed but others were queued', async () => {
        const result = await runBackfillBatches(pages({ queued: 8, failed: 2, complete: true }), vi.fn(), reason);
        expect(result.outcome).toBe('partial');
        expect(result.message).toBe('Backfill: 8 queued, 0 skipped, 2 failed');
    });

    it('stops at the first batch where every photo failed instead of hammering the rest of the library', async () => {
        const post = pages({ queued: 0, failed: 10, complete: false, continuation: 'p09.jpg' });
        const result = await runBackfillBatches(post, vi.fn(), reason);
        expect(post).toHaveBeenCalledTimes(1);
        expect(result.outcome).toBe('failed');
        expect(result.message).toBe('Backfill: 0 queued, 0 skipped, 10 failed — stopped, every photo in the last batch failed');
    });

    it('surfaces the reason when the very first request is refused', async () => {
        const post = vi.fn().mockRejectedValue(new Error('wave owned'));
        expect(await runBackfillBatches(post, vi.fn(), reason)).toEqual({ outcome: 'failed', message: 'reason: wave owned', detail: 'reason: wave owned' });
    });

    it('keeps what was already queued when a later request fails', async () => {
        const post = vi.fn()
            .mockResolvedValueOnce({ queued: 10, complete: false, continuation: 'a' })
            .mockRejectedValueOnce(new Error('metadata scan failed'));
        const result = await runBackfillBatches(post, vi.fn(), reason);
        expect(result.outcome).toBe('partial');
        expect(result.message).toBe('Backfill: 10 queued, 0 skipped, 0 failed — reason: metadata scan failed');
    });

    it('does not loop forever when the backend gives no continuation but is not complete', async () => {
        const post = pages({ queued: 10, complete: false, continuation: null });
        const result = await runBackfillBatches(post, vi.fn(), reason);
        expect(post).toHaveBeenCalledTimes(1);
        expect(result.outcome).toBe('partial');
        expect(result.message).toContain('stopped, no way to continue');
    });

    it('has a hard cap on batches', async () => {
        const post = vi.fn().mockResolvedValue({ queued: 10, complete: false, continuation: 'next' });
        const result = await runBackfillBatches(post, vi.fn(), reason);
        expect(post).toHaveBeenCalledTimes(BACKFILL_MAX_BATCHES);
        expect(result.message).toContain('stopped at the batch limit');
    });
});
