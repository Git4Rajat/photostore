import { useEffect, useState } from 'react';

// Eligible once .pt-body is scrolled this far, and only revealed after scrolling has settled.
const SHOW_AFTER_PX = 400;
const SETTLE_MS = 150;
// A scroll event this soon after a wheel/touch/key/pointer input is the user's own (momentum
// included). Any other scroll is the browser adjusting scrollTop because rows landed above the
// viewport (photos pouring in during an upload), which must not make the buttons flicker.
const USER_SCROLL_MS = 1500;
const USER_INPUTS = ['wheel', 'touchstart', 'touchmove', 'touchend', 'pointerdown', 'keydown'] as const;

/**
 * Whether a floating scroll control should show: scrolled past the threshold and settled.
 * Hidden while the user is scrolling; left alone while content shifts under a still reader.
 * `eligible` can veto the reveal (e.g. nothing to select on this page).
 */
export const useScrollSettle = (eligible: () => boolean = () => true): boolean => {
    const [visible, setVisible] = useState(false);

    useEffect(() => {
        const container = document.querySelector<HTMLElement>('.pt-body');
        if (!container) return undefined;
        let timer: ReturnType<typeof setTimeout> | null = null;
        let lastInput = Number.NEGATIVE_INFINITY;
        let shown = false;
        const show = (next: boolean) => { shown = next; setVisible(next); };
        const onInput = () => { lastInput = Date.now(); };
        const onScroll = () => {
            if (container.scrollTop <= SHOW_AFTER_PX) {
                if (timer) clearTimeout(timer);
                timer = null;
                show(false);
                return;
            }
            const byUser = Date.now() - lastInput < USER_SCROLL_MS;
            if (!byUser && shown) return; // content shifted under a reader who is not scrolling
            if (timer) clearTimeout(timer);
            show(false);
            timer = setTimeout(() => { timer = null; show(eligible()); }, SETTLE_MS);
        };
        USER_INPUTS.forEach((name) => container.addEventListener(name, onInput, { passive: true }));
        container.addEventListener('scroll', onScroll, { passive: true });
        return () => {
            USER_INPUTS.forEach((name) => container.removeEventListener(name, onInput));
            container.removeEventListener('scroll', onScroll);
            if (timer) clearTimeout(timer);
        };
        // `eligible` is read at reveal time; callers pass a closure over stable inputs.
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    return visible;
};
