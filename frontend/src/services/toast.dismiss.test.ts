import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { showToast } from './toast';
import { EXIT_MS } from './swipeDismiss';
import { gesture } from '../test/pointerGesture';

// Pages without the notification panel (the public album) get these DOM toasts.
describe('showToast without a notification panel', () => {
    beforeEach(() => vi.useFakeTimers());
    afterEach(() => { document.querySelector('.app-toast-stack')?.remove(); vi.useRealTimers(); });

    it('closes on a swipe, well before its timeout', () => {
        showToast('Link copied', { timeout: 60_000 });
        const el = document.querySelector('.app-toast')!;
        expect(el.textContent).toContain('Link copied');
        gesture(el, 0, 90, 500);
        vi.advanceTimersByTime(EXIT_MS + 400);
        expect(document.querySelector('.app-toast')).toBeNull();
    });

    it('closes on a tap', () => {
        showToast('Download failed', { timeout: 60_000, variant: 'error' });
        gesture(document.querySelector('.app-toast')!, 0, 0, 100);
        vi.advanceTimersByTime(EXIT_MS + 400);
        expect(document.querySelector('.app-toast')).toBeNull();
    });
});
