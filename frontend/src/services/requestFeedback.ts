import { classifyApiError, type ApiError } from './apiError';
import { showToast } from './toast';

// Central place that turns a failed API call into user feedback. Call sites hand
// it whatever they caught; it classifies (idempotent for ApiError), logs the
// details for diagnostics, and shows one consistent, friendly error toast.
//
// It deliberately stays silent for outages the global backend banner already
// owns (unreachable / timeout) so a single dropped connection doesn't stack a
// per-action toast on top of the banner. Ordinary app errors (500) and 4xx are
// the cases that were previously raw or silent, so those get the toast.

export interface NotifyApiErrorOptions {
    // Re-runs the failed action (typically the component's own handler, so a
    // successful retry updates local state correctly). Shown as a "Retry" button
    // only when retrying could plausibly help.
    retry?: () => void | Promise<unknown>;
    // Contextual lead-in shown instead of the generic classified copy, e.g.
    // "Couldn't save rating." The classified detail is still logged.
    context?: string;
}

const shouldOfferRetry = (error: ApiError): boolean =>
    error.kind === 'server' || error.kind === 'unknown';

export const notifyApiError = (error: unknown, options: NotifyApiErrorOptions = {}): ApiError => {
    const apiError = classifyApiError(error);

    // Always log the specifics so "it failed" is traceable to a request.
    // eslint-disable-next-line no-console
    console.error(
        `[api] ${apiError.kind}${apiError.status ? ` ${apiError.status}` : ''} (ref ${apiError.requestId}): ${apiError.rawMessage}`,
    );

    // The backend banner already surfaces reachability outages; don't double up.
    if (apiError.kind === 'unreachable' || apiError.kind === 'timeout' || apiError.kind === 'canceled') {
        return apiError;
    }

    const base = options.context ? options.context : apiError.message;
    // A short ref only where it helps a support conversation (server-side / unknown
    // faults), not on routine client-side validation messages.
    const withRef = shouldOfferRetry(apiError) ? `${base} (ref ${apiError.requestId})` : base;

    const offerRetry = Boolean(options.retry) && shouldOfferRetry(apiError);
    showToast(withRef, {
        variant: 'error',
        dedupeKey: `${apiError.kind}:${options.context ?? apiError.message}`,
        action: offerRetry
            ? { label: 'Retry', onClick: () => { void options.retry?.(); } }
            : undefined,
    });

    return apiError;
};
