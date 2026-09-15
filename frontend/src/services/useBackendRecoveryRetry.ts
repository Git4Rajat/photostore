import { useEffect, useRef } from 'react';
import type { ApiError } from './apiError';
import { subscribeBackendStatus, getBackendStatusSnapshot } from './backendStatus';

// Re-runs a failed fetch automatically once the backend comes back, instead of
// leaving the user stuck on an error screen until they manually retry. Only
// subscribes while the current error is retriable (unreachable/timeout/server
// — see apiError.ts) so an unrelated recovery elsewhere in the app doesn't
// re-trigger a fetch that failed for an ordinary 4xx reason.
export const useBackendRecoveryRetry = (error: ApiError | null | undefined, retry: () => void) => {
    const retryRef = useRef(retry);
    retryRef.current = retry;

    useEffect(() => {
        if (!error?.retriable) {
            return undefined;
        }
        return subscribeBackendStatus(() => {
            if (getBackendStatusSnapshot().justRecovered) {
                retryRef.current();
            }
        });
    }, [error?.retriable, error?.requestId]);
};
