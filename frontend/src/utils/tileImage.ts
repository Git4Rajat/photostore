import { useLayoutEffect, useState, type CSSProperties, type RefObject } from 'react';

// How a square photo tile shows the 120 px thumbnail and the sharp grid image
// (backend grid_images.py). Dependency-free so the shared-album page, which
// also ships in the slim static-album bundle, can use it without the app's
// media services.

// Widths declared in tile srcsets. Tiles are square and crop with object-fit:
// cover, so what has to cover a tile is an image's SHORT edge, not its width:
// ~90 px for a typical 4:3 photo's 120 px thumbnail, and 400 px for the grid
// image (GRID_SHORT_EDGE in backend/image_utils.py). The browser therefore
// keeps the thumbnail only for tiles up to ~90 device px -- the smallest zoom
// level on a 1x screen -- and takes the grid image everywhere else.
const THUMB_COVER_WIDTH = 90;
const GRID_COVER_WIDTH = 400;

export interface TileImage {
    src: string;
    srcSet?: string;
    style?: CSSProperties;
}

const normRot = (value?: number) => { const r = Number(value || 0) % 360; return r < 0 ? r + 360 : r; };
const rotateStyle = (degrees: number): CSSProperties | undefined => (degrees ? { transform: `rotate(${degrees}deg)` } : undefined);

/** The turn a tile showing the thumbnail needs: only what the user rotated beyond its baked rotation. */
export const thumbnailTileStyle = (rotation?: number, thumbnailRotation?: number): CSSProperties | undefined => (
    rotateStyle(normRot(normRot(rotation) - normRot(thumbnailRotation)))
);

/**
 * What a tile's <img> shows: the thumbnail plus, when there is one, the sharp
 * grid image in srcset.
 *
 * The thumbnail bakes in `thumbnailRotation`, so its tile turns by only what
 * the user rotated on top; the grid image, like the preview, bakes in nothing
 * but EXIF orientation, so its tile turns by the full `rotation`. One <img>
 * carries one transform, so the two can share a srcset only when the
 * thumbnail has no baked rotation; otherwise the tile shows the grid image
 * alone, which is sharp at every zoom level anyway.
 */
export const composeTileImage = (
    thumbSrc: string | undefined, gridSrc: string | undefined, rotation?: number, thumbnailRotation?: number,
): TileImage | null => {
    if (!gridSrc) {
        return thumbSrc ? { src: thumbSrc, style: thumbnailTileStyle(rotation, thumbnailRotation) } : null;
    }
    const style = rotateStyle(normRot(rotation));
    if (!thumbSrc || normRot(thumbnailRotation)) {
        return { src: gridSrc, style };
    }
    return { src: thumbSrc, srcSet: `${thumbSrc} ${THUMB_COVER_WIDTH}w, ${gridSrc} ${GRID_COVER_WIDTH}w`, style };
};

/**
 * `sizes` for a grid's tile srcsets: the rendered width of its first tile,
 * re-measured whenever that tile resizes (zoom level, window). Measured in a
 * layout effect so it lands before lazy tiles start loading. Undefined until
 * measured, which the browser reads as 100vw -- it then picks the grid image.
 */
export function useTileSizes(gridRef: RefObject<HTMLElement>, firstTileKey?: string): string | undefined {
    const [width, setWidth] = useState(0);
    useLayoutEffect(() => {
        const tile = gridRef.current?.firstElementChild;
        if (!tile) return undefined;
        const measure = () => setWidth(Math.round(tile.getBoundingClientRect().width));
        measure();
        if (typeof ResizeObserver === 'undefined') return undefined;
        const observer = new ResizeObserver(measure);
        observer.observe(tile);
        return () => observer.disconnect();
    }, [gridRef, firstTileKey]);
    return width > 0 ? `${width}px` : undefined;
}
