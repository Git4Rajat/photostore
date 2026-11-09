import { describe, expect, it } from 'vitest';
import { canRetry, describeWave } from './StuckWavesCard';

describe('StuckWavesCard helpers', () => {
    it('offers retry only for a failed or unqueued stage', () => {
        expect(canRetry({ state: 'processing', health: { state: 'failed', stage: 'vision' } })).toBe(true);
        expect(canRetry({ state: 'ready', health: { state: 'pending', reason: 'dispatch_missing', stage: 'cluster' } })).toBe(true);
        expect(canRetry({ state: 'ready', health: { state: 'blocked', stage: 'cluster', blockedByWaveId: 'w1' } })).toBe(false);
        expect(canRetry({ state: 'processing', health: { state: 'processing', stage: 'vision' } })).toBe(false);
        const idle = { state: 'processing_pending', health: { state: 'pending', reason: 'dispatch_pending', stage: 'vision', ownerActive: false } };
        expect(canRetry({ ...idle, health: { ...idle.health, noProgressSeconds: 3600 } })).toBe(true);
        expect(canRetry({ ...idle, health: { ...idle.health, noProgressSeconds: 60 } })).toBe(false);
        expect(canRetry({ ...idle, health: { ...idle.health, noProgressSeconds: 3600, ownerActive: true } })).toBe(false);
    });

    it('explains blocked and failed waves', () => {
        expect(describeWave({ state: 'processing', health: { state: 'failed', stage: 'vision', dispatchAttempts: 6 } }))
            .toBe('Failed in vision after 6 attempts');
        expect(describeWave({ state: 'ready', health: { state: 'blocked', blockedByWaveId: 'w1' } }))
            .toBe('Waiting for an earlier wave (w1) to publish');
    });
});
