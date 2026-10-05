/**
 * How many tiles fit on screen, so the gallery can load a viewport-sized window
 * of thumbnails per step instead of a fixed page. With the media token the
 * thumbnails come straight from storage, so loading "as many as the page can
 * show" costs no backend calls -- only the metadata enrichment is chunked.
 */
export interface GridCapacityInput {
    columns: number;
    tileWidth: number;
    gap: number;
    viewportHeight: number;
}

/** Tiles visible at once (a partially visible last row counts as a full row). */
export const computeGridCapacity = ({ columns, tileWidth, gap, viewportHeight }: GridCapacityInput): number => {
    const safeColumns = Math.max(1, Math.floor(columns));
    const pitch = Math.max(1, tileWidth + gap);
    return safeColumns * Math.max(1, Math.ceil(viewportHeight / pitch));
};

export const MIN_PAGE_SIZE = 60;
export const MAX_PAGE_SIZE = 600;
export const SCREENS_PER_PAGE = 3;

/** Page size = a few screens' worth of tiles, clamped to sane bounds. */
export const pageSizeForCapacity = (capacity: number, screens = SCREENS_PER_PAGE): number => (
    Math.min(MAX_PAGE_SIZE, Math.max(MIN_PAGE_SIZE, Math.round(capacity * screens)))
);

const FALLBACK_TILE_MIN = 120;
const FALLBACK_GAP = 6;

/**
 * Reads the live column count / tile width from the rendered `.pt-grid` (a CSS
 * auto-fill grid), falling back to an estimate from the window size before the
 * first grid has mounted.
 */
export const measureGridCapacity = (doc: Document = document, win: Window = window): number => {
    const viewportHeight = win.innerHeight || 800;
    const grid = doc.querySelector<HTMLElement>('.pt-grid');
    if (grid) {
        const style = win.getComputedStyle(grid);
        const tracks = style.gridTemplateColumns.split(/\s+/).map((t) => parseFloat(t)).filter((n) => Number.isFinite(n) && n > 0);
        const gap = parseFloat(style.columnGap) || FALLBACK_GAP;
        if (tracks.length > 0) {
            return computeGridCapacity({ columns: tracks.length, tileWidth: tracks[0], gap, viewportHeight });
        }
    }
    const width = Math.max(320, (win.innerWidth || 1200) - ((win.innerWidth || 0) > 900 ? 260 : 0));
    const columns = Math.max(1, Math.floor((width + FALLBACK_GAP) / (FALLBACK_TILE_MIN + FALLBACK_GAP)));
    return computeGridCapacity({ columns, tileWidth: width / columns, gap: FALLBACK_GAP, viewportHeight });
};

export const chunk = <T,>(items: T[], size: number): T[][] => {
    const out: T[][] = [];
    for (let i = 0; i < items.length; i += size) out.push(items.slice(i, i + size));
    return out;
};
