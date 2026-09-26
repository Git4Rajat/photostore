import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
    ArrowLeftIcon,
    ArrowUturnLeftIcon,
    ArrowUturnRightIcon,
    ChevronLeftIcon,
    ChevronRightIcon,
    EllipsisHorizontalIcon,
    MagnifyingGlassMinusIcon,
    MagnifyingGlassPlusIcon,
    PlusIcon,
    TrashIcon,
} from '@heroicons/react/24/outline';
import { HeartIcon as HeartSolid } from '@heroicons/react/24/solid';
import { HeartIcon as HeartOutline } from '@heroicons/react/24/outline';
import { Menu, Stars } from './bits';
import { AddToAlbumMenu } from './AddToAlbumMenu';
import { useStore, isVideoFilename } from '../store';
import { useMainMedia, downloadPhoto, fetchPhotoMetadata, setPhotoRotation } from '../media';
import type { PhotoMetadata } from '../media';

const ZOOM_MIN = 1;
const ZOOM_MAX = 4;
const ZOOM_STEP = 0.5;

const dash = (value?: string | number): string => {
    const text = value === undefined || value === null ? '' : String(value).trim();
    return text || '—';
};

/** Full-screen photo viewer with a persistent action bar, prev/next, keyboard,
 *  rotate + zoom controls, an EXIF/location info panel, and on-demand full-res. */
