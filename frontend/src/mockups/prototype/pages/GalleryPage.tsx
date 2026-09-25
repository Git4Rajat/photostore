import React, { useEffect, useRef, useState } from 'react';
import { ArrowUpTrayIcon, MagnifyingGlassMinusIcon, MagnifyingGlassPlusIcon, PhotoIcon, UserPlusIcon } from '@heroicons/react/24/outline';
import { useStore } from '../store';
import { useAppServices } from '../../../components/AppServicesProvider';
import PhotoGrid from '../components/PhotoGrid';

const TILE_MIN = 120;
const TILE_STEP = 30;
const TILE_RANGE = { min: 72, max: 260 };
const clampTile = (n: number) => Math.min(TILE_RANGE.max, Math.max(TILE_RANGE.min, n));

/** Gallery — the populated grid + drag-and-drop upload + the empty first-run state. */
export const GalleryPage: React.FC = () => {
    const { photos, navigate, selectMany, clearSelection, selection, photosLoading, hasMorePhotos, loadMorePhotos, reloadPhotos } = useStore();
    const { requestUpload, startUpload, uploading, pendingUploadSummary, stopActiveUpload, notifications, registerUploadCompletionHandler } = useAppServices();
    const [dragging, setDragging] = useState(false);
    const [tileMin, setTileMin] = useState(TILE_MIN);
    const depth = useRef(0);
    const gridRef = useRef<HTMLDivElement>(null);
    const sentinelRef = useRef<HTMLDivElement>(null);
    const pinchRef = useRef<{ dist: number; tile: number } | null>(null);

    const touchDistance = (t: React.TouchList) => Math.hypot(t[0].clientX - t[1].clientX, t[0].clientY - t[1].clientY);
    const onTouchStart = (e: React.TouchEvent) => {
        if (e.touches.length === 2) pinchRef.current = { dist: touchDistance(e.touches), tile: tileMin };
    };
    const onTouchMove = (e: React.TouchEvent) => {
        if (e.touches.length === 2 && pinchRef.current) {
            const ratio = touchDistance(e.touches) / pinchRef.current.dist;
            // Pinch out (ratio > 1) => bigger tiles; pinch in => smaller.
            setTileMin(clampTile(Math.round(pinchRef.current.tile * ratio)));
        }
    };
    const onTouchEnd = (e: React.TouchEvent) => {
        if (e.touches.length < 2) pinchRef.current = null;
    };

    // Refresh the grid whenever an upload session finishes so new photos appear.
    useEffect(() => registerUploadCompletionHandler(() => { reloadPhotos(); }), [registerUploadCompletionHandler, reloadPhotos]);

    // Infinite scroll: load the next page when the bottom sentinel scrolls into view.
    useEffect(() => {
        const node = sentinelRef.current;
        if (!node || !hasMorePhotos) return undefined;
        const observer = new IntersectionObserver((entries) => {
            if (entries.some((e) => e.isIntersecting)) loadMorePhotos();
        }, { rootMargin: '600px' });
        observer.observe(node);
        return () => observer.disconnect();
    }, [hasMorePhotos, loadMorePhotos, photos.length]);

    const selectVisible = () => {
        if (!gridRef.current) return;
        const container = gridRef.current;
        const containerRect = container.getBoundingClientRect();
        const tiles = container.querySelectorAll('.pt-tile');
        const visible: string[] = [];
        tiles.forEach((tile) => {
            const rect = tile.getBoundingClientRect();
            if (rect.bottom > containerRect.top && rect.top < containerRect.bottom) {
                const id = tile.getAttribute('data-photo-id');
                if (id) visible.push(id);
            }
        });
        if (visible.length > 0) selectMany(visible);
    };

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

    if (photos.length === 0 && photosLoading) {
        return (
            <div className="pt-arrive">
                <div className="empty-state">
                    <p className="empty-state-message">Loading your photos…</p>
                </div>
            </div>
        );
    }

    if (photos.length === 0 && !uploading) {
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

    return (
        <div>
            <div className="pt-toolbar">
                <div>
                    <h1 className="pt-page-title">Gallery</h1>
                    <p className="pt-page-sub">{photos.length}{hasMorePhotos ? '+' : ''} photos</p>
                </div>
                <div className="pt-toolbar-actions">
                    <div className="pt-zoom" role="group" aria-label="Thumbnail size">
                        <button type="button" className="btn" aria-label="Smaller thumbnails" disabled={tileMin <= TILE_RANGE.min} onClick={() => setTileMin((n) => clampTile(n - TILE_STEP))}>
                            <MagnifyingGlassMinusIcon className="toolbar-icon" />
                        </button>
                        <button type="button" className="btn" aria-label="Larger thumbnails" disabled={tileMin >= TILE_RANGE.max} onClick={() => setTileMin((n) => clampTile(n + TILE_STEP))}>
                            <MagnifyingGlassPlusIcon className="toolbar-icon" />
                        </button>
                    </div>
                    {selection.length === 0 ? (
                        <button type="button" className="btn" onClick={selectVisible}>
                            Select visible
                        </button>
                    ) : (
                        <button type="button" className="btn" onClick={clearSelection}>
                            Clear
                        </button>
                    )}
                </div>
            </div>

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
                <PhotoGrid photos={photos} gridRef={gridRef} />
                <div ref={sentinelRef} className="pt-scroll-sentinel" aria-hidden="true" />
                {photosLoading && photos.length > 0 && <p className="pt-grid-empty">Loading more…</p>}
                <div className="pt-drop-overlay">
                    <span className="drop-icon"><ArrowUpTrayIcon /></span>
                    <b>Drop to add to Keepsake</b>
                    <span className="sub">Release to start uploading</span>
                </div>
            </div>
        </div>
    );
};

export default GalleryPage;
