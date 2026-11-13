import { useCallback, useRef } from 'react';

// Dismiss a popup (the bell's notification preview, a toast) by tapping it or
// flicking it away in any direction. Framework-free, so the DOM toasts in
// services/toast.ts and the React popups share one gesture.
//
// The popup follows the finger or mouse while dragged and fades with distance.
// Let go past DISMISS_DISTANCE_PX, or with a quick flick, and it slides out the
// way it was thrown; let go short of that and it springs back. A press that
// barely moves is a tap and closes it in place. Presses on its own buttons
// (Undo, ...) are left to those buttons.

const TAP_SLOP_PX = 8;
export const DISMISS_DISTANCE_PX = 60;
const FLICK_PX_PER_MS = 0.5;
export const EXIT_MS = 160;
const INTERACTIVE = 'button, a, input, select, textarea';

export const attachSwipeDismiss = (el: HTMLElement, onDismiss: () => void): (() => void) => {
    let drag: { id: number; x: number; y: number; t: number } | null = null;
    let exitTimer: number | undefined;

    const place = (dx: number, dy: number) => {
        const moved = Math.hypot(dx, dy);
        el.style.transform = moved ? `translate(${dx}px, ${dy}px)` : '';
        el.style.opacity = moved ? String(Math.max(0.25, 1 - moved / 220)) : '';
    };
    const leave = (dx: number, dy: number) => {
        const moved = Math.hypot(dx, dy);
        el.style.transition = `transform ${EXIT_MS}ms ease-out, opacity ${EXIT_MS}ms ease-out`;
        if (moved) {
            // Carry on the way it was thrown, far enough to clear its own size.
            const far = Math.max(el.offsetWidth, el.offsetHeight, 120) + 40;
            el.style.transform = `translate(${dx + (dx / moved) * far}px, ${dy + (dy / moved) * far}px)`;
        } else {
            el.style.transform = 'scale(0.96)';
        }
        el.style.opacity = '0';
        exitTimer = window.setTimeout(onDismiss, EXIT_MS);
    };

    const onDown = (e: PointerEvent) => {
        if (drag || exitTimer !== undefined || e.button > 0) return;
        if (e.target instanceof Element && e.target.closest(INTERACTIVE)) return;
        drag = { id: e.pointerId, x: e.clientX, y: e.clientY, t: e.timeStamp };
        el.style.transition = 'none';
        try { el.setPointerCapture?.(e.pointerId); } catch { /* not capturable */ }
    };
    const onMove = (e: PointerEvent) => {
        if (drag?.id !== e.pointerId) return;
        place(e.clientX - drag.x, e.clientY - drag.y);
    };
    const onUp = (e: PointerEvent) => {
        if (drag?.id !== e.pointerId) return;
        const dx = e.clientX - drag.x;
        const dy = e.clientY - drag.y;
        const moved = Math.hypot(dx, dy);
        const speed = moved / Math.max(1, e.timeStamp - drag.t);
        drag = null;
        if (moved < TAP_SLOP_PX) {
            leave(0, 0);
        } else if (moved > DISMISS_DISTANCE_PX || speed > FLICK_PX_PER_MS) {
            leave(dx, dy);
        } else {
            el.style.transition = `transform ${EXIT_MS}ms ease, opacity ${EXIT_MS}ms ease`;
            place(0, 0);
        }
    };
    const onCancel = (e: PointerEvent) => {
        if (drag?.id !== e.pointerId) return;
        drag = null;
        el.style.transition = `transform ${EXIT_MS}ms ease, opacity ${EXIT_MS}ms ease`;
        place(0, 0);
    };

    el.addEventListener('pointerdown', onDown);
    el.addEventListener('pointermove', onMove);
    el.addEventListener('pointerup', onUp);
    el.addEventListener('pointercancel', onCancel);
    return () => {
        el.removeEventListener('pointerdown', onDown);
        el.removeEventListener('pointermove', onMove);
        el.removeEventListener('pointerup', onUp);
        el.removeEventListener('pointercancel', onCancel);
        window.clearTimeout(exitTimer);
    };
};

/** Callback ref that makes the element it lands on tap- and swipe-dismissible.
 *  Re-attaches when React swaps the element (a keyed popup), and a popup that
 *  unmounts mid-exit never calls `onDismiss` for whatever replaced it. */
export function useSwipeDismiss<T extends HTMLElement>(onDismiss: () => void): (el: T | null) => void {
    const dismissRef = useRef(onDismiss);
    dismissRef.current = onDismiss;
    const detachRef = useRef<(() => void) | null>(null);
    return useCallback((el: T | null) => {
        detachRef.current?.();
        detachRef.current = el ? attachSwipeDismiss(el, () => dismissRef.current()) : null;
    }, []);
}