export const PhotoViewer: React.FC = () => {
    const { viewer, photoById, openViewer, closeViewer, viewerStep, ratePhotos, toggleLike, applyPhotoRotation, deletePhotos, navigate, toast, route, registerPhotos } = useStore();

    const live = useMemo(
        () => (viewer ? viewer.ids.map((id) => photoById(id)).filter((p) => Boolean(p)) : []),
        [viewer, photoById],
    );

    // Resync ids after a deletion removes a photo from under the viewer.
    useEffect(() => {
        if (!viewer) return;
        if (live.length !== viewer.ids.length) {
            if (live.length === 0) closeViewer();
            else openViewer(live.map((p) => p!.id), Math.min(viewer.index, live.length - 1));
        }
    }, [viewer, live, closeViewer, openViewer]);

    const index = viewer ? Math.min(viewer.index, Math.max(0, live.length - 1)) : 0;
    const photo = viewer && live.length ? live[index] : null;

    // `rotation` is the photo's *absolute* manual orientation (0/90/180/270).
    // The server bakes only EXIF orientation into the preview it serves, never
    // this manual rotation (confirmed: nothing in the backend reads the
    // `rotation` field when generating thumbnails/previews), so we apply the
    // full value as a CSS transform on top of the already-upright preview —
    // matching how PhotoTile renders it in the grid. It's seeded from the
    // stored rotation on each photo and persisted back through the store.
    const [rotation, setRotation] = useState(0);
    const [zoom, setZoom] = useState(1);
    // Mirrors `zoom` for the imperative pinch/gesture listeners below, so they
    // can read the current zoom as a pinch baseline without listing `zoom` as
    // an effect dependency -- re-running those effects mid-gesture detaches and
    // reattaches the listeners, dropping events partway through a pinch.
    const zoomRef = useRef(1);
    useEffect(() => { zoomRef.current = zoom; }, [zoom]);
    const [pan, setPan] = useState({ x: 0, y: 0 });
    const [fullRes, setFullRes] = useState(false);
    const [showInfo, setShowInfo] = useState(false);
    const [meta, setMeta] = useState<PhotoMetadata | null>(null);
    const panRef = useRef<{ x: number; y: number; px: number; py: number } | null>(null);
    // Rotation is applied to the CSS transform immediately (see `rotate` below)
    // but the save request is deliberately deferred -- it fires once the user
    // navigates away from this photo (prev/next) or closes the viewer, not on
    // every click, via this effect's cleanup below.
    const pendingRotationRef = useRef<{ filename: string; rotation: number; previous: number } | null>(null);
    const flushPendingRotation = useCallback(() => {
        const pending = pendingRotationRef.current;
        if (!pending) return;
        pendingRotationRef.current = null;
        // Persist to the store immediately (optimistic) so the grid and any
        // reopen reflect the new orientation, then write it through to the
        // backend. On failure, revert the store to the saved-before value.
        applyPhotoRotation(pending.filename, pending.rotation);
        void setPhotoRotation(pending.filename, pending.rotation).catch(() => {
            applyPhotoRotation(pending.filename, pending.previous);
            toast('Couldn’t save rotation');
        });
    }, [toast, applyPhotoRotation]);

    // Reset transient view state whenever the active photo changes, flushing
    // any pending rotation for the photo being left (see flushPendingRotation).
    // Keyed on photo?.id only, deliberately not photo.rotation: persisting a
    // new rotation writes it back to the store (applyPhotoRotation), and we
    // must NOT let that re-run this reset and clobber the user's just-applied
    // in-session rotation.
    useEffect(() => {
        setRotation(photo?.rotation ? ((photo.rotation % 360) + 360) % 360 : 0);
        setZoom(1);
        setPan({ x: 0, y: 0 });
        setFullRes(false);
        setShowInfo(false);
        setMeta(null);
        return () => flushPendingRotation();
    }, [photo?.id, flushPendingRotation]); // eslint-disable-line react-hooks/exhaustive-deps

    // Also flush on unmount (the viewer stays mounted but renders null when
    // closed, so this covers a hard teardown; the photo-change cleanup above
    // covers close-via-Back, which flips photo?.id to undefined).
    useEffect(() => () => flushPendingRotation(), [flushPendingRotation]);

    const displayRotation = ((rotation % 360) + 360) % 360;

    // Fetch metadata lazily the first time the info panel is opened for a photo.
    useEffect(() => {
        if (!showInfo || !photo || meta) return;
        let active = true;
        void fetchPhotoMetadata(photo.filename).then((m) => { if (active) setMeta(m); });
        return () => { active = false; };
    }, [showInfo, photo, meta]);

    const { url: mainSrc, loading: fullResLoading, progress: fullResProgress } = useMainMedia(photo, fullRes);
    const swipeRef = useRef<{ x: number; y: number } | null>(null);

    const resetZoom = useCallback(() => { setZoom(1); setPan({ x: 0, y: 0 }); }, []);
    const zoomBy = useCallback((delta: number) => {
        setZoom((z) => {
            const next = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, +(z + delta).toFixed(2)));
            if (next === 1) setPan({ x: 0, y: 0 });
            return next;
        });
    }, []);

    const rotate = useCallback((delta: number) => {
        if (!photo) return;
        const previous = photo.rotation ? ((photo.rotation % 360) + 360) % 360 : 0;
        setRotation((r) => {
            const next = ((r + delta) % 360 + 360) % 360;
            pendingRotationRef.current = { filename: photo.filename, rotation: next, previous };
            return next;
        });
    }, [photo]);

    const photoRef = useRef<HTMLDivElement>(null);

    useEffect(() => {
        if (!viewer) return undefined;
        const onKey = (e: KeyboardEvent) => {
            if (e.key === 'Escape') closeViewer();
            if (e.key === 'ArrowLeft') viewerStep(-1);
            if (e.key === 'ArrowRight') viewerStep(1);
            if (e.key === '+' || e.key === '=') zoomBy(ZOOM_STEP);
            if (e.key === '-' || e.key === '_') zoomBy(-ZOOM_STEP);
        };
        document.addEventListener('keydown', onKey);
        return () => document.removeEventListener('keydown', onKey);
    }, [viewer, closeViewer, viewerStep, zoomBy]);

    useEffect(() => {
        const el = photoRef.current;
        if (!el) return undefined;
        const onWheel = (e: WheelEvent) => {
            if (!e.ctrlKey && !e.metaKey) return;
            e.preventDefault();
            zoomBy(e.deltaY < 0 ? ZOOM_STEP : -ZOOM_STEP);
        };
        el.addEventListener('wheel', onWheel, { passive: false });
        return () => el.removeEventListener('wheel', onWheel);
        // photo?.id gates re-attachment: the viewer renders null while closed,
        // so photoRef.current is null on the initial mount and only becomes the
        // real element once a photo opens -- without re-running here, the
        // listener would never attach and pinch/scroll-zoom silently no-ops.
    }, [zoomBy, photo?.id]);

    // Two-finger trackpad pinch on desktop: Chrome/Firefox report it as a
    // ctrl/cmd+wheel event (handled above); Safari instead fires its
    // non-standard gesture* events, which never carry a ctrlKey wheel at all.
    const gestureStartZoomRef = useRef(1);
    useEffect(() => {
        const el = photoRef.current;
        if (!el) return undefined;
        const onGestureStart = (e: Event) => {
            e.preventDefault();
            gestureStartZoomRef.current = zoomRef.current;
        };
        const onGestureChange = (e: Event) => {
            e.preventDefault();
            const scale = (e as unknown as { scale: number }).scale;
            setZoom(Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, +(gestureStartZoomRef.current * scale).toFixed(2))));
        };
        const onGestureEnd = (e: Event) => e.preventDefault();
        el.addEventListener('gesturestart', onGestureStart);
        el.addEventListener('gesturechange', onGestureChange);
        el.addEventListener('gestureend', onGestureEnd);
        return () => {
            el.removeEventListener('gesturestart', onGestureStart);
            el.removeEventListener('gesturechange', onGestureChange);
            el.removeEventListener('gestureend', onGestureEnd);
        };
    }, [photo?.id]);

    // iOS Safari two-finger pinch via touch events -- its gesture* events are
    // unreliable inside a position:fixed overlay, so drive zoom from raw touch
    // points. touch-action:none on .pt-viewer-photo (see CSS) stops the browser
    // from claiming the gesture for its own page-zoom; passive:false lets us
    // preventDefault so the pinch scales the photo instead of the page.
    const touchPinchRef = useRef<{ dist: number; zoom: number } | null>(null);
    useEffect(() => {
        const el = photoRef.current;
        if (!el) return undefined;
        const touchDist = (t: TouchList) => t.length >= 2 ? Math.hypot(t[0].clientX - t[1].clientX, t[0].clientY - t[1].clientY) : 0;
        const onTouchStart = (e: TouchEvent) => {
            if (e.touches.length === 2) {
                e.preventDefault();
                touchPinchRef.current = { dist: touchDist(e.touches) || 1, zoom: zoomRef.current };
            }
        };
        const onTouchMove = (e: TouchEvent) => {
            if (e.touches.length >= 2 && touchPinchRef.current) {
                e.preventDefault();
                const ratio = touchDist(e.touches) / touchPinchRef.current.dist;
                setZoom(Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, +(touchPinchRef.current.zoom * ratio).toFixed(2))));
            }
        };
        const onTouchEnd = (e: TouchEvent) => {
            if (e.touches.length < 2) touchPinchRef.current = null;
        };
        el.addEventListener('touchstart', onTouchStart, { passive: false });
        el.addEventListener('touchmove', onTouchMove, { passive: false });
        el.addEventListener('touchend', onTouchEnd);
        el.addEventListener('touchcancel', onTouchEnd);
        return () => {
            el.removeEventListener('touchstart', onTouchStart);
            el.removeEventListener('touchmove', onTouchMove);
            el.removeEventListener('touchend', onTouchEnd);
            el.removeEventListener('touchcancel', onTouchEnd);
        };
    }, [photo?.id]);

    if (!viewer) return null;
    if (!photo) return null;

    const isVideo = isVideoFilename(photo.filename);
    const zoomed = zoom > 1;
    const exif = meta?.exifSummary ?? {};
    const loc = meta?.location ?? {};
    const infoTags = Array.from(new Set([...(meta?.tags ?? []), ...(meta?.objects ?? []), ...(photo.tags ?? [])])).slice(0, 24);
    const locationLine = [loc.city, loc.country].filter(Boolean).join(', ');

    return (
        <div className="pt-viewer" role="dialog" aria-modal="true" aria-label="Photo viewer">
            <div className="pt-viewer-top">
                <button type="button" className="pt-viewer-back" onClick={closeViewer}>
                    <ArrowLeftIcon /> Back
                </button>
                <span className="pt-viewer-title">
                    {photo.filename}{photo.dateLabel ? ` · ${photo.dateLabel}` : ''}
                </span>
                <span className="pt-viewer-pos">{index + 1} / {live.length}</span>
            </div>

            <div className="pt-viewer-stage-wrap">
                <div
                    className="pt-viewer-stage"
                    onTouchStart={(e) => { if (e.touches.length === 1 && !zoomed) swipeRef.current = { x: e.touches[0].clientX, y: e.touches[0].clientY }; }}
                    onTouchEnd={(e) => {
                        const start = swipeRef.current;
                        swipeRef.current = null;
                        if (!start || zoomed || e.changedTouches.length === 0) return;
                        const dx = e.changedTouches[0].clientX - start.x;
                        const dy = e.changedTouches[0].clientY - start.y;
                        if (Math.abs(dx) > 50 && Math.abs(dx) > Math.abs(dy) * 1.5) viewerStep(dx < 0 ? 1 : -1);
                    }}
                >
                    <button
                        type="button"
                        className="pt-viewer-nav prev"
                        onClick={() => viewerStep(-1)}
                        disabled={index === 0}
                        aria-label="Previous"
                    >
                        <ChevronLeftIcon />
                    </button>
                    <div
                        ref={photoRef}
                        className="pt-viewer-photo"
                        onDoubleClick={() => (zoomed ? resetZoom() : zoomBy(ZOOM_STEP * 2))}
                        onMouseDown={(e) => { if (zoomed) panRef.current = { x: e.clientX, y: e.clientY, px: pan.x, py: pan.y }; }}
                        onMouseMove={(e) => {
                            const p = panRef.current;
                            if (!p) return;
                            setPan({ x: p.px + (e.clientX - p.x), y: p.py + (e.clientY - p.y) });
                        }}
                        onMouseUp={() => { panRef.current = null; }}
                        onMouseLeave={() => { panRef.current = null; }}
                        style={{ cursor: zoomed ? 'grab' : 'default' }}
                    >
                        {mainSrc ? (
                            <img
                                className="pt-viewer-img"
                                src={mainSrc}
                                alt={photo.filename}
                                draggable={false}
                                style={{ transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom}) rotate(${displayRotation}deg)` }}
                            />
                        ) : (
                            <div className="pt-viewer-loading">Loading…</div>
                        )}
                    </div>
                    <button
                        type="button"
                        className="pt-viewer-nav next"
                        onClick={() => viewerStep(1)}
                        disabled={index === live.length - 1}
                        aria-label="Next"
                    >
                        <ChevronRightIcon />
                    </button>
                </div>

                {/* Floating zoom / rotate / full-res controls over the stage. */}
                <div className="pt-viewer-tools" role="toolbar" aria-label="Image controls">
                    <button type="button" onClick={() => zoomBy(-ZOOM_STEP)} disabled={zoom <= ZOOM_MIN} aria-label="Zoom out"><MagnifyingGlassMinusIcon /></button>
                    <span className="pt-viewer-zoom-label">{Math.round(zoom * 100)}%</span>
                    <button type="button" onClick={() => zoomBy(ZOOM_STEP)} disabled={zoom >= ZOOM_MAX} aria-label="Zoom in"><MagnifyingGlassPlusIcon /></button>
                    <span className="pt-viewer-tools-sep" />
                    <button type="button" onClick={() => rotate(-90)} aria-label="Rotate left"><ArrowUturnLeftIcon /></button>
                    <button type="button" onClick={() => rotate(90)} aria-label="Rotate right"><ArrowUturnRightIcon /></button>
                    {!isVideo && (
                        <>
                            <span className="pt-viewer-tools-sep" />
                            <button
                                type="button"
                                className={fullRes ? 'on' : undefined}
                                onClick={() => setFullRes((v) => !v)}
                                aria-pressed={fullRes}
                                aria-label={fullResLoading ? `Loading full resolution, ${fullResProgress}%` : 'Full resolution'}
                                title={fullRes ? (fullResLoading ? `Loading full resolution (${fullResProgress}%)` : 'Showing full resolution') : 'Load full resolution'}
                            >
                                {fullResLoading ? (
                                    <span className="pt-viewer-fr-loading">
                                        <svg viewBox="0 0 36 36" className="pt-viewer-fr-ring" aria-hidden="true">
                                            <circle cx="18" cy="18" r="16" fill="none" stroke="currentColor" strokeOpacity="0.25" strokeWidth="3" />
                                            <circle
                                                cx="18" cy="18" r="16" fill="none"
                                                stroke="currentColor" strokeWidth="3" strokeLinecap="round"
                                                strokeDasharray={100.53}
                                                strokeDashoffset={100.53 * (1 - fullResProgress / 100)}
                                                transform="rotate(-90 18 18)"
                                            />
                                        </svg>
                                        <span className="pt-viewer-fr-percent">{fullResProgress}%</span>
                                    </span>
                                ) : (
                                    <span className="pt-viewer-fr">FR</span>
                                )}
                            </button>
                        </>
                    )}
                </div>

                {showInfo && (
                    <aside className="pt-viewer-info card-glass" aria-label="Photo information">
                        <div className="pt-viewer-info-head">
                            <h3>Photo info</h3>
                            <button type="button" onClick={() => setShowInfo(false)} aria-label="Close info">×</button>
                        </div>
                        {!meta ? (
                            <p className="pt-viewer-info-empty">Loading details…</p>
                        ) : (
                            <dl className="pt-viewer-info-list">
                                <dt>File</dt><dd>{photo.filename}</dd>
                                {(photo.dateLabel || exif.capturedAt) && (<><dt>Captured</dt><dd>{photo.dateLabel || exif.capturedAt}</dd></>)}
                                {meta.resolution?.width ? (<><dt>Dimensions</dt><dd>{meta.resolution.width} × {meta.resolution.height}</dd></>) : null}
                                <dt>Camera</dt><dd>{dash(exif.camera)}</dd>
                                <dt>Lens</dt><dd>{dash(exif.lens)}</dd>
                                {(exif.fNumber || exif.exposureTime || exif.iso || exif.focalLength) && (
                                    <>
                                        <dt>Exposure</dt>
                                        <dd>
                                            {[
                                                exif.focalLength && `${exif.focalLength}`,
                                                exif.fNumber && `ƒ/${exif.fNumber}`,
                                                exif.exposureTime && `${exif.exposureTime}s`,
                                                exif.iso && `ISO ${exif.iso}`,
                                            ].filter(Boolean).join(' · ') || '—'}
                                        </dd>
                                    </>
                                )}
                                {locationLine && (<><dt>Location</dt><dd>{locationLine}</dd></>)}
                                {infoTags.length > 0 && (
                                    <>
                                        <dt>Tags</dt>
                                        <dd className="pt-viewer-info-tags">{infoTags.map((t) => <span key={t} className="pt-chip">{t}</span>)}</dd>
                                    </>
                                )}
                                {meta.ocrText && meta.ocrText.trim() && (<><dt>Text</dt><dd className="pt-viewer-info-ocr">{meta.ocrText.trim().slice(0, 400)}</dd></>)}
                            </dl>
                        )}
                    </aside>
                )}
            </div>

            <div className="pt-viewer-bar">
                <div className="pt-vb-group left">
                    <Stars value={photo.rating} onRate={(n) => ratePhotos([photo.id], n)} size={20} />
                </div>
                <div className="pt-vb-group center">
                    <button type="button" className={`pt-vb-btn${photo.liked ? ' on' : ''}`} onClick={() => toggleLike(photo.id)}>
                        {photo.liked ? <HeartSolid /> : <HeartOutline />} <span>Like</span>
                    </button>
                    <AddToAlbumMenu
                        photoIds={[photo.id]}
                        align="right"
                        renderTrigger={(toggle) => (
                            <button type="button" className="pt-vb-btn" onClick={toggle}>
                                <PlusIcon /> <span>Album</span>
                            </button>
                        )}
                    />
                    <Menu
                        align="right"
                        renderTrigger={(toggle) => (
                            <button type="button" className="pt-vb-btn" onClick={toggle}>
                                <EllipsisHorizontalIcon /> <span>More</span>
                            </button>
                        )}
                    >
                        {(close) => (
                            <div className="pt-more-menu">
                                <button type="button" onClick={() => { setShowInfo(true); close(); }}>Photo info</button>
                                <button type="button" onClick={() => { close(); void downloadPhoto(photo).then(() => toast('Download started')).catch(() => toast('Download failed')); }}>Download</button>
                                <button type="button" onClick={() => { close(); navigate('tools', { filenames: photo.filename }); }}>Open in Workbench</button>
                                {route.page !== 'gallery' && (
                                    <button
                                        type="button"
                                        onClick={() => {
                                            close();
                                            // Hand the photo off to the Gallery via a route
                                            // param and let it open the viewer inside its own
                                            // (paged, swipeable) sequence once loaded -- see
                                            // GalleryPage's photo-param effect. registerPhotos
                                            // first so the viewer can still resolve this photo
                                            // even if it's on a gallery page not yet fetched.
                                            registerPhotos([photo]);
                                            navigate('gallery', { photo: photo.id });
                                        }}
                                    >
                                        Show in Gallery
                                    </button>
                                )}
                            </div>
                        )}
                    </Menu>
                </div>
                <div className="pt-vb-group right">
                    <button type="button" className="pt-vb-btn" onClick={() => deletePhotos([photo.id])}>
                        <TrashIcon /> <span>Delete</span>
                    </button>
                </div>
            </div>
        </div>
    );
};

export default PhotoViewer;
