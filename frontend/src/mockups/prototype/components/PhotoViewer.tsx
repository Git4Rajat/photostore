import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
    MoreHorizontal as EllipsisHorizontalIcon,
    Plus as PlusIcon,
    Trash2 as TrashIcon,
    Heart,
} from 'lucide-react';
import { Menu, Stars } from './bits';
import { AddToAlbumMenu } from './AddToAlbumMenu';
import { confirmDeleteItems } from './confirmDelete';
import ViewerShell from './ViewerShell';
import { useStore, isVideoFilename } from '../store';
import { useMainMedia, useVideoSource, downloadPhoto, fetchPhotoMetadata, preloadMediaAccessUrls, useLiveClipSource } from '../media';
import type { PhotoMetadata } from '../media';

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

    // Fetch metadata lazily the first time the info panel is opened for a photo.
    useEffect(() => {
        if (!showInfo || !photo || meta) return;
        let active = true;
        void fetchPhotoMetadata(photo.filename).then((m) => { if (active) setMeta(m); });
        return () => { active = false; };
    }, [showInfo, photo, meta]);

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
    const startDownload = () => void downloadPhoto(photo).then(() => toast('Download started')).catch(() => toast('Download failed', undefined, undefined, 'error'));

    return (
        <ViewerShell
            photoKey={photo.id}
            filename={photo.filename}
            title={`${photo.filename}${photo.dateLabel ? ` · ${photo.dateLabel}` : ''}`}
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
            showFullResControl={!isVideo}
            onDownloadOriginal={startDownload}
            info={showInfo ? (
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
            ) : null}
            bar={(
                <>
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
                                <button type="button" onClick={() => { close(); startDownload(); }}>Download</button>
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
                    <button type="button" className="pt-vb-btn" onClick={() => void confirmDeleteItems(1).then((ok) => { if (ok) deletePhotos([photo.id]); })}>
                        <TrashIcon /> <span>Delete</span>
                    </button>
                </div>
                </>
            )}
        />
    );
};

export default PhotoViewer;
