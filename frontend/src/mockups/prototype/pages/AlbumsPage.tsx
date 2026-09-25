import React, { useState } from 'react';
import { ArrowLeftIcon, ArrowPathIcon, ClipboardDocumentIcon, PlusIcon, ShareIcon } from '@heroicons/react/24/outline';
import { useStore } from '../store';
import { Swatch } from '../components/bits';
import PhotoGrid from '../components/PhotoGrid';

/** Albums — list first on mobile, detail view on larger screens. */
export const AlbumsPage: React.FC = () => {
    const { albums, route, navigate, photoById, photosByIds, createAlbum, renameAlbum, setAlbumShare, addPhotos, addPhotosToAlbum, toast } = useStore();
    const [showMobileDetail, setShowMobileDetail] = useState(false);
    const selectedId = route.params.albumId ?? albums[0]?.id;
    const album = albums.find((a) => a.id === selectedId) ?? albums[0];
    const [renaming, setRenaming] = useState(false);
    const [draft, setDraft] = useState('');
    const [copied, setCopied] = useState(false);

    if (!album) {
        return (
            <div className="pt-empty-page">
                <p>No albums yet.</p>
                <button type="button" className="btn mock-cta" onClick={() => navigate('albums', { albumId: createAlbum('New album') })}>
                    <PlusIcon className="toolbar-icon" /> New album
                </button>
            </div>
        );
    }

    const url = `keepsake.app/s/${album.id}-${album.share.code.toLowerCase()}`;
    const copy = () => {
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1400);
        void navigator.clipboard?.writeText(url).catch(() => {});
    };
    const commitRename = () => {
        if (draft.trim()) renameAlbum(album.id, draft.trim());
        setRenaming(false);
    };

    return (
        <div className="pt-albums-wrapper">
            {/* Mobile list view */}
            <div className={`pt-albums-mobile-list${showMobileDetail ? ' hidden' : ''}`}>
                <div className="pt-menu-label">Your albums</div>
                {albums.map((a) => {
                    const cover = a.coverPhotoId ? photoById(a.coverPhotoId) : undefined;
                    return (
                        <button
                            key={a.id}
                            type="button"
                            className={`pt-album-row${a.id === album.id ? ' active' : ''}`}
                            onClick={() => {
                                navigate('albums', { albumId: a.id });
                                setShowMobileDetail(true);
                            }}
                        >
                            {cover ? <Swatch swatch={cover.swatch} className="pt-album-row-cover" /> : <span className="pt-album-row-cover empty" />}
                            <span className="pt-album-row-meta">
                                <b>{a.name}</b>
                                <span>{a.photoIds.length} photos</span>
                            </span>
                        </button>
                    );
                })}
                <button type="button" className="albm-newbtn" onClick={() => { const id = createAlbum('New album'); navigate('albums', { albumId: id }); setDraft('New album'); setRenaming(true); setShowMobileDetail(true); }}>
                    <PlusIcon /> New album
                </button>
            </div>

            {/* Desktop sidebar + mobile detail view */}
            <div className="pt-albums-desktop-sidebar">
                {showMobileDetail && (
                    <button
                        type="button"
                        className="pt-back"
                        onClick={() => setShowMobileDetail(false)}
                        aria-label="Back to albums"
                    >
                        <ArrowLeftIcon /> Back
                    </button>
                )}
                <div className={`pt-albums${showMobileDetail ? ' show-detail' : ''}`}>
                    <aside className="pt-album-sidebar">
                        <div className="pt-menu-label">Your albums</div>
                        {albums.map((a) => {
                            const cover = a.coverPhotoId ? photoById(a.coverPhotoId) : undefined;
                            return (
                                <button
                                    key={a.id}
                                    type="button"
                                    className={`pt-album-row${a.id === album.id ? ' active' : ''}`}
                                    onClick={() => navigate('albums', { albumId: a.id })}
                                >
                                    {cover ? <Swatch swatch={cover.swatch} className="pt-album-row-cover" /> : <span className="pt-album-row-cover empty" />}
                                    <span className="pt-album-row-meta">
                                        <b>{a.name}</b>
                                        <span>{a.photoIds.length} photos</span>
                                    </span>
                                </button>
                            );
                        })}
                        <button type="button" className="albm-newbtn" onClick={() => { const id = createAlbum('New album'); navigate('albums', { albumId: id }); setDraft('New album'); setRenaming(true); }}>
                            <PlusIcon /> New album
                        </button>
                    </aside>

                    <section className="pt-album-detail">
                        <div className="pt-album-detail-head">
                            <div>
                                {renaming ? (
                                    <input
                                        className="field albm-title-input"
                                        autoFocus
                                        value={draft}
                                        onChange={(e) => setDraft(e.target.value)}
                                        onBlur={commitRename}
                                        onKeyDown={(e) => { if (e.key === 'Enter') commitRename(); if (e.key === 'Escape') setRenaming(false); }}
                                        aria-label="Album name"
                                    />
                                ) : (
                                    <h1 className="pt-page-title" onDoubleClick={() => { setDraft(album.name); setRenaming(true); }}>{album.name}</h1>
                                )}
                                <p className="pt-page-sub">
                                    {album.photoIds.length} photos ·{' '}
                                    <button type="button" className="pt-linkish" onClick={() => { setDraft(album.name); setRenaming(true); }}>Rename</button>
                                </p>
                            </div>
                            <button type="button" className="btn mock-cta" onClick={() => toast('Share modal')}><ShareIcon className="toolbar-icon" /> <span>Share</span></button>
                        </div>

                        <div className="pt-share-section">
                            <div className="pt-menu-label">Sharing</div>
                            <div className="share-sheet">
                            <div className="share-row">
                                <span className="lbl"><b>Public link</b><span>Anyone with the link can view</span></span>
                                <button
                                    type="button"
                                    role="switch"
                                    aria-checked={album.share.isPublic}
                                    aria-label="Public link"
                                    className="mock-switch"
                                    onClick={() => setAlbumShare(album.id, { isPublic: !album.share.isPublic })}
                                />
                            </div>
                            {album.share.isPublic && (
                                <>
                                    <div className="share-row">
                                        <span className="lbl"><b>Link expires</b></span>
                                        <select className="field field-select" value={album.share.expiry} onChange={(e) => setAlbumShare(album.id, { expiry: e.target.value })}>
                                            <option value="1">In 1 day</option>
                                            <option value="7">In 7 days</option>
                                            <option value="30">In 30 days</option>
                                            <option value="never">Never</option>
                                        </select>
                                    </div>
                                    <div className="share-row">
                                        <span className="share-url">{url}</span>
                                        <button type="button" className="btn" onClick={copy}><ClipboardDocumentIcon className="toolbar-icon" />{copied ? 'Copied' : 'Copy'}</button>
                                    </div>
                                    <div className="share-row">
                                        <span className="lbl"><b>Access code</b><span>Share it separately from the link</span></span>
                                        <span className="share-code">{album.share.code}</span>
                                        <button type="button" className="btn" onClick={() => setAlbumShare(album.id, { code: Math.random().toString(16).slice(2, 6).toUpperCase() })}><ArrowPathIcon className="toolbar-icon" /> New code</button>
                                    </div>
                                </>
                            )}
                        </div>
                        </div>

                        <div className="pt-album-photos-head">
                            <div className="pt-menu-label" style={{ margin: 0 }}>Photos</div>
                            <button type="button" className="pt-linkish" onClick={() => { const ids = addPhotos(6); addPhotosToAlbum(album.id, ids); }}>+ Add photos</button>
                        </div>
                        <PhotoGrid photos={photosByIds(album.photoIds)} emptyHint="No photos yet — add some to set a cover." />
                    </section>
                </div>
            </div>
        </div>
    );
};

export default AlbumsPage;
