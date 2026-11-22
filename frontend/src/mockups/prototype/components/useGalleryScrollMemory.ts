import { useEffect, useLayoutEffect, useRef, type RefObject } from 'react';

/**
 * Remembers where the Gallery was scrolled to so leaving it (an album, Search,
 * People...) and coming back lands on the same photos instead of the top.
 *
 * The Gallery unmounts on navigation, and so does its scroll position, but the
 * loaded photos live in the store -- so we only need to remember *which tile
 * was on screen and where*, not an offset. Anchoring on a tile id keeps this
 * right when the tile size or window changed meanwhile, and when more photos
 * arrived above or below.
 *
 * Browser back/forward remount the Gallery too, so they restore the same way.
 * Tapping the Gallery tab while already on it never remounts: it keeps its
 * existing behaviour (back to Days, full library).
 */
export type GalleryScrollAnchor = { id: string; top: number };

// Module-level on purpose: it must outlive the page component, and it is a
// per-tab view detail, not app data.
let remembered: GalleryScrollAnchor | null = null;
export const forgetGalleryScroll = () => { remembered = null; };
export const rememberedGalleryScroll = () => remembered;

/** .pt-body scrolls on phones; elsewhere the document does. */
const scrollerOf = (): HTMLElement | null => {
    const body = document.querySelector<HTMLElement>('.pt-body');
    if (!body) return null;
    const oy = window.getComputedStyle(body).overflowY;
    return oy === 'auto' || oy === 'scroll' ? body : null;
};

const scrollOffset = (scroller: HTMLElement | null) => (scroller ? scroller.scrollTop : window.scrollY);

const tileAtCentre = (): HTMLElement | null => {
    const at = document.elementFromPoint?.(window.innerWidth / 2, window.innerHeight / 2);
    return at?.closest<HTMLElement>('[data-tile-id]') ?? null;
};

const findTile = (grid: HTMLElement, id: string) =>
    Array.from(grid.querySelectorAll<HTMLElement>('[data-tile-id]')).find((t) => t.dataset.tileId === id) ?? null;

const MAX_RESTORE_PAGES = 40;

type Options = {
    gridRef: RefObject<HTMLElement | null>;
    /** Only the plain Days view over the full library is remembered. */
    trackable: boolean;
    photoCount: number;
    hasMore: boolean;
    loading: boolean;
    loadMore: () => void;
};

export const useGalleryScrollMemory = ({ gridRef, trackable, photoCount, hasMore, loading, loadMore }: Options) => {
    // Captured on first render: only a fresh mount restores.
    const pendingRef = useRef<GalleryScrollAnchor | null>(remembered);
    const pagesLoadedRef = useRef(0);
    const trackableRef = useRef(trackable);
    trackableRef.current = trackable;

    // Restore: wait for the anchor tile, paging in more photos if the gallery
    // has not loaded that far yet.
    useLayoutEffect(() => {
        const target = pendingRef.current;
        const grid = gridRef.current;
        if (!target) return;
        if (!trackable) { pendingRef.current = null; return; }
        if (!grid) return;
        const tile = findTile(grid, target.id);
        if (tile) {
            const delta = tile.getBoundingClientRect().top - target.top;
            const scroller = scrollerOf();
            if (scroller) scroller.scrollTop += delta; else window.scrollBy(0, delta);
            pendingRef.current = null;
        } else if (hasMore) {
            // Bounded: a photo hidden by a filter would otherwise page in the whole library.
            if (pagesLoadedRef.current >= MAX_RESTORE_PAGES) pendingRef.current = null;
            else if (!loading) { pagesLoadedRef.current += 1; loadMore(); }
        } else if (photoCount > 0) {
            pendingRef.current = null; // the photo is gone: stay at the top
        }
    }, [gridRef, trackable, photoCount, hasMore, loading, loadMore]);

    // A deliberate scroll while we are still loading towards the anchor wins.
    useEffect(() => {
        const cancel = () => { pendingRef.current = null; };
        window.addEventListener('wheel', cancel, { passive: true });
        window.addEventListener('touchstart', cancel, { passive: true });
        return () => {
            window.removeEventListener('wheel', cancel);
            window.removeEventListener('touchstart', cancel);
        };
    }, []);

    // Track: remember the tile under the viewport centre as the user scrolls.
    useEffect(() => {
        let raf = 0;
        const save = () => {
            raf = 0;
            if (pendingRef.current) return; // still restoring: don't overwrite with the top
            if (!trackableRef.current) { remembered = null; return; }
            if (scrollOffset(scrollerOf()) <= 0) { remembered = null; return; }
            const tile = tileAtCentre();
            if (tile?.dataset.tileId) remembered = { id: tile.dataset.tileId, top: tile.getBoundingClientRect().top };
        };
        const onScroll = () => { if (!raf) raf = requestAnimationFrame(save); };
        const scroller = scrollerOf();
        scroller?.addEventListener('scroll', onScroll, { passive: true });
        window.addEventListener('scroll', onScroll, { passive: true });
        return () => {
            if (raf) cancelAnimationFrame(raf);
            scroller?.removeEventListener('scroll', onScroll);
            window.removeEventListener('scroll', onScroll);
        };
    }, []);
};
