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

const setResourceTiming = (entries: Array<{ transferSize: number; requestStart: number; responseEnd: number; startTime?: number }>) => {
    vi.spyOn(performance, 'getEntriesByType').mockReturnValue(
        entries.map((e) => ({ startTime: 0, ...e })) as unknown as PerformanceEntryList,
    );
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

    it('reaches the top tier on mobile too when there is strong real evidence (fast, low-latency wifi)', () => {
        setConnection({ effectiveType: '4g', downlink: 50, rtt: 20, type: 'wifi' });
        setMatchMedia({ mobile: true });
        expect(getAdaptiveUploadProfile(bigFile(1 * MB)).fileParallelism).toBe(20);
    });

    it('gives mobile a modest, non-top-tier profile when there is no network signal at all', () => {
        setConnection(undefined);
        setMatchMedia({ mobile: true });
        const profile = getAdaptiveUploadProfile(bigFile(1 * MB));
        expect(profile.fileParallelism).toBe(4);
        expect(profile.reason).toMatch(/mobile device, no network signal/);
    });

    it('keeps a low-memory device capped even on a fast, low-latency connection', () => {
        setConnection({ effectiveType: '4g', downlink: 50, rtt: 20, type: 'wifi' });
        setDeviceMemory(4);
        setMatchMedia({ mobile: true });
        const profile = getAdaptiveUploadProfile(bigFile(1 * MB));
        expect(profile.fileParallelism).toBe(2);
        expect(profile.reason).toBe('low device memory');
    });

    it('raises the ceiling for a confirmed fast wired/wifi connection', () => {
        setConnection({ downlink: 50, rtt: 20, type: 'ethernet' });
        setMatchMedia({});
        const profile = getAdaptiveUploadProfile(bigFile(1 * MB));
        expect(profile.fileParallelism).toBe(20);
        expect(profile.reason).toMatch(/reliable wired/);
    });

    it('reaches the top tier on good downlink+rtt even when connection.type is unreported (typical desktop Chrome)', () => {
        // Chrome only has "partial support" for connection.type and commonly
        // reports it as empty/'unknown' even on a real wired/wifi connection --
        // the top tier must not silently require it.
        setConnection({ downlink: 50, rtt: 20, type: '' });
        setMatchMedia({});
        const profile = getAdaptiveUploadProfile(bigFile(1 * MB));
        expect(profile.fileParallelism).toBe(20);
    });

    it('lets a recent observed transfer raise a live but conservative connection.downlink reading', () => {
        // Chrome's downlink is a coarse, traffic-history-based NQE estimate that
        // can under-report an idle-but-fast link; a recent real transfer should
        // be allowed to override it upward (never downward).
        setConnection({ downlink: 5, rtt: 20, type: '' });
        setMatchMedia({});
        setResourceTiming([{ transferSize: 20 * MB, requestStart: 0, responseEnd: 1000, startTime: 0 }]);
        const profile = getAdaptiveUploadProfile(bigFile(1 * MB));
        expect(profile.fileParallelism).toBe(20);
    });

    it('ignores a stale observed transfer when a live connection.downlink reading disagrees', () => {
        // An old Resource Timing entry (e.g. from a fast wifi network before the
        // user switched to slow cellular) must not override a live, low, and now
        // -correct downlink reading.
        setConnection({ downlink: 1, rtt: 20, type: '' });
        setMatchMedia({});
        setResourceTiming([{ transferSize: 20 * MB, requestStart: -120_000, responseEnd: -119_000, startTime: -120_000 }]);
        vi.spyOn(performance, 'now').mockReturnValue(0);
        const profile = getAdaptiveUploadProfile(bigFile(1 * MB));
        expect(profile.reason).toBe('high latency or congested network');
    });

    it('lets a fast measured RTT override a bad reported connection.rtt', () => {
        // connection.rtt is a coarse NQE estimate, not a ping, and can
        // misreport high latency on a genuinely fast link -- a real production
        // upload ran at fileParallelism 1 for its whole session even though the
        // earliest, pre-congestion round-trips measured 25-350ms. A fast
        // measured probe should lift this out of the "high latency" tier,
        // mirroring the existing downlink cross-check (only ever lowers a bad
        // reading, never raises a good one into a false negative).
        setConnection({ downlink: 50, rtt: 450, type: 'wifi' });
        setMatchMedia({});
        const profile = getAdaptiveUploadProfile(bigFile(1 * MB), { measuredRttMs: 40 });
        expect(profile.reason).not.toBe('high latency or congested network');
        expect(profile.fileParallelism).toBeGreaterThan(1);
    });

    it('keeps a bad reported connection.rtt when no measured RTT contradicts it', () => {
        setConnection({ downlink: 50, rtt: 450, type: 'wifi' });
        setMatchMedia({});
        const profile = getAdaptiveUploadProfile(bigFile(1 * MB));
        expect(profile.reason).toBe('high latency or congested network');
        expect(profile.fileParallelism).toBe(1);
    });

    it('does not let a slow measured RTT excuse a bad reported connection.rtt', () => {
        // A slow probe is just as likely to be a cold-started backend as a
        // slow link, so it must not be used as evidence either way here.
        setConnection({ downlink: 50, rtt: 450, type: 'wifi' });
        setMatchMedia({});
        const profile = getAdaptiveUploadProfile(bigFile(1 * MB), { measuredRttMs: 5000 });
        expect(profile.reason).toBe('high latency or congested network');
        expect(profile.fileParallelism).toBe(1);
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
        expect(profile.reason).toMatch(/fast connection \(measured\)/);
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
        // Mobile-with-no-network-info tier normally uses 2MB chunks; none of
        // these files individually trip the "large file" threshold (40MB), so
        // parallelism should be unaffected by the batch-size floor -- only
        // chunkSizeBytes should move.
        setMatchMedia({ mobile: true });
        const manyFiles = Array.from({ length: 5 }, () => ({ size: 35 * MB })); // 175MB total
        const profile = getAdaptiveUploadProfile(manyFiles);
        expect(profile.fileParallelism).toBe(4);
        expect(profile.chunkSizeBytes).toBeGreaterThanOrEqual(16 * MB);
    });
});
