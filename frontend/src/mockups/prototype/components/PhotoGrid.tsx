import React from 'react';
import { CheckCircleIcon, HeartIcon } from '@heroicons/react/24/solid';
import { StarIcon } from '@heroicons/react/24/solid';
import { useStore } from '../store';
import { usePhotoThumbnails } from '../media';
import { useDragSelect } from '../../../services/useDragSelect';
import type { Photo } from '../types';

/**
 * Selection-aware photo grid. Clicking a tile opens the viewer; the corner
 * checkbox toggles selection (which surfaces the command bar). Rating and like
 * state show as small overlays.
 */
export const PhotoGrid: React.FC<{ photos: Photo[]; emptyHint?: string; gridRef?: React.RefObject<HTMLDivElement>; extendable?: boolean }> = ({ photos, emptyHint, extendable }) => {
    const { selection, toggleSelect, selectMany, openViewer } = useStore();
    const thumbs = usePhotoThumbnails(photos);
    const [lastSelected, setLastSelected] = React.useState<number | null>(null);
    // Drag origin is a ref, not state: mouseenter fires as the pointer moves and
    // must read the current origin synchronously. Reading it from state raced the
    // setState re-render, so a fast sweep saw a stale `null` and selected nothing.
    const dragStartRef = React.useRef<number | null>(null);
    const draggedRef = React.useRef(false);
    const gridRef = React.useRef<HTMLDivElement>(null);
    // Long-press on touch devices enters/toggles selection without opening the
    // viewer; a short tap still opens it. `longPressedRef` suppresses the click
    // that fires right after a long-press so the viewer doesn't pop open.
    const longPressTimer = React.useRef<number | null>(null);
    const longPressedRef = React.useRef(false);
    const clearLongPress = () => {
        if (longPressTimer.current !== null) {
            window.clearTimeout(longPressTimer.current);
            longPressTimer.current = null;
        }
    };

    // Mirrors `selection` synchronously so a touch drag-select gesture (which
    // fires several setSelected calls before React commits a re-render) always
    // reads the up-to-date set instead of a stale closure -- same hazard the
    // mouse sweep above works around with dragStartRef.
    const selectionRef = React.useRef<string[]>(selection);
    React.useEffect(() => {
        selectionRef.current = selection;
    }, [selection]);

    // Press-and-drag multi-select anchored to each tile's checkbox, for touch --
    // the mouse sweep above (mousedown + mouseenter) has no touch equivalent.
    const touchDragSelect = useDragSelect({
        isSelected: (id) => selectionRef.current.includes(id),
        setSelected: (id, isOn) => {
            const cur = selectionRef.current;
            if (cur.includes(id) === isOn) return;
            const next = isOn ? [...cur, id] : cur.filter((x) => x !== id);
            selectionRef.current = next;
            selectMany(next);
        },
    });

    if (!photos.length) {
        return <p className="pt-grid-empty">{emptyHint ?? 'Nothing here yet.'}</p>;
    }

    const ids = photos.map((p) => p.id);

    const handleCheckClick = (e: React.MouseEvent, i: number) => {
        e.stopPropagation();
        if (e.shiftKey && lastSelected !== null) {
            const start = Math.min(lastSelected, i);
            const end = Math.max(lastSelected, i);
            const rangeIds = ids.slice(start, end + 1);
            selectMany(Array.from(new Set([...selection, ...rangeIds])));
        } else {
            toggleSelect(ids[i]);
        }
        setLastSelected(i);
    };

    const handleMouseDown = (i: number) => {
        dragStartRef.current = i;
        draggedRef.current = false;
    };

    const handleMouseEnter = (i: number) => {
        const origin = dragStartRef.current;
        if (origin === null || origin === i) return;
        draggedRef.current = true;
        const start = Math.min(origin, i);
        const end = Math.max(origin, i);
        // Recompute the whole origin..current range each move (merged with the
        // pre-drag selection), so it's robust to the closure's `selection` being
        // a render behind. lastSelected anchors a later shift-click.
        selectMany(Array.from(new Set([...selection, ...ids.slice(start, end + 1)])));
        setLastSelected(i);
    };

    const handleMouseUp = () => {
        dragStartRef.current = null;
    };

    return (
        <div
            className="pt-grid"
            ref={gridRef}
            onMouseUp={handleMouseUp}
            onMouseLeave={handleMouseUp}
        >
            {photos.map((photo, i) => {
                const selected = selection.includes(photo.id);
                return (
                    <div
                        key={photo.id}
                        className={`pt-tile mock-swatch ${photo.swatch}${selected ? ' selected' : ''}`}
                        role="button"
                        tabIndex={0}
                        data-photo-id={photo.id}
                        data-tile-id={photo.id}
                        onClick={() => {
                            // Suppress the click that follows a long-press or a
                            // drag-select so it doesn't also open the viewer.
                            if (longPressedRef.current || draggedRef.current) {
                                longPressedRef.current = false;
                                draggedRef.current = false;
                                return;
                            }
                            openViewer(ids, i, { extendable });
                        }}
                        onKeyDown={(e) => {
                            if (e.key === 'Enter' || e.key === ' ') {
                                e.preventDefault();
                                openViewer(ids, i, { extendable });
                            }
                        }}
                        onMouseDown={() => handleMouseDown(i)}
                        onMouseEnter={() => handleMouseEnter(i)}
                        onTouchStart={() => {
                            longPressedRef.current = false;
                            clearLongPress();
                            longPressTimer.current = window.setTimeout(() => {
                                longPressedRef.current = true;
                                toggleSelect(ids[i]);
                                setLastSelected(i);
                            }, 400);
                        }}
                        onTouchMove={clearLongPress}
                        onTouchEnd={clearLongPress}
                    >
                        {thumbs[photo.filename] && (
                            <img
                                className="pt-tile-img"
                                src={thumbs[photo.filename]}
                                alt={photo.filename}
                                loading="lazy"
                                draggable={false}
                            />
                        )}
                        <button
                            type="button"
                            className={`pt-tile-check${selected ? ' on' : ''}`}
                            aria-label={selected ? 'Deselect' : 'Select'}
                            aria-pressed={selected}
                            onClick={(e) => handleCheckClick(e, i)}
                            // The tile's own onTouchStart (below) starts a
                            // long-press timer on any touch, including this
                            // button; stop it here so pressing the checkbox
                            // to start a drag doesn't also toggle via long-press.
                            onTouchStart={(e) => e.stopPropagation()}
                            {...touchDragSelect}
                        >
                            <CheckCircleIcon />
                        </button>
                        <span className="pt-tile-badges" aria-hidden="true">
                            {photo.liked && <HeartIcon className="liked" />}
                            {photo.rating > 0 && (
                                <span className="rating">
                                    <StarIcon /> {photo.rating}
                                </span>
                            )}
                        </span>
                    </div>
                );
            })}
        </div>
    );
};

export default PhotoGrid;
