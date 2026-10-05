import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
    ArrowLeft as ArrowLeftIcon,
    RotateCcw as ArrowUturnLeftIcon,
    RotateCw as ArrowUturnRightIcon,
    ChevronLeft as ChevronLeftIcon,
    ChevronRight as ChevronRightIcon,
    MoreHorizontal as EllipsisHorizontalIcon,
    ZoomOut as MagnifyingGlassMinusIcon,
    ZoomIn as MagnifyingGlassPlusIcon,
    Plus as PlusIcon,
    Trash2 as TrashIcon,
    Heart,
} from 'lucide-react';
import { Menu, Stars, Spinner } from './bits';
import { AddToAlbumMenu } from './AddToAlbumMenu';
import { useStore, isVideoFilename } from '../store';
import { useMainMedia, downloadPhoto, fetchPhotoMetadata, setPhotoRotation, preloadMediaAccessUrls } from '../media';
import type { PhotoMetadata } from '../media';

const ZOOM_MIN = 1;
const ZOOM_MAX = 4;
const ZOOM_STEP = 0.5;

const dash = (value?: string | number): string => {
    const text = value === undefined || value === null ? '' : String(value).trim();
    return text || '—';
};

// EXIF arrives as raw, unrounded numeric strings (e.g. a computed FNumber of
// "7.66082624558859") -- these convert them to the rounded, unit-suffixed
// form a camera's own display would show (ISO 6400, 500mm, f8, 1/100s).
const formatIso = (iso?: string): string | null => {
    if (!iso) return null;
    const n = parseFloat(iso);
    return `ISO ${Number.isFinite(n) ? Math.round(n) : iso}`;
};
const formatFocalLength = (focalLength?: string): string | null => {
    if (!focalLength) return null;
    const n = parseFloat(focalLength);
    return Number.isFinite(n) ? `${Math.round(n)} mm` : focalLength;
};
const formatAperture = (fNumber?: string): string | null => {
    if (!fNumber) return null;
    const n = parseFloat(fNumber);
    return `f${Number.isFinite(n) ? Math.round(n) : fNumber}`;
};
const formatShutterSpeed = (exposureTime?: string): string | null => {
    if (!exposureTime) return null;
    const n = parseFloat(exposureTime);
    if (!Number.isFinite(n) || n <= 0) return `${exposureTime} s`;
    return n >= 1 ? `${Number.isInteger(n) ? n : n.toFixed(1)} s` : `1/${Math.round(1 / n)} s`;
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

    // Warm the immediate next/prev neighbors' preview-access URLs in one
    // batched call (see preloadMediaAccessUrls/mediaAccessCache.ts) whenever
    // the active photo changes, so stepping through the viewer finds the URL
    // already cached instead of firing a fresh backend round trip per photo.
    useEffect(() => {
        if (!photo) return;
        const neighbors = [live[index - 1], live[index + 1]]
            .filter((p): p is NonNullable<typeof p> => Boolean(p) && !isVideoFilename(p!.filename))
            .map((p) => p!.filename);
        preloadMediaAccessUrls(neighbors);
    }, [live, index, photo]);

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
    // Mirrors `pan` for the same reason zoomRef mirrors `zoom` -- the pinch/
    // gesture listeners below read it as a baseline without depending on it,
    // since depending on it would re-attach the listeners mid-gesture.
    const panStateRef = useRef(pan);
    useEffect(() => { panStateRef.current = pan; }, [pan]);
    const [fullRes, setFullRes] = useState(false);
    const [showInfo, setShowInfo] = useState(false);
    const [meta, setMeta] = useState<PhotoMetadata | null>(null);
    // On touch devices the floating zoom/rotate toolbar sits over the top of the
    // photo (it overlaps tall portrait shots). Hide it until the user taps the
    // photo, iOS-style; a tap toggles it. Desktop keeps it always visible (CSS).
    const [showTools, setShowTools] = useState(false);
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
            toast('Couldn’t save rotation', undefined, undefined, 'error');
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
        setShowTools(false);
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

    const photoRef = useRef<HTMLDivElement>(null);
    // Focus management for the modal (HIG: a modal traps focus and restores it
    // to the trigger on close).
    const rootRef = useRef<HTMLDivElement>(null);
    const restoreFocusRef = useRef<HTMLElement | null>(null);
    const isOpen = Boolean(viewer);
    useEffect(() => {
        if (!isOpen) return undefined;
        restoreFocusRef.current = (document.activeElement as HTMLElement) ?? null;
        // Focus the dialog itself so Tab starts inside it and SR announces it.
        rootRef.current?.focus();
        return () => {
            const el = restoreFocusRef.current;
            if (el && typeof el.focus === 'function' && document.contains(el)) el.focus();
        };
    }, [isOpen]);

    const resetZoom = useCallback(() => { setZoom(1); setPan({ x: 0, y: 0 }); }, []);
    const zoomBy = useCallback((delta: number) => {
        setZoom((z) => {
            const next = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, +(z + delta).toFixed(2)));
            if (next === 1) setPan({ x: 0, y: 0 });
            return next;
        });
    }, []);
    // Zooms toward a screen point (a click, cursor position, or pinch/gesture
    // center) instead of always scaling around the image's own center -- the
    // standard "zoom toward where you clicked/pinched" lightbox behavior.
    // Keeps whatever content point was under (clientX, clientY) under it after
    // the zoom change: with the transform as `translate(pan) scale(zoom)`
    // around the container's center, that point stays fixed when
    // `pan' = pan*ratio + offsetFromCenter*(1-ratio)`, ratio = newZoom/oldZoom.
    const zoomAtPoint = useCallback((targetZoom: number, clientX: number, clientY: number) => {
        const oldZoom = zoomRef.current;
        const next = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, +targetZoom.toFixed(2)));
        if (next === 1) {
            setZoom(1);
            setPan({ x: 0, y: 0 });
            return;
        }
        const rect = photoRef.current?.getBoundingClientRect();
        if (rect && next !== oldZoom) {
            const ratio = next / oldZoom;
            const offsetX = clientX - (rect.left + rect.width / 2);
            const offsetY = clientY - (rect.top + rect.height / 2);
            setPan((p) => ({
                x: p.x * ratio + offsetX * (1 - ratio),
                y: p.y * ratio + offsetY * (1 - ratio),
            }));
        }
        setZoom(next);
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

    // Zoom listeners (wheel/gesture/touch) bind here, to the whole media area,
    // not just the image element -- a real pinch or ctrl+scroll rarely lands
    // exactly on the photo, and when it lands on the surrounding stage the
    // browser was zooming the *page* instead (reported "page zooms instead").
    const stageWrapRef = useRef<HTMLDivElement>(null);

    useEffect(() => {
        if (!viewer) return undefined;
        const onKey = (e: KeyboardEvent) => {
            if (e.key === 'Escape') closeViewer();
            if (e.key === 'ArrowLeft') viewerStep(-1);
            if (e.key === 'ArrowRight') viewerStep(1);
            if (e.key === '+' || e.key === '=') zoomBy(ZOOM_STEP);
            if (e.key === '-' || e.key === '_') zoomBy(-ZOOM_STEP);
            if (e.key === 'Tab') {
                // Trap Tab within the dialog so focus can't wander to the
                // (visually hidden) page behind the overlay.
                const root = rootRef.current;
                if (!root) return;
                const focusables = Array.from(
                    root.querySelectorAll<HTMLElement>(
                        'button:not([disabled]), [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
                    ),
                ).filter((el) => el.offsetParent !== null || el === root);
                if (!focusables.length) { e.preventDefault(); root.focus(); return; }
                const first = focusables[0];
                const last = focusables[focusables.length - 1];
                const active = document.activeElement as HTMLElement;
                if (e.shiftKey && (active === first || active === root)) {
                    e.preventDefault();
                    last.focus();
                } else if (!e.shiftKey && active === last) {
                    e.preventDefault();
                    first.focus();
                }
            }
        };
        document.addEventListener('keydown', onKey);
        return () => document.removeEventListener('keydown', onKey);
    }, [viewer, closeViewer, viewerStep, zoomBy]);

    useEffect(() => {
        const el = stageWrapRef.current;
        if (!el) return undefined;
        const onWheel = (e: WheelEvent) => {
            if (!e.ctrlKey && !e.metaKey) return;
            e.preventDefault();
            zoomAtPoint(zoomRef.current + (e.deltaY < 0 ? ZOOM_STEP : -ZOOM_STEP), e.clientX, e.clientY);
        };
        el.addEventListener('wheel', onWheel, { passive: false });
        return () => el.removeEventListener('wheel', onWheel);
        // photo?.id gates re-attachment: the viewer renders null while closed,
        // so the ref is null on the initial mount and only becomes the real
        // element once a photo opens -- without re-running here, the listener
        // would never attach and pinch/scroll-zoom silently no-ops.
    }, [zoomAtPoint, photo?.id]);

    // Two-finger trackpad pinch on desktop: Chrome/Firefox report it as a
    // ctrl/cmd+wheel event (handled above); Safari instead fires its
    // non-standard gesture* events, which never carry a ctrlKey wheel at all.
    const gestureStartRef = useRef<{ zoom: number; pan: { x: number; y: number }; offsetX: number; offsetY: number }>({
        zoom: 1, pan: { x: 0, y: 0 }, offsetX: 0, offsetY: 0,
    });
    useEffect(() => {
        const el = stageWrapRef.current;
        if (!el) return undefined;
        const onGestureStart = (e: Event) => {
            e.preventDefault();
            const { clientX, clientY } = e as unknown as { clientX?: number; clientY?: number };
            const rect = photoRef.current?.getBoundingClientRect();
            const hasPoint = rect && typeof clientX === 'number' && typeof clientY === 'number';
            gestureStartRef.current = {
                zoom: zoomRef.current,
                pan: panStateRef.current,
                offsetX: hasPoint ? clientX - (rect.left + rect.width / 2) : 0,
                offsetY: hasPoint ? clientY - (rect.top + rect.height / 2) : 0,
            };
        };
        const onGestureChange = (e: Event) => {
            e.preventDefault();
            const scale = (e as unknown as { scale: number }).scale;
            const start = gestureStartRef.current;
            const next = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, +(start.zoom * scale).toFixed(2)));
            if (next === 1) {
                setPan({ x: 0, y: 0 });
            } else {
                const ratio = next / start.zoom;
                setPan({
                    x: start.pan.x * ratio + start.offsetX * (1 - ratio),
                    y: start.pan.y * ratio + start.offsetY * (1 - ratio),
                });
            }
            setZoom(next);
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
    const touchPinchRef = useRef<{ dist: number; zoom: number; pan: { x: number; y: number }; offsetX: number; offsetY: number } | null>(null);
    // Single-finger drag-to-pan while zoomed. The mouse handlers on
    // .pt-viewer-photo cover pointer devices, but touch devices never get those
    // synthesized reliably, so a pinched-in photo couldn't be moved around on
    // mobile (reported: "can't pan the zoom"). Baseline (px,py) is seeded from
    // the current pan and deltas are measured from the finger's start point.
    const touchPanRef = useRef<{ x: number; y: number; px: number; py: number } | null>(null);
    useEffect(() => {
        const el = stageWrapRef.current;
        if (!el) return undefined;
        const touchDist = (t: TouchList) => t.length >= 2 ? Math.hypot(t[0].clientX - t[1].clientX, t[0].clientY - t[1].clientY) : 0;
        const touchMid = (t: TouchList) => ({ x: (t[0].clientX + t[1].clientX) / 2, y: (t[0].clientY + t[1].clientY) / 2 });
        const seedPan = (t: Touch) => {
            touchPanRef.current = { x: t.clientX, y: t.clientY, px: panStateRef.current.x, py: panStateRef.current.y };
        };
        const onTouchStart = (e: TouchEvent) => {
            if (e.touches.length === 2) {
                e.preventDefault();
                const mid = touchMid(e.touches);
                const rect = photoRef.current?.getBoundingClientRect();
                touchPinchRef.current = {
                    dist: touchDist(e.touches) || 1,
                    zoom: zoomRef.current,
                    pan: panStateRef.current,
                    offsetX: rect ? mid.x - (rect.left + rect.width / 2) : 0,
                    offsetY: rect ? mid.y - (rect.top + rect.height / 2) : 0,
                };
                // A second finger starts a pinch, so drop any single-finger pan.
                touchPanRef.current = null;
            } else if (e.touches.length === 1 && zoomRef.current > 1) {
                // Only claim the single-finger gesture when zoomed -- otherwise
                // leave it to the swipe-to-navigate handler on .pt-viewer-stage.
                e.preventDefault();
                seedPan(e.touches[0]);
            }
        };
        const onTouchMove = (e: TouchEvent) => {
            if (e.touches.length >= 2 && touchPinchRef.current) {
                e.preventDefault();
                const start = touchPinchRef.current;
                const ratio = touchDist(e.touches) / start.dist;
                const next = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, +(start.zoom * ratio).toFixed(2)));
                if (next === 1) {
                    setPan({ x: 0, y: 0 });
                } else {
                    const zoomRatio = next / start.zoom;
                    setPan({
                        x: start.pan.x * zoomRatio + start.offsetX * (1 - zoomRatio),
                        y: start.pan.y * zoomRatio + start.offsetY * (1 - zoomRatio),
                    });
                }
                setZoom(next);
            } else if (e.touches.length === 1 && zoomRef.current > 1) {
                e.preventDefault();
                // Seed lazily if the drag began before zoom, or after lifting one
                // finger out of a pinch, so the photo doesn't jump.
                if (!touchPanRef.current) { seedPan(e.touches[0]); return; }
                const start = touchPanRef.current;
                const t = e.touches[0];
                setPan({ x: start.px + (t.clientX - start.x), y: start.py + (t.clientY - start.y) });
            }
        };
        const onTouchEnd = (e: TouchEvent) => {
            if (e.touches.length < 2) touchPinchRef.current = null;
            if (e.touches.length === 0) {
                touchPanRef.current = null;
            } else if (e.touches.length === 1 && zoomRef.current > 1) {
                // Transitioning from pinch (2 fingers) down to one: re-seed the
                // pan baseline against the remaining finger so it doesn't jump.
                seedPan(e.touches[0]);
            }
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
        <div ref={rootRef} tabIndex={-1} className={`pt-viewer${showTools ? ' tools-visible' : ''}`} role="dialog" aria-modal="true" aria-label="Photo viewer">
            <div className="pt-viewer-top">
                <button type="button" className="pt-viewer-back" onClick={closeViewer}>
                    <ArrowLeftIcon /> Back
                </button>
                <span className="pt-viewer-title">
                    {photo.filename}{photo.dateLabel ? ` · ${photo.dateLabel}` : ''}
                </span>
                <span className="pt-viewer-pos">{index + 1} / {live.length}</span>
            </div>

            <div className="pt-viewer-stage-wrap" ref={stageWrapRef}>
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
                        // Tapping the photo toggles the floating tools on touch
                        // (they're always shown on desktop via CSS, so this is a
                        // no-op there).
                        onClick={() => setShowTools((v) => !v)}
                        onDoubleClick={(e) => (zoomed ? resetZoom() : zoomAtPoint(zoom + ZOOM_STEP * 2, e.clientX, e.clientY))}
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
                            <div className="pt-viewer-loading"><Spinner label="" center={false} /></div>
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
                                                formatIso(exif.iso),
                                                formatFocalLength(exif.focalLength),
                                                formatAperture(exif.fNumber),
                                                formatShutterSpeed(exif.exposureTime),
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
                        <Heart fill={photo.liked ? 'currentColor' : 'none'} /> <span>Like</span>
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
                                <button type="button" onClick={() => { close(); void downloadPhoto(photo).then(() => toast('Download started')).catch(() => toast('Download failed', undefined, undefined, 'error')); }}>Download</button>
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
