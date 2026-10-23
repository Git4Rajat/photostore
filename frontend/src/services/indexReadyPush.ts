import { postTools } from './apiClient';
import { getActiveLibraryFromToken } from './passwordAuthClient';
import { isLikelyAuthenticated } from './jobNotifications';
import { pendingEditScope } from './pendingEdits';

// How long a backgrounded tab is still treated as foreground-active before
// negotiation/reconnect stop -- see docs/audits/index_ready_idle_reproduction.cjs,
// which reproduces the un-gated behavior this closes (this module previously
// negotiated/reconnected on its own timers regardless of tab visibility).
const FOREGROUND_IDLE_GATE_MS = 5 * 60000;

// The push carries an invalidation only; index reads keep their normal authorization.
export const startIndexReadyPush = (ready: () => void): (() => void) => {
    let stopped = false;
    let socket: WebSocket | null = null;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let retry = 1000;
    let generation = 0;
    let scope = pendingEditScope();
    const hasVisibility = typeof document !== 'undefined' && typeof document.addEventListener === 'function';
    let hiddenSince: number | null = hasVisibility && document.visibilityState === 'hidden' ? Date.now() : null;
    // No visibility signal at all (non-browser context) behaves as always-
    // foreground, unchanged from before this gate existed.
    const isGated = () => hiddenSince !== null && Date.now() - hiddenSince >= FOREGROUND_IDLE_GATE_MS;
    const onVisibilityChange = () => {
        if (document.visibilityState === 'hidden') { hiddenSince = Date.now(); return; }
        const wasGated = isGated();
        hiddenSince = null;
        if (wasGated) {
            // Authorized catch-up on return: don't wait out whatever poll
            // interval was already scheduled while backgrounded.
            clearTimeout(timer);
            void connect();
        }
    };
    if (hasVisibility) document.addEventListener('visibilitychange', onVisibilityChange);
    const connect = async () => {
        const attempt = ++generation;
        const requestedScope = pendingEditScope();
        scope = requestedScope;
        try {
            if (isGated()) { timer = setTimeout(() => void connect(), 30000); return; }
            if (!isLikelyAuthenticated() || !navigator.onLine) { timer = setTimeout(() => void connect(), 30000); return; }
            const result = await postTools<{ available: boolean; url?: string; libraryId?: string }>(
                '/api/tools/index-events/negotiate', {},
            );
            if (stopped || attempt !== generation || requestedScope !== pendingEditScope()) return;
            if (!result.available || !result.url) {
                timer = setTimeout(() => void connect(), 60000);
                return;
            }
            const connection = new WebSocket(result.url);
            socket = connection;
            timer = setTimeout(() => connection.close(), 15000);
            connection.onopen = () => {
                if (stopped || attempt !== generation || requestedScope !== pendingEditScope()) return;
                clearTimeout(timer);
                retry = 1000;
                ready(); // Recover publications missed while disconnected.
                timer = setTimeout(() => connection.close(), 4 * 60000);
            };
            connection.onmessage = event => {
                if (stopped || attempt !== generation || requestedScope !== pendingEditScope()) return;
                try {
                    const message = JSON.parse(String(event.data));
                    if (message.type === 'index-ready' && message.libraryId === getActiveLibraryFromToken()) ready();
                } catch { /* Ignore malformed or non-index messages. */ }
            };
            connection.onerror = () => connection.close();
            connection.onclose = () => {
                if (attempt !== generation || stopped) return;
                clearTimeout(timer);
                socket = null;
                timer = setTimeout(() => void connect(), retry);
                retry = Math.min(30000, retry * 2);
            };
        } catch {
            if (stopped || attempt !== generation) return;
            timer = setTimeout(() => void connect(), retry);
            retry = Math.min(30000, retry * 2);
        }
    };
    const scopeTimer = setInterval(() => {
        if (scope === pendingEditScope()) return;
        generation += 1;
        clearTimeout(timer);
        socket?.close(); socket = null;
        void connect();
    }, 1000);
    void connect();
    return () => {
        stopped = true; generation += 1;
        clearTimeout(timer); clearInterval(scopeTimer); socket?.close();
        if (hasVisibility) document.removeEventListener('visibilitychange', onVisibilityChange);
    };
};
