import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { renderHook } from '@testing-library/react';
import type { ApiError } from './apiError';

type BackendStatusModule = typeof import('./backendStatus');
type HookModule = typeof import('./useBackendRecoveryRetry');

const makeError = (overrides: Partial<ApiError> = {}): ApiError =>
    ({ retriable: true, requestId: 'req-1', kind: 'unreachable', message: 'down', ...overrides }) as ApiError;

describe('useBackendRecoveryRetry', () => {
    let backendStatus: BackendStatusModule;
    let useBackendRecoveryRetry: HookModule['useBackendRecoveryRetry'];

    beforeEach(async () => {
        vi.resetModules();
        backendStatus = await import('./backendStatus');
        ({ useBackendRecoveryRetry } = await import('./useBackendRecoveryRetry'));
        backendStatus.configureBackendStatus({ healthUrl: '/health' });
    });

    afterEach(() => {
        vi.restoreAllMocks();
    });

    it('does not subscribe when there is no error', () => {
        const retry = vi.fn();
        renderHook(() => useBackendRecoveryRetry(null, retry));
        backendStatus.reportBackendUnreachable('down');
        backendStatus.reportBackendReachable();
        expect(retry).not.toHaveBeenCalled();
    });

    it('does not retry for a non-retriable (e.g. 4xx) error', () => {
        const retry = vi.fn();
        const error = makeError({ retriable: false, kind: 'client' });
        renderHook(() => useBackendRecoveryRetry(error, retry));
        backendStatus.reportBackendUnreachable('down');
        backendStatus.reportBackendReachable();
        expect(retry).not.toHaveBeenCalled();
    });

    it('calls retry once the backend flips back online after a retriable error', () => {
        const retry = vi.fn();
        const error = makeError();
        renderHook(() => useBackendRecoveryRetry(error, retry));

        backendStatus.reportBackendUnreachable('down');
        expect(retry).not.toHaveBeenCalled();

        backendStatus.reportBackendReachable();
        expect(retry).toHaveBeenCalledTimes(1);
    });

    it('always calls the latest retry callback, not a stale closure', () => {
        const firstRetry = vi.fn();
        const secondRetry = vi.fn();
        const error = makeError();
        const { rerender } = renderHook(
            ({ retry }: { retry: () => void }) => useBackendRecoveryRetry(error, retry),
            { initialProps: { retry: firstRetry } },
        );

        rerender({ retry: secondRetry });

        backendStatus.reportBackendUnreachable('down');
        backendStatus.reportBackendReachable();

        expect(firstRetry).not.toHaveBeenCalled();
        expect(secondRetry).toHaveBeenCalledTimes(1);
    });

    it('unsubscribes on unmount so a later recovery does not fire', () => {
        const retry = vi.fn();
        const error = makeError();
        const { unmount } = renderHook(() => useBackendRecoveryRetry(error, retry));
        unmount();

        backendStatus.reportBackendUnreachable('down');
        backendStatus.reportBackendReachable();

        expect(retry).not.toHaveBeenCalled();
    });
});
