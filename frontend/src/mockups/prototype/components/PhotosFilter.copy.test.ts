import { describe, expect, it } from 'vitest';
import { noMatchCopy, type PhotosFilterState } from './PhotosFilter';

const base: PhotosFilterState = { mediaFilter: 'all', likedOnly: false, rating: 0, captureRange: null };

describe('noMatchCopy', () => {
    it('names the filters that caused an empty result, as the redesign draws it', () => {
        const state = { ...base, mediaFilter: 'video' as const, likedOnly: true, captureRange: { start: '2026-10-19', end: '2026-11-18', label: 'Last 30 days' } };
        expect(noMatchCopy(state)).toEqual({ title: 'No liked videos lately', hint: 'Try a longer time range, or take a filter off.' });
    });

    it('covers rating and custom date ranges', () => {
        const state = { ...base, rating: 4, captureRange: { start: '2024-01-01', end: '2024-01-31', label: '1 Jan 2024 – 31 Jan 2024' } };
        expect(noMatchCopy(state).title).toBe('No photos rated 4+ stars in 1 Jan 2024 – 31 Jan 2024');
    });

    it('suggests a rating change when no date range is on', () => {
        expect(noMatchCopy({ ...base, rating: 5 }).hint).toBe('Try a different rating, or take a filter off.');
    });
});
