import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const getMock = vi.fn();
vi.mock('../../services/apiClient', () => ({ get: (...a: unknown[]) => getMock(...a) }));
vi.mock('../../services/passwordAuthClient', () => ({ getActiveLibraryFromToken: () => 'lib-1' }));
vi.mock('../../services/mediaToken', () => ({ getCachedMediaToken: () => null, thumbnailUrlForBlob: () => '' }));

import { __resetAskForTests, getAskState, loadMoreAsk, runAskSearch, setAskQuery } from './askSearchStore';

const photo = (n: number) => ({ filename: `p${n}.jpg` });

describe('askSearchStore', () => {
    beforeEach(() => { getMock.mockReset(); __resetAskForTests(); });
    afterEach(() => { __resetAskForTests(); });

    it('keeps running and keeps its results with no page mounted', async () => {
        let release: (v: unknown) => void = () => undefined;
        getMock.mockReturnValueOnce(new Promise((resolve) => { release = resolve; }));
        runAskSearch('beach');
        expect(getAskState().searching).toBe(true);
        release({ photos: [photo(1), photo(2)], total: 2, hasMore: false });
        await vi.waitFor(() => expect(getAskState().searching).toBe(false));
        expect(getAskState().results.map((r) => r.id)).toEqual(['p1.jpg', 'p2.jpg']);
        expect(getAskState().query).toBe('beach');
    });

    it('appends further pages and ignores a page that lands after a new search', async () => {
        getMock.mockResolvedValueOnce({ photos: [photo(1)], total: 2, hasMore: true });
        runAskSearch('dog');
        await vi.waitFor(() => expect(getAskState().searching).toBe(false));
        let late: (v: unknown) => void = () => undefined;
        getMock.mockReturnValueOnce(new Promise((resolve) => { late = resolve; }));
        loadMoreAsk();
        getMock.mockResolvedValueOnce({ photos: [photo(9)], total: 1, hasMore: false });
        runAskSearch('cat');
        late({ photos: [photo(2)], total: 2, hasMore: false });
        await vi.waitFor(() => expect(getAskState().searching).toBe(false));
        expect(getAskState().results.map((r) => r.id)).toEqual(['p9.jpg']);
    });

    it('clearing the box empties the results', async () => {
        getMock.mockResolvedValueOnce({ photos: [photo(1)], total: 1, hasMore: false });
        runAskSearch('x');
        await vi.waitFor(() => expect(getAskState().results).toHaveLength(1));
        setAskQuery('');
        expect(getAskState().results).toEqual([]);
        expect(getAskState().activeQuery).toBe('');
    });
});
