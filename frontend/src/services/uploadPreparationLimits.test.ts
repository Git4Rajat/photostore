import {BASE_PREPARATION_LIMITS, FAST_PREPARATION_LIMITS, uploadPreparationLimits} from './uploadPreparationLimits';

describe('uploadPreparationLimits', () => {
    it('raises decode, admission and PUT limits together on a capable desktop', () => {
        expect(uploadPreparationLimits({parallel: true, constrained: false, cores: 10})).toBe(FAST_PREPARATION_LIMITS);
        expect(uploadPreparationLimits({parallel: true, constrained: false})).toBe(FAST_PREPARATION_LIMITS);
    });

    it('keeps two decoders on phones, tablets and low-memory devices', () => {
        const limits = uploadPreparationLimits({parallel: true, constrained: true, cores: 8});
        expect(limits).toBe(BASE_PREPARATION_LIMITS);
        expect(limits.decoders).toBe(2);
    });

    it('keeps the old limits on machines with few cores', () => {
        expect(uploadPreparationLimits({parallel: true, constrained: false, cores: 4})).toBe(BASE_PREPARATION_LIMITS);
    });

    it('restores the old limits when the switch is off', () => {
        expect(uploadPreparationLimits({parallel: false, constrained: false, cores: 10})).toBe(BASE_PREPARATION_LIMITS);
    });

    it('never admits more files than decoders plus queued decodes, which would skip preparation as busy', () => {
        for (const limits of [BASE_PREPARATION_LIMITS, FAST_PREPARATION_LIMITS]) {
            expect(limits.admission).toBeLessThanOrEqual(limits.decoders + limits.queuedDecodes);
        }
    });
});
