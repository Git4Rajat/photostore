import { useEffect, useState, type RefObject } from 'react';

const COLLAPSE_AFTER_PX = 80;
const STEP_PX = 8;

/**
 * True while `ref`'s scroll container is being scrolled down past a short threshold; false
 * again on any upward scroll or back near the top. Movements under STEP_PX are ignored (the
 * reference point only advances on a real step), so a jittery or slow drag doesn't flicker.
 */
export const useCollapseOnScroll = (ref: RefObject<HTMLElement | null>): boolean => {
    const [collapsed, setCollapsed] = useState(false);
    useEffect(() => {
        const el = ref.current;
        if (!el) {
            return undefined;
        }
        let last = el.scrollTop;
        const onScroll = () => {
            const top = el.scrollTop;
            const delta = top - last;
            if (top <= COLLAPSE_AFTER_PX) {
                setCollapsed(false);
            } else if (delta > STEP_PX) {
                setCollapsed(true);
            } else if (delta < -STEP_PX) {
                setCollapsed(false);
            } else {
                return;
            }
            last = top;
        };
        el.addEventListener('scroll', onScroll, { passive: true });
        return () => el.removeEventListener('scroll', onScroll);
    }, [ref]);
    return collapsed;
};
