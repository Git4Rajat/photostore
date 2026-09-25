import React, { useEffect, useMemo } from 'react';
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

/** Full-screen photo viewer with a persistent action bar, prev/next, keyboard. */
export const PhotoViewer: React.FC = () => {
    const { viewer, photoById, openViewer, closeViewer, viewerStep, ratePhotos, toggleLike, deletePhotos, toast } = useStore();

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

    if (!viewer) return null;
    const index = Math.min(viewer.index, live.length - 1);
    const photo = live[index];
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

            <div className="pt-viewer-stage">
                <button
                    type="button"
                    className="pt-viewer-nav prev"
                    onClick={() => viewerStep(-1)}
                    disabled={index === 0}
                    aria-label="Previous"
                >
                    <ChevronLeftIcon />
                </button>
                <div className={`pt-viewer-photo mock-swatch ${photo.swatch}`} aria-hidden="true" />
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
                <Stars value={photo.rating} onRate={(n) => ratePhotos([photo.id], n)} size={22} />
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
                <button type="button" className="pt-vb-btn" onClick={() => toast('Shared 1 photo')}>
                    <ArrowUpTrayIcon /> <span>Share</span>
                </button>
                <button type="button" className="pt-vb-btn" onClick={() => deletePhotos([photo.id])}>
                    <TrashIcon /> <span>Delete</span>
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
                            <button type="button" onClick={() => { toast('Photo info'); close(); }}>Photo info</button>
                            <button type="button" onClick={() => { toast('Download started'); close(); }}>Download</button>
                            <button type="button" onClick={() => { toast('Opened in Workbench'); close(); }}>Open in Workbench</button>
                        </div>
                    )}
                </Menu>
            </div>
        </div>
    );
};

export default PhotoViewer;
