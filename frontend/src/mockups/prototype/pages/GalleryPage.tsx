import React, { useEffect, useMemo, useRef, useState } from 'react';
import { CalendarSearch as CalendarSearchIcon, ChevronDown as ChevronDownIcon, Heart as HeartIcon, SlidersHorizontal as FilterIcon, Star as StarIcon, Upload as ArrowUpTrayIcon, ZoomOut as MagnifyingGlassMinusIcon, ZoomIn as MagnifyingGlassPlusIcon, Image as PhotoIcon, UserPlus as UserPlusIcon } from 'lucide-react';
import { useStore, isVideoFilename } from '../store';
import type { CaptureRange, MediaFilter } from '../store';
import { useAppServices } from '../../../components/AppServicesProvider';
import { invalidateLocalSortIndex } from '../../../services/localSortIndex';
import PhotoGrid from '../components/PhotoGrid';
import TimelineBrowser from '../components/TimelineBrowser';
import { Menu, Spinner } from '../components/bits';
import { useTileSize, TILE_RANGE, TILE_STEP } from '../components/controls';

const MEDIA_FILTERS: { value: MediaFilter; label: string }[] = [
    { value: 'all', label: 'All' },
    { value: 'photo', label: 'Photos' },
    { value: 'video', label: 'Videos' },
];

type ZoomLevel = 'days' | 'months' | 'years';

// iOS's own "All Photos" zoom-out packs ~12 tiny tiles per row before it
// switches to the Months grouping -- the shared TILE_RANGE.min (72px, ~4
// columns on a phone) stopped shrinking far short of that and jumped to
// Months too early, so the Gallery gets its own, much smaller floor.
const GALLERY_TILE_MIN = 24;
const clampGalleryTile = (n: number) => Math.min(TILE_RANGE.max, Math.max(GALLERY_TILE_MIN, n));

