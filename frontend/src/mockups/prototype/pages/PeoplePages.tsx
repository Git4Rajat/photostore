import React, { useEffect, useMemo, useRef, useState } from 'react';
import { ArrowLeft as ArrowLeftIcon, Check as CheckIcon, MoreHorizontal as MoreIcon, Search as SearchIcon, Trash2 as TrashIcon, Users as UserGroupIcon } from 'lucide-react';
import { useStore } from '../store';
import { Menu, Swatch, Spinner, SelectionBar } from '../components/bits';
import { BottomSheet } from '../components/BottomSheet';
import { captureWallClock } from '../../../utils/captureDate';
import { useTileSize } from '../components/controls';
import PhotoGrid from '../components/PhotoGrid';
import ScrollSentinel from '../components/ScrollSentinel';
import { FloatingSelectButton } from '../components/FloatingSelectButton';
import { useProtectedBlobUrls } from '../../../services/imageClient';
import { confirmDialog } from '../../../components/shared/dialogs';
import { enqueueBackgroundRequest } from '../../../services/backgroundRequestQueue';
import type { Person } from '../types';
import { plural } from '../../../utils/format';

// The merge target when several selected clusters are merged at once: prefer
// a named person (merging *into* a name is the common case -- folding
// unnamed duplicate clusters into someone already identified), falling back
// to selection order if none are named.
const MERGE_PICKER_MAX_OPTIONS = 200;
const FIRST_SEEN_FORMAT = new Intl.DateTimeFormat(undefined, { day: 'numeric', month: 'long', year: 'numeric' });

/** A face in the merge list. */
const MergeFace: React.FC<{ person: Person }> = ({ person }) => {
    const urls = useProtectedBlobUrls(person.coverThumbnailUrl ? [person.coverThumbnailUrl] : []);
    const src = person.coverThumbnailUrl ? urls[person.coverThumbnailUrl] : undefined;
    return src ? <img className="pt-merge-face" src={src} alt="" /> : <Swatch swatch={person.swatch} className="pt-merge-face" />;
};
const pickMergeTarget = (selected: Person[]): Person => selected.find((p) => p.name) ?? selected[0];

/** People — a grid of face clusters; unnamed ones are flagged for naming.
 *  "Select" mode (or a long-press on a card) lets several clusters be picked and merged into one in a
 *  single action, instead of the one-at-a-time merge on the detail page. */
