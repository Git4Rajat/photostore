import React, { useEffect, useState } from 'react';
import {
    ArrowLeft as ArrowLeftIcon,
    CalendarDays as CalendarDaysIcon,
    Check as CheckIcon,
    Clock as ClockIcon,
    ImagePlus as AddPhotosIcon,
    Link as LinkIcon,
    MapPin as MapPinIcon,
    MoreHorizontal as MoreIcon,
    Plus as PlusIcon,
    Share2 as ShareIcon,
    Sparkles as SparklesIcon,
    Tag as TagIcon,
    Trash2 as TrashIcon,
    Users as UserGroupIcon,
} from 'lucide-react';
import { shownAlbum, useStore } from '../store';
import PhotoGrid from '../components/PhotoGrid';
import ScrollSentinel from '../components/ScrollSentinel';
import { Menu, Spinner, SelectionBar } from '../components/bits';
import { BrowseLinks } from '../components/BrowseLinks';
import { ShareAlbumSheet, sharingStatus } from '../components/ShareAlbumSheet';
import { AddPhotosPicker } from '../components/AddPhotosPicker';
import { dayHeading } from '../components/PhotoGrid';
import { plural } from '../../../utils/format';
import { BottomSheet } from '../components/BottomSheet';
import { ThumbSizeControl, useTileSize } from '../components/controls';
import { confirmDialog, promptDialog } from '../../../components/shared/dialogs';
import { useProtectedBlobUrls } from '../../../services/imageClient';
import { enqueueBackgroundRequest } from '../../../services/backgroundRequestQueue';

const SMART_ALBUM_RULES: Array<{ id: string; label: string; description: string; Icon: React.ComponentType<React.SVGProps<SVGSVGElement>> }> = [
    { id: 'location', label: 'By location', description: 'Places across the library', Icon: MapPinIcon },
    { id: 'recent-upload', label: 'By recent upload', description: 'Latest upload window', Icon: ClockIcon },
    { id: 'person', label: 'By person', description: 'Matched people clusters', Icon: UserGroupIcon },
    { id: 'event-window', label: 'By event/time', description: 'Capture date window', Icon: CalendarDaysIcon },
    { id: 'tag-object', label: 'By tag/object', description: 'AI tags and detected objects', Icon: TagIcon },
];

