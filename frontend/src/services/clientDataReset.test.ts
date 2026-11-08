import { afterEach, describe, expect, it, vi } from 'vitest';
import { clearLibraryClientData } from './clientDataReset';

describe('clearLibraryClientData', () => {
    afterEach(() => vi.unstubAllGlobals());

    it('wipes the device-local home snapshot along with the other library caches', () => {
        const deleteDatabase = vi.fn();
        vi.stubGlobal('indexedDB', { deleteDatabase });
        clearLibraryClientData();
        const names = deleteDatabase.mock.calls.map((c) => c[0]);
        expect(names).toContain('photostore-home-snapshot');
        expect(names).toContain('photostore-sort-index');
    });
});
