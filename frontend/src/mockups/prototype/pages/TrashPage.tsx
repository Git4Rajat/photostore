import React, { useEffect } from 'react';
import { ArrowUturnLeftIcon, TrashIcon } from '@heroicons/react/24/outline';
import { useStore } from '../store';
import { usePhotoThumbnails } from '../media';

/** Recently Deleted — restore individually or all, or purge for good. */
export const TrashPage: React.FC = () => {
    const { trash, trashLoading, reloadTrash, restorePhotos, restoreAllTrash, purgePhoto, purgeAllTrash, navigate } = useStore();
    const thumbs = usePhotoThumbnails(trash.map((t) => t.photo));

    useEffect(() => {
        reloadTrash();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    return (
        <div>
            <div className="pt-toolbar">
                <div>
                    <h1 className="pt-page-title">Recently Deleted</h1>
                    <p className="pt-page-sub">{trash.length} photo{trash.length === 1 ? '' : 's'} · purges after 30 days</p>
                </div>
                {trash.length > 0 && (
                    <div className="pt-toolbar-actions">
                        <button type="button" className="btn" onClick={restoreAllTrash}>Restore all</button>
                        <button type="button" className="btn btn-danger" onClick={purgeAllTrash}>Empty trash</button>
                    </div>
                )}
            </div>

            {trash.length === 0 ? (
                <div className="empty-state">
                    <span className="empty-state-icon"><TrashIcon /></span>
                    <p className="empty-state-title">{trashLoading ? 'Loading…' : 'Nothing in Recently Deleted'}</p>
                    <p className="empty-state-message">Deleted photos rest here for 30 days before they’re gone for good.</p>
                    <div className="empty-state-action">
                        <button type="button" className="btn" onClick={() => navigate('gallery')}>Back to Gallery</button>
                    </div>
                </div>
            ) : (
                <div className="pt-grid">
                    {trash.map((t) => (
                        <div key={t.photo.id} className={`pt-tile pt-trash-tile mock-swatch ${t.photo.swatch}`}>
                            {thumbs[t.photo.filename] && (
                                <img className="pt-tile-img" src={thumbs[t.photo.filename]} alt={t.photo.filename} loading="lazy" draggable={false} />
                            )}
                            <div className="pt-trash-actions">
                                <button type="button" title="Restore" aria-label="Restore" onClick={() => restorePhotos([t.photo.id])}><ArrowUturnLeftIcon /></button>
                                <button type="button" title="Delete forever" aria-label="Delete forever" onClick={() => purgePhoto(t.photo.id)}><TrashIcon /></button>
                            </div>
                            <span className="pt-trash-days">{t.purgesInDays}d</span>
                        </div>
                    ))}
                </div>
            )}
        </div>
    );
};

export default TrashPage;
