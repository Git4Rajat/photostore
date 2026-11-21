import { describe, expect, it } from 'vitest';
import { FINALIZE_BATCH_MAX_SIZE, FINALIZE_SLOW_CALL_MS, finalizeBatchWindow } from './finalizeBatchPolicy';

describe('finalizeBatchWindow', () => {
    it('keeps the short window when nothing is in flight and the last call was fast', () => {
        expect(finalizeBatchWindow({ inFlight: 0, lastCallMs: 800 })).toEqual({ collectMs: 100, maxWaitMs: 500 });
    });

    it('keeps the short window before any call has finished', () => {
        expect(finalizeBatchWindow({ inFlight: 0, lastCallMs: null }).collectMs).toBe(100);
    });

    it('collects longer while a call is already in flight', () => {
        expect(finalizeBatchWindow({ inFlight: 1, lastCallMs: 200 })).toEqual({ collectMs: 1000, maxWaitMs: 2000 });
    });

    it('collects longer after a slow call even when none is in flight', () => {
        expect(finalizeBatchWindow({ inFlight: 0, lastCallMs: FINALIZE_SLOW_CALL_MS }).collectMs).toBe(1000);
    });

    it('treats a call just under the slow limit as fast', () => {
        expect(finalizeBatchWindow({ inFlight: 0, lastCallMs: FINALIZE_SLOW_CALL_MS - 1 }).collectMs).toBe(100);
    });

    it('never waits longer than the max-wait cap for the same mode', () => {
        for (const load of [{ inFlight: 0, lastCallMs: null }, { inFlight: 3, lastCallMs: 9000 }]) {
            const w = finalizeBatchWindow(load);
            expect(w.collectMs).toBeLessThanOrEqual(w.maxWaitMs);
        }
    });

    it('survives bad numbers instead of stalling uploads', () => {
        expect(finalizeBatchWindow({ inFlight: NaN, lastCallMs: NaN })).toEqual({ collectMs: 100, maxWaitMs: 500 });
        expect(finalizeBatchWindow({ inFlight: -1, lastCallMs: -5 }).collectMs).toBe(100);
    });

    it('keeps the batch size cap at the backend limit', () => {
        expect(FINALIZE_BATCH_MAX_SIZE).toBe(24);
    });
});
