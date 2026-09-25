import React, { useState } from 'react';
import { ArrowLeftIcon, SparklesIcon } from '@heroicons/react/24/outline';
import { useStore } from '../store';
import { Swatch } from '../components/bits';
import PhotoGrid from '../components/PhotoGrid';

/** People — a grid of face clusters; unnamed ones are flagged for naming. */
export const PeoplePage: React.FC = () => {
    const { people, navigate, photoById } = useStore();
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
                    const cover = photoById(person.photoIds[0]);
                    return (
                        <button key={person.id} type="button" className="pt-person-card" onClick={() => navigate('person', { personId: person.id })}>
                            {cover ? <Swatch swatch={cover.swatch} className="pt-person-face" /> : <Swatch swatch={person.swatch} className="pt-person-face" />}
                            <span className={`pt-person-name${person.name ? '' : ' unnamed'}`}>{person.name ?? 'Add name'}</span>
                            <span className="pt-person-count">{person.photoIds.length} photos</span>
                        </button>
                    );
                })}
            </div>
        </div>
    );
};

/** Person detail — rename / name, browse their photos, and merge in another cluster. */
export const PersonDetailPage: React.FC = () => {
    const { route, people, personById, photosByIds, navigate, renamePerson, mergePeople, toast } = useStore();
    const person = route.params.personId ? personById(route.params.personId) : undefined;
    const [draft, setDraft] = useState(person?.name ?? '');
    const [mergeId, setMergeId] = useState('');

    if (!person) {
        return (
            <div className="pt-empty-page">
                <p>That person no longer exists.</p>
                <button type="button" className="btn" onClick={() => navigate('people')}>Back to People</button>
            </div>
        );
    }

    const others = people.filter((p) => p.id !== person.id);

    return (
        <div>
            <button type="button" className="pt-back" onClick={() => navigate('people')}><ArrowLeftIcon /> People</button>
            <div className="pt-toolbar">
                <div className="pt-person-head">
                    <Swatch swatch={person.swatch} className="pt-person-head-face" />
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
                        <p className="pt-page-sub">{person.photoIds.length} photos</p>
                    </div>
                </div>
                <button type="button" className="btn" onClick={() => toast('Scanning for more faces…')}><SparklesIcon className="toolbar-icon" /> Find more faces</button>
            </div>

            <PhotoGrid photos={photosByIds(person.photoIds)} emptyHint="No photos for this person yet." />

            <div className="card-glass pt-merge">
                <div className="pt-menu-label">Merge another person into {person.name ?? 'this person'}</div>
                <div className="pt-merge-row">
                    <select className="field field-select" value={mergeId} onChange={(e) => setMergeId(e.target.value)} aria-label="Person to merge">
                        <option value="">Choose a person…</option>
                        {others.map((o) => (
                            <option key={o.id} value={o.id}>{o.name ?? 'Unnamed'} · {o.photoIds.length} photos</option>
                        ))}
                    </select>
                    <button type="button" className="btn" disabled={!mergeId} onClick={() => { mergePeople(mergeId, person.id); setMergeId(''); }}>Merge</button>
                </div>
            </div>
        </div>
    );
};
