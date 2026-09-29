import React, { useEffect, useState } from 'react';
import { ArrowLeft as ArrowLeftIcon, Check as CheckIcon, Sparkles as SparklesIcon, Trash2 as TrashIcon, Users as UserGroupIcon } from 'lucide-react';
import { useStore } from '../store';
import { Swatch, Spinner, SelectionBar } from '../components/bits';
import { ThumbSizeControl, useTileSize } from '../components/controls';
import PhotoGrid from '../components/PhotoGrid';
import { useProtectedBlobUrls } from '../../../services/imageClient';
import { confirmDialog } from '../../../components/shared/dialogs';
import type { Person } from '../types';

// The merge target when several selected clusters are merged at once: prefer
// a named person (merging *into* a name is the common case -- folding
// unnamed duplicate clusters into someone already identified), falling back
// to selection order if none are named.
const pickMergeTarget = (selected: Person[]): Person => selected.find((p) => p.name) ?? selected[0];

/** People — a grid of face clusters; unnamed ones are flagged for naming.
 *  "Select" mode lets several clusters be picked and merged into one in a
 *  single action, instead of the one-at-a-time merge on the detail page. */
export const PeoplePage: React.FC = () => {
    const { people, peopleLoading, navigate, mergePeopleBatch, deletePeopleBatch } = useStore();
    const covers = useProtectedBlobUrls(people.map((p) => p.coverThumbnailUrl).filter((u): u is string => Boolean(u)));
    const unnamedCount = people.filter((p) => !p.name).length;
    const [selectMode, setSelectMode] = useState(false);
    const [selectedIds, setSelectedIds] = useState<string[]>([]);

    const exitSelectMode = () => {
        setSelectMode(false);
        setSelectedIds([]);
    };

    const toggleSelect = (id: string) => {
        setSelectedIds((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));
    };

    const handleMerge = () => {
        const selected = people.filter((p) => selectedIds.includes(p.id));
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
            <div className="pt-people-grid">
                {people.map((person) => {
                    const coverSrc = person.coverThumbnailUrl ? covers[person.coverThumbnailUrl] : undefined;
                    const checked = selectedIds.includes(person.id);
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
    const { route, people, personById, openPerson, personPhotosById, personPhotosLoading, navigate, renamePerson, mergePeople, deletePerson, reloadPeople, toast, selectMode: photoSelectMode, setSelectMode: setPhotoSelectMode } = useStore();
    const personId = route.params.personId;
    const person = personId ? personById(personId) : undefined;
    const [draft, setDraft] = useState(person?.name ?? '');
    const [mergeId, setMergeId] = useState('');
    const [personTile, setPersonTile] = useTileSize('photostore.personTileSize');
    const headCover = useProtectedBlobUrls(person?.coverThumbnailUrl ? [person.coverThumbnailUrl] : []);

    useEffect(() => {
        if (personId) void openPerson(personId);
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [personId]);

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

    const others = people.filter((p) => p.id !== person.id);
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
                    <select className="field field-select" value={mergeId} onChange={(e) => setMergeId(e.target.value)} aria-label="Person to merge">
                        <option value="">Choose a person…</option>
                        {others.map((o) => (
                            <option key={o.id} value={o.id}>{o.name ?? 'Unnamed'} · {o.faceCount ?? 0} photos</option>
                        ))}
                    </select>
                    <button type="button" className="btn" disabled={!mergeId} onClick={() => { mergePeople(mergeId, person.id); setMergeId(''); }}>Merge</button>
                </div>
            </div>
        </div>
    );
};
