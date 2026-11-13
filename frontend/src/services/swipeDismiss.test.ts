import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { attachSwipeDismiss, EXIT_MS } from './swipeDismiss';
import { gesture, pointer } from '../test/pointerGesture';

describe('attachSwipeDismiss', () => {
    let el: HTMLDivElement;
    let onDismiss: ReturnType<typeof vi.fn<() => void>>;
    beforeEach(() => {
        vi.useFakeTimers();
        el = document.createElement('div');
        document.body.appendChild(el);
        onDismiss = vi.fn<() => void>();
    });
    afterEach(() => { el.remove(); vi.useRealTimers(); });

    it('a tap closes it', () => {
        attachSwipeDismiss(el, onDismiss);
        gesture(el, 2, 1, 120);
        expect(el.style.opacity).toBe('0');
        expect(onDismiss).not.toHaveBeenCalled();
        vi.advanceTimersByTime(EXIT_MS);
        expect(onDismiss).toHaveBeenCalledTimes(1);
    });

    it.each([
        ['left', -90, 0], ['right', 90, 0], ['up', 0, -90], ['down', 0, 90], ['diagonally', 70, -70],
    ])('a slow drag %s past the threshold throws it out that way', (_dir, dx, dy) => {
        attachSwipeDismiss(el, onDismiss);
        gesture(el, dx, dy, 900);
        const [, x, y] = /translate\((-?[\d.]+)px, (-?[\d.]+)px\)/.exec(el.style.transform) ?? [];
        expect(Math.sign(Number(x))).toBe(Math.sign(dx));
        expect(Math.sign(Number(y))).toBe(Math.sign(dy));
        expect(Math.abs(Number(x)) + Math.abs(Number(y))).toBeGreaterThan(Math.abs(dx) + Math.abs(dy));
        vi.advanceTimersByTime(EXIT_MS);
        expect(onDismiss).toHaveBeenCalledTimes(1);
    });

    it('follows the finger while dragged', () => {
        attachSwipeDismiss(el, onDismiss);
        pointer(el, 'pointerdown', 100, 100, 1000);
        pointer(el, 'pointermove', 130, 80, 1100);
        expect(el.style.transform).toBe('translate(30px, -20px)');
        expect(Number(el.style.opacity)).toBeLessThan(1);
    });

    it('a quick flick closes it even when short', () => {
        attachSwipeDismiss(el, onDismiss);
        gesture(el, 30, 0, 40);
        vi.advanceTimersByTime(EXIT_MS);
        expect(onDismiss).toHaveBeenCalledTimes(1);
    });

    it('a short, slow drag springs back', () => {
        attachSwipeDismiss(el, onDismiss);
        gesture(el, 30, 20, 900);
        expect(el.style.transform).toBe('');
        expect(el.style.opacity).toBe('');
        vi.advanceTimersByTime(EXIT_MS * 3);
        expect(onDismiss).not.toHaveBeenCalled();
    });

    it('leaves presses on its own buttons to the buttons', () => {
        const undo = document.createElement('button');
        el.appendChild(undo);
        attachSwipeDismiss(el, onDismiss);
        gesture(el, 0, 0, 100, undo);
        gesture(el, 120, 0, 900, undo);
        vi.advanceTimersByTime(EXIT_MS * 3);
        expect(onDismiss).not.toHaveBeenCalled();
        expect(el.style.transform).toBe('');
    });

    it('detaching cancels a dismissal already on its way out', () => {
        const detach = attachSwipeDismiss(el, onDismiss);
        gesture(el, 0, 0, 100);
        detach();
        vi.advanceTimersByTime(EXIT_MS * 3);
        expect(onDismiss).not.toHaveBeenCalled();
    });
});
