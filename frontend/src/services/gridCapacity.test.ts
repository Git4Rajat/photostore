import { describe, expect, it } from 'vitest';
import { chunk, computeGridCapacity, MAX_PAGE_SIZE, MIN_PAGE_SIZE, pageSizeForCapacity } from './gridCapacity';

describe('gridCapacity', () => {
    it('counts a partially visible last row as a full row', () => {
        // 10 columns of 120px tiles + 6px gap = 126px pitch; 800px tall -> 7 rows (6.35 rounds up)
        expect(computeGridCapacity({ columns: 10, tileWidth: 120, gap: 6, viewportHeight: 800 })).toBe(70);
    });

    it('never returns less than one row of at least one tile', () => {
        expect(computeGridCapacity({ columns: 0, tileWidth: 0, gap: 0, viewportHeight: 0 })).toBe(1);
    });

    it('sizes a page to a few screens, clamped to sane bounds', () => {
        expect(pageSizeForCapacity(70)).toBe(210);
        expect(pageSizeForCapacity(5)).toBe(MIN_PAGE_SIZE);
        expect(pageSizeForCapacity(100000)).toBe(MAX_PAGE_SIZE);
    });

    it('chunks lists for parallel enrichment', () => {
        expect(chunk([1, 2, 3, 4, 5], 2)).toEqual([[1, 2], [3, 4], [5]]);
        expect(chunk([], 100)).toEqual([]);
    });
});
