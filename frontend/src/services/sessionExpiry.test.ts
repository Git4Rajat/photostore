import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('./authClient', () => ({ isAuthEnabled: () => true }));

describe('reportUnauthorized', () => {
    const assign = vi.fn();

    beforeEach(() => {
        vi.resetModules();
        vi.useFakeTimers();
        assign.mockReset();
        vi.stubGlobal('location', { pathname: '/', assign });
    });

    afterEach(() => {
        vi.useRealTimers();
        vi.unstubAllGlobals();
    });

    it('redirects to login once and flags the redirect so unload guards stand down', async () => {
        const { reportUnauthorized, isSessionExpiryRedirecting } = await import('./sessionExpiry');
        expect(isSessionExpiryRedirecting()).toBe(false);
        reportUnauthorized();
        reportUnauthorized();
        expect(assign).toHaveBeenCalledTimes(1);
        expect(assign).toHaveBeenCalledWith('/login');
        expect(isSessionExpiryRedirecting()).toBe(true);
    });

    it('re-arms when the navigation never happened, so the next 401 retries', async () => {
        const { reportUnauthorized, isSessionExpiryRedirecting } = await import('./sessionExpiry');
        reportUnauthorized();
        vi.advanceTimersByTime(6000);
        expect(isSessionExpiryRedirecting()).toBe(false);
        reportUnauthorized();
        expect(assign).toHaveBeenCalledTimes(2);
    });
});
