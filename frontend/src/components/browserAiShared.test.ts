import { afterEach, describe, expect, it, vi } from 'vitest';
import { MB, getAdaptiveUploadProfile } from './browserAiShared';

const setConnection = (connection: Record<string, unknown> | undefined) => {
    Object.defineProperty(navigator, 'connection', { value: connection, configurable: true });
};

const setDeviceMemory = (value: number | undefined) => {
    Object.defineProperty(navigator, 'deviceMemory', { value, configurable: true });
};

const setMatchMedia = (matches: { mobile?: boolean; coarse?: boolean }) => {
    window.matchMedia = vi.fn().mockImplementation((query: string) => ({
        matches: query.includes('max-width') ? Boolean(matches.mobile) : Boolean(matches.coarse),
    })) as unknown as typeof window.matchMedia;
};

const setResourceTiming = (entries: Array<{ transferSize: number; requestStart: number; responseEnd: number }>) => {
    vi.spyOn(performance, 'getEntriesByType').mockReturnValue(entries as unknown as PerformanceEntryList);
};

const bigFile = (size: number) => [{ size }];

describe('getAdaptiveUploadProfile', () => {
    afterEach(() => {
        setConnection(undefined);
        setDeviceMemory(undefined);
        setMatchMedia({});
        vi.restoreAllMocks();
    });

    it('throttles hard on data-saver / 2g', () => {
        setConnection({ saveData: true });
        setMatchMedia({});
        setResourceTiming([]);
        expect(getAdaptiveUploadProfile(bigFile(1 * MB)).fileParallelism).toBe(1);
    });

    it('keeps mobile viewports conservative regardless of reported downlink', () => {
        setConnection({ effectiveType: '4g', downlink: 50, rtt: 20, type: 'wifi' });
        setMatchMedia({ mobile: true });
        expect(getAdaptiveUploadProfile(bigFile(1 * MB)).fileParallelism).toBe(2);
    });

    it('raises the ceiling for a confirmed fast wired/wifi connection', () => {
        setConnection({ downlink: 50, rtt: 20, type: 'ethernet' });
        setMatchMedia({});
        const profile = getAdaptiveUploadProfile(bigFile(1 * MB));
        expect(profile.fileParallelism).toBe(20);
        expect(profile.reason).toMatch(/reliable wired/);
    });

    it('uses a lower ceiling for large files even on a fast connection', () => {
        setConnection({ downlink: 50, rtt: 20, type: 'wifi' });
        setMatchMedia({});
        const profile = getAdaptiveUploadProfile(bigFile(100 * MB));
        expect(profile.fileParallelism).toBe(12);
    });

    it('reaches the top tier via a measured RTT when the Network Information API is unavailable', () => {
        setConnection(undefined);
        setMatchMedia({});
        setResourceTiming([{ transferSize: 20 * MB, requestStart: 0, responseEnd: 1000 }]); // ~160Mbps observed
        const profile = getAdaptiveUploadProfile(bigFile(1 * MB), { measuredRttMs: 40 });
        expect(profile.fileParallelism).toBe(20);
        expect(profile.reason).toMatch(/measured round trip/);
    });

    it('does not use a slow measured RTT as evidence of a bad connection', () => {
        setConnection(undefined);
        setMatchMedia({});
        setResourceTiming([{ transferSize: 20 * MB, requestStart: 0, responseEnd: 1000 }]);
        // A slow warm-up call could just mean the backend container cold-started
        // -- it should never be treated as proof the network itself is bad, so
        // this should land in the ordinary "good network" tier, not get downgraded.
        const profile = getAdaptiveUploadProfile(bigFile(1 * MB), { measuredRttMs: 5000 });
        expect(profile.reason).toBe('good network');
        expect(profile.fileParallelism).toBe(6);
    });

    it('falls back to a real bandwidth probe when the Network Information API is unavailable (e.g. Safari)', () => {
        setConnection(undefined);
        setMatchMedia({});
        // 20MB in 1s ~= 160Mbps observed downlink -> should land in the
        // "good network" tier (>= 8 Mbps) rather than the blind desktop guess.
        setResourceTiming([{ transferSize: 20 * MB, requestStart: 0, responseEnd: 1000 }]);
        const profile = getAdaptiveUploadProfile(bigFile(1 * MB));
        expect(profile.reason).toBe('good network');
        expect(profile.fileParallelism).toBe(6);
    });

    it('ignores cached (transferSize 0) resource timing entries when probing', () => {
        setConnection(undefined);
        setMatchMedia({});
        setResourceTiming([{ transferSize: 0, requestStart: 0, responseEnd: 1000 }]);
        const profile = getAdaptiveUploadProfile(bigFile(1 * MB));
        expect(profile.reason).toMatch(/no network info available/);
    });

    it('floors the chunk size for huge batches without cutting parallelism', () => {
        setConnection(undefined);
        // Mobile tier normally uses 2MB chunks; none of these files individually
        // trip the "large file" threshold (40MB), so parallelism should be
        // unaffected by the batch-size floor -- only chunkSizeBytes should move.
        setMatchMedia({ mobile: true });
        const manyFiles = Array.from({ length: 5 }, () => ({ size: 35 * MB })); // 175MB total
        const profile = getAdaptiveUploadProfile(manyFiles);
        expect(profile.fileParallelism).toBe(2);
        expect(profile.chunkSizeBytes).toBeGreaterThanOrEqual(16 * MB);
    });
});
