import React, { useState } from 'react';
import {
    Heart as HeartIcon,
    ImagePlus as AddToAlbumIcon,
    MinusCircle as MinusCircleIcon,
    MoreHorizontal as MoreIcon,
    Star as StarIcon,
    Trash2 as TrashIcon,
    Wrench as WrenchScrewdriverIcon,
    X as XMarkIcon,
} from 'lucide-react';
import { Menu, Stars } from './bits';
import { AddToAlbumSheet } from './AddToAlbumMenu';
import { confirmDeleteItems } from './confirmDelete';
import { shownAlbum, useStore } from '../store';

/**
 * The selection's action bar. On a phone it takes the tab bar's place (the tab
 * bar hides while selecting) instead of floating over it; on a desktop it sits
 * at the bottom of the window. Labelled actions, Delete last and in red.
 * Sharing is album-only, so there is no per-photo Share here.
 * The Gallery shows Cancel and the count in its own header; every other page
 * gets them here, the one always-reachable way out once scrolled away.
 */
const CommandBar: React.FC = () => {
    const { selection, selectMode, ratePhotos, toggleLike, photoById, deletePhotos, removePhotosFromAlbum, route, albums, navigate, toast, clearSelection, setSelectMode } = useStore();
    // The album on screen, not just ?albumId: the bare Albums tab shows the
    // first album without putting its id in the URL.
    const openAlbumId = shownAlbum(route, albums)?.id;
    const [albumOpen, setAlbumOpen] = useState(false);
    const inGallerySelect = route.page === 'gallery' && selectMode;
    if (!selection.length && !inGallerySelect) return null;
    const none = selection.length === 0;

    const confirmDelete = async () => {
        const ids = selection;
        if (await confirmDeleteItems(ids.length)) deletePhotos(ids);
    };
    // Like everything selected; if it is all liked already, unlike it all.
    const allLiked = !none && selection.every((id) => photoById(id)?.liked);
    const likeSelection = () => {
        selection.filter((id) => Boolean(photoById(id)?.liked) === allLiked).forEach((id) => toggleLike(id));
        toast(allLiked ? `Removed ${selection.length} from Likes` : `Liked ${selection.length}`);
    };

    return (
        <div className="pt-action-bar" role="toolbar" aria-label="Selection actions">
            {!inGallerySelect && (
                <button type="button" className="pt-ab-btn pt-ab-count" onClick={() => { clearSelection(); setSelectMode(false); }} aria-label="Clear selection">
                    <XMarkIcon /> <span>{selection.length} selected</span>
                </button>
            )}
            <button type="button" className="pt-ab-btn" disabled={none} onClick={() => setAlbumOpen(true)}>
                <AddToAlbumIcon /> <span>Add to album</span>
            </button>
            <button type="button" className={`pt-ab-btn${allLiked ? ' on' : ''}`} disabled={none} aria-pressed={allLiked} onClick={likeSelection}>
                <HeartIcon fill={allLiked ? 'currentColor' : 'none'} /> <span>Like</span>
            </button>
            <Menu
                align="right"
                renderTrigger={(toggle) => (
                    <button type="button" className="pt-ab-btn" disabled={none} onClick={toggle}>
                        <MoreIcon /> <span>More</span>
                    </button>
                )}
            >
                {(close) => (
                    <div className="pt-fm-menu">
                        <div className="pt-fm-rate">
                            <span><StarIcon /> Rate</span>
                            <Stars value={0} onRate={(n) => { ratePhotos(selection, n); toast(`Rated ${selection.length} · ${n}★`); close(); clearSelection(); }} size={24} />
                        </div>
                        {openAlbumId && (
                            <button type="button" className="pt-fm-item" onClick={() => { removePhotosFromAlbum(openAlbumId, selection); close(); }}>
                                <MinusCircleIcon /> Remove from album
                            </button>
                        )}
                        <button type="button" className="pt-fm-item" onClick={() => { navigate('tools', { filenames: selection.slice(0, 50).join(',') }); close(); }}>
                            <WrenchScrewdriverIcon /> Open in Workbench
                        </button>
                    </div>
                )}
            </Menu>
            <button type="button" className="pt-ab-btn danger" disabled={none} onClick={() => void confirmDelete()}>
                <TrashIcon /> <span>Delete</span>
            </button>

            {/* Rendered outside the More menu so the sheet survives that menu
                closing (it's portalled to <body>; a click inside it reads as an
                outside-click to the popover). */}
            <AddToAlbumSheet open={albumOpen} onClose={() => setAlbumOpen(false)} photoIds={selection} onAdded={clearSelection} />
        </div>
    );
};

export default CommandBar;
