import React, { useEffect, useState } from 'react';
import { ZoomOut as MagnifyingGlassMinusIcon, ZoomIn as MagnifyingGlassPlusIcon } from 'lucide-react';

// Shared thumbnail-size ("zoom") state + control, reused by the Gallery, an
// open album, the Workbench, and a person's photos so every photo grid resizes
// the same way (previously only the Gallery had it).
export const TILE_STEP = 30;
export const TILE_RANGE = { min: 72, max: 260 };
export const TILE_DEFAULT = 120;
export const clampTile = (n: number): number => Math.min(TILE_RANGE.max, Math.max(TILE_RANGE.min, n));

// Per-view size bounds. Photo grids share the default range; the Workbench
// tiles carry a step-badge footer, so they use a larger floor/ceiling.
export interface TileSizeOptions { min?: number; max?: number; default?: number; step?: number }

const loadStoredTileSize = (key: string, min: number, max: number, def: number): number => {
    try {
        const raw = window.localStorage.getItem(key);
        const parsed = raw ? Number(raw) : NaN;
        return Number.isFinite(parsed) ? Math.min(max, Math.max(min, parsed)) : def;
    } catch {
        return def;
    }
};

/** Tile size persisted under `key` so returning to a view keeps the size the
 *  user picked (matches the Gallery's original localStorage behaviour). */
export const useTileSize = (key: string, opts: TileSizeOptions = {}): [number, React.Dispatch<React.SetStateAction<number>>] => {
    const min = opts.min ?? TILE_RANGE.min;
    const max = opts.max ?? TILE_RANGE.max;
    const def = opts.default ?? TILE_DEFAULT;
    const [size, setSize] = useState<number>(() => loadStoredTileSize(key, min, max, def));
    useEffect(() => {
        try {
            window.localStorage.setItem(key, String(size));
        } catch {
            // ignore storage failures (private browsing, quota, ...)
        }
    }, [key, size]);
    return [size, setSize];
};

/** The +/- thumbnail-size control. `onZoomOutFurther`/`onZoomInFurther` let a
 *  host (the Gallery) hook the bounds to step to Months/Years zoom levels
 *  rather than just disabling the button. */
export const ThumbSizeControl: React.FC<{
    value: number;
    onChange: (next: number) => void;
    min?: number;
    max?: number;
    step?: number;
    onZoomOutFurther?: () => void;
    onZoomInFurther?: () => void;
}> = ({ value, onChange, min = TILE_RANGE.min, max = TILE_RANGE.max, step = TILE_STEP, onZoomOutFurther, onZoomInFurther }) => {
    const clamp = (n: number) => Math.min(max, Math.max(min, n));
    const atMin = value <= min;
    const atMax = value >= max;
    return (
        <div className="pt-zoom" role="group" aria-label="Thumbnail size">
            <button
                type="button"
                className="btn"
                aria-label="Smaller thumbnails"
                disabled={atMin && !onZoomOutFurther}
                onClick={() => (atMin ? onZoomOutFurther?.() : onChange(clamp(value - step)))}
            >
                <MagnifyingGlassMinusIcon className="toolbar-icon" />
            </button>
            <button
                type="button"
                className="btn"
                aria-label="Larger thumbnails"
                disabled={atMax && !onZoomInFurther}
                onClick={() => (atMax ? onZoomInFurther?.() : onChange(clamp(value + step)))}
            >
                <MagnifyingGlassPlusIcon className="toolbar-icon" />
            </button>
        </div>
    );
};

export default ThumbSizeControl;
