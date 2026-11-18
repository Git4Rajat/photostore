import { describe, expect, it } from 'vitest';
import { albumDisplayName } from './albumName';

describe('albumDisplayName', () => {
    it('drops the smart-album kind before the name', () => {
        expect(albumDisplayName('Location: Malmö, Sweden')).toBe('Malmö, Sweden');
        expect(albumDisplayName('Person: Asha Verma')).toBe('Asha Verma');
        expect(albumDisplayName('Uploaded: Jan 1')).toBe('Jan 1');
        expect(albumDisplayName('Tag/Object: Mountain')).toBe('Mountain');
    });
    it('leaves ordinary names alone', () => {
        expect(albumDisplayName('Trip: day one')).toBe('Trip: day one');
        expect(albumDisplayName('Location')).toBe('Location');
        expect(albumDisplayName('Person: ')).toBe('Person: ');
        expect(albumDisplayName(undefined)).toBe('');
    });
});
