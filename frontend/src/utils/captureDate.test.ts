import { describe, expect, it } from 'vitest';
import { captureWallClock } from './captureDate';

const clock = (d: Date | null) => d && [d.getFullYear(), d.getMonth() + 1, d.getDate(), d.getHours(), d.getMinutes()];

describe('captureWallClock', () => {
    it('keeps a camera time with an offset on the clock where it was taken, whatever the viewer zone', () => {
        expect(clock(captureWallClock('2021-09-07T23:32:34+05:30'))).toEqual([2021, 9, 7, 23, 32]);
        expect(clock(captureWallClock('2024-01-01T00:02:07.250000-05:00'))).toEqual([2024, 1, 1, 0, 2]);
    });

    it('shows a time with no recorded zone as written', () => {
        expect(clock(captureWallClock('2015-05-10T22:59:21'))).toEqual([2015, 5, 10, 22, 59]);
    });

    it('keeps UTC instants in the viewer zone, as before', () => {
        for (const iso of ['2026-11-10T21:53:14+00:00', '2026-11-10T21:53:14.764000+00:00', '2026-11-10T21:53:14Z']) {
            expect(captureWallClock(iso)?.getTime()).toBe(new Date(iso).getTime());
        }
    });

    it('returns null for missing or unreadable dates', () => {
        expect(captureWallClock(null)).toBeNull();
        expect(captureWallClock('')).toBeNull();
        expect(captureWallClock('not a date')).toBeNull();
    });
});
