import { useRef } from 'react';

const LONG_PRESS_MS = 500;
const MOVE_CANCEL_PX = 10;

export interface LongPressHandlers {
    onPointerDown: (event: React.PointerEvent) => void;
    onPointerMove: (event: React.PointerEvent) => void;
    onPointerUp: (event: React.PointerEvent) => void;
    onPointerCancel: (event: React.PointerEvent) => void;
    onClickCapture: (event: React.MouseEvent) => void;
}

// Touch/pen-only long-press: holding a pointer still for LONG_PRESS_MS fires
// `onLongPress` and swallows the click the browser synthesizes on release, so
// the tile's normal tap behavior (select / open lightbox) doesn't also fire.
// Mouse input is left alone entirely -- desktop already has hover for this.
export const useLongPress = (onLongPress?: () => void): LongPressHandlers => {
    const timerRef = useRef<number | null>(null);
    const originRef = useRef<{ x: number; y: number } | null>(null);
    const firedRef = useRef(false);

    const clearTimer = () => {
        if (timerRef.current !== null) {
            window.clearTimeout(timerRef.current);
            timerRef.current = null;
        }
    };

    const onPointerDown = (event: React.PointerEvent) => {
        if (!onLongPress || event.pointerType === 'mouse') {
            return;
        }
        firedRef.current = false;
        originRef.current = { x: event.clientX, y: event.clientY };
        clearTimer();
        timerRef.current = window.setTimeout(() => {
            firedRef.current = true;
            navigator.vibrate?.(10);
            onLongPress();
        }, LONG_PRESS_MS);
    };

    const onPointerMove = (event: React.PointerEvent) => {
        if (!originRef.current) {
            return;
        }
        const dx = event.clientX - originRef.current.x;
        const dy = event.clientY - originRef.current.y;
        if (Math.hypot(dx, dy) > MOVE_CANCEL_PX) {
            clearTimer();
        }
    };

    const onPointerUp = () => {
        clearTimer();
    };

    const onPointerCancel = () => {
        clearTimer();
        firedRef.current = false;
    };

    // Capture phase, on the same element as onPointerDown -- runs before that
    // element's (or any descendant's) own bubble-phase onClick, so this is
    // what actually prevents a fired long-press from also registering as a tap.
    const onClickCapture = (event: React.MouseEvent) => {
        if (firedRef.current) {
            firedRef.current = false;
            event.preventDefault();
            event.stopPropagation();
        }
    };

    return { onPointerDown, onPointerMove, onPointerUp, onPointerCancel, onClickCapture };
};
