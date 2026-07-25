// Keeps the tab processing photos at full speed while it is hidden or
// minimized. Browsers throttle background tabs hard (timers clamped to >=1s, and
// to >=1min under "intensive throttling" after ~5min hidden), which otherwise
// stalls the on-device processing loop. This controller combines three
// progressive-enhancement mechanisms, all torn down the moment processing goes
// idle so nothing runs -- and no battery is spent -- when there is no work:
//
//   1. A heartbeat Web Worker whose timer is not subject to the main-thread
//      background clamp; its ticks drive the processing loop.
//   2. A near-silent Web Audio loop. Chromium does not apply intensive
//      throttling to a page that is producing audio, so this keeps even the
//      worker timer and any remaining main-thread timers running at full rate.
//   3. A Screen Wake Lock, so mobile devices do not sleep mid-batch.
//
// Everything is feature-detected and wrapped in try/catch: on a browser missing
// any piece we degrade gracefully rather than break processing.

type WakeLockSentinelLike = {
    released: boolean;
    release: () => Promise<void>;
    addEventListener?: (type: 'release', listener: () => void) => void;
};

type WakeLockNavigator = Navigator & {
    wakeLock?: { request: (type: 'screen') => Promise<WakeLockSentinelLike> };
};

type AudioContextCtor = typeof AudioContext;

const getAudioContextCtor = (): AudioContextCtor | null => {
    if (typeof window === 'undefined') {
        return null;
    }
    const scoped = window as unknown as {
        AudioContext?: AudioContextCtor;
        webkitAudioContext?: AudioContextCtor;
    };
    return scoped.AudioContext || scoped.webkitAudioContext || null;
};

export type BackgroundKeepAliveOptions = {
    // Invoked on every heartbeat tick while the controller is active. Should be
    // cheap and self-guarding against re-entrancy (the processing loop already
    // ignores overlapping calls via an in-flight ref).
    onTick: () => void;
    intervalMs?: number;
};

export class BackgroundKeepAlive {
    private readonly onTick: () => void;
    private readonly intervalMs: number;

    private active = false;
    private worker: Worker | null = null;
    private fallbackTimer: number | null = null;

    private audioContext: AudioContext | null = null;

    private wakeLock: WakeLockSentinelLike | null = null;
    private wakeLockRequestInFlight = false;
    private visibilityListenerBound = false;

    constructor(options: BackgroundKeepAliveOptions) {
        this.onTick = options.onTick;
        this.intervalMs = Math.max(250, options.intervalMs ?? 1000);
    }

    isActive(): boolean {
        return this.active;
    }

    // Must be called from within a user gesture (e.g. the "enable browser AI"
    // click). Creates and unlocks the AudioContext so later resume() calls made
    // outside a gesture succeed. Safe to call repeatedly.
    primeAudio(): void {
        try {
            this.ensureAudioGraph();
            const context = this.audioContext;
            if (!context) {
                return;
            }
            void context.resume().then(() => {
                // Leave it suspended until a batch actually starts, unless one is
                // already running.
                if (!this.active && context.state === 'running') {
                    void context.suspend().catch(() => undefined);
                }
            }).catch(() => undefined);
        } catch (err) {
            console.warn('Background keep-alive: audio priming failed.', err);
        }
    }

    start(): void {
        if (this.active) {
            return;
        }
        this.active = true;
        this.startHeartbeat();
        this.resumeAudio();
        void this.acquireWakeLock();
        this.bindVisibilityListener();
    }

    stop(): void {
        if (!this.active) {
            return;
        }
        this.active = false;
        this.stopHeartbeat();
        this.suspendAudio();
        void this.releaseWakeLock();
        this.unbindVisibilityListener();
    }

    // Full teardown for unmount: also disposes the audio graph and worker.
    dispose(): void {
        this.stop();
        if (this.worker) {
            try {
                this.worker.terminate();
            } catch {
                // ignore
            }
            this.worker = null;
        }
        if (this.audioContext) {
            const context = this.audioContext;
            this.audioContext = null;
            void context.close().catch(() => undefined);
        }
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

    // --- Silent audio -------------------------------------------------------

    private ensureAudioGraph(): void {
        if (this.audioContext) {
            return;
        }
        const Ctor = getAudioContextCtor();
        if (!Ctor) {
            return;
        }
        const context = new Ctor();
        const gain = context.createGain();
        // Well below the threshold of human hearing (~ -80dB) but still a real,
        // non-zero signal so the tab registers as "producing audio" and earns
        // the throttling exemption.
        gain.gain.value = 0.0001;
        const oscillator = context.createOscillator();
        oscillator.frequency.value = 440;
        oscillator.connect(gain);
        gain.connect(context.destination);
        oscillator.start();
        this.audioContext = context;
    }

    private resumeAudio(): void {
        try {
            this.ensureAudioGraph();
            if (this.audioContext && this.audioContext.state !== 'running') {
                void this.audioContext.resume().catch(() => undefined);
            }
        } catch (err) {
            console.warn('Background keep-alive: could not resume silent audio.', err);
        }
    }

    private suspendAudio(): void {
        try {
            if (this.audioContext && this.audioContext.state === 'running') {
                void this.audioContext.suspend().catch(() => undefined);
            }
        } catch {
            // ignore
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
            // NotAllowedError etc. -- non-fatal; worker timer + audio still help.
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
