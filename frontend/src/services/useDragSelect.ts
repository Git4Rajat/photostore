import { useRef } from 'react';

const MOVE_THRESHOLD_PX = 6;

export interface DragSelectHandlers {
    onPointerDown: (event: React.PointerEvent) => void;
    onPointerMove: (event: React.PointerEvent) => void;
    onPointerUp: (event: React.PointerEvent) => void;
    onPointerCancel: (event: React.PointerEvent) => void;
    onClickCapture: (event: React.MouseEvent) => void;
}

interface UseDragSelectOptions {
    isSelected: (id: string) => boolean;
    setSelected: (id: string, selected: boolean) => void;
}

// Press-and-drag multi-select, anchored to each tile's checkbox: pressing one
// down and dragging across neighboring tiles paints the same selection state
// onto every tile the pointer crosses -- the web equivalent of iOS Photos'
// slide-to-select, and doubles as a click-and-drag select for the mouse.
// Anchoring to the checkbox (rather than the whole tile) means a touch-drag
// starting anywhere else on a tile is left alone and still scrolls the page
// normally, and a plain tap/click still falls through to the checkbox's own
// onChange exactly as before.
export const useDragSelect = ({ isSelected, setSelected }: UseDragSelectOptions): DragSelectHandlers => {
    const originRef = useRef<{ x: number; y: number } | null>(null);
    const draggingRef = useRef(false);
    const targetStateRef = useRef(false);
    const visitedRef = useRef<Set<string>>(new Set());

    const tileIdAt = (x: number, y: number): string | undefined => {
        const el = document.elementFromPoint(x, y);
        const tile = el instanceof Element ? el.closest<HTMLElement>('[data-tile-id]') : null;
        return tile?.dataset.tileId;
    };

    const applyAt = (x: number, y: number) => {
        const id = tileIdAt(x, y);
        if (!id || visitedRef.current.has(id)) {
            return;
        }
        visitedRef.current.add(id);
        setSelected(id, targetStateRef.current);
    };

    const onPointerDown = (event: React.PointerEvent) => {
        const id = tileIdAt(event.clientX, event.clientY);
        if (!id) {
            return;
        }
        originRef.current = { x: event.clientX, y: event.clientY };
        draggingRef.current = false;
        targetStateRef.current = !isSelected(id);
        visitedRef.current = new Set();
        (event.currentTarget as Element).setPointerCapture(event.pointerId);
    };

    const onPointerMove = (event: React.PointerEvent) => {
        if (!originRef.current) {
            return;
        }
        if (!draggingRef.current) {
            const dx = event.clientX - originRef.current.x;
            const dy = event.clientY - originRef.current.y;
            if (Math.hypot(dx, dy) < MOVE_THRESHOLD_PX) {
                return;
            }
            draggingRef.current = true;
            // Confirmed drag rather than a tap -- paint the tile the gesture
            // started on too, since its own click/onChange won't fire now.
            applyAt(originRef.current.x, originRef.current.y);
        }
        applyAt(event.clientX, event.clientY);
    };

    const onPointerUp = (event: React.PointerEvent) => {
        if (draggingRef.current) {
            // For touch, this suppresses the synthetic click that would
            // otherwise double-toggle whichever tile the finger lifts over.
            event.preventDefault();
        }
        originRef.current = null;
    };

    const onPointerCancel = () => {
        originRef.current = null;
        draggingRef.current = false;
    };

    // Capture phase, same element as onPointerDown -- runs before that
    // element's onChange, so this is what stops a confirmed drag from also
    // registering as a click (mirrors useLongPress's onClickCapture).
    const onClickCapture = (event: React.MouseEvent) => {
        if (draggingRef.current) {
            draggingRef.current = false;
            event.preventDefault();
            event.stopPropagation();
        }
    };

    return { onPointerDown, onPointerMove, onPointerUp, onPointerCancel, onClickCapture };
};
