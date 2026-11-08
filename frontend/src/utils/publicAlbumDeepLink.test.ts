import { describe, expect, it } from 'vitest';
import { photoFromDeepLink } from './publicAlbumDeepLink';

describe('photoFromDeepLink', () => {
    it('returns the index when that photo is loaded', () => {
        expect(photoFromDeepLink('?photo=3', 120)).toBe(3);
        expect(photoFromDeepLink('?x=1&photo=0', 120)).toBe(0);
    });

    it('ignores a missing, malformed or not-yet-loaded index', () => {
        expect(photoFromDeepLink('', 120)).toBeNull();
        expect(photoFromDeepLink('?photo=', 120)).toBeNull();
        expect(photoFromDeepLink('?photo=-1', 120)).toBeNull();
        expect(photoFromDeepLink('?photo=1.5', 120)).toBeNull();
        expect(photoFromDeepLink('?photo=abc', 120)).toBeNull();
        expect(photoFromDeepLink('?photo=120', 120)).toBeNull();
        expect(photoFromDeepLink('?photo=0', 0)).toBeNull();
    });
});
