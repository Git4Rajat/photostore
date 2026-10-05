import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const getMock = vi.fn();
vi.mock('./apiClient', () => ({ get: (...args: unknown[]) => getMock(...args) }));

import { __resetIndexBuildingForTests, onIndexReady, reportIndexBuilding } from './indexBuilding';

// useIndexBuilding is a thin useSyncExternalStore wrapper; the behaviour lives in report/poll.
describe('indexBuilding', () => {
    beforeEach(() => { vi.useFakeTimers(); getMock.mockReset(); __resetIndexBuildingForTests(); });
    afterEach(() => { __resetIndexBuildingForTests(); vi.useRealTimers(); });

    it('polls index-status while the library builds, then clears and tells listeners once it is ready', async () => {
        getMock
            .mockResolvedValueOnce({ ready: true, building: true })
            .mockResolvedValueOnce({ ready: true, building: false });
        const ready = vi.fn();
        onIndexReady(ready);
        reportIndexBuilding('gallery', true);

        await vi.advanceTimersByTimeAsync(8000);
        expect(getMock).toHaveBeenCalledTimes(1);
        expect(ready).not.toHaveBeenCalled();                    // still building

        await vi.advanceTimersByTimeAsync(8000);
        expect(getMock).toHaveBeenCalledTimes(2);
        expect(ready).toHaveBeenCalledTimes(1);

        await vi.advanceTimersByTimeAsync(60000);
        expect(getMock).toHaveBeenCalledTimes(2);                // stopped polling
    });

    it('does not poll for sources the page retries itself (people)', async () => {
        reportIndexBuilding('people', true);
        await vi.advanceTimersByTimeAsync(30000);
        expect(getMock).not.toHaveBeenCalled();
    });
});
