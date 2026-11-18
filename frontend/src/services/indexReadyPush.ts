import { hasForegroundDemand, onForegroundReturn } from './foregroundDemand';
import { hasIndexDemand, onIndexDemandReturn } from './indexDemand';
import { post } from './apiClient';
import { getActiveLibraryFromToken } from './passwordAuthClient';
import { isLikelyAuthenticated } from './jobNotifications';
import { pendingEditScope } from './pendingEdits';

// The push carries an invalidation only; index reads keep their normal authorization.
export interface IndexPushOptions {
    // "A wave finished processing" advisory; the listener reads the summary, the push carries no data.
    onProcessing?: () => void;
    // A step or stage change of a wave's progress bar; advisory, the summary read is authoritative.
    onProgress?: (message: { waveId: string; stage: string; step?: number | null; etaSeconds?: number | null }) => void;
    // Whether the socket is up, so pollers can slow down while it is and speed back up when it is not.
    onConnection?: (connected: boolean) => void;
}

export const startIndexReadyPush = (ready: () => void, options: IndexPushOptions = {}): (() => void) => {
    let live = false;
    const report = (value: boolean) => {
        if (live === value) return;
        live = value;
        options.onConnection?.(value);
    };
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
            // Subscription authorization is core work. Sending renewals to
            // the indexer would wake its maintenance sidecar without any work.
            const result = await post<{ available: boolean; url?: string; libraryId?: string }>(
                '/api/index-events/negotiate', {},
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
                report(true);
                ready(); // Recover publications missed while disconnected.
                // Bound subscription authorization by periodically checking
                // library access again. This renewal uses core, not indexer.
                timer = setTimeout(() => connection.close(), 4 * 60000);
            };
            connection.onmessage = event => {
                if (stopped || isGated() || attempt !== generation || requestedScope !== pendingEditScope()) return;
                try {
                    const message = JSON.parse(String(event.data));
                    if (message.libraryId !== getActiveLibraryFromToken()) return;
                    if (message.type === 'index-ready') ready();
                    else if (message.type === 'processing') options.onProcessing?.();
                    else if (message.type === 'processing-progress') options.onProgress?.(message);
                } catch { /* Ignore malformed or non-index messages. */ }
            };
            connection.onerror = () => connection.close();
            connection.onclose = () => {
                if (attempt !== generation || stopped) return;
                report(false);
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
            if (!gated) { gated = true; generation += 1; clearTimeout(timer); socket?.close(); socket = null; report(false); }
            return;
        }
        if (scope === pendingEditScope()) return;
        generation += 1;
        clearTimeout(timer);
        socket?.close(); socket = null; report(false);
        void connect();
    }, 1000);
    const foregroundReturn = onForegroundReturn(() => {
        gated = false; generation += 1; clearTimeout(timer); socket?.close(); socket = null; report(false);
        void connect();
    });
    const indexDemandReturn = onIndexDemandReturn(() => {
        gated = false; generation += 1; clearTimeout(timer); socket?.close(); socket = null; report(false);
        void connect();
    });
    void connect();
    return () => {
        foregroundReturn();
        indexDemandReturn();
        stopped = true; generation += 1;
        clearTimeout(timer); clearInterval(scopeTimer); socket?.close();
        report(false);
    };
};
