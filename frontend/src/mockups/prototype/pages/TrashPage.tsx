import React, { useEffect } from 'react';
import { Undo2 as ArrowUturnLeftIcon, Trash2 as TrashIcon } from 'lucide-react';
import { useStore } from '../store';
import { usePhotoThumbnails } from '../media';
import { TileThumbnail } from '../components/PhotoGrid';
import { confirmDialog } from '../../../components/shared/dialogs';

/** Recently Deleted — restore individually or all, or purge for good. */
export const TrashPage: React.FC = () => {
    const {
        trash, trashLoading, reloadTrash, restorePhotos, restoreAllTrash, purgePhoto, purgeAllTrash,
        albumTrash, albumTrashLoading, reloadAlbumTrash, restoreAlbum, purgeAlbum,
        navigate,
    } = useStore();
    const thumbs = usePhotoThumbnails(trash.map((t) => t.photo));

    useEffect(() => {
        reloadTrash();
        reloadAlbumTrash();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    // Purging is the one irreversible action in the app, so each entry point
    // confirms first (HIG: confirm destructive, unrecoverable actions).
    const confirmPurgePhoto = async (id: string) => {
        const ok = await confirmDialog({
            title: 'Delete this photo forever?',
            message: 'This permanently removes it. This can’t be undone.',
            confirmLabel: 'Delete forever',
            danger: true,
        });
        if (ok) purgePhoto(id);
    };
    const confirmPurgeAll = async () => {
        const ok = await confirmDialog({
            title: `Empty Recently Deleted?`,
            message: `This permanently removes all ${trash.length} photo${trash.length === 1 ? '' : 's'}. This can’t be undone.`,
            confirmLabel: 'Empty trash',
            danger: true,
        });
        if (ok) purgeAllTrash();
    };
    const confirmPurgeAlbum = async (id: string, name: string) => {
        const ok = await confirmDialog({
            title: `Delete “${name}” forever?`,
            message: 'This permanently removes the album. This can’t be undone.',
            confirmLabel: 'Delete forever',
            danger: true,
        });
        if (ok) purgeAlbum(id);
    };

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
                        <button type="button" className="btn btn-danger" onClick={() => void confirmPurgeAll()}>Empty trash</button>
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
                                <TileThumbnail key={thumbs[t.photo.filename]} src={thumbs[t.photo.filename]} alt={t.photo.filename} />
                            )}
                            <div className="pt-trash-actions">
                                <button type="button" title="Restore" aria-label="Restore" onClick={() => restorePhotos([t.photo.id])}><ArrowUturnLeftIcon /></button>
                                <button type="button" title="Delete forever" aria-label="Delete forever" onClick={() => void confirmPurgePhoto(t.photo.id)}><TrashIcon /></button>
                            </div>
                            <span className="pt-trash-days">{t.purgesInDays}d</span>
                        </div>
                    ))}
                </div>
            )}

            {(albumTrash.length > 0 || albumTrashLoading) && (
                <>
                    <div className="pt-toolbar pt-album-trash-header">
                        <h2 className="pt-page-title pt-album-trash-title">Deleted albums</h2>
                    </div>
                    <div className="card-glass pt-history">
                        {albumTrashLoading && albumTrash.length === 0 ? (
                            <div className="pt-history-row">Loading…</div>
                        ) : (
                            albumTrash.map((t) => (
                                <div key={t.album.id} className="pt-history-row pt-album-trash-row">
                                    <span className="pt-album-trash-label">{t.album.name || 'Untitled album'} · purges in {t.purgesInDays}d</span>
                                    <span className="pt-album-trash-actions">
                                        <button type="button" className="btn-link" onClick={() => restoreAlbum(t.album.id)}>Restore</button>
                                        <button type="button" className="btn-link pt-album-trash-purge" onClick={() => void confirmPurgeAlbum(t.album.id, t.album.name || 'Untitled album')}>Delete forever</button>
                                    </span>
                                </div>
                            ))
                        )}
                    </div>
                </>
            )}
        </div>
    );
};

export default TrashPage;
