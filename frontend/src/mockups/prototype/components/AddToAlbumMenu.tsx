import React from 'react';
import { PlusIcon } from '@heroicons/react/24/outline';
import { Menu, Swatch } from './bits';
import { useStore } from '../store';

/**
 * "Add to album" popover — lists existing albums (with covers) and a create
 * option. Reused by the selection command bar and the photo viewer.
 */
export const AddToAlbumMenu: React.FC<{
    photoIds: string[];
    renderTrigger: (toggle: () => void, open: boolean) => React.ReactNode;
    align?: 'left' | 'right';
}> = ({ photoIds, renderTrigger, align = 'left' }) => {
    const { albums, addPhotosToAlbum, createAlbum, photoById } = useStore();

    return (
        <Menu renderTrigger={renderTrigger} align={align}>
            {(close) => (
                <div className="pt-album-menu">
                    <div className="pt-menu-label">Add to album</div>
                    {albums.map((a) => {
                        const cover = a.coverPhotoId ? photoById(a.coverPhotoId) : undefined;
                        return (
                            <button
                                key={a.id}
                                type="button"
                                className="pt-album-menu-row"
                                onClick={() => {
                                    addPhotosToAlbum(a.id, photoIds);
                                    close();
                                }}
                            >
                                {cover ? <Swatch swatch={cover.swatch} className="pt-album-menu-cover" /> : <span className="pt-album-menu-cover empty" />}
                                <span className="pt-album-menu-name">{a.name}</span>
                                <span className="pt-album-menu-count">{a.photoIds.length}</span>
                            </button>
                        );
                    })}
                    <button
                        type="button"
                        className="pt-album-menu-new"
                        onClick={() => {
                            const id = createAlbum('New album');
                            addPhotosToAlbum(id, photoIds);
                            close();
                        }}
                    >
                        <PlusIcon /> New album from selection
                    </button>
                </div>
            )}
        </Menu>
    );
};

export default AddToAlbumMenu;
