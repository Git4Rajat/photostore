import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { FOREGROUND_IDLE_MS, hasForegroundDemand, noteForegroundActivity, onForegroundReturn } from './foregroundDemand';
beforeEach(() => {
    vi.useFakeTimers();
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' });
    noteForegroundActivity();
});
afterEach(() => vi.useRealTimers());
it('expires on a visible unattended page and resumes only on real activity', () => {
    const resumed=vi.fn(); const stop=onForegroundReturn(resumed);
    expect(hasForegroundDemand()).toBe(true);
    vi.advanceTimersByTime(FOREGROUND_IDLE_MS);
    expect(hasForegroundDemand()).toBe(false);
    window.dispatchEvent(new Event('pointerdown'));
    expect(hasForegroundDemand()).toBe(true);
    expect(resumed).toHaveBeenCalledTimes(1);
    window.dispatchEvent(new Event('pointermove'));
    expect(resumed).toHaveBeenCalledTimes(1);
    stop();
});
it('does not turn background work into foreground demand', () => {
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'hidden' });
    noteForegroundActivity();
    expect(hasForegroundDemand()).toBe(false);
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' });
    document.dispatchEvent(new Event('visibilitychange'));
    expect(hasForegroundDemand()).toBe(true);
});