/** Gallery — the populated grid + drag-and-drop upload + the empty first-run state. */
export const GalleryPage: React.FC = () => {
    const {
        photos, navigate, selectMany, photosLoading, hasMorePhotos,
        loadMorePhotos, reloadPhotos, totalPhotos, mediaFilter, setMediaFilter, galleryFilters, setGalleryRating, setGalleryLikedOnly, jumpToGalleryDate, captureRange, setCaptureRange, timeline,
        route, focusPhoto, selectMode, setSelectMode,
    } = useStore();
    const { requestUpload, startUpload, uploading, pendingUploadSummary, stopActiveUpload, notifications, registerUploadCompletionHandler } = useAppServices();
    const [dragging, setDragging] = useState(false);
    const [tileMin, setTileMin] = useTileSize('photostore.galleryTileSize', { min: GALLERY_TILE_MIN });
    // iOS-style zoom levels: keep zooming out past the smallest tiles to browse
    // Months, then Years (replaces the old timeline rail).
    const [level, setLevel] = useState<ZoomLevel>('days');
    const [focusYear, setFocusYear] = useState<string | null>(null);
    const [dateJump, setDateJump] = useState('');

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

    const scrollToTile = (filename: string | null) => {
        if (!filename || !gridRef.current) return;
        const tile = Array.from(gridRef.current.querySelectorAll<HTMLElement>('[data-tile-id]'))
            .find((el) => el.dataset.tileId === filename);
        if (tile) {
            tile.scrollIntoView({ block: 'start', behavior: 'smooth' });
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
        const files = Array.from(e.dataTransfer?.files ?? []).filter((f) => f.type.startsWith('image/') || f.type.startsWith('video/') || /\.(heic|heif|cr2|cr3|arw|nef|dng|raf)$/i.test(f.name));
        if (files.length) void startUpload(files);
    };

    // Live upload progress from the active upload notification.
    const progress = notifications.map((n) => n.progress).find((p) => p && p.totalCount > 0);
    const pct = progress && progress.totalCount ? Math.round((progress.uploadedCount / progress.totalCount) * 100) : 0;
    const showFailed = !uploading && (pendingUploadSummary?.failedCount ?? 0) > 0;

    // Client-side photo/video split over the loaded page(s).
    const visiblePhotos = useMemo(() => {
        if (mediaFilter === 'all') return photos;
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
    const handleDateJump = (value: string) => {
        setDateJump(value);
        if (!value) return;
        setLevel('days');
        setFocusYear(null);
        void jumpToGalleryDate(value).then((filename) => {
            pendingScrollIdRef.current = filename;
            window.requestAnimationFrame(() => scrollToTile(filename));
        });
    };

    const countLabel = totalPhotos !== null
        ? `${totalPhotos.toLocaleString()} photo${totalPhotos === 1 ? '' : 's'}`
        : `${photos.length}${hasMorePhotos ? '+' : ''} photos`;
    const activeFilterCount = (galleryFilters.rating > 0 ? 1 : 0)
        + (galleryFilters.likedOnly ? 1 : 0)
        + (mediaFilter !== 'all' ? 1 : 0);

    const zoomControl = (
        <div className="pt-zoom" role="group" aria-label="Zoom level">
            <button type="button" className="btn" aria-label="Zoom out" disabled={zoomedOutFully} onClick={zoomOut}>
                <MagnifyingGlassMinusIcon className="toolbar-icon" />
            </button>
            <button type="button" className="btn" aria-label="Zoom in" disabled={zoomedInFully} onClick={zoomIn}>
                <MagnifyingGlassPlusIcon className="toolbar-icon" />
            </button>
        </div>
    );

    const renderFilterControls = () => (
        <>
            <Menu
                className="pt-rating-filter"
                align="left"
                renderTrigger={(toggle, open) => (
                    <button
                        type="button"
                        className={`btn pt-filter-button${galleryFilters.rating > 0 ? ' active' : ''}`}
                        aria-haspopup="menu"
                        aria-expanded={open}
                        onClick={toggle}
                    >
                        <StarIcon className="toolbar-icon" fill={galleryFilters.rating > 0 ? 'currentColor' : 'none'} />
                        {galleryFilters.rating > 0 ? `${galleryFilters.rating} star${galleryFilters.rating === 1 ? '' : 's'}` : 'Rating'}
                        <ChevronDownIcon className="pt-filter-chevron" />
                    </button>
                )}
            >
                {(close) => (
                    <div className="pt-rating-options" aria-label="Filter by rating">
                        {[0, 1, 2, 3, 4, 5].map((rating) => (
                            <button
                                key={rating}
                                type="button"
                                role="menuitemradio"
                                aria-checked={galleryFilters.rating === rating}
                                className={galleryFilters.rating === rating ? 'active' : undefined}
                                onClick={() => { setGalleryRating(rating); close(); }}
                            >
                                <StarIcon className="toolbar-icon" fill={rating > 0 ? 'currentColor' : 'none'} />
                                {rating === 0 ? 'All ratings' : `${rating} star${rating === 1 ? '' : 's'}`}
                            </button>
                        ))}
                    </div>
                )}
            </Menu>
            <button type="button" className={`btn pt-like-filter${galleryFilters.likedOnly ? ' active' : ''}`} aria-pressed={galleryFilters.likedOnly} onClick={() => setGalleryLikedOnly(!galleryFilters.likedOnly)}>
                <HeartIcon className="toolbar-icon" fill={galleryFilters.likedOnly ? 'currentColor' : 'none'} /> Likes
            </button>
            <label className="pt-date-jump">
                <CalendarSearchIcon className="toolbar-icon" aria-hidden="true" />
                <input
                    type="date"
                    aria-label="Jump to date"
                    value={dateJump}
                    min={timeline?.firstDate ?? undefined}
                    max={timeline?.lastDate ?? undefined}
                    onChange={(e) => handleDateJump(e.target.value)}
                />
            </label>
            <div className="mock-seg pt-media-filter" role="group" aria-label="Media type">
                {MEDIA_FILTERS.map((f) => (
                    <button key={f.value} type="button" className={mediaFilter === f.value ? 'active' : undefined} onClick={() => setMediaFilter(f.value)}>
                        {f.label}
                    </button>
                ))}
            </div>
        </>
    );

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

    if (photos.length === 0 && level === 'days' && !captureRange && !uploading) {
        return (
            <div className="pt-arrive">
                <div className="empty-state">
                    <span className="empty-state-icon"><PhotoIcon /></span>
                    <p className="empty-state-title">This library is empty</p>
                    <p className="empty-state-message">
                        Upload your first photos and Keepsake starts sorting faces, places and moments as they come in.
                    </p>
                    <div className="empty-state-action pt-arrive-actions">
                        <button type="button" className="btn mock-cta" onClick={requestUpload}>
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

    const levelTitle = level === 'years' ? 'Years' : level === 'months' ? (focusYear ?? 'Months') : 'Gallery';

    return (
        <div>
            <div className="pt-toolbar">
                <div>
                    <h1 className="pt-page-title">{levelTitle}</h1>
                    <p className="pt-page-sub">{countLabel}{captureRange ? ` · ${captureRange.label}` : ''}</p>
                </div>
                <div className="pt-toolbar-actions">
                    {level === 'days' && (
                        <Menu
                            className="pt-filter-menu"
                            align="right"
                            renderTrigger={(toggle, open) => (
                                <button
                                    type="button"
                                    className={`btn pt-filter-button${activeFilterCount > 0 ? ' active' : ''}`}
                                    aria-haspopup="menu"
                                    aria-expanded={open}
                                    onClick={toggle}
                                >
                                    <FilterIcon className="toolbar-icon" /> Filters{activeFilterCount > 0 ? ` (${activeFilterCount})` : ''}
                                    <ChevronDownIcon className="pt-filter-chevron" />
                                </button>
                            )}
                        >
                            {() => (
                                <div className="pt-filter-popover-content">
                                    {renderFilterControls()}
                                </div>
                            )}
                        </Menu>
                    )}
                    {zoomControl}
                    {level === 'days' && (
                        selectMode ? (
                            <>
                                <button type="button" className="btn" onClick={() => selectMany(visiblePhotos.map((p) => p.id))}>
                                    Select all
                                </button>
                                <button type="button" className="btn mock-cta" onClick={() => setSelectMode(false)}>
                                    Done
                                </button>
                            </>
                        ) : (
                            <button type="button" className="btn" onClick={() => setSelectMode(true)}>
                                Select
                            </button>
                        )
                    )}
                </div>
            </div>

            {scopeCrumbs}

            {(uploading || showFailed) && (
                <div className={`upload-dock${!uploading ? ' is-done' : ''}`}>
                    {uploading ? (
                        <>
                            <span className="count">{progress ? `Uploading ${progress.uploadedCount} / ${progress.totalCount}` : 'Uploading…'}</span>
                            <span className="track"><span className="fill" style={{ width: `${pct}%` }} /></span>
                            {progress?.mbPerSecond ? <span className="rate">{progress.mbPerSecond.toFixed(1)} MB/s</span> : null}
                            <button type="button" className="btn" onClick={stopActiveUpload}>Stop</button>
                        </>
                    ) : (
                        <>
                            <span className="done-label">Upload finished</span>
                            <span className="track"><span className="fill" style={{ width: '100%' }} /></span>
                            <span className="failed">{pendingUploadSummary?.failedCount} failed</span>
                        </>
                    )}
                </div>
            )}

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
                    <PhotoGrid photos={visiblePhotos} gridRef={gridRef} extendable />
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
