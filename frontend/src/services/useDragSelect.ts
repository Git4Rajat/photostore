import { useEffect, useRef } from 'react';

const MOVE_THRESHOLD_PX = 6;
// Start auto-scrolling once the pointer gets this close to the scroll
// container's top/bottom edge, so a drag-select can reach photos below the
// fold without the user lifting their finger to scroll manually first.
const AUTO_SCROLL_EDGE_PX = 72;
const AUTO_SCROLL_MAX_SPEED_PX = 18;

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
    // Snapshot of every tile's id (in rendered/DOM order) and which of them
    // were already selected, taken once at gesture start -- see applyRange.
    const orderedIdsRef = useRef<string[]>([]);
    const originIndexRef = useRef<number | null>(null);
    const preDragSelectedRef = useRef<Set<string>>(new Set());
    const appliedRef = useRef<Set<string>>(new Set());
    const lastPointerRef = useRef<{ x: number; y: number } | null>(null);
    const autoScrollFrameRef = useRef<number | null>(null);
    // Guards the scroll-cancel handler below against the auto-scroll loop's
    // own scrollBy() calls -- container.scrollBy() fires a non-bubbling
    // 'scroll' event that the capture-phase window listener still observes,
    // and without this it would cancel the very drag that's auto-scrolling.
    const isAutoScrollingRef = useRef(false);

    // Cancel drag selection if the user scrolls, since scroll changes which
    // tiles are under the pointer coordinates.
    useEffect(() => {
        const handleScroll = () => {
            if (isAutoScrollingRef.current) {
                return;
            }
            originRef.current = null;
            draggingRef.current = false;
            if (autoScrollFrameRef.current !== null) {
                cancelAnimationFrame(autoScrollFrameRef.current);
                autoScrollFrameRef.current = null;
            }
        };
        window.addEventListener('scroll', handleScroll, { capture: true });
        return () => {
            window.removeEventListener('scroll', handleScroll, { capture: true });
            if (autoScrollFrameRef.current !== null) {
                cancelAnimationFrame(autoScrollFrameRef.current);
            }
        };
    }, []);

    const tileIdAt = (x: number, y: number): string | undefined => {
        const el = document.elementFromPoint(x, y);
        const tile = el instanceof Element ? el.closest<HTMLElement>('[data-tile-id]') : null;
        return tile?.dataset.tileId;
    };

    // Recomputes the whole origin..current range each move (rather than just
    // marking each newly-crossed tile "visited" and never revisiting it), and
    // reconciles it against the *previous* applied range -- a tile that falls
    // out of range as the gesture reverses back toward its origin is restored
    // to its pre-drag state instead of staying stuck at whatever it was set to
    // on the way out. Mirrors the equivalent mouse-drag fix in PhotoGrid.tsx.
    const applyRange = (currentId: string) => {
        const ids = orderedIdsRef.current;
        const origin = originIndexRef.current;
        if (origin === null) {
            return;
        }
        const currentIndex = ids.indexOf(currentId);
        if (currentIndex === -1) {
            return;
        }
        const start = Math.min(origin, currentIndex);
        const end = Math.max(origin, currentIndex);
        const rangeIds = new Set(ids.slice(start, end + 1));
        appliedRef.current.forEach((id) => {
            if (!rangeIds.has(id)) {
                const shouldBe = preDragSelectedRef.current.has(id);
                if (isSelected(id) !== shouldBe) {
                    setSelected(id, shouldBe);
                }
            }
        });
        rangeIds.forEach((id) => {
            if (isSelected(id) !== targetStateRef.current) {
                setSelected(id, targetStateRef.current);
            }
        });
        appliedRef.current = rangeIds;
    };

    const applyAt = (x: number, y: number) => {
        const id = tileIdAt(x, y);
        if (id) {
            applyRange(id);
        }
    };

    const stopAutoScroll = () => {
        if (autoScrollFrameRef.current !== null) {
            cancelAnimationFrame(autoScrollFrameRef.current);
            autoScrollFrameRef.current = null;
        }
    };

    // .pt-body is the app's one page-content scroll container (see
    // PrototypeApp.tsx's Shell) -- every grid this hook is wired into lives
    // inside it, so there's no need to thread a container ref through props.
    const scrollContainer = (): HTMLElement | null => document.querySelector<HTMLElement>('.pt-body');

    const autoScrollTick = () => {
        // Cleared here, one full frame after it was last set to true by a
        // scrollBy() below -- not synchronously right after that call, since
        // the 'scroll' event it triggers fires asynchronously (the next
        // frame), and clearing the flag immediately closed the guard window
        // before that event arrived, so the very first auto-scroll frame
        // always self-cancelled the drag.
        isAutoScrollingRef.current = false;
        autoScrollFrameRef.current = null;
        if (!draggingRef.current || !lastPointerRef.current) {
            return;
        }
        const container = scrollContainer();
        const pointer = lastPointerRef.current;
        if (container) {
            const rect = container.getBoundingClientRect();
            let delta = 0;
            if (pointer.y < rect.top + AUTO_SCROLL_EDGE_PX) {
                const intensity = Math.min(1, (rect.top + AUTO_SCROLL_EDGE_PX - pointer.y) / AUTO_SCROLL_EDGE_PX);
                delta = -Math.ceil(AUTO_SCROLL_MAX_SPEED_PX * intensity);
            } else if (pointer.y > rect.bottom - AUTO_SCROLL_EDGE_PX) {
                const intensity = Math.min(1, (pointer.y - (rect.bottom - AUTO_SCROLL_EDGE_PX)) / AUTO_SCROLL_EDGE_PX);
                delta = Math.ceil(AUTO_SCROLL_MAX_SPEED_PX * intensity);
            }
            if (delta !== 0) {
                isAutoScrollingRef.current = true;
                container.scrollBy({ top: delta });
                // The pointer didn't move, but the content under it just did --
                // resample so tiles scrolled into place still get painted.
                applyAt(pointer.x, pointer.y);
            }
        }
        autoScrollFrameRef.current = requestAnimationFrame(autoScrollTick);
    };

    const startAutoScrollLoop = () => {
        if (autoScrollFrameRef.current === null) {
            autoScrollFrameRef.current = requestAnimationFrame(autoScrollTick);
        }
    };

    const onPointerDown = (event: React.PointerEvent) => {
        const id = tileIdAt(event.clientX, event.clientY);
        if (!id) {
            return;
        }
        originRef.current = { x: event.clientX, y: event.clientY };
        draggingRef.current = false;
        targetStateRef.current = !isSelected(id);
        const ids = Array.from(document.querySelectorAll<HTMLElement>('[data-tile-id]'))
            .map((el) => el.dataset.tileId)
            .filter((tileId): tileId is string => Boolean(tileId));
        orderedIdsRef.current = ids;
        originIndexRef.current = ids.indexOf(id);
        preDragSelectedRef.current = new Set(ids.filter((tileId) => isSelected(tileId)));
        appliedRef.current = new Set();
        (event.currentTarget as Element).setPointerCapture(event.pointerId);
    };

    const onPointerMove = (event: React.PointerEvent) => {
        if (!originRef.current) {
            return;
        }
        lastPointerRef.current = { x: event.clientX, y: event.clientY };
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
            startAutoScrollLoop();
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
        stopAutoScroll();
    };

    const onPointerCancel = () => {
        originRef.current = null;
        draggingRef.current = false;
        stopAutoScroll();
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
