import React, { useEffect, useMemo, useState } from 'react';
import { ArrowLeft as ArrowLeftIcon, Check as CheckIcon, Sparkles as SparklesIcon, Trash2 as TrashIcon, Users as UserGroupIcon } from 'lucide-react';
import { useStore } from '../store';
import { Swatch, Spinner, SelectionBar } from '../components/bits';
import { ThumbSizeControl, useTileSize } from '../components/controls';
import PhotoGrid from '../components/PhotoGrid';
import { useProtectedBlobUrls } from '../../../services/imageClient';
import { confirmDialog } from '../../../components/shared/dialogs';
import { enqueueBackgroundRequest } from '../../../services/backgroundRequestQueue';
import { useWindowedGrid } from '../../../services/useWindowedGrid';
import type { Person } from '../types';

// The merge target when several selected clusters are merged at once: prefer
// a named person (merging *into* a name is the common case -- folding
// unnamed duplicate clusters into someone already identified), falling back
// to selection order if none are named.
const MERGE_PICKER_MAX_OPTIONS = 200;
const pickMergeTarget = (selected: Person[]): Person => selected.find((p) => p.name) ?? selected[0];

/** People — a grid of face clusters; unnamed ones are flagged for naming.
 *  "Select" mode lets several clusters be picked and merged into one in a
 *  single action, instead of the one-at-a-time merge on the detail page. */
