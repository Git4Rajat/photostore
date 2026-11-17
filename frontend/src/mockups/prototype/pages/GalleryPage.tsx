import React, { useEffect, useMemo, useRef, useState } from 'react';
import { SlidersHorizontal as FilterIcon, Upload as ArrowUpTrayIcon, Grid2x2 as LargePhotosIcon, Grid3x3 as SmallPhotosIcon, Image as PhotoIcon, UserPlus as UserPlusIcon } from 'lucide-react';
import { useStore, isVideoFilename } from '../store';
import type { CaptureRange } from '../store';
import { useAppServices } from '../../../components/AppServicesProvider';
import { UploadDock } from '../../../components/UploadDock';
import { invalidateLocalSortIndex } from '../../../services/localSortIndex';
import PhotoGrid from '../components/PhotoGrid';
import TimelineBrowser from '../components/TimelineBrowser';
import { Spinner } from '../components/bits';
import { FilterSentence, PhotosFilterBar, activeFilterCount as countFilters, type PhotosFilterActions, type PhotosFilterState } from '../components/PhotosFilter';
import { useTileSize, TILE_RANGE, TILE_STEP, galleryTileKey, defaultGalleryTile } from '../components/controls';

type ZoomLevel = 'days' | 'months' | 'years';

// iOS's own "All Photos" zoom-out packs ~12 tiny tiles per row before it
// switches to the Months grouping -- the shared TILE_RANGE.min (72px, ~4
// columns on a phone) stopped shrinking far short of that and jumped to
// Months too early, so the Gallery gets its own, much smaller floor.
const GALLERY_TILE_MIN = 24;
const clampGalleryTile = (n: number) => Math.min(TILE_RANGE.max, Math.max(GALLERY_TILE_MIN, n));

