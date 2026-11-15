import { describe, expect, it } from 'vitest';
import { NAV_ITEMS } from './components/HomeChrome';

describe('primary navigation', () => {
    it('leads with Photos, Search and Albums, the three phone tabs', () => {
        expect(NAV_ITEMS.slice(0, 3).map((item) => item.label)).toEqual(['Photos', 'Search', 'Albums']);
    });

    it('keeps Tools and Sharing out of the primary navigation', () => {
        const ids = NAV_ITEMS.map((item) => item.id as string);
        expect(ids).not.toContain('tools');
        expect(ids).not.toContain('sharing');
    });
});
