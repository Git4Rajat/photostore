import { useLayoutEffect, useMemo, useRef, useState } from 'react';

// Pure "scroll position -> visible row range" arithmetic for a CSS auto-fill
// grid, split out from the hook below so it's testable with plain numbers --
// no DOM, no ResizeObserver, no jsdom layout quirks involved.
export interface WindowRangeInput {
    itemCount: number;
    columnCount: number;
    rowHeight: number;
    rowGap: number;
    overscanRows: number;
    // Distance (px) from the top of the viewport to the top of the windowed
    // container (i.e. `container.getBoundingClientRect().top`). Negative
    // means the container has scrolled past the top of the viewport.
    containerTopOffset: number;
    viewportHeight: number;
}

export interface WindowRange {
    firstRow: number;
    lastRowExclusive: number;
    totalRows: number;
}

export const computeWindowRange = ({
    itemCount, columnCount, rowHeight, rowGap, overscanRows, containerTopOffset, viewportHeight,
}: WindowRangeInput): WindowRange => {
    const safeColumnCount = Math.max(1, columnCount);
    const rowPitch = Math.max(1, rowHeight + rowGap);
    const totalRows = Math.max(0, Math.ceil(itemCount / safeColumnCount));

    if (totalRows === 0) {
        return { firstRow: 0, lastRowExclusive: 0, totalRows: 0 };
    }

    // How far the viewport's visible band sits within the container's own
    // (unscrolled) coordinate space: the container's content starts at y=0,
    // so the point currently at the top of the screen is `-containerTopOffset`
    // into the container's content.
    const viewTop = -containerTopOffset - overscanRows * rowPitch;
    const viewBottom = -containerTopOffset + viewportHeight + overscanRows * rowPitch;

    const firstRow = Math.min(Math.max(0, Math.floor(viewTop / rowPitch)), totalRows - 1);
    const lastRowExclusive = Math.min(totalRows, Math.max(firstRow + 1, Math.ceil(viewBottom / rowPitch)));

    return { firstRow, lastRowExclusive, totalRows };
};

// Row range (already known) -> item-index slice + pixel offsets. Kept
// separate from computeWindowRange because the render path derives this from
// already-committed state (no fresh scroll position available mid-render),
// while computeWindowRange derives the row range itself from a live measurement.
const deriveSlice = (itemCount: number, columnCount: number, rowHeight: number, rowGap: number, firstRow: number, lastRowExclusive: number) => {
    const safeColumnCount = Math.max(1, columnCount);
    const rowPitch = Math.max(1, rowHeight + rowGap);
    const totalRows = Math.max(0, Math.ceil(itemCount / safeColumnCount));
    const totalHeightPx = totalRows > 0 ? totalRows * rowHeight + (totalRows - 1) * rowGap : 0;
    return {
        startIndex: firstRow * safeColumnCount,
        endIndex: Math.min(itemCount, lastRowExclusive * safeColumnCount),
        offsetPx: firstRow * rowPitch,
        totalHeightPx,
    };
};

export interface UseWindowedGridOptions<T> {
    items: T[];
    getKey: (item: T) => string;
    // Extra rows kept mounted above/below the viewport so a fast scroll/fling
    // doesn't show blank space before the next recompute lands.
    overscanRows?: number;
    // Anything layout-affecting the hook can't observe itself (a per-tile
    // expand/collapse toggle that changes row height, a zoom level that
    // changes tile size but not container width) -- bumping one of these
    // forces an immediate remeasure instead of waiting for the next
    // ResizeObserver/scroll tick.
    layoutDeps?: readonly unknown[];
    // false = render every item, no windowing. Default true.
    enabled?: boolean;
}

export interface UseWindowedGridResult<T> {
    containerRef: React.MutableRefObject<HTMLDivElement | null>;
    innerRef: React.MutableRefObject<HTMLDivElement | null>;
    spacerStyle: React.CSSProperties;
    innerStyle: React.CSSProperties;
    visibleItems: T[];
    startIndex: number;
    isVirtualized: boolean;
    shouldAnimateEntrance: (key: string) => boolean;
}

const DEFAULT_OVERSCAN_ROWS = 3;
const INITIAL_ROW_HEIGHT_GUESS = 200;

interface Metrics {
    columnCount: number;
    rowHeight: number;
    rowGap: number;
}

const estimateInitialColumnCount = () => (
    typeof window === 'undefined' ? 4 : Math.max(1, Math.floor(window.innerWidth / 180))
);