export const PeoplePage: React.FC = () => {
    const { people, peopleLoading, peopleTotal, peopleUnnamedTotal, peopleHasMore, peopleUnavailable, loadMorePeople, navigate, mergePeopleBatch, deletePeopleBatch, fetchPeople, route } = useStore();

    // Loads people when this tab is actually visited, queued behind whatever
    // else is in flight, aborted if the user navigates away before its turn.
    // See the 2026-10-01 boot-request audit.
    useEffect(() => {
        const controller = new AbortController();
        void enqueueBackgroundRequest(() => fetchPeople(), { signal: controller.signal }).catch(() => {});
        return () => controller.abort();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    // Clusters arrive a screenful at a time (the server pages them, named first) and more load as the
    // user scrolls, so only what has been scrolled to is ever requested or rendered.
    const covers = useProtectedBlobUrls(
        people.map((p) => p.coverThumbnailUrl).filter((u): u is string => Boolean(u)),
    );
    const unnamedCount = peopleUnnamedTotal;
    const [selectMode, setSelectMode] = useState(false);
    const [selectedIds, setSelectedIds] = useState<string[]>([]);
    const selectedSet = useMemo(() => new Set(selectedIds), [selectedIds]);

    const exitSelectMode = () => {
        setSelectMode(false);
        setSelectedIds([]);
    };

    const toggleSelect = (id: string) => {
        setSelectedIds((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));
    };

    // Long-press a card to start selecting with that person picked, the same gesture as the
    // photo grid. Scrolling cancels the timer (onTouchMove), and `longPressedRef` swallows the
    // click that follows the hold so it doesn't open the person or undo the pick.
    const longPressTimer = useRef<number | null>(null);
    const longPressedRef = useRef(false);
    const clearLongPress = () => {
        if (longPressTimer.current !== null) {
            window.clearTimeout(longPressTimer.current);
            longPressTimer.current = null;
        }
    };
    useEffect(() => clearLongPress, []);
    const startLongPress = (id: string) => {
        longPressedRef.current = false;
        clearLongPress();
        longPressTimer.current = window.setTimeout(() => {
            longPressTimer.current = null;
            longPressedRef.current = true;
            setSelectMode(true);
            toggleSelect(id);
        }, 400);
    };

    const handleMerge = () => {
        const selected = people.filter((p) => selectedSet.has(p.id));
        if (selected.length < 2) return;
        const target = pickMergeTarget(selected);
        const sourceIds = selected.filter((p) => p.id !== target.id).map((p) => p.id);
        mergePeopleBatch(target.id, sourceIds);
        exitSelectMode();
    };

    const handleDelete = async () => {
        const count = selectedIds.length;
        if (!count) return;
        const confirmed = await confirmDialog({
            title: `Delete ${count} ${count === 1 ? 'person' : 'people'}?`,
            message: 'Their faces become unassigned — the photos themselves are untouched.',
            confirmLabel: 'Delete',
            danger: true,
        });
        if (!confirmed) return;
        deletePeopleBatch(selectedIds);
        exitSelectMode();
    };

    if (peopleLoading && people.length === 0) {
        return (
            <div>
                <div className="pt-toolbar"><div><h1 className="pt-page-title">People</h1></div></div>
                <Spinner label="Loading people…" />
            </div>
        );
    }

    if (peopleUnavailable && people.length === 0) {
        return (
            <div>
                <div className="pt-toolbar"><div><h1 className="pt-page-title">People</h1></div></div>
                <p className="pt-grid-empty">Your people are still being prepared. This can take a few minutes for a large library.</p>
                <button type="button" className="btn" onClick={() => void fetchPeople()}>Try again</button>
            </div>
        );
    }

    const renderCard = (person: Person) => {
        const coverSrc = person.coverThumbnailUrl ? covers[person.coverThumbnailUrl] : undefined;
        const checked = selectedSet.has(person.id);
        return (
            <button
                key={person.id}
                type="button"
                className={`pt-person-card${checked ? ' selected' : ''}${person.id === route.params.personId ? ' current' : ''}`}
                onClick={() => {
                    if (longPressedRef.current) {
                        longPressedRef.current = false;
                        return;
                    }
                    if (selectMode) toggleSelect(person.id);
                    else navigate('person', { personId: person.id });
                }}
                onTouchStart={() => startLongPress(person.id)}
                onTouchMove={clearLongPress}
                onTouchEnd={clearLongPress}
                onTouchCancel={clearLongPress}
                // Android opens the image context menu on a hold; the hold selects here.
                onContextMenu={(e) => e.preventDefault()}
            >
                <span className="pt-person-face-wrap">
                    {coverSrc
                        ? <img className="pt-person-face" src={coverSrc} alt={person.name ?? 'Unnamed person'} draggable={false} />
                        : <Swatch swatch={person.swatch} className="pt-person-face" />}
                    {selectMode && (
                        <span className={`pt-album-row-check pt-person-check${checked ? ' on' : ''}`} aria-hidden="true">
                            <CheckIcon />
                        </span>
                    )}
                </span>
                <span className={`pt-person-name${person.name ? '' : ' unnamed'}`}>{person.name ?? 'Add name'}</span>
                <span className="pt-person-count">{plural(person.faceCount ?? 0, 'photo')}</span>
            </button>
        );
    };
    // Named first, then the ones waiting for a name (the server already pages named first).
    const named = people.filter((p) => p.name);
    const unnamed = people.filter((p) => !p.name);
    const firstUnnamed = unnamed[0];

    return (
        <div className="pt-people">
            {/* Phones reach People from the Albums tab, so the way back says Albums. */}
            <button type="button" className="pt-back pt-phone-only" onClick={() => navigate('albums')}><ArrowLeftIcon /> Albums</button>
            <div className="pt-toolbar pt-people-toolbar">
                <div>
                    <h1 className="pt-page-title">People</h1>
                    <p className="pt-page-sub">{peopleTotal.toLocaleString()} people{unnamedCount > 0 ? ` · ${unnamedCount} to name` : ''}</p>
                </div>
                {people.length > 1 && (
                    <button type="button" className="pt-linkish" onClick={() => (selectMode ? exitSelectMode() : setSelectMode(true))}>
                        {selectMode ? 'Cancel' : 'Select'}
                    </button>
                )}
            </div>
            {/* A prompt to name the rest: names make people searchable. */}
            {!selectMode && unnamedCount > 0 && firstUnnamed && (
                <div className="pt-name-banner">
                    <span className="pt-face-stack" aria-hidden="true">
                        {unnamed.slice(0, 3).map((p) => {
                            const src = p.coverThumbnailUrl ? covers[p.coverThumbnailUrl] : undefined;
                            return src ? <img key={p.id} src={src} alt="" /> : <Swatch key={p.id} swatch={p.swatch} />;
                        })}
                    </span>
                    <span className="pt-name-banner-text">
                        <b>Name {unnamedCount.toLocaleString()} more {unnamedCount === 1 ? 'person' : 'people'}</b>
                        <small>Names make them searchable.</small>
                    </span>
                    <button type="button" className="btn btn-primary btn-sm" onClick={() => navigate('person', { personId: firstUnnamed.id })}>Start</button>
                </div>
            )}
            {named.length > 0 && (
                <>
                    <h2 className="pt-section-label">Named</h2>
                    <div className="pt-people-grid">{named.map(renderCard)}</div>
                </>
            )}
            {unnamed.length > 0 && (
                <>
                    <h2 className="pt-section-label">Not named yet</h2>
                    <div className="pt-people-grid">{unnamed.map(renderCard)}</div>
                </>
            )}
            {peopleHasMore && <ScrollSentinel onVisible={loadMorePeople} deps={people.length} />}
            {people.length > 1 && (
                <FloatingSelectButton
                    selectMode={selectMode}
                    onToggle={() => (selectMode ? exitSelectMode() : setSelectMode(true))}
                    suppressed={selectedIds.length > 0}
                    noun="people"
                    grid=".pt-people-grid"
                    hideOnTouch
                />
            )}
            {selectMode && selectedIds.length > 0 && (
                <SelectionBar count={selectedIds.length} onClear={exitSelectMode} label="People selection actions">
                    <button
                        type="button"
                        className="pt-fm-delete"
                        onClick={() => void handleDelete()}
                        aria-label={`Delete ${selectedIds.length} ${selectedIds.length > 1 ? 'people' : 'person'}`}
                    >
                        <TrashIcon />
                    </button>
                    {selectedIds.length > 1 && (
                        <button type="button" className="pt-fm-more" onClick={handleMerge} aria-label="Merge selected people">
                            <UserGroupIcon />
                        </button>
                    )}
                </SelectionBar>
            )}
        </div>
    );
};

/** Person detail — rename / name, browse their photos, and merge in another cluster. */
export const PersonDetailPage: React.FC = () => {
    const { route, personById, searchPeople, openPerson, personPhotosById, personPhotosTotal, personPhotosHasMore, loadMorePersonPhotos, personPhotosLoading, navigate, renamePerson, findMoreFaces, mergePeople, deletePerson, toast, selectMode: photoSelectMode, setSelectMode: setPhotoSelectMode } = useStore();
    const personId = route.params.personId;
    const person = personId ? personById(personId) : undefined;
    const [draft, setDraft] = useState(person?.name ?? '');
    const [mergeOpen, setMergeOpen] = useState(false);
    const [mergeQuery, setMergeQuery] = useState('');
    const [mergeResults, setMergeResults] = useState<Person[]>([]);
    // The merge picker only searches once it is used (typing or focus), not on every person that is opened.
    const [mergeActive, setMergeActive] = useState(false);
    const [findingFaces, setFindingFaces] = useState(false);
    const [personTile] = useTileSize('photostore.personTileSize');
    const headCover = useProtectedBlobUrls(person?.coverThumbnailUrl ? [person.coverThumbnailUrl] : []);

    useEffect(() => {
        if (personId) void openPerson(personId);
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [personId]);

    // A deep link to a person outside the loaded pages is resolved by openPerson (ensurePerson fetches just
    // that cluster), so opening a person never reloads the whole people list.

    useEffect(() => {
        if (!mergeActive && !mergeQuery.trim()) return undefined;
        let active = true;
        const timer = setTimeout(() => {
            void searchPeople(mergeQuery.trim(), MERGE_PICKER_MAX_OPTIONS).then((rows) => { if (active) setMergeResults(rows); });
        }, 250);
        return () => { active = false; clearTimeout(timer); };
    }, [mergeQuery, mergeActive, searchPeople]);

    useEffect(() => {
        setDraft(person?.name ?? '');
    }, [person?.name]);

    if (!person) {
        return (
            <div className="pt-empty-page">
                <p>That person no longer exists.</p>
                <button type="button" className="btn" onClick={() => navigate('people')}>Back to People</button>
            </div>
        );
    }

    // The merge picker searches on the server (name contains), so it works however many clusters exist.
    const mergeMatches = mergeResults.filter((p) => p.id !== person.id);
    const photos = personPhotosById(person.id);
    const photoCount = personPhotosHasMore(person.id)
        ? (personPhotosTotal(person.id) ?? photos?.length ?? person.faceCount ?? 0)
        : (photos?.length ?? personPhotosTotal(person.id) ?? person.faceCount ?? 0);
    const coverSrc = person.coverThumbnailUrl ? headCover[person.coverThumbnailUrl] : undefined;

    const handleDeletePerson = async () => {
        const confirmed = await confirmDialog({
            title: `Delete ${person.name ?? 'this person'}?`,
            message: 'Their faces become unassigned — the photos themselves are untouched.',
            confirmLabel: 'Delete',
            danger: true,
        });
        if (!confirmed) return;
        deletePerson(person.id);
        navigate('people');
    };

    const saveName = () => {
        const next = draft.trim();
        if (!next || next === person.name) return;
        renamePerson(person.id, next);
        toast('Name saved');
    };
    const firstSeen = photos && photos.length && !personPhotosHasMore(person.id)
        ? photos.map((p) => p.captureDate).filter((d): d is string => Boolean(d)).sort()[0]
        : undefined;
    const firstSeenDay = firstSeen ? captureWallClock(firstSeen) : null;

    return (
        <div className="pt-person">
            <div className="pt-person-nav">
                <button type="button" className="pt-back" onClick={() => navigate('people')}><ArrowLeftIcon /> People</button>
                <Menu
                    align="right"
                    renderTrigger={(toggle) => (
                        <button type="button" className="btn btn-icon btn-sm" aria-label="Person options" onClick={toggle}><MoreIcon /></button>
                    )}
                >
                    {(close) => (
                        <div className="pt-more-menu">
                            <button type="button" onClick={() => { close(); setMergeOpen(true); setMergeActive(true); }}>Merge with someone else…</button>
                            <button type="button" disabled={findingFaces} onClick={() => {
                                close();
                                setFindingFaces(true);
                                void findMoreFaces(person.id).finally(() => setFindingFaces(false));
                            }}>{findingFaces ? 'Looking for more faces…' : 'Find more faces'}</button>
                            <button type="button" className="danger" onClick={() => { close(); void handleDeletePerson(); }}>Delete person</button>
                        </div>
                    )}
                </Menu>
            </div>
            {/* Name them right here: the field is the heading. */}
            <div className="pt-who">
                {coverSrc
                    ? <img className="pt-who-face" src={coverSrc} alt={person.name ?? 'Unnamed person'} />
                    : <Swatch swatch={person.swatch} className="pt-who-face" />}
                <input
                    className="field pt-who-name"
                    value={draft}
                    placeholder="Add a name"
                    aria-label="Name"
                    onChange={(e) => setDraft(e.target.value)}
                    onKeyDown={(e) => { if (e.key === 'Enter') { saveName(); (e.target as HTMLInputElement).blur(); } }}
                    onBlur={saveName}
                />
                <small>
                    {photoCount.toLocaleString()} {photoCount === 1 ? 'photo' : 'photos'}
                    {firstSeenDay ? ` · first seen ${FIRST_SEEN_FORMAT.format(firstSeenDay)}` : ''}
                </small>
            </div>

            <div className="pt-person-photos-head">
                <h2 className="pt-section-label">Photos</h2>
                {photos && photos.length > 0 && (
                    <button type="button" className="pt-linkish" onClick={() => setPhotoSelectMode(!photoSelectMode)}>
                        {photoSelectMode ? 'Done' : 'Select'}
                    </button>
                )}
            </div>

            {photos === undefined && personPhotosLoading ? (
                <Spinner label="Loading photos…" center={false} />
            ) : (
                <div style={{ ['--pt-tile-min' as string]: `${personTile}px` } as React.CSSProperties}>
                    <PhotoGrid photos={photos ?? []} emptyHint="No photos for this person yet." />
                    {personPhotosHasMore(person.id) && <ScrollSentinel onVisible={() => loadMorePersonPhotos(person.id)} deps={photos?.length ?? 0} />}
                </div>
            )}

            <BottomSheet
                open={mergeOpen}
                onClose={() => setMergeOpen(false)}
                title={`Merge into ${person.name ?? 'this person'}`}
                header={(
                    <div className="pt-sheet-search">
                        <SearchIcon />
                        <input
                            type="search"
                            value={mergeQuery}
                            onChange={(e) => setMergeQuery(e.target.value)}
                            placeholder="Search people by name…"
                            aria-label="Search people to merge"
                        />
                    </div>
                )}
            >
                <p className="pt-sheet-intro">Their photos join {person.name ?? 'this person'}, and the two become one person.</p>
                <div className="pt-merge-list">
                    {mergeMatches.length === 0 && <p className="pt-grid-empty">No one matches.</p>}
                    {mergeMatches.map((o) => (
                        <button key={o.id} type="button" className="pt-merge-option" onClick={() => { mergePeople(o.id, person.id); setMergeOpen(false); }}>
                            <MergeFace person={o} />
                            <span><b>{o.name ?? 'Not named yet'}</b><small>{plural(o.faceCount ?? 0, 'photo')}</small></span>
                        </button>
                    ))}
                </div>
            </BottomSheet>
        </div>
    );
};
