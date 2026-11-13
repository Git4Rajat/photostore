import React, { useState } from 'react';
import {
    MoreVertical as EllipsisVerticalIcon,
    MinusCircle as MinusCircleIcon,
    Plus as PlusIcon,
    Star as StarIcon,
    Trash2 as TrashIcon,
    Wrench as WrenchScrewdriverIcon,
} from 'lucide-react';
import { Menu, Stars, SelectionBar } from './bits';
import { AddToAlbumSheet } from './AddToAlbumMenu';
import { confirmDeleteItems } from './confirmDelete';
import { shownAlbum, useStore } from '../store';

/**
 * Compact floating menu for multi-selection. Delete button always visible,
 * other actions (rate/album/workbench) in the options menu. Sharing is
 * intentionally album-only, so there's no per-photo/selection share here.
 * Also carries its own "clear selection" close button -- the toolbar's Clear
 * button scrolls out of view with the page, so this is the only always-reachable
 * way to cancel a selection once the user has scrolled away from the top.
 */
const CommandBar: React.FC = () => {
    const { selection, ratePhotos, deletePhotos, removePhotosFromAlbum, route, albums, navigate, toast, clearSelection } = useStore();
    // The album on screen, not just ?albumId: the bare Albums tab shows the
    // first album without putting its id in the URL.
    const openAlbumId = shownAlbum(route, albums)?.id;
    const [albumOpen, setAlbumOpen] = useState(false);
    if (!selection.length) return null;

    const confirmDelete = async () => {
        const ids = selection;
        if (await confirmDeleteItems(ids.length)) deletePhotos(ids);
    };

    return (
        <SelectionBar count={selection.length} onClear={clearSelection} label="Selection actions">
            <button
                type="button"
                className="pt-fm-delete"
                onClick={() => void confirmDelete()}
                aria-label="Delete from library"
                title="Delete from library"
            >
                <TrashIcon />
            </button>

            <Menu
                align="right"
                renderTrigger={(toggle) => (
                    <button type="button" className="pt-fm-more" onClick={toggle} aria-label="More options">
                        <EllipsisVerticalIcon />
                    </button>
                )}
            >
                {(close) => (
                    <div className="pt-fm-menu">
                        {openAlbumId && (
                            <button type="button" className="pt-fm-item" onClick={() => { removePhotosFromAlbum(openAlbumId, selection); close(); }}>
                                <MinusCircleIcon /> Remove from album
                            </button>
                        )}

                        <Menu
                            align="left"
                            renderTrigger={(toggle) => (
                                <button type="button" className="pt-fm-item" onClick={toggle}>
                                    <StarIcon /> Rate
                                </button>
                            )}
                        >
                            {(rateClose) => (
                                <div className="pt-rate-pop">
                                    <Stars value={0} onRate={(n) => { ratePhotos(selection, n); toast(`Rated ${selection.length} · ${n}★`); rateClose(); close(); clearSelection(); }} size={26} />
                                </div>
                            )}
                        </Menu>

                        <button type="button" className="pt-fm-item" onClick={() => { setAlbumOpen(true); close(); }}>
                            <PlusIcon /> Album
                        </button>

                        <button type="button" className="pt-fm-item" onClick={() => { navigate('tools', { filenames: selection.slice(0, 50).join(',') }); close(); }}>
                            <WrenchScrewdriverIcon /> Workbench
                        </button>
                    </div>
                )}
            </Menu>

            {/* Rendered outside the More menu so the sheet survives that menu
                closing (it's portalled to <body>; a click inside it reads as an
                outside-click to the popover). */}
            <AddToAlbumSheet open={albumOpen} onClose={() => setAlbumOpen(false)} photoIds={selection} onAdded={clearSelection} />
        </SelectionBar>
    );
};

export default CommandBar;
