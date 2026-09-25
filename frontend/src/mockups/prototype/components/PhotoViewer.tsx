import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
    ArrowDownTrayIcon,
    ArrowLeftIcon,
    ArrowUturnLeftIcon,
    ArrowUturnRightIcon,
    ArrowsPointingOutIcon,
    ChevronLeftIcon,
    ChevronRightIcon,
    EllipsisHorizontalIcon,
    InformationCircleIcon,
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
    const { viewer, photoById, openViewer, closeViewer, viewerStep, ratePhotos, toggleLike, deletePhotos, navigate, toast } = useStore();

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

    // Per-photo view state. `rotation` is the *absolute* orientation we persist;
    // the preview the server serves is already corrected to the stored rotation,
    // so we only ever apply the delta since mount as a CSS transform (baseRef) —
    // otherwise a stored rotation would double-rotate on screen.
    const [rotation, setRotation] = useState(0);
    const baseRef = useRef(0);
    const [zoom, setZoom] = useState(1);
    const [pan, setPan] = useState({ x: 0, y: 0 });
    const [fullRes, setFullRes] = useState(false);
    const [showInfo, setShowInfo] = useState(false);
    const [meta, setMeta] = useState<PhotoMetadata | null>(null);
    const panRef = useRef<{ x: number; y: number; px: number; py: number } | null>(null);

    // Reset transient view state whenever the active photo changes.
    useEffect(() => {
        const base = photo?.rotation ? ((photo.rotation % 360) + 360) % 360 : 0;
        baseRef.current = base;
        setRotation(base);
        setZoom(1);
        setPan({ x: 0, y: 0 });
        setFullRes(false);
        setShowInfo(false);
        setMeta(null);
    }, [photo?.id]); // eslint-disable-line react-hooks/exhaustive-deps

    const displayRotation = ((rotation - baseRef.current) % 360 + 360) % 360;

    // Fetch metadata lazily the first time the info panel is opened for a photo.
    useEffect(() => {
        if (!showInfo || !photo || meta) return;
        let active = true;
        void fetchPhotoMetadata(photo.filename).then((m) => { if (active) setMeta(m); });
        return () => { active = false; };
    }, [showInfo, photo, meta]);

    const mainSrc = useMainMedia(photo, fullRes);
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
        setRotation((r) => {
            const next = ((r + delta) % 360 + 360) % 360;
            void setPhotoRotation(photo.filename, next).catch(() => toast('Couldn’t save rotation'));
            return next;
        });
    }, [photo, toast]);

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
                        className="pt-viewer-photo"
                        onDoubleClick={() => (zoomed ? resetZoom() : zoomBy(ZOOM_STEP * 2))}
                        onWheel={(e) => { if (e.ctrlKey || e.metaKey) { e.preventDefault(); zoomBy(e.deltaY < 0 ? ZOOM_STEP : -ZOOM_STEP); } }}
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
                                aria-label="Full resolution"
                                title={fullRes ? 'Showing full resolution' : 'Load full resolution'}
                            >
                                <ArrowsPointingOutIcon /> <span className="pt-viewer-fr">FR</span>
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
                    <button
                        type="button"
                        className="pt-vb-btn"
                        onClick={() => { void downloadPhoto(photo).then(() => toast('Download started')).catch(() => toast('Download failed')); }}
                    >
                        <ArrowDownTrayIcon /> <span>Download</span>
                    </button>
                    <button type="button" className={`pt-vb-btn${showInfo ? ' on' : ''}`} onClick={() => setShowInfo((v) => !v)}>
                        <InformationCircleIcon /> <span>Info</span>
                    </button>
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
