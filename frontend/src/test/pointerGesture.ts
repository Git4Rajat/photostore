// jsdom's events carry real timestamps, and the swipe-to-dismiss gesture
// (services/swipeDismiss.ts) reads flick speed from them, so tests build
// pointer events with a chosen time.
export const pointer = (el: Element, type: string, x: number, y: number, t: number, target: Element = el) => {
    const event = new Event(type, { bubbles: true, cancelable: true });
    Object.defineProperties(event, {
        pointerId: { value: 1 }, button: { value: 0 }, clientX: { value: x }, clientY: { value: y }, timeStamp: { value: t },
    });
    target.dispatchEvent(event);
};

/** Press, move halfway, and let go `dx`/`dy` from the start, `ms` later. */
export const gesture = (el: Element, dx: number, dy: number, ms: number, target: Element = el) => {
    pointer(el, 'pointerdown', 100, 100, 1000, target);
    pointer(el, 'pointermove', 100 + dx / 2, 100 + dy / 2, 1000 + ms / 2, target);
    pointer(el, 'pointerup', 100 + dx, 100 + dy, 1000 + ms, target);
};
