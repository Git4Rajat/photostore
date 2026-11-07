import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
    ArrowLeft as ArrowLeftIcon,
    RotateCcw as ArrowUturnLeftIcon,
    RotateCw as ArrowUturnRightIcon,
    ChevronLeft as ChevronLeftIcon,
    ChevronRight as ChevronRightIcon,
    Download as DownloadIcon,
    ZoomOut as MagnifyingGlassMinusIcon,
    ZoomIn as MagnifyingGlassPlusIcon,
} from 'lucide-react';
import { Spinner } from './bits';

export const ZOOM_MIN = 1;
export const ZOOM_MAX = 4;
export const ZOOM_STEP = 0.5;

export interface ViewerMedia {
    url: string | null | undefined;
    loading: boolean;
    progress: number;
    status?: string;
}

export interface ViewerShellProps {
    /** Changes whenever the active photo does; resets zoom/pan/rotation. */
    photoKey: string;
    filename: string;
    title: string;
    index: number;
    count: number;
    /** The photo's stored orientation, in degrees. */
    initialRotation?: number;
    /** Called with the new absolute rotation and the one it replaced. */
    onRotate?: (next: number, previous: number) => void;
    onClose: () => void;
    onStep: (delta: number) => void;
    media: ViewerMedia;
    /** When set the stage plays this instead of showing an image. */
    videoSrc?: string;
    fullRes: boolean;
    onToggleFullRes: () => void;
    showFullResControl: boolean;
    onDownloadOriginal: () => void;
    /** Rendered inside the stage (the info panel). */
    info?: React.ReactNode;
    /** The bottom action bar's groups. */
    bar: React.ReactNode;
}

/** Store-free photo viewer chrome shared by the Gallery's viewer and the
 *  shared album's: top bar, stage with prev/next + swipe, zoom/pan/pinch,
 *  rotate and full-res controls, focus trap and keyboard. What a viewer does
 *  with a photo (rate, album, delete, info) is supplied by the caller. */