export const PeoplePage: React.FC = () => {
    const { people, peopleLoading, navigate, mergePeopleBatch, deletePeopleBatch, fetchPeople } = useStore();

    // Loads people when this tab is actually visited, queued behind whatever
    // else is in flight, aborted if the user navigates away before its turn.
    // See the 2026-10-01 boot-request audit.
    useEffect(() => {
        const controller = new AbortController();
        void enqueueBackgroundRequest(() => fetchPeople(), { signal: controller.signal }).catch(() => {});
        return () => controller.abort();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    // Accounts can have tens of thousands of clusters: render (and request cover
    // images for) only the rows near the viewport, never the whole list.
    const peopleWindow = useWindowedGrid({ items: people, getKey: (p: Person) => p.id, overscanRows: 3 });
    const covers = useProtectedBlobUrls(
        peopleWindow.visibleItems.map((p) => p.coverThumbnailUrl).filter((u): u is string => Boolean(u)),
    );
    const unnamedCount = useMemo(() => people.filter((p) => !p.name).length, [people]);
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

    return (
        <div>
            <div className="pt-toolbar">
                <div>
                    <h1 className="pt-page-title">People</h1>
                    <p className="pt-page-sub">{people.length} people{unnamedCount > 0 ? ` · ${unnamedCount} to name` : ''}</p>
                </div>
                {people.length > 1 && (
                    <button type="button" className="pt-linkish" onClick={() => (selectMode ? exitSelectMode() : setSelectMode(true))}>
                        {selectMode ? 'Cancel' : 'Select'}
                    </button>
                )}
            </div>
            <div ref={peopleWindow.containerRef} style={peopleWindow.spacerStyle}>
            <div ref={peopleWindow.innerRef} className="pt-people-grid" style={peopleWindow.innerStyle}>
                {peopleWindow.visibleItems.map((person) => {
                    const coverSrc = person.coverThumbnailUrl ? covers[person.coverThumbnailUrl] : undefined;
                    const checked = selectedSet.has(person.id);
                    return (
                        <button
                            key={person.id}
                            type="button"
                            className={`pt-person-card${checked ? ' selected' : ''}`}
                            onClick={() => (selectMode ? toggleSelect(person.id) : navigate('person', { personId: person.id }))}
                        >
                            {selectMode && (
                                <span className={`pt-album-row-check pt-person-check${checked ? ' on' : ''}`} aria-hidden="true">
                                    <CheckIcon />
                                </span>
                            )}
                            {coverSrc
                                ? <img className="pt-person-face" src={coverSrc} alt={person.name ?? 'Unnamed person'} />
                                : <Swatch swatch={person.swatch} className="pt-person-face" />}
                            <span className={`pt-person-name${person.name ? '' : ' unnamed'}`}>{person.name ?? 'Add name'}</span>
                            <span className="pt-person-count">{person.faceCount ?? 0} photos</span>
                        </button>
                    );
                })}
            </div>
            </div>
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
    const { route, people, personById, openPerson, personPhotosById, personPhotosLoading, navigate, renamePerson, mergePeople, deletePerson, reloadPeople, fetchPeople, toast, selectMode: photoSelectMode, setSelectMode: setPhotoSelectMode } = useStore();
    const personId = route.params.personId;
    const person = personId ? personById(personId) : undefined;
    const [draft, setDraft] = useState(person?.name ?? '');
    const [mergeId, setMergeId] = useState('');
    const [mergeQuery, setMergeQuery] = useState('');
    const [personTile, setPersonTile] = useTileSize('photostore.personTileSize');
    const headCover = useProtectedBlobUrls(person?.coverThumbnailUrl ? [person.coverThumbnailUrl] : []);

    useEffect(() => {
        if (personId) void openPerson(personId);
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [personId]);

    // personById reads from the SAME people list the People tab's own mount
    // effect populates -- a direct deep link to a person's detail page
    // (shared link, browser back/forward) without ever visiting the People
    // tab would otherwise find nobody and render the empty-state below even
    // though the person exists. Queued/aborted same as every other tab
    // fetch; redundant (and cheap, queue-deduped by nothing in particular
    // but harmless) if People was already visited this session.
    useEffect(() => {
        const controller = new AbortController();
        void enqueueBackgroundRequest(() => fetchPeople(), { signal: controller.signal }).catch(() => {});
        return () => controller.abort();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

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

    // The merge picker must stay usable with tens of thousands of clusters: search
    // by name, show the biggest matches first, and cap the rendered options (a
    // <select> with 30k <option>s freezes the tab). The chosen person always stays
    // listed so the selection never silently disappears.
    const mergeQ = mergeQuery.trim().toLowerCase();
    const others = people.filter((p) => p.id !== person.id);
    const mergeMatches = others
        .filter((p) => !mergeQ || (p.name ?? 'unnamed').toLowerCase().includes(mergeQ))
        .sort((a, b) => (b.faceCount ?? 0) - (a.faceCount ?? 0))
        .slice(0, MERGE_PICKER_MAX_OPTIONS);
    const mergeOptions = mergeId && !mergeMatches.some((o) => o.id === mergeId)
        ? [...mergeMatches, ...others.filter((o) => o.id === mergeId)]
        : mergeMatches;
    const photos = personPhotosById(person.id);
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

    return (
        <div>
            <button type="button" className="pt-back" onClick={() => navigate('people')}><ArrowLeftIcon /> People</button>
            <div className="pt-toolbar pt-person-toolbar">
                <div className="pt-person-head">
                    {coverSrc
                        ? <img className="pt-person-head-face" src={coverSrc} alt={person.name ?? 'Unnamed person'} />
                        : <Swatch swatch={person.swatch} className="pt-person-head-face" />}
                    <div className="pt-person-name-row">
                        <input
                            className="field"
                            value={draft}
                            placeholder="Add a name"
                            onChange={(e) => setDraft(e.target.value)}
                            onKeyDown={(e) => { if (e.key === 'Enter' && draft.trim()) { renamePerson(person.id, draft.trim()); toast('Name saved'); } }}
                        />
                        <button type="button" className="btn mock-cta" disabled={!draft.trim()} onClick={() => { renamePerson(person.id, draft.trim()); toast('Name saved'); }}>Save</button>
                    </div>
                </div>
                <div className="pt-toolbar-actions">
                    <button type="button" className="btn" onClick={() => { toast('Scanning for more faces…'); reloadPeople(); }}><SparklesIcon className="toolbar-icon" /> Find more faces</button>
                    <button type="button" className="btn btn-danger" onClick={() => void handleDeletePerson()}><TrashIcon className="toolbar-icon" /> Delete</button>
                </div>
            </div>
            <div className="pt-album-photos-head pt-person-photos-head">
                <p className="pt-page-sub" style={{ margin: 0 }}>{photos?.length ?? person.faceCount ?? 0} photos</p>
                <div className="pt-album-photos-actions">
                    <ThumbSizeControl value={personTile} onChange={setPersonTile} />
                    {photos && photos.length > 0 && (
                        <button type="button" className="pt-linkish" onClick={() => setPhotoSelectMode(!photoSelectMode)}>
                            {photoSelectMode ? 'Done' : 'Select'}
                        </button>
                    )}
                </div>
            </div>

            {photos === undefined && personPhotosLoading ? (
                <Spinner label="Loading photos…" center={false} />
            ) : (
                <div style={{ ['--pt-tile-min' as string]: `${personTile}px` } as React.CSSProperties}>
                    <PhotoGrid photos={photos ?? []} emptyHint="No photos for this person yet." />
                </div>
            )}

            <div className="card-glass pt-merge">
                <div className="pt-menu-label">Merge another person into {person.name ?? 'this person'}</div>
                <div className="pt-merge-row">
                    <input
                        className="field"
                        type="search"
                        value={mergeQuery}
                        onChange={(e) => setMergeQuery(e.target.value)}
                        placeholder={`Search ${others.length.toLocaleString()} people…`}
                        aria-label="Search people to merge"
                    />
                    <select className="field field-select" value={mergeId} onChange={(e) => setMergeId(e.target.value)} aria-label="Person to merge">
                        <option value="">{mergeMatches.length < others.filter((p) => !mergeQ || (p.name ?? 'unnamed').toLowerCase().includes(mergeQ)).length ? `Choose a person… (top ${mergeMatches.length} shown — search to narrow)` : 'Choose a person…'}</option>
                        {mergeOptions.map((o) => (
                            <option key={o.id} value={o.id}>{o.name ?? 'Unnamed'} · {o.faceCount ?? 0} photos</option>
                        ))}
                    </select>
                    <button type="button" className="btn" disabled={!mergeId} onClick={() => { mergePeople(mergeId, person.id); setMergeId(''); }}>Merge</button>
                </div>
            </div>
        </div>
    );
};