export function useWindowedGrid<T>({
    items,
    getKey,
    overscanRows = DEFAULT_OVERSCAN_ROWS,
    layoutDeps = [],
    enabled = true,
}: UseWindowedGridOptions<T>): UseWindowedGridResult<T> {
    const containerRef = useRef<HTMLDivElement | null>(null);
    const innerRef = useRef<HTMLDivElement | null>(null);
    const seenKeysRef = useRef<Set<string>>(new Set());
    // recompute() is expected to bail out (no setState) once measurements
    // settle, but it's driven by live browser layout (getBoundingClientRect /
    // getComputedStyle), which isn't guaranteed to converge to a fixed point
    // every time -- a measurement that ping-pongs between two values would
    // otherwise re-trigger this effect synchronously forever and crash the
    // whole page with "Maximum update depth exceeded". This counts
    // consecutive recomputes within a single synchronous burst and bails
    // out (leaving the grid in whatever state it last reached) rather than
    // letting React hit that ceiling. The counter resets on the next
    // macrotask, since a burst caused by legitimate rapid re-renders never
    // runs that many times before yielding back to the browser.
    const recomputeGuardRef = useRef({ count: 0, resetScheduled: false });
    const [metrics, setMetrics] = useState<Metrics>(() => (
        { columnCount: estimateInitialColumnCount(), rowHeight: INITIAL_ROW_HEIGHT_GUESS, rowGap: 16 }
    ));
    const [range, setRange] = useState<{ firstRow: number; lastRowExclusive: number }>(
        () => ({ firstRow: 0, lastRowExclusive: overscanRows * 2 + 6 }),
    );

    const recompute = () => {
        const containerEl = containerRef.current;
        const innerEl = innerRef.current;
        if (!enabled || !containerEl || !innerEl || typeof window === 'undefined') {
            return;
        }

        const style = window.getComputedStyle(innerEl);
        const columnTracks = style.gridTemplateColumns.trim().split(/\s+/).filter(Boolean);
        // An empty/unmeasurable value (e.g. before first layout, or a test
        // environment without real grid layout) keeps the previous guess
        // rather than collapsing to a 1-column layout.
        const columnCount = columnTracks.length > 0 ? columnTracks.length : metrics.columnCount;
        const rowGapRaw = style.rowGap && style.rowGap !== 'normal' ? style.rowGap : (style.gap || '0px').split(' ')[0];
        const rowGap = parseFloat(rowGapRaw) || 0;

        const renderedCount = innerEl.children.length;
        const renderedRows = renderedCount > 0 ? Math.ceil(renderedCount / columnCount) : 0;
        const innerHeight = innerEl.getBoundingClientRect().height;
        const measuredRowHeight = renderedRows > 0
            ? Math.max(1, (innerHeight - rowGap * (renderedRows - 1)) / renderedRows)
            : metrics.rowHeight;

        setMetrics((prev) => (
            prev.columnCount === columnCount && Math.abs(prev.rowHeight - measuredRowHeight) < 1 && prev.rowGap === rowGap
                ? prev
                : { columnCount, rowHeight: measuredRowHeight, rowGap }
        ));

        const containerTopOffset = containerEl.getBoundingClientRect().top;
        const next = computeWindowRange({
            itemCount: items.length,
            columnCount,
            rowHeight: measuredRowHeight,
            rowGap,
            overscanRows,
            containerTopOffset,
            viewportHeight: window.innerHeight,
        });
        setRange((prev) => (
            prev.firstRow === next.firstRow && prev.lastRowExclusive === next.lastRowExclusive
                ? prev
                : { firstRow: next.firstRow, lastRowExclusive: next.lastRowExclusive }
        ));
    };

    // Runs after every commit; each setState call above is normally a no-op
    // once measurements settle, so this doesn't loop in practice. The guard
    // above is the backstop for when it doesn't.
    useLayoutEffect(() => {
        const guard = recomputeGuardRef.current;
        guard.count += 1;
        if (!guard.resetScheduled) {
            guard.resetScheduled = true;
            window.setTimeout(() => {
                guard.count = 0;
                guard.resetScheduled = false;
            }, 0);
        }
        if (guard.count > 30) {
            if (typeof console !== 'undefined') {
                console.error('useWindowedGrid: recompute() re-triggered itself too many times in a row; bailing out to avoid an infinite render loop.');
            }
            return;
        }
        recompute();
    });

    useLayoutEffect(() => {
        const containerEl = containerRef.current;
        if (!containerEl || typeof window === 'undefined') {
            return undefined;
        }

        let ticking = false;
        const onScrollOrResize = () => {
            if (ticking) {
                return;
            }
            ticking = true;
            window.requestAnimationFrame(() => {
                ticking = false;
                recompute();
            });
        };

        window.addEventListener('scroll', onScrollOrResize, { passive: true });
        window.addEventListener('resize', onScrollOrResize, { passive: true });

        let resizeObserver: ResizeObserver | undefined;
        if (typeof ResizeObserver !== 'undefined') {
            resizeObserver = new ResizeObserver(onScrollOrResize);
            resizeObserver.observe(containerEl);
        }

        return () => {
            window.removeEventListener('scroll', onScrollOrResize);
            window.removeEventListener('resize', onScrollOrResize);
            resizeObserver?.disconnect();
        };
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [...layoutDeps]);

    const { columnCount, rowHeight, rowGap } = metrics;
    const slice = useMemo(
        () => deriveSlice(items.length, columnCount, rowHeight, rowGap, range.firstRow, range.lastRowExclusive),
        [items.length, columnCount, rowHeight, rowGap, range.firstRow, range.lastRowExclusive],
    );

    const startIndex = enabled ? slice.startIndex : 0;
    const endIndex = enabled ? slice.endIndex : items.length;
    const visibleItems = useMemo(() => items.slice(startIndex, endIndex), [items, startIndex, endIndex]);

    useLayoutEffect(() => {
        visibleItems.forEach((item) => seenKeysRef.current.add(getKey(item)));
    });

    return {
        containerRef,
        innerRef,
        spacerStyle: enabled ? { position: 'relative', height: slice.totalHeightPx } : {},
        innerStyle: enabled
            ? { position: 'absolute', top: 0, left: 0, right: 0, transform: `translateY(${slice.offsetPx}px)` }
            : {},
        visibleItems,
        startIndex,
        isVirtualized: enabled && endIndex - startIndex < items.length,
        shouldAnimateEntrance: (key: string) => !seenKeysRef.current.has(key),
    };
}
