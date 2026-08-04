import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// The tracker is a module-level singleton, so each test re-imports a fresh copy
// via resetModules to isolate its state.
type BackendStatusModule = typeof import('./backendStatus');

describe('backendStatus', () => {
    let mod: BackendStatusModule;

    beforeEach(async () => {
        vi.resetModules();
        vi.useFakeTimers();
        mod = await import('./backendStatus');
        mod.configureBackendStatus({ healthUrl: '/health' });
    });

    afterEach(() => {
        vi.useRealTimers();
        vi.restoreAllMocks();
        vi.unstubAllGlobals();
    });

    it('starts online', () => {
        expect(mod.getBackendStatusSnapshot().status).toBe('online');
    });

    it('flips to offline and notifies subscribers when a request is unreachable', () => {
        const listener = vi.fn();
        mod.subscribeBackendStatus(listener);
        mod.reportBackendUnreachable('Network Error');
        const state = mod.getBackendStatusSnapshot();
        expect(state.status).toBe('offline');
        expect(state.lastError).toBe('Network Error');
        expect(state.offlineSince).toBeTypeOf('number');
        expect(listener).toHaveBeenCalled();
    });

    it('recovers immediately when a real request succeeds while offline', () => {
        mod.reportBackendUnreachable('down');
        expect(mod.getBackendStatusSnapshot().status).toBe('offline');
        mod.reportBackendReachable();
        const state = mod.getBackendStatusSnapshot();
        expect(state.status).toBe('online');
        expect(state.justRecovered).toBe(true);
        expect(state.offlineSince).toBeNull();
    });

    it('polls /health on a backoff and flips back online once it answers', async () => {
        const fetchMock = vi.fn()
            .mockRejectedValueOnce(new TypeError('network error'))
            .mockResolvedValueOnce({ status: 200 } as Response);
        vi.stubGlobal('fetch', fetchMock);

        mod.reportBackendUnreachable('down');
        expect(mod.getBackendStatusSnapshot().status).toBe('offline');

        // First scheduled probe fails -> still offline, another probe scheduled.
        await vi.advanceTimersByTimeAsync(2000);
        expect(fetchMock).toHaveBeenCalledTimes(1);
        expect(mod.getBackendStatusSnapshot().status).toBe('offline');

        // Second probe succeeds -> online.
        await vi.advanceTimersByTimeAsync(3000);
        expect(fetchMock).toHaveBeenCalledTimes(2);
        expect(mod.getBackendStatusSnapshot().status).toBe('online');
    });

    it('treats a gateway 503 from the health probe as still offline', async () => {
        const fetchMock = vi.fn().mockResolvedValue({ status: 503 } as Response);
        vi.stubGlobal('fetch', fetchMock);

        mod.reportBackendUnreachable('down');
        await vi.advanceTimersByTimeAsync(2000);
        expect(fetchMock).toHaveBeenCalled();
        expect(mod.getBackendStatusSnapshot().status).toBe('offline');
    });

    it('retryBackendNow probes immediately without waiting out the backoff', async () => {
        const fetchMock = vi.fn().mockResolvedValue({ status: 200 } as Response);
        vi.stubGlobal('fetch', fetchMock);

        mod.reportBackendUnreachable('down');
        expect(mod.getBackendStatusSnapshot().status).toBe('offline');

        mod.retryBackendNow();
        // Let the in-flight probe promise settle (no timer advance needed).
        await vi.advanceTimersByTimeAsync(0);
        expect(fetchMock).toHaveBeenCalledTimes(1);
        expect(mod.getBackendStatusSnapshot().status).toBe('online');
    });
});
