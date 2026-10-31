import { hasForegroundDemand, onForegroundReturn } from './foregroundDemand';
import { hasIndexDemand, onIndexDemandReturn } from './indexDemand';
import { postTools } from './apiClient';
import { getActiveLibraryFromToken } from './passwordAuthClient';
import { isLikelyAuthenticated } from './jobNotifications';
import { pendingEditScope } from './pendingEdits';

// The push carries an invalidation only; index reads keep their normal authorization.
export const startIndexReadyPush = (ready: () => void): (() => void) => {
    let stopped = false;
    let socket: WebSocket | null = null;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let retry = 1000;
    let generation = 0;
    let scope = pendingEditScope();
    const isGated = () => !hasForegroundDemand() || !hasIndexDemand();
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
            if (stopped || isGated() || attempt !== generation || requestedScope !== pendingEditScope()) return;
            if (!result.available || !result.url) {
                timer = setTimeout(() => void connect(), 60000);
                return;
            }
            const connection = new WebSocket(result.url);
            socket = connection;
            timer = setTimeout(() => connection.close(), 15000);
            connection.onopen = () => {
                if (stopped || isGated() || attempt !== generation || requestedScope !== pendingEditScope()) return;
                clearTimeout(timer);
                retry = 1000;
                ready(); // Recover publications missed while disconnected.
                timer = setTimeout(() => connection.close(), 4 * 60000);
            };
            connection.onmessage = event => {
                if (stopped || isGated() || attempt !== generation || requestedScope !== pendingEditScope()) return;
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
    let gated = false;
    const scopeTimer = setInterval(() => {
        if (isGated()) {
            if (!gated) { gated = true; generation += 1; clearTimeout(timer); socket?.close(); socket = null; }
            return;
        }
        if (scope === pendingEditScope()) return;
        generation += 1;
        clearTimeout(timer);
        socket?.close(); socket = null;
        void connect();
    }, 1000);
    const foregroundReturn = onForegroundReturn(() => {
        gated = false; generation += 1; clearTimeout(timer); socket?.close(); socket = null;
        void connect();
    });
    const indexDemandReturn = onIndexDemandReturn(() => {
        gated = false; generation += 1; clearTimeout(timer); socket?.close(); socket = null;
        void connect();
    });
    void connect();
    return () => {
        foregroundReturn();
        indexDemandReturn();
        stopped = true; generation += 1;
        clearTimeout(timer); clearInterval(scopeTimer); socket?.close();
    };
};
