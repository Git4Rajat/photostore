import React from 'react';
import { CheckCircleIcon, HeartIcon } from '@heroicons/react/24/solid';
import { StarIcon } from '@heroicons/react/24/solid';
import { useStore } from '../store';
import type { Photo } from '../types';

/**
 * Selection-aware photo grid. Clicking a tile opens the viewer; the corner
 * checkbox toggles selection (which surfaces the command bar). Rating and like
 * state show as small overlays.
 */
export const PhotoGrid: React.FC<{ photos: Photo[]; emptyHint?: string; gridRef?: React.RefObject<HTMLDivElement> }> = ({ photos, emptyHint }) => {
    const { selection, toggleSelect, selectMany, openViewer } = useStore();
    const [lastSelected, setLastSelected] = React.useState<number | null>(null);
    const [dragStart, setDragStart] = React.useState<number | null>(null);
    const [touchDragStart, setTouchDragStart] = React.useState<number | null>(null);
    const gridRef = React.useRef<HTMLDivElement>(null);

    const getTileIndex = (target: Element): number | null => {
        const tile = (target as Element).closest('.pt-tile');
        if (!tile || !gridRef.current) return null;
        const tiles = Array.from(gridRef.current.querySelectorAll('.pt-tile'));
        return tiles.indexOf(tile);
    };

    const getTileAtPoint = (x: number, y: number): number | null => {
        if (!gridRef.current) return null;
        const element = document.elementFromPoint(x, y);
        return getTileIndex(element as Element);
    };

    const handleTouchStart = (e: React.TouchEvent) => {
        if (e.touches.length === 0) return;
        const touch = e.touches[0];
        const index = getTileAtPoint(touch.clientX, touch.clientY);
        if (index !== null) setTouchDragStart(index);
    };

    const handleTouchMove = (e: React.TouchEvent) => {
        if (touchDragStart === null || e.touches.length === 0) return;
        const touch = e.touches[0];
        const index = getTileAtPoint(touch.clientX, touch.clientY);
        if (index !== null) {
            const start = Math.min(touchDragStart, index);
            const end = Math.max(touchDragStart, index);
            const rangeIds = ids.slice(start, end + 1);
            selectMany(Array.from(new Set([...selection, ...rangeIds])));
        }
    };

    const handleTouchEnd = () => {
        setTouchDragStart(null);
    };

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
        setDragStart(i);
    };

    const handleMouseEnter = (i: number) => {
        if (dragStart !== null) {
            const start = Math.min(dragStart, i);
            const end = Math.max(dragStart, i);
            const rangeIds = ids.slice(start, end + 1);
            selectMany(Array.from(new Set([...selection, ...rangeIds])));
        }
    };

    const handleMouseUp = () => {
        setDragStart(null);
    };

    return (
        <div
            className="pt-grid"
            ref={gridRef}
            onMouseUp={handleMouseUp}
            onMouseLeave={handleMouseUp}
            onTouchStart={handleTouchStart}
            onTouchMove={handleTouchMove}
            onTouchEnd={handleTouchEnd}
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
                        onClick={() => openViewer(ids, i)}
                        onKeyDown={(e) => {
                            if (e.key === 'Enter' || e.key === ' ') {
                                e.preventDefault();
                                openViewer(ids, i);
                            }
                        }}
                        onMouseDown={() => handleMouseDown(i)}
                        onMouseEnter={() => handleMouseEnter(i)}
                    >
                        <button
                            type="button"
                            className={`pt-tile-check${selected ? ' on' : ''}`}
                            aria-label={selected ? 'Deselect' : 'Select'}
                            aria-pressed={selected}
                            onClick={(e) => handleCheckClick(e, i)}
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
