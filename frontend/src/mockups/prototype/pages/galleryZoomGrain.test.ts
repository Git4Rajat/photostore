import { describe, expect, it } from 'vitest';
import { headingGrainFor, MONTH_GROUP_BELOW, YEAR_GROUP_BELOW } from './GalleryPage';

describe('headingGrainFor', () => {
    it('coarsens day -> month -> year as tiles shrink, and reverses on the way back', () => {
        expect(headingGrainFor(140)).toBe('day');
        expect(headingGrainFor(MONTH_GROUP_BELOW)).toBe('day');
        expect(headingGrainFor(MONTH_GROUP_BELOW - 1)).toBe('month');
        expect(headingGrainFor(YEAR_GROUP_BELOW)).toBe('month');
        expect(headingGrainFor(YEAR_GROUP_BELOW - 1)).toBe('year');
        expect(headingGrainFor(24)).toBe('year');
    });
});
