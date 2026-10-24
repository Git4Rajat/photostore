// Network demand comes from a person using the page, never upload progress or
// background timers. An already loaded idle page can keep local thumbnails.
export const FOREGROUND_IDLE_MS = 5 * 60 * 1000;
let lastInput = Date.now();
const listeners = new Set<() => void>();
export const hasForegroundDemand = (): boolean =>
    (typeof document === 'undefined' || document.visibilityState !== 'hidden')
    && Date.now() - lastInput < FOREGROUND_IDLE_MS;
export const noteForegroundActivity = (): void => {
    const active = hasForegroundDemand();
    lastInput = Date.now();
    if (!active && hasForegroundDemand()) listeners.forEach(listener => listener());
};
export const onForegroundReturn = (listener: () => void): (() => void) => {
    listeners.add(listener);
    return () => { listeners.delete(listener); };
};
if (typeof window !== 'undefined') {
    ['pointerdown', 'pointermove', 'keydown', 'wheel', 'touchstart', 'focus'].forEach(event =>
        window.addEventListener(event, noteForegroundActivity, { passive: true }));
    document.addEventListener('visibilitychange', () => {
        if (document.visibilityState !== 'hidden') {
            lastInput = Date.now();
            listeners.forEach(listener => listener());
        }
    });
}
