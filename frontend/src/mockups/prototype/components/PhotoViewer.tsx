import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Wrench as WorkbenchIcon } from 'lucide-react';
import {
    AddToAlbumGlyph as AddToAlbumIcon,
    PinGlyph as MapPinIcon,
    DownloadGlyph as DownloadIcon,
    HeartGlyph as Heart,
    InfoGlyph as InfoIcon,
    MoreGlyph as EllipsisHorizontalIcon,
    StarGlyph as StarIcon,
    TrashGlyph as TrashIcon,
} from './RedesignIcons';
import { Menu, Stars } from './bits';
import { AddToAlbumMenu, AddToAlbumSheet } from './AddToAlbumMenu';
import { confirmDeleteItems } from './confirmDelete';
import ViewerShell from './ViewerShell';
import { useStore, isVideoFilename } from '../store';
import { useMainMedia, useVideoSource, downloadPhoto, fetchPhotoMetadata, preloadMediaAccessUrls, useLiveClipSource } from '../media';
import type { PhotoMetadata } from '../media';
import { captureWallClock } from '../../../utils/captureDate';

const DAY_FORMAT = new Intl.DateTimeFormat(undefined, { day: 'numeric', month: 'short', year: 'numeric' });
const LONG_DAY_FORMAT = new Intl.DateTimeFormat(undefined, { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' });
const TIME_FORMAT = new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit' });

const formatFileSize = (bytes: number): string => (
    bytes >= 1_000_000 ? `${(bytes / 1_000_000).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1000))} KB`
);

// EXIF arrives as raw, unrounded numeric strings (e.g. a computed FNumber of
// "7.66082624558859") -- these convert them to the rounded, unit-suffixed
// form a camera's own display would show (ISO 6400, 500 mm, f/8, 1/100 s).
const formatIso = (iso?: string): string | null => {
    if (!iso) return null;
    const n = parseFloat(iso);
    return `ISO ${Number.isFinite(n) ? Math.round(n) : iso}`;
};
const formatFocalLength = (focalLength?: string): string | null => {
    if (!focalLength) return null;
    const n = parseFloat(focalLength);
    // Phone lenses are a few millimetres: keep one decimal there (2.2 mm, not 2 mm).
    return Number.isFinite(n) ? `${n < 10 ? Number(n.toFixed(1)) : Math.round(n)} mm` : focalLength;
};
// One decimal, as a lens barrel prints it: f/2.2, f/8.
const formatAperture = (fNumber?: string): string | null => {
    if (!fNumber) return null;
    const n = parseFloat(fNumber);
    return `f/${Number.isFinite(n) ? Number(n.toFixed(1)) : fNumber}`;
};
const formatShutterSpeed = (exposureTime?: string): string | null => {
    if (!exposureTime) return null;
    const n = parseFloat(exposureTime);
    if (!Number.isFinite(n) || n <= 0) return `${exposureTime} s`;
    return n >= 1 ? `${Number.isInteger(n) ? n : n.toFixed(1)} s` : `1/${Math.round(1 / n)} s`;
};

/** Full-screen photo viewer with a persistent action bar, prev/next, keyboard,
 *  rotate + zoom controls, an EXIF/location info panel, and on-demand full-res. */
const PhotoViewer: React.FC = () => {
    const { viewer, photoById, openViewer, closeViewer, viewerStep, ratePhotos, toggleLike, applyPhotoRotation, savePhotoRotation, deletePhotos, navigate, toast, route, registerPhotos } = useStore();

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

    // The photo's rotation is applied to the stage immediately, but saving it is
    // deferred until the user leaves the photo (prev/next/close) so a run of
    // clicks writes once. The server bakes only EXIF orientation into the
    // preview, never this manual rotation, so the viewer applies the full value
    // as a CSS transform and persists it through the store's edits journal.
    const pendingRotationRef = useRef<{ filename: string; rotation: number; previous: number } | null>(null);
    const flushPendingRotation = useCallback(() => {
        const pending = pendingRotationRef.current;
        if (!pending) return;
        pendingRotationRef.current = null;
        // Optimistic in the store first so the grid and any reopen reflect it; on
        // failure the save reverts the store to the saved-before value itself.
        applyPhotoRotation(pending.filename, pending.rotation);
        savePhotoRotation(pending.filename, pending.rotation, pending.previous);
    }, [applyPhotoRotation, savePhotoRotation]);

    const [fullRes, setFullRes] = useState(false);
    const [showInfo, setShowInfo] = useState(false);
    const [albumSheetOpen, setAlbumSheetOpen] = useState(false);
    const [meta, setMeta] = useState<PhotoMetadata | null>(null);

    // Reset per-photo state, flushing any pending rotation for the photo being
    // left. Keyed on photo?.id only, deliberately not photo.rotation (see above).
    useEffect(() => {
        setFullRes(false);
        setShowInfo(false);
        setMeta(null);
        return () => flushPendingRotation();
    }, [photo?.id, flushPendingRotation]); // eslint-disable-line react-hooks/exhaustive-deps

    // Also flush on unmount (covers a hard teardown; the photo-change cleanup
    // above covers close-via-Back, which flips photo?.id to undefined).
    useEffect(() => () => flushPendingRotation(), [flushPendingRotation]);

    const handleRotate = useCallback((next: number, previous: number) => {
        if (!photo) return;
        pendingRotationRef.current = { filename: photo.filename, rotation: next, previous };
    }, [photo]);

    // Fetch metadata as each photo opens: the title names the place it was
    // taken, and the info panel reads from the same response.
    useEffect(() => {
        if (!photo || meta) return;
        let active = true;
        void fetchPhotoMetadata(photo.filename).then((m) => { if (active) setMeta(m); });
        return () => { active = false; };
    }, [photo, meta]);

    const media = useMainMedia(photo && isVideoFilename(photo.filename) ? null : photo, fullRes);
    const video = useVideoSource(photo);
    const liveSrc = useLiveClipSource(photo && !isVideoFilename(photo.filename) ? photo.liveClip : undefined);

    if (!viewer) return null;
    if (!photo) return null;

    const isVideo = isVideoFilename(photo.filename);
    const exif = meta?.exifSummary ?? {};
    const loc = meta?.location ?? {};
    const infoTags = Array.from(new Set([...(meta?.tags ?? []), ...(meta?.objects ?? []), ...(photo.tags ?? [])])).slice(0, 24);
    const locationLine = [loc.city, loc.country].filter(Boolean).join(', ');
    // One line of camera settings, with the lens only when its name is short ("Ultra Wide").
    const shortLens = exif.lens && exif.lens.length <= 24 ? exif.lens : null;
    const exposure = [shortLens, formatFocalLength(exif.focalLength), formatAperture(exif.fNumber), formatShutterSpeed(exif.exposureTime), formatIso(exif.iso)]
        .filter(Boolean).join(' · ');
    const fileSize = typeof meta?.size === 'number' && meta.size > 0 ? formatFileSize(meta.size) : '';
    // Title: where, then when. Without a known place the day takes the title.
    const captured = captureWallClock(photo.captureDate ?? null);
    const dayText = captured ? DAY_FORMAT.format(captured) : '';
    const timeText = captured ? TIME_FORMAT.format(captured) : '';
    const place = loc.city || loc.country || '';
    const title = place || dayText || photo.filename;
    const subtitle = place ? [dayText, timeText].filter(Boolean).join(' · ') : timeText;
    const startDownload = () => {
        toast('Preparing download…');
        void downloadPhoto(photo).then(() => toast('Download started')).catch(() => toast('Download failed', undefined, undefined, 'error'));
    };

    return (
        <ViewerShell
            photoKey={photo.id}
            filename={photo.filename}
            title={title}
            subtitle={subtitle}
            index={index}
            count={live.length}
            initialRotation={photo.rotation}
            onRotate={handleRotate}
            onClose={closeViewer}
            onStep={viewerStep}
            media={{ url: media.url, loading: media.loading, progress: media.progress, status: media.status }}
            videoSrc={video.src}
            onVideoError={video.reportError}
            videoFailed={video.failed}
            videoPoster={video.poster}
            liveSrc={liveSrc}
            fullRes={fullRes}
            onToggleFullRes={() => setFullRes((v) => !v)}
            onFullResFailed={() => {
                setFullRes(false);
                toast('This browser can’t show this photo’s original. Showing the preview instead.');
            }}
            // For a RAW, FR loads the backend's native-size embedded preview (see useMainMedia).
            showFullResControl={!isVideo}
            onDownloadOriginal={startDownload}
            info={showInfo ? (
                    <aside className="pt-viewer-info" aria-label="Photo information">
                            {!meta ? (
                                <p className="pt-viewer-info-empty">Loading details…</p>
                            ) : (
                                <>
                                    {/* When, where, the camera, then what's in it -- as the redesign lays it out. */}
                                    <div className="pt-vi-block">
                                        <b>{captured ? LONG_DAY_FORMAT.format(captured) : (exif.capturedAt || 'Date unknown')}</b>
                                        <span>{[timeText, photo.filename, fileSize].filter(Boolean).join(' · ')}</span>
                                    </div>
                                    {locationLine && (
                                        <div className="pt-vi-place"><MapPinIcon aria-hidden="true" /><span>{locationLine}</span></div>
                                    )}
                                    {(exif.camera || exposure) && (
                                        <div className="pt-vi-block">
                                            <b>{exif.camera || 'Camera unknown'}</b>
                                            {exposure && <span>{exposure}</span>}
                                            {meta.resolution?.width ? <span>{meta.resolution.width} × {meta.resolution.height}</span> : null}
                                        </div>
                                    )}
                                    {infoTags.length > 0 && (
                                        <div className="pt-vi-tags">{infoTags.map((t) => <span key={t}>{t}</span>)}</div>
                                    )}
                                    <div className="pt-vi-block">
                                        <span className="pt-vi-label">Your rating</span>
                                        <Stars value={photo.rating} onRate={(n) => ratePhotos([photo.id], n)} size={20} />
                                    </div>
                                    {meta.ocrText && meta.ocrText.trim() && (
                                        <div className="pt-vi-block">
                                            <span className="pt-vi-label">Text in the photo</span>
                                            <span className="pt-viewer-info-ocr">{meta.ocrText.trim().slice(0, 400)}</span>
                                        </div>
                                    )}
                                </>
                            )}
                        </aside>
            ) : null}
            bar={(
                <>
                <div className="pt-vb-group center">
                    <button type="button" className={`pt-vb-btn like${photo.liked ? ' on' : ''}`} aria-pressed={photo.liked} title="Like" onClick={() => toggleLike(photo.id)}>
                        <Heart fill={photo.liked ? 'currentColor' : 'none'} /> <span>Like</span>
                    </button>
                    <Menu
                        align="right"
                        renderTrigger={(toggle) => (
                            <button type="button" className={`pt-vb-btn rate${photo.rating ? ' on' : ''}`} title="Rate" onClick={toggle}>
                                <StarIcon fill={photo.rating ? 'currentColor' : 'none'} /> <span>Rate</span>
                            </button>
                        )}
                    >
                        {(close) => (
                            <div className="pt-more-menu">
                                <div className="pt-fm-rate solo">
                                    <Stars value={photo.rating} onRate={(n) => { ratePhotos([photo.id], n); close(); }} size={22} />
                                </div>
                            </div>
                        )}
                    </Menu>
                    <button type="button" className={`pt-vb-btn info${showInfo ? ' on' : ''}`} aria-pressed={showInfo} title="Info" onClick={() => setShowInfo((v) => !v)}>
                        <InfoIcon /> <span>Info</span>
                    </button>
                    <AddToAlbumMenu
                        photoIds={[photo.id]}
                        align="right"
                        renderTrigger={(toggle) => (
                            <button type="button" className="pt-vb-btn desk" title="Add to album" onClick={toggle}>
                                <AddToAlbumIcon /> <span>Album</span>
                            </button>
                        )}
                    />
                    <button type="button" className="pt-vb-btn desk" title="Open in Workbench" onClick={() => navigate('tools', { filenames: photo.filename })}>
                        <WorkbenchIcon /> <span>Workbench</span>
                    </button>
                    <Menu
                        align="right"
                        className={`pt-vb-more${route.page === 'gallery' ? ' phone-menu-only' : ''}`}
                        renderTrigger={(toggle) => (
                            <button type="button" className="pt-vb-btn" title="More" onClick={toggle}>
                                <EllipsisHorizontalIcon /> <span>More</span>
                            </button>
                        )}
                    >
                        {(close) => (
                            <div className="pt-more-menu">
                                {/* Phones keep the bar to Like, Rate, Info and More; the rest live here. */}
                                <button type="button" className="phone-only" onClick={() => { close(); setAlbumSheetOpen(true); }}>Add to album</button>
                                <button type="button" className="phone-only" onClick={() => { close(); navigate('tools', { filenames: photo.filename }); }}>Open in Workbench</button>
                                <button type="button" className="phone-only" onClick={() => { close(); startDownload(); }}>Download</button>
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
                                <button type="button" className="phone-only danger" onClick={() => { close(); void confirmDeleteItems(1).then((ok) => { if (ok) deletePhotos([photo.id]); }); }}>Delete</button>
                            </div>
                        )}
                    </Menu>
                    <AddToAlbumSheet open={albumSheetOpen} onClose={() => setAlbumSheetOpen(false)} photoIds={[photo.id]} />
                </div>
                <div className="pt-vb-group right">
                    {/* Desktops show Download in the toolbar; phones keep it under More. */}
                    <button type="button" className="pt-vb-btn wide-only" title="Download original" onClick={startDownload}>
                        <DownloadIcon /> <span>Download</span>
                    </button>
                    <button type="button" className="pt-vb-btn desk" title="Delete" onClick={() => void confirmDeleteItems(1).then((ok) => { if (ok) deletePhotos([photo.id]); })}>
                        <TrashIcon /> <span>Delete</span>
                    </button>
                </div>
                </>
            )}
        />
    );
};

export default PhotoViewer;
