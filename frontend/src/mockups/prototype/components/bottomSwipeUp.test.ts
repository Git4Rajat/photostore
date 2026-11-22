import { describe, expect, it } from 'vitest';
import { isOpenSwipe, startsInBottomZone, START_ZONE_PX, SYSTEM_EDGE_PX } from './bottomSwipeUp';

describe('startsInBottomZone', () => {
    it('accepts a start above the system edge and within the zone', () => {
        expect(startsInBottomZone(800 - 60, 800)).toBe(true);
        expect(startsInBottomZone(800 - START_ZONE_PX, 800)).toBe(true);
    });
    it('rejects the home-indicator strip and starts higher up', () => {
        expect(startsInBottomZone(800 - SYSTEM_EDGE_PX, 800)).toBe(false);
        expect(startsInBottomZone(799, 800)).toBe(false);
        expect(startsInBottomZone(800 - START_ZONE_PX - 1, 800)).toBe(false);
    });
});

describe('isOpenSwipe', () => {
    it('opens on a long, mostly vertical upward drag', () => {
        expect(isOpenSwipe(10, -80, 400)).toBe(true);
    });
    it('opens on a short fast flick but not a short slow drag', () => {
        expect(isOpenSwipe(0, -40, 60)).toBe(true);
        expect(isOpenSwipe(0, -40, 600)).toBe(false);
    });
    it('ignores downward, tiny and diagonal or horizontal drags', () => {
        expect(isOpenSwipe(0, 80, 200)).toBe(false);
        expect(isOpenSwipe(0, -10, 10)).toBe(false);
        expect(isOpenSwipe(80, -80, 200)).toBe(false);
        expect(isOpenSwipe(120, -20, 100)).toBe(false);
    });
});
