// Screen Wake Lock helper for long, unattended browser-driven jobs that need
// to stop the device from sleeping/locking mid-job -- e.g. the client-
// orchestrated library export in libraryExportDownloader.ts, which can run
// for hours over hundreds of thousands of files. Feature-detected and
// wrapped in try/catch: on a browser without the API we degrade gracefully
// rather than break the job.
//
// A near-identical wake lock lives inside backgroundKeepAlive.ts, paired
// there with a heartbeat worker for jobs that are also throttling-sensitive
// (their progress is driven by a setTimeout-gated loop, which the background
// tab clamp can stall). A library export is driven by fetch()/IndexedDB
// promises instead, so it doesn't need that half -- just the wake lock.
//
// The Wake Lock spec releases the lock automatically whenever the page goes
// hidden; this re-acquires it on visibilitychange back to visible so a
// returning user keeps the device awake for the rest of the job.

type WakeLockSentinelLike = {
    released: boolean;
    release: () => Promise<void>;
    addEventListener?: (type: 'release', listener: () => void) => void;
};

type WakeLockNavigator = Navigator & {
    wakeLock?: { request: (type: 'screen') => Promise<WakeLockSentinelLike> };
};

export class ScreenWakeLockController {
    private active = false;
    private sentinel: WakeLockSentinelLike | null = null;
    private requestInFlight = false;
    private listenerBound = false;

    acquire(): void {
        if (this.active) return;
        this.active = true;
        void this.requestLock();
        this.bindVisibilityListener();
    }

    // Idempotent -- safe to call from multiple exit paths (explicit cancel,
    // normal completion) without tracking which one fired first.
    release(): void {
        if (!this.active) return;
        this.active = false;
        void this.releaseLock();
        this.unbindVisibilityListener();
    }

    private async requestLock(): Promise<void> {
        if (typeof navigator === 'undefined' || typeof document === 'undefined') return;
        const nav = navigator as WakeLockNavigator;
        if (!nav.wakeLock || this.sentinel || this.requestInFlight) return;
        // A wake lock can only be acquired while the page is visible.
        if (document.visibilityState !== 'visible') return;
        this.requestInFlight = true;
        try {
            const sentinel = await nav.wakeLock.request('screen');
            if (!this.active) {
                // release() was called while awaiting; drop it immediately.
                await sentinel.release().catch(() => undefined);
                return;
            }
            this.sentinel = sentinel;
            sentinel.addEventListener?.('release', () => {
                this.sentinel = null;
            });
        } catch (err) {
            // NotAllowedError etc. -- non-fatal, the job keeps running.
            console.warn('Screen wake lock unavailable.', err);
        } finally {
            this.requestInFlight = false;
        }
    }

    private async releaseLock(): Promise<void> {
        const sentinel = this.sentinel;
        this.sentinel = null;
        if (sentinel && !sentinel.released) {
            await sentinel.release().catch(() => undefined);
        }
    }

    private bindVisibilityListener(): void {
        if (this.listenerBound || typeof document === 'undefined') return;
        document.addEventListener('visibilitychange', this.handleVisibilityChange);
        this.listenerBound = true;
    }

    private unbindVisibilityListener(): void {
        if (!this.listenerBound || typeof document === 'undefined') return;
        document.removeEventListener('visibilitychange', this.handleVisibilityChange);
        this.listenerBound = false;
    }

    private handleVisibilityChange = (): void => {
        if (this.active && typeof document !== 'undefined' && document.visibilityState === 'visible') {
            void this.requestLock();
        }
    };
}