const ViewerShell: React.FC<ViewerShellProps> = ({
    photoKey, filename, title, index, count, initialRotation = 0, onRotate, onClose, onStep,
    media, videoSrc, fullRes, onToggleFullRes, showFullResControl, onDownloadOriginal, info, bar,
}) => {
    const { url: mainSrc, loading: fullResLoading, progress: fullResProgress, status: mediaStatus } = media;
    const [rotation, setRotation] = useState(0);
    const [zoom, setZoom] = useState(1);
    // Mirrors `zoom` for the imperative pinch/gesture listeners below, so they
    // can read the current zoom as a pinch baseline without listing `zoom` as
    // an effect dependency -- re-running those effects mid-gesture detaches and
    // reattaches the listeners, dropping events partway through a pinch.
    const zoomRef = useRef(1);
    useEffect(() => { zoomRef.current = zoom; }, [zoom]);
    const [pan, setPan] = useState({ x: 0, y: 0 });
    // Mirrors `pan` for the same reason zoomRef mirrors `zoom`.
    const panStateRef = useRef(pan);
    useEffect(() => { panStateRef.current = pan; }, [pan]);
    // On touch devices the floating zoom/rotate toolbar sits over the top of the
    // photo (it overlaps tall portrait shots). Hide it until the user taps the
    // photo, iOS-style; a tap toggles it. Desktop keeps it always visible (CSS).
    const [showTools, setShowTools] = useState(false);
    const panRef = useRef<{ x: number; y: number; px: number; py: number } | null>(null);
    const onRotateRef = useRef(onRotate);
    useEffect(() => { onRotateRef.current = onRotate; }, [onRotate]);
    const previousRotationRef = useRef(0);

    // Reset transient view state whenever the active photo changes. Keyed on the
    // photo only, deliberately not its rotation: persisting a new rotation
    // writes it back to the store, and that must not clobber the user's
    // just-applied in-session rotation.
    useEffect(() => {
        const start = initialRotation ? ((initialRotation % 360) + 360) % 360 : 0;
        previousRotationRef.current = start;
        setRotation(start);
        setZoom(1);
        setPan({ x: 0, y: 0 });
        setShowTools(false);
    }, [photoKey]); // eslint-disable-line react-hooks/exhaustive-deps

    const displayRotation = ((rotation % 360) + 360) % 360;
    // Defense in depth: a tier whose fetch succeeds (200, bytes arrive) but whose
    // content the browser can't decode as an image shows up here, as the <img>
    // onError, not as a network error. Treated like the hook's 'unavailable'.
    const [decodeFailed, setDecodeFailed] = useState(false);
    useEffect(() => { setDecodeFailed(false); }, [mainSrc]);
    const swipeRef = useRef<{ x: number; y: number } | null>(null);

    const photoRef = useRef<HTMLDivElement>(null);
    // Focus management for the modal (HIG: a modal traps focus and restores it
    // to the trigger on close).
    const rootRef = useRef<HTMLDivElement>(null);
    const restoreFocusRef = useRef<HTMLElement | null>(null);
    useEffect(() => {
                restoreFocusRef.current = (document.activeElement as HTMLElement) ?? null;
        // Focus the dialog itself so Tab starts inside it and SR announces it.
        rootRef.current?.focus();
        return () => {
            const el = restoreFocusRef.current;
            if (el && typeof el.focus === 'function' && document.contains(el)) el.focus();
        };
    }, []);

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
        setRotation((r) => {
            const next = ((r + delta) % 360 + 360) % 360;
            onRotateRef.current?.(next, previousRotationRef.current);
            return next;
        });
    }, []);

    // Zoom listeners (wheel/gesture/touch) bind here, to the whole media area,
    // not just the image element -- a real pinch or ctrl+scroll rarely lands
    // exactly on the photo, and when it lands on the surrounding stage the
    // browser was zooming the *page* instead (reported "page zooms instead").
    const stageWrapRef = useRef<HTMLDivElement>(null);

    useEffect(() => {
        const onKey = (e: KeyboardEvent) => {
            if (e.key === 'Escape') onClose();
            if (e.key === 'ArrowLeft') onStep(-1);
            if (e.key === 'ArrowRight') onStep(1);
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
    }, [onClose, onStep, zoomBy]);

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
        // photoKey gates re-attachment: the viewer renders null while closed,
        // so the ref is null on the initial mount and only becomes the real
        // element once a photo opens -- without re-running here, the listener
        // would never attach and pinch/scroll-zoom silently no-ops.
    }, [zoomAtPoint, photoKey]);

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
    }, [photoKey]);

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
    }, [photoKey]);
    const zoomed = zoom > 1;

    return (
        <div ref={rootRef} tabIndex={-1} className={`pt-viewer${showTools ? ' tools-visible' : ''}`} role="dialog" aria-modal="true" aria-label="Photo viewer">
            <div className="pt-viewer-top">
                <button type="button" className="pt-viewer-back" onClick={onClose}>
                    <ArrowLeftIcon /> Back
                </button>
                <span className="pt-viewer-title">{title}</span>
                <span className="pt-viewer-pos">{index + 1} / {count}</span>
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
                        if (Math.abs(dx) > 50 && Math.abs(dx) > Math.abs(dy) * 1.5) onStep(dx < 0 ? 1 : -1);
                    }}
                >
                    <button
                        type="button"
                        className="pt-viewer-nav prev"
                        onClick={() => onStep(-1)}
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
                        {videoSrc ? (
                            <video className="pt-viewer-img" src={videoSrc} poster={mainSrc || undefined} controls playsInline preload="metadata" />
                        ) : mainSrc && !decodeFailed ? (
                            <img
                                className="pt-viewer-img"
                                src={mainSrc}
                                alt={filename}
                                draggable={false}
                                style={{ transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom}) rotate(${displayRotation}deg)` }}
                                onError={() => setDecodeFailed(true)}
                            />
                        ) : mediaStatus === 'unavailable' || decodeFailed ? (
                            <div className="pt-viewer-unavailable">
                                <p>Unsupported format — unable to generate a preview.</p>
                                <button
                                    type="button"
                                    onClick={onDownloadOriginal}
                                >
                                    <DownloadIcon size={16} aria-hidden="true" />
                                    Download original
                                </button>
                            </div>
                        ) : (
                            <div className="pt-viewer-loading">
                                <Spinner label="" center={false} />
                                {mediaStatus === 'pending' && <p>Preparing preview…</p>}
                            </div>
                        )}
                    </div>
                    <button
                        type="button"
                        className="pt-viewer-nav next"
                        onClick={() => onStep(1)}
                        disabled={index === count - 1}
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
                    {showFullResControl && (
                        <>
                            <span className="pt-viewer-tools-sep" />
                            <button
                                type="button"
                                className={fullRes ? 'on' : undefined}
                                onClick={onToggleFullRes}
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

                {info}
            </div>

            <div className="pt-viewer-bar">{bar}</div>
        </div>
    );
};

export default ViewerShell;
