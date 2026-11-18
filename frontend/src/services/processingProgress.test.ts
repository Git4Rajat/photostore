import { describe, expect, it } from 'vitest';
import { clampMonotonic, etaLabel, overallFraction, stepNumber, takingLonger, type ProcessingWave } from './processingProgress';

const wave = (extra: Partial<ProcessingWave>): ProcessingWave => ({
    waveId: 'w', stage: 'vision', state: 'processing', startedAt: 0, stageEnteredAt: 0, ...extra,
});

describe('processing progress', () => {
    it('counts vision by its fraction and later stages by completion', () => {
        expect(overallFraction(wave({ done: 50, total: 100 }))).toBeCloseTo(0.35);
        expect(overallFraction(wave({ stage: 'cluster' }))).toBeCloseTo(0.7);
        expect(overallFraction(wave({ stage: 'index' }))).toBeCloseTo(0.85);
        expect(overallFraction(wave({ stage: 'done' }))).toBe(1);
        expect(overallFraction(wave({ done: 100, total: 100 }))).toBeLessThan(1);
    });

    it('never moves the shown fraction backwards', () => {
        const shown: Record<string, number> = {};
        expect(clampMonotonic(shown, wave({ done: 60, total: 100 }))).toBeCloseTo(0.42);
        expect(clampMonotonic(shown, wave({ done: 20, total: 100 }))).toBeCloseTo(0.42);
    });

    it('labels time coarsely and omits it when unknown', () => {
        expect(etaLabel(null)).toBeNull();
        expect(etaLabel(-1)).toBeNull();
        expect(etaLabel(45)).toBe('less than a minute left');
        expect(etaLabel(300)).toBe('about 5 min left');
        expect(etaLabel(7200)).toBe('about an hour or more left');
    });

    it('numbers the steps and flags a stage that overran its estimate', () => {
        expect(stepNumber('vision')).toBe(1);
        expect(stepNumber('index')).toBe(3);
        expect(takingLonger(wave({ etaSeconds: 300, stageEnteredAt: 0 }), 700_000)).toBe(true);
        expect(takingLonger(wave({ etaSeconds: 300, stageEnteredAt: 0 }), 200_000)).toBe(false);
        expect(takingLonger(wave({ etaSeconds: null }), 9_999_999)).toBe(false);
    });
});
