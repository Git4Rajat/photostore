import React from 'react';
import {
    MoreVertical as EllipsisVerticalIcon,
    Plus as PlusIcon,
    Star as StarIcon,
    Trash2 as TrashIcon,
    Wrench as WrenchScrewdriverIcon,
} from 'lucide-react';
import { Menu, Stars, SelectionBar } from './bits';
import { AddToAlbumMenu } from './AddToAlbumMenu';
import { useStore } from '../store';

/**
 * Compact floating menu for multi-selection. Delete button always visible,
 * other actions (rate/album/workbench) in the options menu. Sharing is
 * intentionally album-only, so there's no per-photo/selection share here.
 * Also carries its own "clear selection" close button -- the toolbar's Clear
 * button scrolls out of view with the page, so this is the only always-reachable
 * way to cancel a selection once the user has scrolled away from the top.
 */
export const CommandBar: React.FC = () => {
    const { selection, ratePhotos, deletePhotos, navigate, toast, clearSelection } = useStore();
    if (!selection.length) return null;

    return (
        <SelectionBar count={selection.length} onClear={clearSelection} label="Selection actions">
            <button
                type="button"
                className="pt-fm-delete"
                onClick={() => deletePhotos(selection)}
                aria-label={`Delete ${selection.length} photo${selection.length > 1 ? 's' : ''}`}
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
                                    <Stars value={0} onRate={(n) => { ratePhotos(selection, n); toast(`Rated ${selection.length} · ${n}★`); rateClose(); close(); }} size={26} />
                                </div>
                            )}
                        </Menu>

                        <AddToAlbumMenu
                            photoIds={selection}
                            renderTrigger={(toggle) => (
                                <button type="button" className="pt-fm-item" onClick={toggle}>
                                    <PlusIcon /> Album
                                </button>
                            )}
                        />

                        <button type="button" className="pt-fm-item" onClick={() => { navigate('tools', { filenames: selection.slice(0, 50).join(',') }); close(); }}>
                            <WrenchScrewdriverIcon /> Workbench
                        </button>
                    </div>
                )}
            </Menu>
        </SelectionBar>
    );
};

export default CommandBar;
