import React, { useEffect, useState } from 'react';
import {
    ArrowLeft as ArrowLeftIcon,
    RefreshCw as ArrowPathIcon,
    CalendarDays as CalendarDaysIcon,
    Check as CheckIcon,
    Copy as ClipboardDocumentIcon,
    Clock as ClockIcon,
    MapPin as MapPinIcon,
    Plus as PlusIcon,
    Share2 as ShareIcon,
    Sparkles as SparklesIcon,
    Tag as TagIcon,
    Trash2 as TrashIcon,
    Users as UserGroupIcon,
} from 'lucide-react';
import { useStore } from '../store';
import PhotoGrid from '../components/PhotoGrid';
import ScrollSentinel from '../components/ScrollSentinel';
import { Spinner, SelectionBar } from '../components/bits';
import { BottomSheet } from '../components/BottomSheet';
import { ThumbSizeControl, useTileSize } from '../components/controls';
import { confirmDialog, promptDialog } from '../../../components/shared/dialogs';
import { useProtectedBlobUrls } from '../../../services/imageClient';
import { enqueueBackgroundRequest } from '../../../services/backgroundRequestQueue';

const EXPIRY_OPTIONS: { value: string; label: string; days: number }[] = [
    { value: '1', label: 'In 1 day', days: 1 },
    { value: '7', label: 'In 7 days', days: 7 },
    { value: '30', label: 'In 30 days', days: 30 },
    { value: 'never', label: 'Never', days: 0 },
];

const SMART_ALBUM_RULES: Array<{ id: string; label: string; description: string; Icon: React.ComponentType<React.SVGProps<SVGSVGElement>> }> = [
    { id: 'location', label: 'By location', description: 'Places across the library', Icon: MapPinIcon },
    { id: 'recent-upload', label: 'By recent upload', description: 'Latest upload window', Icon: ClockIcon },
    { id: 'person', label: 'By person', description: 'Matched people clusters', Icon: UserGroupIcon },
    { id: 'event-window', label: 'By event/time', description: 'Capture date window', Icon: CalendarDaysIcon },
    { id: 'tag-object', label: 'By tag/object', description: 'AI tags and detected objects', Icon: TagIcon },
];

const randomCode = () => Math.random().toString(16).slice(2, 6).toUpperCase();

/** Albums — list first on mobile, detail view on larger screens. */
export const AlbumsPage: React.FC = () => {
    const {
        albums, albumsLoading, route, navigate, openAlbum, albumPhotosById, isAlbumPhotosLoading, loadMoreAlbumPhotos, albumPhotosHasMore,
        createAlbum, autoCreateAlbum, renameAlbum, deleteAlbum, deleteAlbums, shareAlbum, revokeAlbum, toast,
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
    const selectedId = route.params.albumId ?? albums[0]?.id;
    const album = albums.find((a) => a.id === selectedId) ?? albums[0];
    const [copied, setCopied] = useState(false);
    const [expiry, setExpiry] = useState('7');
    const covers = useProtectedBlobUrls(albums.map((a) => a.coverThumbnailUrl).filter((u): u is string => Boolean(u)));
    // The backend never returns an album's access code back (it's a stored
    // secret), so remember codes we generate this session to show the owner.
    const [codes, setCodes] = useState<Record<string, string>>({});

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
        const id = await createAlbum('New album');
        if (!id) return;
        navigate('albums', { albumId: id });
        if (thenRename) await renameAlbumPrompt(id, 'New album');
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

    if (!album) {
        return (
            <div className="pt-empty-page">
                {albumsLoading ? <Spinner label="Loading albums…" /> : <p>No albums yet.</p>}
                {!albumsLoading && (
                    <div className="pt-arrive-actions">
                        <button type="button" className="btn mock-cta" onClick={() => void handleCreate(true)}>
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

    const url = album.publicUrl ?? '';
    const copy = () => {
        if (!url) return;
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1400);
        void navigator.clipboard?.writeText(url).catch(() => {});
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
                            <span>{a.photoCount} photos</span>
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
            {/* Mobile list view */}
            <div className={`pt-albums-mobile-list${showDetail ? ' hidden' : ''}`}>
                <div className="pt-album-list-scroll">{albumListRows}</div>
                {albumListFooter}
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
                <div className={`pt-albums${showDetail ? ' show-detail' : ''}`}>
                    <aside className="pt-album-sidebar">
                        <div className="pt-album-list-scroll">{albumListRows}</div>
                        {albumListFooter}
                    </aside>

                    <section className="pt-album-detail">
                        <div className="pt-album-detail-head">
                            <div>
                                <h1 className="pt-page-title">{album.name}</h1>
                                <p className="pt-page-sub">
                                    {album.photoCount} photos ·{' '}
                                    <button type="button" className="pt-linkish" onClick={() => void renameAlbumPrompt()}>Rename</button>
                                    {' · '}
                                    <button type="button" className="pt-linkish danger" onClick={() => void confirmDeleteAlbum()}>Delete</button>
                                </p>
                            </div>
                            <button type="button" className="btn mock-cta" onClick={togglePublic}>
                                <ShareIcon className="toolbar-icon" /> <span>{album.isPublic ? 'Sharing on' : 'Share'}</span>
                            </button>
                        </div>

                        {album.isPublic && (
                            <div className="pt-share-section">
                                <div className="pt-menu-label">Sharing</div>
                                <div className="share-sheet">
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
                                </div>
                            </div>
                        )}

                        <div className="pt-album-photos-head">
                            <div className="pt-menu-label" style={{ margin: 0 }}>Photos</div>
                            <div className="pt-album-photos-actions">
                                <ThumbSizeControl value={albumTile} onChange={setAlbumTile} />
                                {activePhotos && activePhotos.length > 0 && (
                                    <button type="button" className="pt-linkish" onClick={() => setPhotoSelectMode(!photoSelectMode)}>
                                        {photoSelectMode ? 'Done' : 'Select'}
                                    </button>
                                )}
                                <button type="button" className="pt-linkish" onClick={() => { navigate('gallery'); toast('Select photos, then use “Add to album”'); }}><PlusIcon className="toolbar-icon" /> Add photos</button>
                            </div>
                        </div>
                        {activePhotos === undefined && isAlbumPhotosLoading(album.id) ? (
                            <Spinner label="Loading photos…" center={false} />
                        ) : (
                            <div style={{ ['--pt-tile-min' as string]: `${albumTile}px` } as React.CSSProperties}>
                                <PhotoGrid photos={activePhotos ?? []} emptyHint="No photos yet — add some from the gallery." />
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
        </div>
    );
};

export default AlbumsPage;
