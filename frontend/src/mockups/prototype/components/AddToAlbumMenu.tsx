import React, { useMemo, useState } from 'react';
import { Plus as PlusIcon, Search as SearchIcon } from 'lucide-react';
import { BottomSheet } from './BottomSheet';
import { useStore } from '../store';
import { useProtectedBlobUrls } from '../../../services/imageClient';
import { promptDialog } from '../../../components/shared/dialogs';

/**
 * Controlled "Add to album" bottom sheet: lists every album (with a search
 * field for large libraries) plus a "New album" action. It portals to <body>,
 * so it's rendered on its own (not inside a popover menu whose outside-click
 * handler would tear it down while the user is interacting with the sheet).
 */
export const AddToAlbumSheet: React.FC<{
    open: boolean;
    onClose: () => void;
    photoIds: string[];
}> = ({ open, onClose, photoIds }) => {
    const { albums, addPhotosToAlbum, createAlbum } = useStore();
    const [query, setQuery] = useState('');
    const covers = useProtectedBlobUrls(
        albums.map((a) => a.coverThumbnailUrl).filter((u): u is string => Boolean(u)),
    );

    const close = () => { setQuery(''); onClose(); };

    const filtered = useMemo(() => {
        const q = query.trim().toLowerCase();
        return q ? albums.filter((a) => a.name.toLowerCase().includes(q)) : albums;
    }, [albums, query]);

    const createNew = () => {
        void (async () => {
            const name = await promptDialog({
                title: 'New album',
                label: 'Album name',
                defaultValue: query.trim() || 'New album',
                placeholder: 'e.g. Summer trip',
                confirmLabel: 'Create',
            });
            if (name === null) return; // cancelled
            const id = await createAlbum(name.trim() || 'New album');
            if (id) addPhotosToAlbum(id, photoIds);
            close();
        })();
    };

    return (
        <BottomSheet open={open} onClose={close} title={`Add ${photoIds.length} to album`}
            header={(
                <div className="pt-sheet-search">
                    <SearchIcon />
                    <input
                        type="text"
                        placeholder="Search albums…"
                        value={query}
                        onChange={(e) => setQuery(e.target.value)}
                        aria-label="Search albums"
                    />
                </div>
            )}
        >
            <button type="button" className="pt-sheet-new" onClick={createNew}>
                <span className="pt-sheet-new-icon"><PlusIcon /></span>
                <span className="pt-sheet-new-label">New Album{query.trim() ? ` “${query.trim()}”` : ''}</span>
            </button>
            <div className="pt-sheet-albums">
                {filtered.map((a) => (
                    <button
                        key={a.id}
                        type="button"
                        className="pt-sheet-album"
                        onClick={() => { addPhotosToAlbum(a.id, photoIds); close(); }}
                    >
                        {a.coverThumbnailUrl && covers[a.coverThumbnailUrl] ? (
                            <img className="pt-sheet-album-cover" src={covers[a.coverThumbnailUrl]} alt="" />
                        ) : (
                            <span className="pt-sheet-album-cover empty" />
                        )}
                        <span className="pt-sheet-album-meta">
                            <b>{a.name}</b>
                            <span>{a.photoCount} photo{a.photoCount === 1 ? '' : 's'}</span>
                        </span>
                    </button>
                ))}
                {filtered.length === 0 && (
                    <p className="pt-sheet-empty">
                        {albums.length === 0 ? 'No albums yet — create your first one above.' : `No albums match “${query.trim()}”.`}
                    </p>
                )}
            </div>
        </BottomSheet>
    );
};

/**
 * Trigger-driven wrapper around AddToAlbumSheet for call sites that render their
 * own button (the photo viewer). The selection command bar uses AddToAlbumSheet
 * directly instead, since its trigger lives inside a popover menu.
 */
export const AddToAlbumMenu: React.FC<{
    photoIds: string[];
    renderTrigger: (toggle: () => void, open: boolean) => React.ReactNode;
    // Accepted for call-site compatibility with the old popover API; the sheet
    // is always centred/bottom-anchored, so it's otherwise unused.
    align?: 'left' | 'right';
}> = ({ photoIds, renderTrigger }) => {
    const [open, setOpen] = useState(false);
    return (
        <>
            {renderTrigger(() => setOpen((v) => !v), open)}
            <AddToAlbumSheet open={open} onClose={() => setOpen(false)} photoIds={photoIds} />
        </>
    );
};

export default AddToAlbumMenu;
