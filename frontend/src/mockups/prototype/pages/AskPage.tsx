import React, { useEffect, useMemo, useState } from 'react';
import { MagnifyingGlassIcon, PlusIcon } from '@heroicons/react/24/outline';
import { useStore } from '../store';
import { Swatch } from '../components/bits';
import PhotoGrid from '../components/PhotoGrid';

/**
 * Ask — one search box fused across people / places / tags / year. Results
 * update as you type, matched people & places surface as cards, live typeahead
 * disambiguates the trailing term, and a search can be saved as an album.
 */
export const AskPage: React.FC = () => {
    const { photos, people, places, route, createAlbum, addPhotosToAlbum, navigate, toast } = useStore();
    const [query, setQuery] = useState(route.params.query ?? '');

    useEffect(() => {
        if (route.params.query !== undefined) setQuery(route.params.query);
    }, [route.params.query]);

    const tokens = useMemo(() => query.toLowerCase().split(/\s+/).filter(Boolean), [query]);

    const matchesToken = (photo: (typeof photos)[number], token: string): boolean => {
        if (photo.filename.toLowerCase().includes(token)) return true;
        if (String(photo.year) === token) return true;
        if (photo.tags.some((t) => t.includes(token))) return true;
        const place = places.find((pl) => pl.id === photo.placeId);
        if (place && place.name.toLowerCase().includes(token)) return true;
        const names = photo.personIds.map((id) => people.find((pp) => pp.id === id)?.name?.toLowerCase() ?? '');
        return names.some((n) => n && n.includes(token));
    };

    // Only tokens that match at least one facet constrain the results.
    const activeTokens = useMemo(
        () => tokens.filter((tk) => photos.some((p) => matchesToken(p, tk)) || people.some((pp) => pp.name?.toLowerCase().includes(tk)) || places.some((pl) => pl.name.toLowerCase().includes(tk))),
        // eslint-disable-next-line react-hooks/exhaustive-deps
        [tokens, photos, people, places],
    );

    const results = useMemo(() => {
        if (!activeTokens.length) return [];
        return photos.filter((p) => activeTokens.every((tk) => matchesToken(p, tk)));
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [activeTokens, photos]);

    const matchedPeople = useMemo(
        () => people.filter((pp) => pp.name && activeTokens.some((tk) => pp.name!.toLowerCase().includes(tk))),
        [people, activeTokens],
    );
    const matchedPlaces = useMemo(
        () => places.filter((pl) => activeTokens.some((tk) => pl.name.toLowerCase().includes(tk))),
        [places, activeTokens],
    );

    // Typeahead for the trailing term.
    const lastToken = tokens.length ? tokens[tokens.length - 1] : '';
    const suggestions = useMemo(() => {
        if (!lastToken) return [];
        const pool = [
            ...people.filter((p) => p.name).map((p) => ({ label: p.name as string, kind: 'person' })),
            ...places.map((p) => ({ label: p.name, kind: 'place' })),
        ];
        return pool.filter((s) => s.label.toLowerCase().startsWith(lastToken) && s.label.toLowerCase() !== lastToken).slice(0, 5);
    }, [lastToken, people, places]);

    const applySuggestion = (label: string) => {
        const prefix = query.slice(0, query.length - lastToken.length);
        setQuery(`${prefix}${label} `);
    };

    const saveAsAlbum = () => {
        const id = createAlbum(query.trim() || 'Saved search');
        addPhotosToAlbum(id, results.map((r) => r.id));
        navigate('albums', { albumId: id });
    };

    return (
        <div>
            <div className="pt-toolbar">
                <div>
                    <h1 className="pt-page-title">Ask</h1>
                    <p className="pt-page-sub">Try “priya beach”, “lisbon 2022”, or a name</p>
                </div>
            </div>

            <div className="pt-ask-box">
                <MagnifyingGlassIcon />
                <input
                    className="pt-ask-input"
                    autoFocus
                    placeholder="Search people, places, things, years…"
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                />
                {results.length > 0 && (
                    <button type="button" className="btn mock-cta" onClick={saveAsAlbum}><PlusIcon className="toolbar-icon" /> Save as album</button>
                )}
            </div>

            {suggestions.length > 0 && (
                <div className="pt-suggest-row">
                    {suggestions.map((s) => (
                        <button key={s.label} type="button" className="pt-chip" onClick={() => applySuggestion(s.label)}>
                            {s.label} · {s.kind}
                        </button>
                    ))}
                </div>
            )}

            {activeTokens.length === 0 ? (
                <p className="pt-grid-empty">Start typing to search across everything at once.</p>
            ) : (
                <>
                    {(matchedPeople.length > 0 || matchedPlaces.length > 0) && (
                        <div className="pt-match-cards">
                            {matchedPeople.map((p) => (
                                <div key={p.id} className="card-glass pt-match-card" onClick={() => navigate('person', { personId: p.id })} role="button" tabIndex={0}>
                                    <Swatch swatch={p.swatch} className="pt-match-face" />
                                    <div><b>{p.name}</b><span>{p.photoIds.length} photos</span></div>
                                </div>
                            ))}
                            {matchedPlaces.map((pl) => (
                                <div key={pl.id} className="card-glass pt-match-card">
                                    <Swatch swatch={pl.swatch} className="pt-match-face" />
                                    <div><b>{pl.name}</b><span>place</span></div>
                                </div>
                            ))}
                        </div>
                    )}
                    <div className="pt-menu-label">{results.length} result{results.length === 1 ? '' : 's'}</div>
                    <PhotoGrid photos={results} emptyHint="No photos match that search." />
                </>
            )}
        </div>
    );
};

export default AskPage;
