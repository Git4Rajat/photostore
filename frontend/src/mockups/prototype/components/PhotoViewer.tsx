import React, { useEffect, useMemo, useRef } from 'react';
import {
    ArrowLeftIcon,
    ArrowUpTrayIcon,
    ChevronLeftIcon,
    ChevronRightIcon,
    EllipsisHorizontalIcon,
    PlusIcon,
    TrashIcon,
} from '@heroicons/react/24/outline';
import { HeartIcon as HeartSolid } from '@heroicons/react/24/solid';
import { HeartIcon as HeartOutline } from '@heroicons/react/24/outline';
import { Menu, Stars } from './bits';
import { AddToAlbumMenu } from './AddToAlbumMenu';
import { useStore } from '../store';
import { useMainMedia, downloadPhoto, sharePhotos } from '../media';

/** Full-screen photo viewer with a persistent action bar, prev/next, keyboard. */
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

    useEffect(() => {
        if (!viewer) return undefined;
        const onKey = (e: KeyboardEvent) => {
            if (e.key === 'Escape') closeViewer();
            if (e.key === 'ArrowLeft') viewerStep(-1);
            if (e.key === 'ArrowRight') viewerStep(1);
        };
        document.addEventListener('keydown', onKey);
        return () => document.removeEventListener('keydown', onKey);
    }, [viewer, closeViewer, viewerStep]);

    const index = viewer ? Math.min(viewer.index, Math.max(0, live.length - 1)) : 0;
    const photo = viewer && live.length ? live[index] : null;
    // Called unconditionally (before the early returns) to respect the rules of
    // hooks; it no-ops when there's no active photo.
    const mainSrc = useMainMedia(photo);
    const swipeRef = useRef<{ x: number; y: number } | null>(null);

    if (!viewer) return null;
    if (!photo) return null;

    return (
        <div className="pt-viewer" role="dialog" aria-modal="true" aria-label="Photo viewer">
            <div className="pt-viewer-top">
                <button type="button" className="pt-viewer-back" onClick={closeViewer}>
                    <ArrowLeftIcon /> Back
                </button>
                <span className="pt-viewer-title">
                    {photo.filename} · {photo.dateLabel}
                </span>
                <span className="pt-viewer-pos">{index + 1} / {live.length}</span>
            </div>

            <div
                className="pt-viewer-stage"
                onTouchStart={(e) => { if (e.touches.length === 1) swipeRef.current = { x: e.touches[0].clientX, y: e.touches[0].clientY }; }}
                onTouchEnd={(e) => {
                    const start = swipeRef.current;
                    swipeRef.current = null;
                    if (!start || e.changedTouches.length === 0) return;
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
                <div className={`pt-viewer-photo mock-swatch ${photo.swatch}`}>
                    {mainSrc && <img className="pt-viewer-img" src={mainSrc} alt={photo.filename} />}
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
                        onClick={() => {
                            void (async () => {
                                const outcome = await sharePhotos([photo]);
                                if (outcome === 'downloaded') toast('Sharing isn’t supported here — downloaded instead');
                                else if (outcome === 'unsupported') toast('Couldn’t share this photo');
                            })();
                        }}
                    >
                        <ArrowUpTrayIcon /> <span>Share</span>
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
                                <button type="button" onClick={() => { toast(`${photo.filename}${photo.dateLabel ? ` · ${photo.dateLabel}` : ''}`); close(); }}>Photo info</button>
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
