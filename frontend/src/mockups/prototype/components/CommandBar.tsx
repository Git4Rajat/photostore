import React from 'react';
import {
    ArrowUpTrayIcon,
    EllipsisVerticalIcon,
    PlusIcon,
    StarIcon,
    TrashIcon,
    WrenchScrewdriverIcon,
} from '@heroicons/react/24/outline';
import { Menu, Stars } from './bits';
import { AddToAlbumMenu } from './AddToAlbumMenu';
import { useStore } from '../store';
import { sharePhotos } from '../media';

/**
 * Compact floating menu for multi-selection. Delete button always visible,
 * other actions (rate/album/share) in options menu.
 */
export const CommandBar: React.FC = () => {
    const { selection, ratePhotos, deletePhotos, photosByIds, navigate, toast } = useStore();
    if (!selection.length) return null;

    const shareSelection = () => {
        void (async () => {
            const outcome = await sharePhotos(photosByIds(selection));
            if (outcome === 'downloaded') toast('Sharing isn’t supported here — downloaded instead');
            else if (outcome === 'unsupported') toast(selection.length > 1 ? 'Sharing multiple photos isn’t supported here' : 'Couldn’t share');
        })();
    };

    return (
        <div className="pt-floating-menu" role="toolbar" aria-label="Selection actions">
            <div className="pt-fm-badge">{selection.length}</div>

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

                        <button type="button" className="pt-fm-item" onClick={() => { shareSelection(); close(); }}>
                            <ArrowUpTrayIcon /> Share
                        </button>

                        <button type="button" className="pt-fm-item" onClick={() => { navigate('tools', { filenames: selection.slice(0, 50).join(',') }); close(); }}>
                            <WrenchScrewdriverIcon /> Workbench
                        </button>
                    </div>
                )}
            </Menu>
        </div>
    );
};

export default CommandBar;
