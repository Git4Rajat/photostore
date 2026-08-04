// Keeps the tab processing photos at full speed while it is hidden or
// minimized, and stops the device from sleeping/locking mid-batch. Combines
// two progressive-enhancement mechanisms, both torn down the moment
// processing goes idle so nothing runs -- and no battery is spent -- when
// there is no work:
//
//   1. A heartbeat Web Worker whose timer is not subject to the main-thread
//      background clamp (setTimeout/setInterval get clamped to >=1s when
//      hidden, and to >=1min under "intensive throttling" after ~5min
//      hidden); its ticks drive the processing loop.
//   2. A Screen Wake Lock, so the device does not sleep/lock mid-batch.
//
// Both are independent and feature-detected, wrapped in try/catch: on a
// browser missing either we degrade gracefully rather than break processing.
//
// A near-silent Web Audio loop (to also defeat Chromium's "intensive
// throttling" of a *hidden* tab) previously lived here as a third leg. It was
// dropped as not worth the audible-tab tradeoff, but that removal
// accidentally took the unrelated heartbeat worker and wake lock out with it;
// this file restores just those two.

type WakeLockSentinelLike = {
    released: boolean;
    release: () => Promise<void>;
    addEventListener?: (type: 'release', listener: () => void) => void;
};

type WakeLockNavigator = Navigator & {
    wakeLock?: { request: (type: 'screen') => Promise<WakeLockSentinelLike> };
};

export type BackgroundKeepAliveOptions = {
    // Invoked on every heartbeat tick while the controller is active. Should be
    // cheap and self-guarding against re-entrancy (the processing loop already
    // ignores overlapping calls via an in-flight ref).
    onTick: () => void;
    intervalMs?: number;
    // Invoked whenever active state changes so the UI can reflect it.
    onStateChange?: (active: boolean) => void;
};

export class BackgroundKeepAlive {
    private readonly onTick: () => void;
    private readonly intervalMs: number;
    private readonly onStateChange?: (active: boolean) => void;

    private active = false;
    private worker: Worker | null = null;
    private fallbackTimer: number | null = null;

    private wakeLock: WakeLockSentinelLike | null = null;
    private wakeLockRequestInFlight = false;
    private visibilityListenerBound = false;

    constructor(options: BackgroundKeepAliveOptions) {
        this.onTick = options.onTick;
        this.intervalMs = Math.max(250, options.intervalMs ?? 1000);
        this.onStateChange = options.onStateChange;
    }

    isActive(): boolean {
        return this.active;
    }

    start(): void {
        if (this.active) {
            return;
        }
        this.active = true;
        this.startHeartbeat();
        void this.acquireWakeLock();
        this.bindVisibilityListener();
        this.onStateChange?.(true);
    }

    stop(): void {
        if (!this.active) {
            return;
        }
        this.active = false;
        this.stopHeartbeat();
        void this.releaseWakeLock();
        this.unbindVisibilityListener();
        this.onStateChange?.(false);
    }

    // Full teardown for unmount: also disposes the worker.
    dispose(): void {
        this.stop();
        this.disposeWorker();
    }

    // --- Heartbeat worker ---------------------------------------------------

    private startHeartbeat(): void {
        if (this.worker || this.fallbackTimer !== null) {
            // Already running; just make sure the worker timer is (re)started.
            this.worker?.postMessage({ type: 'start', intervalMs: this.intervalMs });
            return;
        }
        try {
            this.worker = new Worker(
                new URL('../workers/schedulerHeartbeatWorker.ts', import.meta.url),
                { type: 'module' },
            );
            this.worker.onmessage = (event: MessageEvent) => {
                if (event.data?.type === 'tick') {
                    this.onTick();
                }
            };
            this.worker.onerror = (event) => {
                console.warn('Background keep-alive: heartbeat worker error; using main-thread fallback.', event.message);
                this.disposeWorker();
                this.startFallbackTimer();
            };
            this.worker.postMessage({ type: 'start', intervalMs: this.intervalMs });
        } catch (err) {
            console.warn('Background keep-alive: heartbeat worker unavailable; using main-thread fallback.', err);
            this.worker = null;
            this.startFallbackTimer();
        }
    }

    private startFallbackTimer(): void {
        if (this.fallbackTimer !== null || typeof window === 'undefined') {
            return;
        }
        this.fallbackTimer = window.setInterval(() => this.onTick(), this.intervalMs);
    }

    private disposeWorker(): void {
        if (this.worker) {
            try {
                this.worker.terminate();
            } catch {
                // ignore
            }
            this.worker = null;
        }
    }

    private stopHeartbeat(): void {
        if (this.worker) {
            this.worker.postMessage({ type: 'stop' });
        }
        if (this.fallbackTimer !== null && typeof window !== 'undefined') {
            window.clearInterval(this.fallbackTimer);
            this.fallbackTimer = null;
        }
    }

    // --- Screen wake lock ---------------------------------------------------

    private async acquireWakeLock(): Promise<void> {
        if (typeof navigator === 'undefined' || typeof document === 'undefined') {
            return;
        }
        const nav = navigator as WakeLockNavigator;
        if (!nav.wakeLock || this.wakeLock || this.wakeLockRequestInFlight) {
            return;
        }
        // A wake lock can only be acquired while the page is visible.
        if (document.visibilityState !== 'visible') {
            return;
        }
        this.wakeLockRequestInFlight = true;
        try {
            const sentinel = await nav.wakeLock.request('screen');
            if (!this.active) {
                // We went idle while awaiting; drop it immediately.
                await sentinel.release().catch(() => undefined);
                return;
            }
            this.wakeLock = sentinel;
            sentinel.addEventListener?.('release', () => {
                this.wakeLock = null;
            });
        } catch (err) {
            // NotAllowedError etc. -- non-fatal; the heartbeat worker still helps.
            console.warn('Background keep-alive: screen wake lock unavailable.', err);
        } finally {
            this.wakeLockRequestInFlight = false;
        }
    }

    private async releaseWakeLock(): Promise<void> {
        const sentinel = this.wakeLock;
        this.wakeLock = null;
        if (sentinel && !sentinel.released) {
            await sentinel.release().catch(() => undefined);
        }
    }

    private bindVisibilityListener(): void {
        if (this.visibilityListenerBound || typeof document === 'undefined') {
            return;
        }
        document.addEventListener('visibilitychange', this.handleVisibilityChange);
        this.visibilityListenerBound = true;
    }

    private unbindVisibilityListener(): void {
        if (!this.visibilityListenerBound || typeof document === 'undefined') {
            return;
        }
        document.removeEventListener('visibilitychange', this.handleVisibilityChange);
        this.visibilityListenerBound = false;
    }

    // The browser silently drops a screen wake lock whenever the page is hidden.
    // Re-acquire it when the page becomes visible again so a returning user keeps
    // the device awake for the remainder of the batch.
    private handleVisibilityChange = (): void => {
        if (this.active && typeof document !== 'undefined' && document.visibilityState === 'visible') {
            void this.acquireWakeLock();
        }
    };
}
