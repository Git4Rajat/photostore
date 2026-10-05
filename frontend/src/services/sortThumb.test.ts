import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('./apiClient', () => ({
    get: vi.fn(async () => ({ available: true, indexUrl: 'https://acct.blob.core.windows.net/idx/sort.json', sourceVersion: 'v1' })),
}));

describe('getCachedSortThumb', () => {
    afterEach(() => vi.restoreAllMocks());

    it('is empty until the sort index loads, then maps filenames to thumbnail blob names', async () => {
        vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({
            rows: [
                { RowKey: 'a.jpg', filename: 'a.jpg', thumb: 'u1/a.jpg', rating: 0, likes: 0 },
                { RowKey: 'b.jpg', filename: 'b.jpg', rating: 0, likes: 0 },
            ],
        }))));
        const { getCachedSortThumb, getLocalSortIndex } = await import('./localSortIndex');
        expect(getCachedSortThumb('a.jpg')).toBe('');
        await getLocalSortIndex();
        expect(getCachedSortThumb('a.jpg')).toBe('u1/a.jpg');
        expect(getCachedSortThumb('b.jpg')).toBe('');   // no thumbnail generated yet -> caller falls back
        expect(getCachedSortThumb('missing.jpg')).toBe('');
    });
});