/** Gallery — the populated grid + drag-and-drop upload + the empty first-run state. */
const GalleryPage: React.FC = () => {
    const {
        photos, navigate, selectMany, photosLoading, hasMorePhotos,
        loadMorePhotos, reloadPhotos, totalPhotos, mediaFilter, setMediaFilter, galleryFilters, setGalleryRating, setGalleryLikedOnly, captureRange, setCaptureRange, timeline,
        route, focusPhoto, selectMode, setSelectMode, selection, clearSelection,
    } = useStore();
    const { requestUpload, startUpload, uploadActive, registerUploadCompletionHandler } = useAppServices();
    const [dragging, setDragging] = useState(false);
    // Four across on a phone, about eight on a desktop. A new storage key so
    // everyone starts from these defaults once, not from the old 3-across size.
    // Picked once per visit: a window resized mid-session must not write one size under the other key.
    const [tileKey] = useState(galleryTileKey);
    const [tileMin, setTileMin] = useTileSize(tileKey, { min: GALLERY_TILE_MIN, default: defaultGalleryTile(GALLERY_TILE_MIN) });
    // iOS-style zoom levels: keep zooming out past the smallest tiles to browse
    // Months, then Years (replaces the old timeline rail).
    const [level, setLevel] = useState<ZoomLevel>('days');
    const [focusYear, setFocusYear] = useState<string | null>(null);

    const depth = useRef(0);
    const gridRef = useRef<HTMLDivElement>(null);
    const sentinelRef = useRef<HTMLDivElement>(null);
    const pinchRef = useRef<{ dist: number; tile: number } | null>(null);
    const pendingScrollIdRef = useRef<string | null>(null);

    // Zoom-out steps: shrink tiles until the floor, then Days → Months → Years.
    const zoomOut = () => {
        if (level === 'years') return;
        if (level === 'months') { setLevel('years'); return; }
        if (tileMin > GALLERY_TILE_MIN) setTileMin((n) => clampGalleryTile(n - TILE_STEP));
        else { setFocusYear(null); setLevel('months'); }
    };
    // Zoom-in steps: Years → Months → Days, then grow tiles.
    const zoomIn = () => {
        if (level === 'years') { setLevel('months'); return; }
        if (level === 'months') { setLevel('days'); return; }
        if (tileMin < TILE_RANGE.max) setTileMin((n) => clampGalleryTile(n + TILE_STEP));
    };
    const zoomedOutFully = level === 'years';
    const zoomedInFully = level === 'days' && tileMin >= TILE_RANGE.max;

    const touchDistance = (t: React.TouchList) => Math.hypot(t[0].clientX - t[1].clientX, t[0].clientY - t[1].clientY);
    const onTouchStart = (e: React.TouchEvent) => {
        if (e.touches.length === 2) pinchRef.current = { dist: touchDistance(e.touches), tile: tileMin };
    };
    const onTouchMove = (e: React.TouchEvent) => {
        if (e.touches.length !== 2 || !pinchRef.current) return;
        const ratio = touchDistance(e.touches) / pinchRef.current.dist;
        if (level === 'days') {
            const target = Math.round(pinchRef.current.tile * ratio);
            // Pinched in past the smallest tile => keep zooming out into Months.
            if (ratio < 1 && target <= GALLERY_TILE_MIN) {
                setFocusYear(null);
                setLevel('months');
                pinchRef.current = null;
                return;
            }
            setTileMin(clampGalleryTile(target));
        } else if (ratio > 1.25) {
            zoomIn();
            pinchRef.current = null;
        } else if (ratio < 0.8) {
            zoomOut();
            pinchRef.current = null;
        }
    };
    const onTouchEnd = (e: React.TouchEvent) => {
        if (e.touches.length < 2) pinchRef.current = null;
    };

    // Returning to the Gallery (a tap on the already-active tab, or a fresh
    // navigation) starts at the top again: full library, Days zoom. Mirror the
    // store's captureRange into a ref so this only re-runs on navigation, not
    // when a drill-down sets the range itself.
    const captureRangeStoreRef = useRef(captureRange);
    useEffect(() => { captureRangeStoreRef.current = captureRange; }, [captureRange]);
    useEffect(() => {
        setLevel('days');
        setFocusYear(null);
        if (captureRangeStoreRef.current) setCaptureRange(null);
    }, [route, setCaptureRange]);
    // Refresh the grid whenever an upload session finishes so new photos
    // appear. The cached sort-index (see localSortIndex.ts) must be dropped
    // first -- otherwise reloadPhotos would re-sort/paginate the same stale
    // in-memory snapshot from before the upload instead of picking up the
    // newly-added photos.
    useEffect(() => registerUploadCompletionHandler(() => { invalidateLocalSortIndex(); reloadPhotos(); }), [registerUploadCompletionHandler, reloadPhotos]);

    // Deleting every currently-loaded photo (e.g. "Select all" + delete) can
    // empty `photos` while more pages still exist further down -- pull the next
    // page in automatically instead of showing the empty first-run screen.
    useEffect(() => {
        if (photos.length === 0 && hasMorePhotos && !photosLoading) {
            loadMorePhotos();
        }
    }, [photos.length, hasMorePhotos, photosLoading, loadMorePhotos]);

    // Infinite scroll: load the next page when the bottom sentinel scrolls into view.
    useEffect(() => {
        const node = sentinelRef.current;
        if (!node || !hasMorePhotos || level !== 'days') return undefined;
        const observer = new IntersectionObserver((entries) => {
            if (entries.some((e) => e.isIntersecting)) loadMorePhotos();
        }, { rootMargin: '600px' });
        observer.observe(node);
        return () => observer.disconnect();
    }, [hasMorePhotos, loadMorePhotos, photos.length, level]);

    // Zooming out shrinks tile size via a CSS var (not added/removed DOM nodes),
    // which can pull the sentinel into view purely through reflow with no scroll
    // event firing. The observer above doesn't reliably catch that case, so
    // re-check the sentinel's position by hand whenever the zoom level changes.
    useEffect(() => {
        if (!hasMorePhotos || photosLoading || level !== 'days') return undefined;
        const id = requestAnimationFrame(() => {
            const rect = sentinelRef.current?.getBoundingClientRect();
            if (rect && rect.top <= window.innerHeight + 600) loadMorePhotos();
        });
        return () => cancelAnimationFrame(id);
    }, [tileMin, level, hasMorePhotos, photosLoading, loadMorePhotos]);

    const scrollToTile = (filename: string | null) => {
        if (!filename || !gridRef.current) return;
        const tile = Array.from(gridRef.current.querySelectorAll<HTMLElement>('[data-tile-id]'))
            .find((el) => el.dataset.tileId === filename);
        if (tile) {
            // 'auto' (instant), not 'smooth' -- a jump swaps in a brand-new page
            // rather than scrolling through one (see jumpToGalleryDate), so the
            // target tile is already at/near the top of a short list; a smooth
            // scroll over that tiny remaining distance reads as "barely moved".
            // The sense of motion comes from the pt-date-jumping blur instead
            // (see handleDateJump) -- this just needs to land instantly once
            // that settles.
            tile.scrollIntoView({ block: 'start', behavior: 'auto' });
            pendingScrollIdRef.current = null;
        }
    };

    useEffect(() => {
        const target = pendingScrollIdRef.current;
        if (!target || !gridRef.current) return;
        const frame = window.requestAnimationFrame(() => scrollToTile(target));
        return () => window.cancelAnimationFrame(frame);
    }, [photos]);

    const onDrop = (e: React.DragEvent) => {
        e.preventDefault();
        depth.current = 0;
        setDragging(false);
        const files = Array.from(e.dataTransfer?.files ?? []).filter((f) => f.type.startsWith('image/') || f.type.startsWith('video/') || /\.(heic|heif|hif|cr2|cr3|arw|nef|dng|raf)$/i.test(f.name));
        if (files.length) void startUpload(files);
    };

    // Client-side photo/video split over the loaded page(s).
    const visiblePhotos = useMemo(() => {
        if (mediaFilter === 'all') return photos;
        if (mediaFilter === 'live') return photos.filter((p) => Boolean(p.liveClip));
        return photos.filter((p) => (mediaFilter === 'video' ? isVideoFilename(p.filename) : !isVideoFilename(p.filename)));
    }, [photos, mediaFilter]);

    // Open the viewer at a photo handed off via ?photo=.
    const openedPhotoParamRef = useRef<string | null>(null);
    useEffect(() => {
        const target = route.params.photo;
        if (!target) { openedPhotoParamRef.current = null; return; }
        if (openedPhotoParamRef.current === target) return;
        openedPhotoParamRef.current = target;
        focusPhoto(target);
    }, [route.params.photo, focusPhoto]);

    // Drill from Years → Months → Days.
    const openYear = (y: string) => { setFocusYear(y); setLevel('months'); };
    const openMonth = (range: CaptureRange, y: string) => { setFocusYear(y); setCaptureRange(range); setLevel('days'); };
    const resetToTop = () => { setLevel('days'); setFocusYear(null); setCaptureRange(null); };

    const countLabel = totalPhotos !== null
        ? `${totalPhotos.toLocaleString()} photo${totalPhotos === 1 ? '' : 's'}`
        : `${photos.length}${hasMorePhotos ? '+' : ''} photos`;

    // A size slider, as the redesign draws it. The small and large photo icons
    // on either side still step past the ends into Months and Years.
    const zoomControl = (
        <div className="pt-size-slider" role="group" aria-label="Photo size">
            <button type="button" className="pt-size-end" aria-label="Smaller photos" title="Smaller photos" disabled={zoomedOutFully} onClick={zoomOut}>
                <SmallPhotosIcon aria-hidden="true" />
            </button>
            {level === 'days' && (
                <input
                    type="range"
                    aria-label="Photo size"
                    min={GALLERY_TILE_MIN}
                    max={TILE_RANGE.max}
                    step={4}
                    value={tileMin}
                    onChange={(e) => setTileMin(clampGalleryTile(Number(e.target.value)))}
                />
            )}
            <button type="button" className="pt-size-end" aria-label="Larger photos" title="Larger photos" disabled={zoomedInFully} onClick={zoomIn}>
                <LargePhotosIcon aria-hidden="true" />
            </button>
        </div>
    );

    const filterState: PhotosFilterState = { mediaFilter, likedOnly: galleryFilters.likedOnly, rating: galleryFilters.rating, captureRange };
    const filterActions: PhotosFilterActions = { setMediaFilter, setLikedOnly: setGalleryLikedOnly, setRating: setGalleryRating, setCaptureRange };
    const activeFilterCount = countFilters(filterState);
    const clearFilters = () => { setMediaFilter('all'); setGalleryLikedOnly(false); setGalleryRating(0); setCaptureRange(null); };
    // The server counts what matches its own filters; the photo/video/live split is local, so it is only
    // known once every page is in.
    const matchCount = mediaFilter === 'all' ? totalPhotos : hasMorePhotos ? null : visiblePhotos.length;
    const filterBar = (
        <PhotosFilterBar state={filterState} actions={filterActions} matchCount={matchCount} onSelect={() => setSelectMode(true)} />
    );
    const filterSummary = activeFilterCount > 0 ? (
        <div className="pt-fsum">
            <FilterSentence state={filterState} />
            <button type="button" className="pt-linkish" onClick={clearFilters}>Clear</button>
        </div>
    ) : null;

    const scopeCrumbs = (level !== 'days' || captureRange) ? (
        <div className="pt-zoom-crumbs" aria-label="Zoom scope">
            <button type="button" onClick={resetToTop}>All Photos</button>
            {level === 'years' && (<><span aria-hidden="true">›</span><span className="on">Years</span></>)}
            {level === 'months' && (<><span aria-hidden="true">›</span><span className="on">{focusYear ?? 'Months'}</span></>)}
            {level === 'days' && captureRange && (<><span aria-hidden="true">›</span><span className="on">{captureRange.label}</span></>)}
        </div>
    ) : null;

    if (photos.length === 0 && level === 'days' && !captureRange && (photosLoading || hasMorePhotos)) {
        return (
            <div className="pt-arrive">
                <Spinner label="Loading your photos…" />
            </div>
        );
    }

    if (photos.length === 0 && level === 'days' && !uploadActive && !photosLoading && (activeFilterCount > 0 || !captureRange)) {
        if (activeFilterCount > 0) {
            const noun = mediaFilter === 'video' ? 'videos' : mediaFilter === 'live' ? 'Live Photos' : 'photos';
            return (
                <div className="pt-arrive">
                    {/* Keep the filters in reach, so one can be dropped instead of all. */}
                    {filterBar}
                    {filterSummary}
                    <div className="empty-state">
                        <span className="empty-state-icon"><FilterIcon /></span>
                        <p className="empty-state-title">{`No ${noun} match`}</p>
                        <p className="empty-state-message">Try a different date or rating, or take a filter off.</p>
                        <div className="empty-state-action pt-arrive-actions">
                            <button type="button" className="btn btn-primary" onClick={clearFilters}>Clear filters</button>
                        </div>
                    </div>
                </div>
            );
        }
        return (
            <div className="pt-arrive">
                <div className="empty-state">
                    <span className="empty-state-icon"><PhotoIcon /></span>
                    <p className="empty-state-title">This library is empty</p>
                    <p className="empty-state-message">
                        Upload your first photos and Keepsake starts sorting faces, places and moments as they come in.
                    </p>
                    <div className="empty-state-action pt-arrive-actions">
                        <button type="button" className="btn btn-primary" onClick={requestUpload}>
                            <ArrowUpTrayIcon className="toolbar-icon" /> Upload photos
                        </button>
                        <button type="button" className="btn" onClick={() => navigate('sharing')}>
                            <UserPlusIcon className="toolbar-icon" /> Invite family
                        </button>
                    </div>
                </div>
            </div>
        );
    }

    const levelTitle = level === 'years' ? 'Years' : level === 'months' ? (focusYear ?? 'Months') : 'Photos';

    return (
        <div className="pt-gallery">
            {level === 'days' && selectMode ? (
                // While selecting, the header becomes Cancel · count · Select all (same
                // height as the title row, so the grid doesn't jump under the finger).
                <div className="pt-select-head" role="toolbar" aria-label="Selection">
                    <button type="button" className="pt-linkish" onClick={() => { clearSelection(); setSelectMode(false); }}>Cancel</button>
                    <b aria-live="polite">{selection.length ? `${selection.length} selected` : 'Select items'}</b>
                    <button type="button" className="pt-linkish" onClick={() => selectMany(visiblePhotos.map((p) => p.id))}>Select all</button>
                </div>
            ) : (
                <div className="pt-toolbar pt-gallery-toolbar">
                    <div>
                        <h1 className="pt-page-title">{levelTitle}</h1>
                        <p className="pt-page-sub">{countLabel}{captureRange ? ` · ${captureRange.label}` : ''}</p>
                    </div>
                    {/* One row on desktops: title, chips, size, Select. Phones keep only the chips. */}
                    {level === 'days' && filterBar}
                    <div className="pt-toolbar-actions">
                        {zoomControl}
                        {level === 'days' && (
                            <button type="button" className="pt-linkish" onClick={() => setSelectMode(true)}>
                                Select
                            </button>
                        )}
                    </div>
                </div>
            )}

            {level === 'days' && filterSummary}
            {scopeCrumbs}

            <UploadDock />

            {level !== 'days' && timeline ? (
                <div onTouchStart={onTouchStart} onTouchMove={onTouchMove} onTouchEnd={onTouchEnd}>
                    <TimelineBrowser
                        timeline={timeline}
                        level={level}
                        focusYear={focusYear}
                        onOpenYear={openYear}
                        onOpenMonth={openMonth}
                    />
                </div>
            ) : (
                <div
                    className={`pt-drop-surface${dragging ? ' dragging' : ''}`}
                    ref={gridRef}
                    style={{ ['--pt-tile-min' as string]: `${tileMin}px` } as React.CSSProperties}
                    onDragEnter={(e) => { e.preventDefault(); depth.current += 1; setDragging(true); }}
                    onDragOver={(e) => e.preventDefault()}
                    onDragLeave={(e) => { e.preventDefault(); depth.current = Math.max(0, depth.current - 1); if (!depth.current) setDragging(false); }}
                    onDrop={onDrop}
                    onTouchStart={onTouchStart}
                    onTouchMove={onTouchMove}
                    onTouchEnd={onTouchEnd}
                >
                    <PhotoGrid photos={visiblePhotos} gridRef={gridRef} extendable dayHeadings emptyHint={mediaFilter !== 'all' ? '' : undefined} />
                    {mediaFilter !== 'all' && visiblePhotos.length === 0 && (
                        <p className="pt-grid-empty">No {mediaFilter === 'video' ? 'videos' : 'photos'} on the loaded pages yet — scroll to load more.</p>
                    )}
                    <div ref={sentinelRef} className="pt-scroll-sentinel" aria-hidden="true" />
                    {photosLoading && photos.length > 0 && <Spinner label="Loading more…" />}
                    <div className="pt-drop-overlay">
                        <span className="drop-icon"><ArrowUpTrayIcon /></span>
                        <b>Drop to add to Keepsake</b>
                        <span className="sub">Release to start uploading</span>
                    </div>
                </div>
            )}
        </div>
    );
};

export default GalleryPage;
