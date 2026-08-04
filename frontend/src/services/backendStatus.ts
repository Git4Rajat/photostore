// App-wide backend-availability tracker.
//
// The backend runs scale-to-zero on Azure Container Apps and has a modest
// memory budget, so it can be transiently unreachable: a cold-start window
// where the ingress answers 503 before any replica is warm, a replica that was
// just OOM-recycled, or a dropped network. `httpClient` already retries these
// transparently, but when the retries are exhausted the whole app would
// otherwise surface a wall of unrelated errors with no shared explanation.
//
// This module is the single source of truth for "is the API reachable right
// now". `httpClient` reports every request outcome here; when we flip to
// offline we start polling `/health` on a backoff until it answers, then flip
// back to online. A tiny React binding (BackendStatusBanner) subscribes and
// shows a global "waiting for the server" banner. It is intentionally
// framework-agnostic (no React, no axios) so it has zero import cycles with the
// clients that report into it.

export type BackendConnectivity = 'online' | 'offline';

export interface BackendStatusState {
    status: BackendConnectivity;
    // ms epoch of the transition to offline; null while online.
    offlineSince: number | null;
    // Number of recovery probes attempted since going offline (drives backoff
    // and the "still trying" copy).
    attempts: number;
    // A recovery probe (or the triggering request) is currently in flight.
    checking: boolean;
    // Set briefly right after we recover so the UI can flash "Back online".
    justRecovered: boolean;
    // Human-readable reason for the outage, for diagnostics / the banner.
    lastError: string | null;
}

type Listener = () => void;

const RECOVERED_FLASH_MS = 2600;
const PROBE_TIMEOUT_MS = 8000;
// Backoff for recovery probes: quick at first, then ease off so a long outage
// doesn't hammer the ingress. Clamped to [minDelay, maxDelay].
const PROBE_BASE_DELAY_MS = 1500;
const PROBE_MAX_DELAY_MS = 10000;

let state: BackendStatusState = {
    status: 'online',
    offlineSince: null,
    attempts: 0,
    checking: false,
    justRecovered: false,
    lastError: null,
};

const listeners = new Set<Listener>();
let healthUrl = '/health';
let probeTimer: ReturnType<typeof setTimeout> | null = null;
let recoveredTimer: ReturnType<typeof setTimeout> | null = null;
let probeInFlight = false;
let browserListenersBound = false;

const emit = () => {
    listeners.forEach((listener) => {
        try {
            listener();
        } catch {
            // A misbehaving subscriber must never break status propagation.
        }
    });
};

const setState = (patch: Partial<BackendStatusState>) => {
    state = { ...state, ...patch };
    emit();
};

export const subscribeBackendStatus = (listener: Listener): (() => void) => {
    listeners.add(listener);
    return () => {
        listeners.delete(listener);
    };
};

// getSnapshot returns a stable reference until the state actually changes, so
// useSyncExternalStore does not loop.
export const getBackendStatusSnapshot = (): BackendStatusState => state;

const clearProbeTimer = () => {
    if (probeTimer !== null) {
        clearTimeout(probeTimer);
        probeTimer = null;
    }
};

const nextProbeDelay = (attempts: number): number => {
    const delay = PROBE_BASE_DELAY_MS * 1.6 ** Math.max(0, attempts - 1);
    return Math.min(PROBE_MAX_DELAY_MS, Math.round(delay));
};

// A bare fetch that deliberately bypasses the axios clients: it must carry no
// auth (so it stays a CORS-simple request with no preflight) and must not be
// swept up by the cold-start retry/dedup logic in httpClient. Resolves true iff
// the app itself answered (a gateway 502/503/504 means still no warm replica).
const probeHealthOnce = async (): Promise<boolean> => {
    if (typeof fetch !== 'function') {
        return false;
    }
    const controller = typeof AbortController !== 'undefined' ? new AbortController() : null;
    const timer = controller
        ? setTimeout(() => controller.abort(), PROBE_TIMEOUT_MS)
        : null;
    try {
        const response = await fetch(healthUrl, {
            method: 'GET',
            cache: 'no-store',
            credentials: 'omit',
            signal: controller?.signal,
        });
        return response.status !== 502 && response.status !== 503 && response.status !== 504;
    } catch {
        return false;
    } finally {
        if (timer !== null) {
            clearTimeout(timer);
        }
    }
};

const runProbe = async () => {
    if (probeInFlight || state.status !== 'offline') {
        return;
    }
    probeInFlight = true;
    setState({ checking: true, attempts: state.attempts + 1 });
    let reachable = false;
    try {
        reachable = await probeHealthOnce();
    } finally {
        probeInFlight = false;
    }
    if (reachable) {
        reportBackendReachable();
        return;
    }
    // Still down: schedule the next probe unless someone flipped us online
    // (e.g. a real request succeeded) while this one was in flight.
    if (state.status === 'offline') {
        setState({ checking: false });
        scheduleProbe();
    }
};

const scheduleProbe = () => {
    clearProbeTimer();
    if (state.status !== 'offline') {
        return;
    }
    probeTimer = setTimeout(() => {
        probeTimer = null;
        void runProbe();
    }, nextProbeDelay(state.attempts));
};

const bindBrowserListeners = () => {
    if (browserListenersBound || typeof window === 'undefined') {
        return;
    }
    browserListenersBound = true;
    // When the OS network returns or the user refocuses the tab, probe right
    // away instead of waiting out the current backoff delay.
    const wake = () => {
        if (state.status === 'offline') {
            retryBackendNow();
        }
    };
    window.addEventListener('online', wake);
    window.addEventListener('focus', wake);
    if (typeof document !== 'undefined') {
        document.addEventListener('visibilitychange', () => {
            if (document.visibilityState === 'visible') {
                wake();
            }
        });
    }
};

// Called once at startup with the resolved absolute /health URL.
export const configureBackendStatus = (options: { healthUrl: string }) => {
    if (options.healthUrl) {
        healthUrl = options.healthUrl;
    }
    bindBrowserListeners();
};

// A request reached the app and got a real answer (any HTTP status, including
// 4xx). That proves the backend is up, so recover immediately if we were down.
export const reportBackendReachable = () => {
    if (state.status === 'online') {
        return;
    }
    clearProbeTimer();
    if (recoveredTimer !== null) {
        clearTimeout(recoveredTimer);
    }
    setState({
        status: 'online',
        offlineSince: null,
        attempts: 0,
        checking: false,
        justRecovered: true,
        lastError: null,
    });
    recoveredTimer = setTimeout(() => {
        recoveredTimer = null;
        setState({ justRecovered: false });
    }, RECOVERED_FLASH_MS);
};

// A request terminally failed in a way that means the app was unreachable
// (connection failure, or a gateway 502/503/504) after httpClient exhausted its
// cold-start retries. Flip to offline and start polling for recovery.
export const reportBackendUnreachable = (message?: string) => {
    if (state.status === 'offline') {
        if (message && message !== state.lastError) {
            setState({ lastError: message });
        }
        return;
    }
    setState({
        status: 'offline',
        offlineSince: Date.now(),
        attempts: 0,
        checking: false,
        justRecovered: false,
        lastError: message ?? null,
    });
    scheduleProbe();
};

// User pressed "Try now" (or the tab/network woke): probe without waiting out
// the backoff.
export const retryBackendNow = () => {
    if (state.status !== 'offline') {
        return;
    }
    clearProbeTimer();
    void runProbe();
};
