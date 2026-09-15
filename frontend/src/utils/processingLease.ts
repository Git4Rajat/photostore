import { isApiError } from '../services/apiError';

// ApiError classifies every 4xx into a friendly, user-safe `message` (e.g.
// "That didn't work...") that drops the backend's actual reason -- so
// matching text patterns against `String(err)` no longer sees "409" or
// "lease_active" once an error has passed through classifyApiError. Check
// the structured `status`/`rawMessage` fields instead; fall back to the old
// text match for non-ApiError throwables (e.g. plain Errors in tests).
export const shouldSuppressLeaseWarning = (err: unknown): boolean => {
    if (isApiError(err)) {
        if (err.status === 409) {
            return true;
        }
        const raw = err.rawMessage || '';
        return /photo not found/i.test(raw)
            || /lease_active/i.test(raw)
            || /already held by another client/i.test(raw)
            || /processing lease/i.test(raw);
    }
    const message = typeof err === 'string' ? err : String(err || '');
    return /photo not found/i.test(message)
        || /lease_active/i.test(message)
        || /already held by another client/i.test(message)
        || /processing lease/i.test(message)
        || /\b409\b/.test(message);
};
