/// <reference lib="webworker" />

// Minimal heartbeat worker. A dedicated worker's timers are NOT subject to the
// aggressive background-tab clamping the browser applies to the main thread
// (main-thread setTimeout/setInterval get clamped to >=1s when hidden, and to
// >=1min under "intensive throttling" after ~5min hidden). So we run the pacing
// timer here and post "tick" messages back to the main thread, letting the
// background-processing loop keep firing at full rate even when the owning tab
// is hidden or minimized. See backgroundKeepAlive.ts for the consumer.

type HeartbeatRequest =
    | { type: 'start'; intervalMs?: number }
    | { type: 'stop' };

const MIN_INTERVAL_MS = 250;
const DEFAULT_INTERVAL_MS = 1000;

let timer: ReturnType<typeof setInterval> | null = null;

const stopTimer = () => {
    if (timer !== null) {
        clearInterval(timer);
        timer = null;
    }
};

self.onmessage = (event: MessageEvent<HeartbeatRequest>) => {
    const data = event.data;
    if (!data || typeof data.type !== 'string') {
        return;
    }
    if (data.type === 'start') {
        const requested = Number(data.intervalMs);
        const intervalMs = Math.max(MIN_INTERVAL_MS, Number.isFinite(requested) ? requested : DEFAULT_INTERVAL_MS);
        stopTimer();
        timer = setInterval(() => {
            self.postMessage({ type: 'tick' });
        }, intervalMs);
    } else if (data.type === 'stop') {
        stopTimer();
    }
};

export {};