/** Albums — list first on mobile, detail view on larger screens. */
export const AlbumsPage: React.FC = () => {
    const {
        albums, albumsLoading, route, navigate, openAlbum, isAlbumMissing, isAlbumOpenFailed, isAlbumOpenTerminal, albumPhotosById, isAlbumPhotosLoading, loadMoreAlbumPhotos, albumPhotosHasMore,
        createAlbum, autoCreateAlbum, renameAlbum, deleteAlbum, deleteAlbums, toast,
        selectMode: photoSelectMode, setSelectMode: setPhotoSelectMode, fetchAlbums,
    } = useStore();

    // Loads albums when this tab is actually visited, queued behind whatever
    // else is already in flight instead of racing it -- rather than
    // StoreProvider firing this unconditionally on every app mount regardless
    // of which tab is open. Aborted if the user navigates away before its
    // turn comes up. See the 2026-10-01 boot-request audit.
    useEffect(() => {
        const controller = new AbortController();
        void enqueueBackgroundRequest(() => fetchAlbums(), { signal: controller.signal }).catch(() => {});
        return () => controller.abort();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);
    // Detail vs. list is derived from the route (?albumId=…) rather than local
    // state, so tapping the Albums tab (which clears the param) always returns
    // to the album list on mobile instead of leaving you stuck inside an album.
    const showDetail = Boolean(route.params.albumId);
    const [selectMode, setSelectMode] = useState(false);
    const [selectedAlbumIds, setSelectedAlbumIds] = useState<string[]>([]);
    const [smartCreatingRule, setSmartCreatingRule] = useState<string | null>(null);
    const [smartSheetOpen, setSmartSheetOpen] = useState(false);
    const [albumTile, setAlbumTile] = useTileSize('photostore.albumTileSize');
    // An explicit albumId (deep link, Ask-page navigation, browser back/
    // forward) must resolve to that exact album or an explicit not-found/
    // loading state -- never silently substitute an unrelated album[0] just
    // because the requested id isn't loaded yet (see
    // docs/audits/red-dress-album-incident-2026-10-22.md). Only the
    // no-selection case (plain "Albums" tab) falls back to the first album
    // for the desktop preview pane.
    const explicitAlbumId = route.params.albumId;
    const album = shownAlbum(route, albums);
    const explicitAlbumMissing = explicitAlbumId ? isAlbumMissing(explicitAlbumId) : false;
    // A transient (network/5xx) failure is distinct from both "still
    // resolving" and a confirmed 404: it must present a retry action rather
    // than spin forever (see docs/audits/album-persistence-2026-10-22.md
    // finding #6) or get silently retried by the fetch effect below.
    const explicitAlbumFailed = explicitAlbumId ? isAlbumOpenFailed(explicitAlbumId) : false;
    const albumOpenTerminal = explicitAlbumId ? !album && isAlbumOpenTerminal(explicitAlbumId) : false;
    const albumResolving = Boolean(explicitAlbumId) && !album && !explicitAlbumMissing && !explicitAlbumFailed && !albumOpenTerminal;
    const albumMissing = Boolean(explicitAlbumId) && !album && explicitAlbumMissing;
    const albumOpenFailed = Boolean(explicitAlbumId) && !album && explicitAlbumFailed;
    const [shareOpen, setShareOpen] = useState(false);
    const [pickerOpen, setPickerOpen] = useState(false);
    const covers = useProtectedBlobUrls(albums.map((a) => a.coverThumbnailUrl).filter((u): u is string => Boolean(u)));
    // The backend never returns an album's access code back (it's a stored
    // secret), so remember codes we generate this session to show the owner.
    const [codes, setCodes] = useState<Record<string, string>>({});

    // An explicit albumId not present in the currently loaded `albums` list
    // (fresh deep link, or an index that hasn't caught up with a just-created
    // album yet) needs its own direct-by-id resolution -- the effect below
    // only fires once `album` already exists, so without this a stale list
    // would otherwise leave the id unresolved forever instead of fetching it.
    useEffect(() => {
        if (!albumResolving || !explicitAlbumId) return;
        const controller = new AbortController();
        void enqueueBackgroundRequest(async () => { await openAlbum(explicitAlbumId); }, { signal: controller.signal }).catch(() => {});
        return () => controller.abort();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [albumResolving, explicitAlbumId]);

    // Fetch the active album's photos whenever the selection changes, unless
    // this album's first page is already cached from a prior visit -- and
    // queue it behind fetchAlbums rather than firing in parallel with it, so
    // landing on the tab doesn't pay for two uncoordinated requests every time.
    useEffect(() => {
        if (!album?.id || albumPhotosById(album.id)) return;
        const controller = new AbortController();
        void enqueueBackgroundRequest(async () => { await openAlbum(album.id); }, { signal: controller.signal }).catch(() => {});
        return () => controller.abort();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [album?.id]);

    const toggleAlbumSelect = (id: string) => {
        setSelectedAlbumIds((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));
    };

    const exitSelectMode = () => {
        setSelectMode(false);
        setSelectedAlbumIds([]);
    };

    const confirmDeleteAlbum = async () => {
        if (!album) return;
        const confirmed = await confirmDialog({
            title: `Delete “${album.name}”?`,
            message: 'The photos inside stay in your library — only the album is removed.',
            confirmLabel: 'Delete',
            danger: true,
        });
        if (!confirmed) return;
        deleteAlbum(album.id);
        navigate('albums', {});
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

    // One rename pattern app-wide: a prompt dialog (matches the library rename on
    // the Sharing page), instead of the old double-click-title / inline-input.
    const renameAlbumPrompt = async (targetId: string = album?.id ?? '', currentName: string = album?.name ?? '') => {
        if (!targetId) return;
        const next = await promptDialog({
            title: 'Rename album',
            label: 'Album name',
            defaultValue: currentName,
            placeholder: 'e.g. Summer trip',
            confirmLabel: 'Rename',
        });
        if (next && next.trim()) renameAlbum(targetId, next.trim());
    };

    const handleCreate = async (thenRename = false) => {
        // Ask for the name first so cancelling doesn't leave a stray "New album".
        let name = 'New album';
        if (thenRename) {
            const entered = await promptDialog({
                title: 'New album',
                label: 'Album name',
                defaultValue: 'New album',
                placeholder: 'e.g. Summer trip',
                confirmLabel: 'Create',
            });
            if (entered === null) return;
            name = entered.trim() || 'New album';
        }
        const id = await createAlbum(name);
        if (!id) return;
        navigate('albums', { albumId: id });
    };

    const handleSmartCreate = async (rule: string) => {
        setSmartSheetOpen(false);
        setSmartCreatingRule(rule);
        try {
            const { albumId, count, message } = await autoCreateAlbum(rule);
            if (albumId) {
                navigate('albums', { albumId });
                toast(`Created smart album with ${count} photo${count === 1 ? '' : 's'}`);
            } else {
                toast(message || 'No matching photos found for that rule.');
            }
        } finally {
            setSmartCreatingRule(null);
        }
    };

    // Shared across the empty-list state and the normal detail view -- both
    // need a way to trigger a smart album, not just a plain "New album".
    const smartAlbumSheet = (
        <BottomSheet open={smartSheetOpen} onClose={() => setSmartSheetOpen(false)} title="New smart album">
            <p className="pt-sheet-intro">Keepsake builds these automatically from your library.</p>
            <div className="pt-sheet-rules">
                {SMART_ALBUM_RULES.map(({ id, label, description, Icon }) => (
                    <button key={id} type="button" className="pt-sheet-rule" onClick={() => void handleSmartCreate(id)} disabled={smartCreatingRule !== null}>
                        <span className="pt-sheet-rule-icon"><Icon /></span>
                        <span className="pt-sheet-rule-text"><b>{label}</b><small>{description}</small></span>
                    </button>
                ))}
            </div>
        </BottomSheet>
    );

    if (albumMissing) {
        return (
            <div className="pt-empty-page">
                <p>Album not found.</p>
                <p className="pt-empty-page-hint">It may have been deleted, or the link may be wrong.</p>
                <button type="button" className="btn" onClick={() => navigate('albums', {})}>Back to albums</button>
            </div>
        );
    }

    if (albumOpenTerminal) {
        return (
            <div className="pt-empty-page">
                <p>This album cannot be opened.</p>
                <p className="pt-empty-page-hint">Check the link and your access to this album.</p>
                <button type="button" className="btn" onClick={() => navigate('albums', {})}>Back to albums</button>
            </div>
        );
    }

    if (albumResolving || albumOpenFailed) {
        return (
            <div className="pt-empty-page">
                <Spinner label="Opening album…" />
                {albumOpenFailed && explicitAlbumId && (
                    <div className="pt-arrive-actions">
                        <p className="pt-empty-page-hint">Something went wrong loading this album.</p>
                        <button type="button" className="btn" onClick={() => void openAlbum(explicitAlbumId)}>Retry</button>
                        <button type="button" className="btn" onClick={() => navigate('albums', {})}>Back to albums</button>
                    </div>
                )}
            </div>
        );
    }

    if (!album) {
        return (
            <div className="pt-empty-page">
                {albumsLoading ? <Spinner label="Loading albums…" /> : <p>No albums yet.</p>}
                {!albumsLoading && (
                    <div className="pt-arrive-actions">
                        <button type="button" className="btn btn-primary" onClick={() => void handleCreate(true)}>
                            <PlusIcon className="toolbar-icon" /> New album
                        </button>
                        <button type="button" className="btn" onClick={() => setSmartSheetOpen(true)} disabled={smartCreatingRule !== null}>
                            <SparklesIcon className="toolbar-icon" /> {smartCreatingRule ? 'Creating…' : 'Smart album'}
                        </button>
                    </div>
                )}
                {smartAlbumSheet}
            </div>
        );
    }

    const activePhotos = albumPhotosById(album.id);
    const setCode = (albumId: string, code: string | null) => setCodes((prev) => {
        const next = { ...prev };
        if (code) next[albumId] = code; else delete next[albumId];
        return next;
    });
    const status = sharingStatus(album);
    // The span of days the album covers, once all of it is loaded (listing is newest first).
    const dated = (activePhotos ?? []).filter((p) => p.captureDate);
    const span = dated.length && !albumPhotosHasMore(album.id)
        ? [dayHeading(dated[dated.length - 1]), dayHeading(dated[0])].filter((d, i, all) => all.indexOf(d) === i).join(' – ')
        : '';
    const cover = album.coverThumbnailUrl ? covers[album.coverThumbnailUrl] : undefined;

    // Split into the scrollable row list and a footer that stays outside the
    // scrolling container -- the sidebar/mobile list wrappers clip overflow
    // (so a long album list scrolls independently of the page), which also
    // clipped the Smart Album popover whenever it rendered inside that same
    // scrolling box, making it appear to silently vanish/render behind the
    // list instead of over it.
    const albumListRows = (
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
                        }}
                    >
                        {selectMode && (
                            <span className={`pt-album-row-check${checked ? ' on' : ''}`} aria-hidden="true">
                                <CheckIcon />
                            </span>
                        )}
                        {a.coverThumbnailUrl && covers[a.coverThumbnailUrl] ? (
                            <img className="pt-album-row-cover" src={covers[a.coverThumbnailUrl]} alt="" />
                        ) : (
                            <span className="pt-album-row-cover empty" />
                        )}
                        <span className="pt-album-row-meta">
                            <b>{a.name}</b>
                            <span>{plural(a.photoCount, 'item')}{a.isPublic ? ' · Shared' : ''}</span>
                        </span>
                    </button>
                );
            })}
        </>
    );

    const albumListFooter = !selectMode && (
        <div className="pt-albums-new-row">
            <button type="button" className="albm-newbtn" onClick={() => void handleCreate(true)}>
                <PlusIcon /> New album
            </button>
            <button type="button" className="albm-newbtn" onClick={() => setSmartSheetOpen(true)} disabled={smartCreatingRule !== null}>
                <SparklesIcon /> {smartCreatingRule ? 'Creating…' : 'Smart album'}
            </button>
        </div>
    );

    return (
        <div className="pt-albums-wrapper">
            {/* Mobile list view: your albums as cover cards, then the ways in to
                People and Places, then the less common ways to make an album. */}
            <div className={`pt-albums-mobile-list${showDetail ? ' hidden' : ''}`}>
                <div className="pt-toolbar pt-albums-head">
                    <h1 className="pt-page-title">Albums</h1>
                    <div className="pt-toolbar-actions">
                        {albums.length > 0 && (
                            <button type="button" className="pt-linkish" onClick={() => (selectMode ? exitSelectMode() : setSelectMode(true))}>
                                {selectMode ? 'Cancel' : 'Select'}
                            </button>
                        )}
                        {!selectMode && (
                            <button type="button" className="btn btn-sm" onClick={() => void handleCreate(true)}>
                                <PlusIcon className="toolbar-icon" /> New album
                            </button>
                        )}
                    </div>
                </div>
                <div className="pt-album-cards">
                    {albums.map((a) => {
                        const checked = selectedAlbumIds.includes(a.id);
                        const src = a.coverThumbnailUrl ? covers[a.coverThumbnailUrl] : undefined;
                        return (
                            <button
                                key={a.id}
                                type="button"
                                className={`pt-album-card${checked ? ' selected' : ''}`}
                                aria-pressed={selectMode ? checked : undefined}
                                onClick={() => (selectMode ? toggleAlbumSelect(a.id) : navigate('albums', { albumId: a.id }))}
                            >
                                <span className="pt-album-card-cover">
                                    {src ? <img src={src} alt="" /> : null}
                                    {selectMode && <span className={`pt-album-row-check${checked ? ' on' : ''}`} aria-hidden="true"><CheckIcon /></span>}
                                </span>
                                <b>{a.name}</b>
                                <span>{a.isPublic && <LinkIcon aria-label="Shared" />}{plural(a.photoCount, 'item')}{a.isPublic ? ' · Shared' : ''}</span>
                            </button>
                        );
                    })}
                </div>
                <BrowseLinks />
                {!selectMode && (
                    <button type="button" className="albm-newbtn" onClick={() => setSmartSheetOpen(true)} disabled={smartCreatingRule !== null}>
                        <SparklesIcon /> {smartCreatingRule ? 'Creating…' : 'Make a smart album'}
                    </button>
                )}
            </div>

            {/* Desktop sidebar + mobile detail view */}
            <div className="pt-albums-desktop-sidebar">
                {showDetail && (
                    <button
                        type="button"
                        className="pt-back"
                        onClick={() => navigate('albums', {})}
                        aria-label="Back to albums"
                    >
                        <ArrowLeftIcon /> Back
                    </button>
                )}
                {!showDetail && <div className="pt-albums-browse-desktop"><BrowseLinks /></div>}
                <div className={`pt-albums${showDetail ? ' show-detail' : ''}`}>
                    <aside className="pt-album-sidebar">
                        <div className="pt-album-list-scroll">{albumListRows}</div>
                        {albumListFooter}
                    </aside>

                    <section className="pt-album-detail">
                        <div className={`pt-album-hero${cover ? '' : ' no-cover'}`}>
                            {cover && <img src={cover} alt="" />}
                            <div className="pt-album-hero-text">
                                <h1 className="pt-page-title">{album.name}</h1>
                                <p>{plural(album.photoCount, 'item')}{span ? ` · ${span}` : ''}</p>
                            </div>
                        </div>
                        <div className="pt-album-actions">
                            <button type="button" className="btn btn-sm" onClick={() => setPickerOpen(true)}>
                                <AddPhotosIcon className="toolbar-icon" /> Add photos
                            </button>
                            <button type="button" className={`btn btn-sm${album.isPublic ? '' : ' btn-primary'}`} onClick={() => setShareOpen(true)}>
                                <ShareIcon className="toolbar-icon" /> Share
                            </button>
                            {status && <span className="pt-album-status inline"><LinkIcon aria-hidden="true" />{status}</span>}
                            <span className="pt-album-actions-spacer" />
                            {activePhotos && activePhotos.length > 0 && (
                                <button type="button" className="pt-linkish" onClick={() => setPhotoSelectMode(!photoSelectMode)}>
                                    {photoSelectMode ? 'Done' : 'Select'}
                                </button>
                            )}
                            <Menu
                                align="right"
                                renderTrigger={(toggle) => (
                                    <button type="button" className="btn btn-icon btn-sm" aria-label="Album options" onClick={toggle}><MoreIcon /></button>
                                )}
                            >
                                {(close) => (
                                    <div className="pt-more-menu">
                                        <button type="button" onClick={() => { close(); void renameAlbumPrompt(); }}>Rename album</button>
                                        <button type="button" className="danger" onClick={() => { close(); void confirmDeleteAlbum(); }}>Delete album</button>
                                    </div>
                                )}
                            </Menu>
                        </div>
                        {status && <p className="pt-album-status stacked"><LinkIcon aria-hidden="true" />{status}</p>}

                        <div className="pt-album-photos-head">
                            <ThumbSizeControl value={albumTile} onChange={setAlbumTile} />
                        </div>
                        {/* Before the fetch starts the cache is empty and nothing is flagged loading yet; an album that has photos must not flash "No photos yet" in that gap. */}
                        {activePhotos === undefined && (isAlbumPhotosLoading(album.id) || (album.photoCount > 0 && !isAlbumOpenFailed(album.id))) ? (
                            <Spinner label="Loading photos…" center={false} />
                        ) : (
                            <div style={{ ['--pt-tile-min' as string]: `${albumTile}px` } as React.CSSProperties}>
                                <PhotoGrid photos={activePhotos ?? []} emptyHint="No photos yet. Use Add photos to pick some from your library." />
                                {albumPhotosHasMore(album.id) && <ScrollSentinel onVisible={() => loadMoreAlbumPhotos(album.id)} deps={activePhotos?.length ?? 0} />}
                            </div>
                        )}
                    </section>
                </div>
            </div>

            {selectMode && selectedAlbumIds.length > 0 && (
                <SelectionBar count={selectedAlbumIds.length} onClear={exitSelectMode} label="Albums selection actions">
                    <button
                        type="button"
                        className="pt-fm-delete"
                        onClick={() => void bulkDelete()}
                        aria-label={`Delete ${selectedAlbumIds.length} album${selectedAlbumIds.length > 1 ? 's' : ''}`}
                    >
                        <TrashIcon />
                    </button>
                </SelectionBar>
            )}

            {smartAlbumSheet}
            <ShareAlbumSheet album={album} open={shareOpen} onClose={() => setShareOpen(false)} codes={codes} onCode={setCode} />
            <AddPhotosPicker album={album} open={pickerOpen} onClose={() => setPickerOpen(false)} />
        </div>
    );
};

export default AlbumsPage;
