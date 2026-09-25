import React from 'react';
import { PlusIcon } from '@heroicons/react/24/outline';
import { Menu } from './bits';
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
    const { albums, addPhotosToAlbum, createAlbum } = useStore();

    return (
        <Menu renderTrigger={renderTrigger} align={align}>
            {(close) => (
                <div className="pt-album-menu">
                    <div className="pt-menu-label">Add to album</div>
                    {albums.map((a) => (
                        <button
                            key={a.id}
                            type="button"
                            className="pt-album-menu-row"
                            onClick={() => {
                                addPhotosToAlbum(a.id, photoIds);
                                close();
                            }}
                        >
                            <span className="pt-album-menu-cover empty" />
                            <span className="pt-album-menu-name">{a.name}</span>
                            <span className="pt-album-menu-count">{a.photoCount}</span>
                        </button>
                    ))}
                    <button
                        type="button"
                        className="pt-album-menu-new"
                        onClick={() => {
                            void (async () => {
                                const id = await createAlbum('New album');
                                if (id) addPhotosToAlbum(id, photoIds);
                                close();
                            })();
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
