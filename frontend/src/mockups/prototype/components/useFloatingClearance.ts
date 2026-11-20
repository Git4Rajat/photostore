import { useEffect } from 'react';

// Fixed bottom-of-screen surfaces that can sit where the floating buttons
// (Scroll to top, Select) live: the upload / processing dock bar or tray, the
// selection action bar and toasts.
export const FLOAT_OBSTACLES = '.up-dock, .pt-action-bar, .pt-toast';
export const FLOAT_CLEAR_VAR = '--pt-float-clear';
const GAP_PX = 12;
// The Select pill sits left of the round button; anything reaching into this
// right-hand strip can collide with one of them.
const BUTTON_STRIP_PX = 220;

/**
 * Publishes `--pt-float-clear` (the lowest `bottom` the floating buttons may
 * use) on <html>, so the CSS can do `bottom: max(<resting position>, var(...))`
 * and the buttons rise above whichever dock / bar / toast is showing, then drop
 * back when it hides, collapses or shrinks.
 */
export const useFloatingClearance = (): void => {
    useEffect(() => {
        const root = document.documentElement;
        const observed = new Set<Element>();
        const resizeObserver = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(() => schedule());
        let frame = 0;

        function measure() {
            frame = 0;
            let clear = 0;
            const found = new Set<Element>();
            document.querySelectorAll(FLOAT_OBSTACLES).forEach((el) => {
                found.add(el);
                if (!observed.has(el)) {
                    resizeObserver?.observe(el);
                    observed.add(el);
                }
                const rect = el.getBoundingClientRect();
                if (rect.width === 0 || rect.height === 0 || rect.right < window.innerWidth - BUTTON_STRIP_PX) {
                    return;
                }
                clear = Math.max(clear, window.innerHeight - rect.top + GAP_PX);
            });
            observed.forEach((el) => {
                if (!found.has(el)) {
                    resizeObserver?.unobserve(el);
                    observed.delete(el);
                }
            });
            if (clear > 0) {
                root.style.setProperty(FLOAT_CLEAR_VAR, `${Math.round(clear)}px`);
            } else {
                root.style.removeProperty(FLOAT_CLEAR_VAR);
            }
        }
        function schedule() {
            if (!frame) {
                frame = requestAnimationFrame(measure);
            }
        }

        const mutationObserver = new MutationObserver(schedule);
        mutationObserver.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ['class', 'hidden', 'style'] });
        window.addEventListener('resize', schedule);
        schedule();
        return () => {
            mutationObserver.disconnect();
            resizeObserver?.disconnect();
            window.removeEventListener('resize', schedule);
            if (frame) {
                cancelAnimationFrame(frame);
            }
            root.style.removeProperty(FLOAT_CLEAR_VAR);
        };
    }, []);
};
