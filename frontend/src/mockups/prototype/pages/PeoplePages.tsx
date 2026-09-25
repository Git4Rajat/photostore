import React, { useEffect, useState } from 'react';
import { ArrowLeftIcon, SparklesIcon } from '@heroicons/react/24/outline';
import { useStore } from '../store';
import { Swatch } from '../components/bits';
import PhotoGrid from '../components/PhotoGrid';
import { useProtectedBlobUrls } from '../../../services/imageClient';

/** People — a grid of face clusters; unnamed ones are flagged for naming. */
export const PeoplePage: React.FC = () => {
    const { people, peopleLoading, navigate } = useStore();
    const covers = useProtectedBlobUrls(people.map((p) => p.coverThumbnailUrl).filter((u): u is string => Boolean(u)));

    if (peopleLoading && people.length === 0) {
        return (
            <div className="pt-toolbar"><div><h1 className="pt-page-title">People</h1><p className="pt-page-sub">Loading…</p></div></div>
        );
    }

    return (
        <div>
            <div className="pt-toolbar">
                <div>
                    <h1 className="pt-page-title">People</h1>
                    <p className="pt-page-sub">{people.length} people · {people.filter((p) => !p.name).length} to name</p>
                </div>
            </div>
            <div className="pt-people-grid">
                {people.map((person) => {
                    const coverSrc = person.coverThumbnailUrl ? covers[person.coverThumbnailUrl] : undefined;
                    return (
                        <button key={person.id} type="button" className="pt-person-card" onClick={() => navigate('person', { personId: person.id })}>
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
    );
};

/** Person detail — rename / name, browse their photos, and merge in another cluster. */
export const PersonDetailPage: React.FC = () => {
    const { route, people, personById, openPerson, personPhotosById, personPhotosLoading, navigate, renamePerson, mergePeople, reloadPeople, toast } = useStore();
    const personId = route.params.personId;
    const person = personId ? personById(personId) : undefined;
    const [draft, setDraft] = useState(person?.name ?? '');
    const [mergeId, setMergeId] = useState('');
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

    return (
        <div>
            <button type="button" className="pt-back" onClick={() => navigate('people')}><ArrowLeftIcon /> People</button>
            <div className="pt-toolbar">
                <div className="pt-person-head">
                    {coverSrc
                        ? <img className="pt-person-head-face" src={coverSrc} alt={person.name ?? 'Unnamed person'} />
                        : <Swatch swatch={person.swatch} className="pt-person-head-face" />}
                    <div>
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
                        <p className="pt-page-sub">{photos?.length ?? person.faceCount ?? 0} photos</p>
                    </div>
                </div>
                <button type="button" className="btn" onClick={() => { toast('Scanning for more faces…'); reloadPeople(); }}><SparklesIcon className="toolbar-icon" /> Find more faces</button>
            </div>

            {photos === undefined && personPhotosLoading ? (
                <p className="pt-grid-empty">Loading photos…</p>
            ) : (
                <PhotoGrid photos={photos ?? []} emptyHint="No photos for this person yet." />
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
