import { useSyncExternalStore } from 'react';
import { ArrowPathIcon, CheckCircleIcon } from '@heroicons/react/24/outline';
import {
    getBackendStatusSnapshot,
    retryBackendNow,
    subscribeBackendStatus,
} from '../../services/backendStatus';

// App-wide banner that appears whenever the backend becomes unreachable and
// disappears once it recovers. It reads the shared backendStatus store (fed by
// every API call through httpClient), so a single mount covers every route and
// every client. Non-modal by design: it informs and lets the user force a
// reconnect attempt, but never blocks the UI.
export const BackendStatusBanner = () => {
    const state = useSyncExternalStore(
        subscribeBackendStatus,
        getBackendStatusSnapshot,
        getBackendStatusSnapshot,
    );

    if (state.status === 'online' && !state.justRecovered) {
        return null;
    }

    if (state.status === 'online') {
        return (
            <div className="backend-status-banner is-recovered" role="status" aria-live="polite">
                <CheckCircleIcon className="backend-status-icon" aria-hidden="true" />
                <span className="backend-status-text">Back online</span>
            </div>
        );
    }

    return (
        <div className="backend-status-banner is-offline" role="alert" aria-live="assertive">
            <ArrowPathIcon className="backend-status-icon spin-icon" aria-hidden="true" />
            <div className="backend-status-copy">
                <span className="backend-status-text">
                    Can’t reach the server — waiting for it to come back online…
                </span>
                <span className="backend-status-sub">
                    {state.checking
                        ? 'Checking the connection now…'
                        : 'This usually clears in a few seconds. We’ll reconnect automatically.'}
                </span>
            </div>
            <button
                type="button"
                className="backend-status-retry"
                onClick={retryBackendNow}
                disabled={state.checking}
            >
                {state.checking ? 'Checking…' : 'Try now'}
            </button>
        </div>
    );
};

export default BackendStatusBanner;
