import { describe, expect, it } from 'vitest';
import { initialsFor, plural } from './format';

describe('plural', () => {
    it('keeps the noun singular for exactly one', () => {
        expect(plural(1, 'photo')).toBe('1 photo');
        expect(plural(0, 'photo')).toBe('0 photos');
        expect(plural(15, 'member')).toBe('15 members');
    });
});

describe('initialsFor', () => {
    it('drops an email’s domain before taking initials', () => {
        expect(initialsFor('rajatverma57@gmail.com')).toBe('RA');
        expect(initialsFor('rajat.verma@example.com')).toBe('RV');
    });

    it('uses the first two words of a name', () => {
        expect(initialsFor('Rajat Verma')).toBe('RV');
        expect(initialsFor('  rajat ')).toBe('RA');
    });

    it('falls back to a placeholder when there is nothing to use', () => {
        expect(initialsFor('')).toBe('?');
        expect(initialsFor('@gmail.com')).toBe('?');
    });
});
