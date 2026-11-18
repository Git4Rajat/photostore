import React, { useEffect, useMemo, useState } from 'react';
import { createPortal } from 'react-dom';
import { CheckGlyph as CheckIcon } from './RedesignIcons';
import { useStore } from '../store';
import { tileImage, usePhotoThumbnails } from '../media';
import { TileThumbnail, dayHeading } from './PhotoGrid';
import ScrollSentinel from './ScrollSentinel';
import { Spinner } from './bits';
import { plural } from '../../../utils/format';
import type { Album } from '../types';

/**
 * "Add photos" opens the library over the album, so the album stays where it
 * was underneath. Photos already in the album are marked Added and can't be
 * picked again. Its own selection, separate from the Gallery's.
 */
export const AddPhotosPicker: React.FC<{ album: Album; open: boolean; onClose: () => void }> = ({ album, open, onClose }) => {
    const { photos, photosLoading, hasMorePhotos, loadMorePhotos, albumPhotosById, addPhotosToAlbum } = useStore();
    const [picked, setPicked] = useState<string[]>([]);
    const thumbs = usePhotoThumbnails(open ? photos : []);
    // Only the album's loaded pages are known; that covers what's usually on screen.
    const inAlbum = useMemo(() => new Set((albumPhotosById(album.id) ?? []).map((p) => p.id)), [albumPhotosById, album.id]);

    useEffect(() => { if (!open) setPicked([]); }, [open]);
    useEffect(() => {
        if (!open) return undefined;
        const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
        document.addEventListener('keydown', onKey);
        return () => document.removeEventListener('keydown', onKey);
    }, [open, onClose]);
    // The Gallery may never have been visited: start its first page.
    useEffect(() => {
        if (open && photos.length === 0 && hasMorePhotos && !photosLoading) loadMorePhotos();
    }, [open, photos.length, hasMorePhotos, photosLoading, loadMorePhotos]);

    if (!open) return null;

    const toggle = (id: string) => setPicked((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));
    const add = () => {
        if (!picked.length) return;
        void addPhotosToAlbum(album.id, picked);
        onClose();
    };
    const addLabel = picked.length ? `Add ${picked.length}` : 'Add';
    const alreadyCount = photos.filter((p) => inAlbum.has(p.id)).length;

    return createPortal(
        <div className="pt-picker" role="dialog" aria-modal="true" aria-label={`Add photos to ${album.name}`}>
            <div className="pt-picker-head">
                <button type="button" className="pt-linkish" onClick={onClose}>Cancel</button>
                <b>Add to “{album.name}”</b>
                <button type="button" className="pt-linkish strong" disabled={!picked.length} onClick={add}>{addLabel}</button>
            </div>
            <div className="pt-picker-body">
                {photos.length === 0 && photosLoading ? <Spinner label="Loading your photos…" /> : (
                    <div className="pt-grid select-mode pt-picker-grid">
                        {photos.map((photo, i) => {
                            const heading = dayHeading(photo);
                            const showHeading = i === 0 || dayHeading(photos[i - 1]) !== heading;
                            const added = inAlbum.has(photo.id);
                            const on = picked.includes(photo.id);
                            const image = tileImage(photo, thumbs[photo.filename]);
                            return (
                                <React.Fragment key={photo.id}>
                                    {showHeading && <h2 className="pt-day-heading">{heading}</h2>}
                                    <button
                                        type="button"
                                        className={`pt-tile mock-swatch ${photo.swatch}${on ? ' selected' : ''}${added ? ' added' : ''}`}
                                        aria-pressed={on}
                                        aria-label={added ? `${photo.filename}, already in this album` : photo.filename}
                                        disabled={added}
                                        onClick={() => toggle(photo.id)}
                                    >
                                        {image && <TileThumbnail key={image.src} src={image.src} srcSet={image.srcSet} alt="" style={image.style} />}
                                        {added
                                            ? <span className="pt-picker-added">Added</span>
                                            : <span className={`pt-tile-check${on ? ' on' : ''}`} aria-hidden="true"><CheckIcon /></span>}
                                    </button>
                                </React.Fragment>
                            );
                        })}
                    </div>
                )}
                {hasMorePhotos && <ScrollSentinel onVisible={loadMorePhotos} deps={photos.length} />}
                {photosLoading && photos.length > 0 && <Spinner label="Loading more…" />}
            </div>
            <div className="pt-picker-foot">
                <span>
                    {picked.length ? `${picked.length} selected` : 'Tap photos to pick them'}
                    {alreadyCount > 0 && ` · ${plural(alreadyCount, 'item')} already in this album`}
                </span>
                <button type="button" className="btn btn-primary" disabled={!picked.length} onClick={add}>{addLabel}</button>
            </div>
        </div>,
        document.body,
    );
};

export default AddPhotosPicker;
