import { describe, expect, it, vi } from 'vitest';

describe('server-paged libraries', () => {
    it('does not download or retry a sort index the server says is too large', async () => {
        vi.resetModules();
        const get = vi.fn(async () => ({ available: false, reason: 'library_too_large', rowCount: 1_200_000 }));
        vi.doMock('./apiClient', () => ({ get }));
        const fetchSpy = vi.fn();
        vi.stubGlobal('fetch', fetchSpy);
        const { getLocalSortIndex, isServerPagedLibrary, getCachedSortThumb } = await import('./localSortIndex');
        expect(isServerPagedLibrary()).toBe(false);
        expect(await getLocalSortIndex()).toBeNull();
        expect(isServerPagedLibrary()).toBe(true);
        expect(fetchSpy).not.toHaveBeenCalled();           // nothing to download
        expect(getCachedSortThumb('a.jpg')).toBe('');      // other grids fall back to the thumbnail names the API returns
        vi.doUnmock('./apiClient');
    });
});
