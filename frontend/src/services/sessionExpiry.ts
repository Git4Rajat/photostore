// Global handler for an authenticated API request coming back 401.
//
// The backend returns 401 only when the session token is absent or invalid
// (permission failures are 403), so a 401 on a normal API call means the
// session is dead and the app cannot recover in place. Before this existed, a
// token cleared mid-session (a logout, another tab, a server-side token-version
// bump, or expiry) just left background pollers firing unauthenticated requests
// in a loop while the gallery showed stale/empty content -- the 401 gap seen in
// the 2026-10-17 account-switch HAR (albums/index + perf kept 401ing, no
// redirect). Instead, clear the dead session and reload into the login page.

import { isAuthEnabled } from './authClient';
import { clearLibraryClientData } from './clientDataReset';

// Routes that are expected to work without a signed-in session: a 401 here is
// normal (anonymous shared views, the auth pages themselves) and must not bounce
// the visitor to login.
const PUBLIC_PATH_PREFIXES = [
    '/public/',
    '/accept-invite',
    '/reset-password',
    '/confirm-library-clean',
    '/login',
    '/logout',
];

const onPublicRoute = (): boolean => {
    if (typeof window === 'undefined') return true;
    const path = window.location.pathname;
    return PUBLIC_PATH_PREFIXES.some((prefix) => path === prefix || path.startsWith(prefix));
};

// One-shot: a burst of concurrent pollers can all 401 at once, but only the
// first should drive the reset -- the rest are moot once we navigate away.
let handling = false;

export const reportUnauthorized = (): void => {
    if (handling) return;
    if (typeof window === 'undefined') return;
    // No-auth deployments never have a session to expire; a 401 there is a real
    // backend error for the caller to handle, not a sign-in prompt.
    if (!isAuthEnabled()) return;
    // Anonymous/shared/auth routes 401 by design; don't redirect them.
    if (onPublicRoute()) return;
    handling = true;
    try {
        window.localStorage.removeItem('photostore.passwordAuthToken');
    } catch { /* ignore storage failures */ }
    clearLibraryClientData();
    // Full load (not an SPA navigate) so every in-flight poller/timer is torn
    // down instead of continuing to 401 against the now-cleared session.
    window.location.assign('/login');
};
