import React, { useEffect, useState } from 'react';
import { createPortal } from 'react-dom';
import { useNavigate } from 'react-router-dom';
import {
    ArrowDownTrayIcon,
    ArrowLeftIcon,
    PhotoIcon,
    PlusIcon,
    RectangleStackIcon,
    TrashIcon,
    UsersIcon,
    WrenchScrewdriverIcon,
} from '@heroicons/react/24/outline';
import { get, post } from '../../services/apiClient';
import { showToast } from '../../services/toast';
import { promptDialog } from './dialogs';
import { plural } from '../../utils/format';
import { libraryFocusHref, workbenchFilenameHref } from './PhotoQuickActions';
import type { PhotoPersonLink } from '../../types/uiTypes';

interface AlbumSummary {
    id: string;
    name: string;
    photoCount: number;
}

interface PhotoActionSheetProps {
    open: boolean;
    onClose: () => void;
    // One filename = single-photo sheet; more than one = bulk sheet.
    filenames: string[];
    // Single-photo only -- ignored in bulk mode.
    people?: PhotoPersonLink[];
    showLibraryLink?: boolean;
    showWorkbenchLink?: boolean;
    // Bulk-only: host provides these since they already carry page-specific
    // optimistic-UI/state handling (e.g. removing deleted tiles).
    onDownload?: () => void;
    onDelete?: () => void;
    // Fires after a successful create/add-to-album, so a host with its own
    // album list (e.g. AlbumsPage's sidebar) can refresh it.
    onAlbumsChanged?: () => void;
}

