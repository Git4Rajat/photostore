// Turbo's baseline concurrency (see getBrowserProcessingConcurrency in
// AppServicesProvider.tsx) is deliberately conservative because it has to
// stay safe while the user is actively on the machine. Once the tab has been
// hidden/unfocused *and* no input has been seen for a while, there is no
// interactive workload to protect, so background AI processing can ramp up
// further and use more of the machine's idle capacity -- useful for draining
// a large backlog overnight.
//
// The ramp is:
//   - gated on sustained inactivity (input + visibility), not just a hidden
//     tab -- mirrors turbo's intent of never fighting the user for the
//     machine, and snaps back to 0 the instant either signals activity.
//   - gradual: one extra worker per RAMP_STEP_MS of continued inactivity, so
//     a brief background dip doesn't jump straight to the ceiling.
//   - self-correcting: a burst of processing failures is treated as a proxy
//     for the device struggling (thrashing / near-OOM) under the current
//     ramp. It halves this session's ceiling (congestion-control style) and
//     forces a cooldown before ramping resumes, so a bad ceiling converges
//     down instead of being retried forever.
//   - never persisted: all state is in-memory only, so a tab reload (e.g.
//     after a crash) starts over at the safe interactive baseline rather
//     than re-attempting whatever ceiling caused it.

const IDLE_THRESHOLD_MS = 5 * 60 * 1000;
const RAMP_STEP_MS = 3 * 60 * 1000;
const MAX_BONUS = 6;
const FAILURE_BURST_WINDOW_MS = 2 * 60 * 1000;
const FAILURE_BURST_THRESHOLD = 3;
const BACKOFF_COOLDOWN_MS = 10 * 60 * 1000;

let lastActivityAt = Date.now();
// Ramp step-counting starts this many ms in the future; once `now` reaches
// it, the ramp is at step 1. Reset on real activity (pushed out again) and on
// backoff (pinned to when the cooldown lifts), so both cases restart the
// climb from zero instead of jumping straight back to wherever it left off.
let rampAnchorAt = lastActivityAt + IDLE_THRESHOLD_MS;
let sessionCeilingBonus = MAX_BONUS;
let suppressedUntil = 0;
const recentFailures: number[] = [];

const noteActivity = (): void => {
    const now = Date.now();
    lastActivityAt = now;
    rampAnchorAt = now + IDLE_THRESHOLD_MS;
};

const bindActivityListeners = (): void => {
    if (typeof window === 'undefined' || typeof document === 'undefined') {
        return;
    }
    (['mousemove', 'mousedown', 'keydown', 'touchstart', 'wheel', 'scroll'] as const).forEach((type) => {
        window.addEventListener(type, noteActivity, { passive: true });
    });
    document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible') {
            noteActivity();
        }
    });
};
bindActivityListeners();

// Call after every browser-processing item settles so a burst of recent
// failures (a reasonable proxy for the device struggling under the current
// ramp) can pull the session ceiling back down. Only failures matter here;
// successes don't need to do anything since the ceiling only ever recovers
// via the ramp's own ordinary gradual climb.
export const reportBrowserProcessingOutcome = (succeeded: boolean): void => {
    if (succeeded || sessionCeilingBonus <= 0) {
        return;
    }
    const now = Date.now();
    recentFailures.push(now);
    while (recentFailures.length > 0 && now - recentFailures[0] > FAILURE_BURST_WINDOW_MS) {
        recentFailures.shift();
    }
    if (recentFailures.length < FAILURE_BURST_THRESHOLD) {
        return;
    }
    recentFailures.length = 0;
    sessionCeilingBonus = Math.floor(sessionCeilingBonus / 2);
    suppressedUntil = now + BACKOFF_COOLDOWN_MS;
    rampAnchorAt = suppressedUntil;
    console.warn(`Unattended processing ramp backed off after a burst of failures; session ceiling bonus is now +${sessionCeilingBonus}.`);
};

// How many workers to add on top of the safe interactive baseline right now.
// 0 whenever the tab looks attended (visible/recently used) or is cooling
// down after a backoff.
export const getUnattendedConcurrencyBonus = (): number => {
    if (sessionCeilingBonus <= 0) {
        return 0;
    }
    const now = Date.now();
    if (now - lastActivityAt < IDLE_THRESHOLD_MS || now < suppressedUntil || now < rampAnchorAt) {
        return 0;
    }
    const steps = Math.floor((now - rampAnchorAt) / RAMP_STEP_MS) + 1;
    return Math.min(steps, sessionCeilingBonus);
};
