import React, { useEffect, useState } from 'react';
import { ArrowLeftIcon, ArrowPathIcon, CheckIcon, ClipboardDocumentIcon, PlusIcon, ShareIcon, TrashIcon } from '@heroicons/react/24/outline';
import { useStore } from '../store';
import PhotoGrid from '../components/PhotoGrid';
import { confirmDialog } from '../../../components/shared/dialogs';

const EXPIRY_OPTIONS: { value: string; label: string; days: number }[] = [
    { value: '1', label: 'In 1 day', days: 1 },
    { value: '7', label: 'In 7 days', days: 7 },
    { value: '30', label: 'In 30 days', days: 30 },
    { value: 'never', label: 'Never', days: 0 },
];

const randomCode = () => Math.random().toString(16).slice(2, 6).toUpperCase();

/** Albums — list first on mobile, detail view on larger screens. */
export const AlbumsPage: React.FC = () => {
    const {
        albums, albumsLoading, route, navigate, openAlbum, albumPhotosById, albumPhotosLoading,
        createAlbum, renameAlbum, deleteAlbum, deleteAlbums, shareAlbum, revokeAlbum, toast,
    } = useStore();
    const [showMobileDetail, setShowMobileDetail] = useState(false);
    const [selectMode, setSelectMode] = useState(false);
    const [selectedAlbumIds, setSelectedAlbumIds] = useState<string[]>([]);
    const selectedId = route.params.albumId ?? albums[0]?.id;
    const album = albums.find((a) => a.id === selectedId) ?? albums[0];
    const [renaming, setRenaming] = useState(false);
    const [draft, setDraft] = useState('');
    const [copied, setCopied] = useState(false);
    const [expiry, setExpiry] = useState('7');
    // The backend never returns an album's access code back (it's a stored
    // secret), so remember codes we generate this session to show the owner.
    const [codes, setCodes] = useState<Record<string, string>>({});

    // Fetch the active album's photos whenever the selection changes.
    useEffect(() => {
        if (album?.id) void openAlbum(album.id);
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [album?.id]);

    const toggleAlbumSelect = (id: string) => {
        setSelectedAlbumIds((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));
    };

    const exitSelectMode = () => {
        setSelectMode(false);
        setSelectedAlbumIds([]);
    };

    const bulkDelete = async () => {
        const count = selectedAlbumIds.length;
        if (!count) return;
        const confirmed = await confirmDialog({
            title: `Delete ${count} album${count === 1 ? '' : 's'}?`,
            message: 'The photos inside stay in your library — only the album is removed.',
            confirmLabel: 'Delete',
            danger: true,
        });
        if (!confirmed) return;
        deleteAlbums(selectedAlbumIds);
        navigate('albums', {});
        exitSelectMode();
    };

    const handleCreate = async (thenRename = false) => {
        const id = await createAlbum('New album');
        if (!id) return;
        navigate('albums', { albumId: id });
        setShowMobileDetail(true);
        if (thenRename) {
            setDraft('New album');
            setRenaming(true);
        }
    };

    if (!album) {
        return (
            <div className="pt-empty-page">
                <p>{albumsLoading ? 'Loading albums…' : 'No albums yet.'}</p>
                {!albumsLoading && (
                    <button type="button" className="btn mock-cta" onClick={() => void handleCreate(true)}>
                        <PlusIcon className="toolbar-icon" /> New album
                    </button>
                )}
            </div>
        );
    }

    const url = album.publicUrl ?? '';
    const copy = () => {
        if (!url) return;
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1400);
        void navigator.clipboard?.writeText(url).catch(() => {});
    };
    const commitRename = () => {
        if (draft.trim()) renameAlbum(album.id, draft.trim());
        setRenaming(false);
    };
    const togglePublic = () => {
        if (album.isPublic) {
            void revokeAlbum(album.id);
        } else {
            const days = EXPIRY_OPTIONS.find((o) => o.value === expiry)?.days ?? 7;
            void shareAlbum(album.id, { expiresInDays: days });
        }
    };
    const changeExpiry = (value: string) => {
        setExpiry(value);
        const days = EXPIRY_OPTIONS.find((o) => o.value === value)?.days ?? 7;
        void shareAlbum(album.id, { expiresInDays: days });
    };

    const activePhotos = albumPhotosById(album.id);

    const albumList = (
        <>
            <div className="pt-album-list-head">
                <div className="pt-menu-label" style={{ margin: 0 }}>Your albums</div>
                {albums.length > 0 && (
                    <button type="button" className="pt-linkish" onClick={() => (selectMode ? exitSelectMode() : setSelectMode(true))}>
                        {selectMode ? 'Cancel' : 'Select'}
                    </button>
                )}
            </div>
            {albums.map((a) => {
                const checked = selectedAlbumIds.includes(a.id);
                return (
                    <button
                        key={a.id}
                        type="button"
                        className={`pt-album-row${a.id === album.id && !selectMode ? ' active' : ''}`}
                        onClick={() => {
                            if (selectMode) {
                                toggleAlbumSelect(a.id);
                                return;
                            }
                            navigate('albums', { albumId: a.id });
                            setShowMobileDetail(true);
                        }}
                    >
                        {selectMode && (
                            <span className={`pt-album-row-check${checked ? ' on' : ''}`} aria-hidden="true">
                                <CheckIcon />
                            </span>
                        )}
                        <span className="pt-album-row-cover empty" />
                        <span className="pt-album-row-meta">
                            <b>{a.name}</b>
                            <span>{a.photoCount} photos</span>
                        </span>
                    </button>
                );
            })}
            {selectMode ? (
                selectedAlbumIds.length > 0 && (
                    <div className="pt-album-select-bar">
                        <span>{selectedAlbumIds.length} selected</span>
                        <button type="button" className="btn btn-danger" onClick={() => void bulkDelete()}>
                            <TrashIcon className="toolbar-icon" /> Delete
                        </button>
                    </div>
                )
            ) : (
                <button type="button" className="albm-newbtn" onClick={() => void handleCreate(true)}>
                    <PlusIcon /> New album
                </button>
            )}
        </>
    );

    return (
        <div className="pt-albums-wrapper">
            {/* Mobile list view */}
            <div className={`pt-albums-mobile-list${showMobileDetail ? ' hidden' : ''}`}>
                {albumList}
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
                        {albumList}
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
                                    {album.photoCount} photos ·{' '}
                                    <button type="button" className="pt-linkish" onClick={() => { setDraft(album.name); setRenaming(true); }}>Rename</button>
                                    {' · '}
                                    <button type="button" className="pt-linkish" onClick={() => { deleteAlbum(album.id); navigate('albums', {}); }}>Delete</button>
                                </p>
                            </div>
                            <button type="button" className="btn mock-cta" onClick={togglePublic}>
                                <ShareIcon className="toolbar-icon" /> <span>{album.isPublic ? 'Sharing on' : 'Share'}</span>
                            </button>
                        </div>

                        <div className="pt-share-section">
                            <div className="pt-menu-label">Sharing</div>
                            <div className="share-sheet">
                                <div className="share-row">
                                    <span className="lbl"><b>Public link</b><span>Anyone with the link can view</span></span>
                                    <button
                                        type="button"
                                        role="switch"
                                        aria-checked={Boolean(album.isPublic)}
                                        aria-label="Public link"
                                        className="mock-switch"
                                        onClick={togglePublic}
                                    />
                                </div>
                                {album.isPublic && (
                                    <>
                                        <div className="share-row">
                                            <span className="lbl"><b>Link expires</b>{album.publicExpiresAt && <span>{new Date(album.publicExpiresAt).toLocaleDateString()}</span>}</span>
                                            <select className="field field-select" value={expiry} onChange={(e) => changeExpiry(e.target.value)}>
                                                {EXPIRY_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                                            </select>
                                        </div>
                                        {url && (
                                            <div className="share-row">
                                                <span className="share-url">{url}</span>
                                                <button type="button" className="btn" onClick={copy}><ClipboardDocumentIcon className="toolbar-icon" />{copied ? 'Copied' : 'Copy'}</button>
                                            </div>
                                        )}
                                        <div className="share-row">
                                            <span className="lbl">
                                                <b>Access code</b>
                                                {codes[album.id]
                                                    ? <span>Share this code with viewers: <code className="pt-access-code">{codes[album.id]}</code></span>
                                                    : <span>{album.hasAccessCode ? 'Protected — generate a new code to reveal one' : 'Add a code to protect the link'}</span>}
                                            </span>
                                            <button type="button" className="btn" onClick={() => {
                                                const code = randomCode();
                                                const days = EXPIRY_OPTIONS.find((o) => o.value === expiry)?.days ?? 7;
                                                void shareAlbum(album.id, { expiresInDays: days, accessCode: code });
                                                setCodes((prev) => ({ ...prev, [album.id]: code }));
                                                toast(`New access code: ${code}`);
                                            }}>
                                                <ArrowPathIcon className="toolbar-icon" /> New code
                                            </button>
                                        </div>
                                    </>
                                )}
                            </div>
                        </div>

                        <div className="pt-album-photos-head">
                            <div className="pt-menu-label" style={{ margin: 0 }}>Photos</div>
                            <button type="button" className="pt-linkish" onClick={() => { navigate('gallery'); toast('Select photos, then use “Add to album”'); }}><PlusIcon className="toolbar-icon" /> Add photos</button>
                        </div>
                        {activePhotos === undefined && albumPhotosLoading ? (
                            <p className="pt-grid-empty">Loading photos…</p>
                        ) : (
                            <PhotoGrid photos={activePhotos ?? []} emptyHint="No photos yet — add some from the gallery." />
                        )}
                    </section>
                </div>
            </div>
        </div>
    );
};

export default AlbumsPage;