// Mobile-native replacement for the desktop hover corner icons (see
// PhotoQuickActions) -- triggered by long-press (useLongPress) instead of
// hover, since touch has no hover state. Self-contained for the album
// actions (fetch/create/add all live here) since those are identical
// regardless of which page triggered the sheet; navigation and the two
// bulk actions are the only page-specific pieces.
const PhotoActionSheet: React.FC<PhotoActionSheetProps> = ({
    open,
    onClose,
    filenames,
    people = [],
    showLibraryLink = true,
    showWorkbenchLink = true,
    onDownload,
    onDelete,
    onAlbumsChanged,
}) => {
    const navigate = useNavigate();
    const [screen, setScreen] = useState<'menu' | 'chooseAlbum'>('menu');
    const [albums, setAlbums] = useState<AlbumSummary[] | null>(null);
    const [albumsLoading, setAlbumsLoading] = useState(false);
    const [busy, setBusy] = useState(false);

    useEffect(() => {
        if (open) {
            setScreen('menu');
        }
    }, [open]);

    if (!open || filenames.length === 0) {
        return null;
    }

    const isBulk = filenames.length > 1;
    const countLabel = plural(filenames.length, 'photo');

    const goToChooseAlbum = async () => {
        setScreen('chooseAlbum');
        if (albums !== null) {
            return;
        }
        setAlbumsLoading(true);
        try {
            const response = await get('/albums');
            setAlbums(Array.isArray(response?.albums) ? response.albums : []);
        } catch {
            setAlbums([]);
            showToast("Couldn't load albums.", { variant: 'error' });
        } finally {
            setAlbumsLoading(false);
        }
    };

    const addToAlbum = async (album: AlbumSummary) => {
        setBusy(true);
        try {
            await post(`/albums/${album.id}/photos/add`, { filenames });
            showToast(`Added ${countLabel} to "${album.name}".`);
            onAlbumsChanged?.();
            onClose();
        } catch {
            showToast("Couldn't add to album.", { variant: 'error' });
        } finally {
            setBusy(false);
        }
    };

    const createAlbum = async () => {
        onClose();
        const suggestedName = `Album ${new Date().toLocaleDateString()}`;
        const input = await promptDialog({
            title: 'Create album',
            label: 'Album name',
            defaultValue: suggestedName,
            confirmLabel: 'Create',
        });
        if (input === null) {
            return;
        }
        const albumName = input.trim();
        if (!albumName) {
            return;
        }
        try {
            const createResponse = await post('/albums', { name: albumName });
            const newAlbumId = String(createResponse?.album?.id || '');
            if (!newAlbumId) {
                throw new Error('Album was created but no album id was returned.');
            }
            await post(`/albums/${newAlbumId}/photos/add`, { filenames });
            showToast(`Created "${albumName}" with ${countLabel}.`);
            onAlbumsChanged?.();
        } catch {
            showToast('Failed to create album from selection.', { variant: 'error' });
        }
    };

    const goTo = (href: string) => {
        onClose();
        navigate(href);
    };

    return createPortal(
        <div className="action-sheet-backdrop" onClick={onClose}>
            <div
                className="action-sheet"
                role="dialog"
                aria-modal="true"
                aria-label={isBulk ? `Actions for ${countLabel}` : `Actions for ${filenames[0]}`}
                onClick={(event) => event.stopPropagation()}
            >
                <span className="action-sheet-grabber" aria-hidden="true" />

                {screen === 'menu' ? (
                    <>
                        <p className="action-sheet-title">
                            {isBulk ? `${countLabel} selected` : filenames[0]}
                        </p>
                        <div className="action-sheet-list">
                            <button type="button" className="action-sheet-row" onClick={() => void goToChooseAlbum()}>
                                <RectangleStackIcon className="action-sheet-row-icon" aria-hidden="true" />
                                <span>Add to existing album</span>
                            </button>
                            <button type="button" className="action-sheet-row" onClick={() => void createAlbum()}>
                                <PlusIcon className="action-sheet-row-icon" aria-hidden="true" />
                                <span>Create new album</span>
                            </button>

                            {!isBulk && showWorkbenchLink && (
                                <button type="button" className="action-sheet-row" onClick={() => goTo(workbenchFilenameHref(filenames[0]))}>
                                    <WrenchScrewdriverIcon className="action-sheet-row-icon" aria-hidden="true" />
                                    <span>Open in Workbench</span>
                                </button>
                            )}
                            {!isBulk && people.map((person) => (
                                <button
                                    key={person.personId}
                                    type="button"
                                    className="action-sheet-row"
                                    onClick={() => goTo(`/people/${encodeURIComponent(person.personId)}`)}
                                >
                                    <UsersIcon className="action-sheet-row-icon" aria-hidden="true" />
                                    <span>Show in cluster: {person.name || 'Unnamed person'}</span>
                                </button>
                            ))}
                            {!isBulk && showLibraryLink && (
                                <button type="button" className="action-sheet-row" onClick={() => goTo(libraryFocusHref(filenames[0]))}>
                                    <PhotoIcon className="action-sheet-row-icon" aria-hidden="true" />
                                    <span>View in Library</span>
                                </button>
                            )}

                            {isBulk && onDownload && (
                                <button type="button" className="action-sheet-row" onClick={() => { onClose(); onDownload(); }}>
                                    <ArrowDownTrayIcon className="action-sheet-row-icon" aria-hidden="true" />
                                    <span>Download selected</span>
                                </button>
                            )}
                            {isBulk && onDelete && (
                                <button type="button" className="action-sheet-row action-sheet-row-danger" onClick={() => { onClose(); onDelete(); }}>
                                    <TrashIcon className="action-sheet-row-icon" aria-hidden="true" />
                                    <span>Delete selected</span>
                                </button>
                            )}
                        </div>
                    </>
                ) : (
                    <>
                        <div className="action-sheet-subheader">
                            <button type="button" className="action-sheet-back" onClick={() => setScreen('menu')} aria-label="Back">
                                <ArrowLeftIcon className="toolbar-icon" aria-hidden="true" />
                            </button>
                            <p className="action-sheet-title">Add to album</p>
                        </div>
                        <div className="action-sheet-list action-sheet-list-scroll">
                            {albumsLoading && <p className="action-sheet-empty">Loading…</p>}
                            {!albumsLoading && albums && albums.length === 0 && (
                                <p className="action-sheet-empty">No albums yet.</p>
                            )}
                            {!albumsLoading && albums && albums.map((album) => (
                                <button
                                    key={album.id}
                                    type="button"
                                    className="action-sheet-row"
                                    disabled={busy}
                                    onClick={() => void addToAlbum(album)}
                                >
                                    <RectangleStackIcon className="action-sheet-row-icon" aria-hidden="true" />
                                    <span>{album.name}</span>
                                    <span className="action-sheet-row-meta">{album.photoCount}</span>
                                </button>
                            ))}
                        </div>
                    </>
                )}

                <button type="button" className="action-sheet-cancel" onClick={onClose}>
                    Cancel
                </button>
            </div>
        </div>,
        document.body,
    );
};

export default PhotoActionSheet;
