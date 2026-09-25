import React, { useEffect, useMemo, useRef, useState } from 'react';
import { MagnifyingGlassIcon, PlusIcon } from '@heroicons/react/24/outline';
import { useStore } from '../store';
import { Swatch } from '../components/bits';
import PhotoGrid from '../components/PhotoGrid';
import { get } from '../../../services/apiClient';
import type { Photo as BackendPhoto } from '../../../types/uiTypes';
import type { Photo } from '../types';

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

const mapResult = (b: BackendPhoto): Photo => {
    const iso = b.captureDate || b.uploadDate || null;
    const d = iso ? new Date(iso) : null;
    const valid = d && !Number.isNaN(d.getTime()) ? d : null;
    return {
        id: b.filename,
        filename: b.filename,
        swatch: 's1',
        dateLabel: valid ? `${MONTHS[valid.getMonth()]} ${valid.getDate()}, ${valid.getFullYear()}` : '',
        year: valid ? valid.getFullYear() : 0,
        rating: b.rating ?? 0,
        liked: Boolean(b.liked),
        likes: b.likes,
        placeId: null,
        personIds: (b.people ?? []).map((p) => p.personId),
        tags: b.tags ?? [],
        thumbnailUrl: b.thumbnailUrl,
        rotation: b.rotation,
        thumbnailRotation: b.thumbnailRotation,
        captureDate: iso,
    };
};

/**
 * Ask — one search box fused across people / places / things / years. Results
 * come from the backend search index (GET /photos/search); matched people and
 * places surface as cards, live typeahead disambiguates the trailing term, and
 * a search can be saved as an album.
 */
export const AskPage: React.FC = () => {
    const { people, places, route, createAlbum, addPhotosToAlbum, navigate } = useStore();
    const [query, setQuery] = useState(route.params.query ?? '');
    const [results, setResults] = useState<Photo[]>([]);
    const [searching, setSearching] = useState(false);
    const seqRef = useRef(0);

    useEffect(() => {
        if (route.params.query !== undefined) setQuery(route.params.query);
    }, [route.params.query]);

    // Debounced server search.
    useEffect(() => {
        const trimmed = query.trim();
        if (!trimmed) {
            setResults([]);
            setSearching(false);
            return;
        }
        const seq = ++seqRef.current;
        setSearching(true);
        const handle = window.setTimeout(async () => {
            try {
                const res = await get<{ photos?: BackendPhoto[] }>(`/photos/search?q=${encodeURIComponent(trimmed)}&offset=0&limit=200`);
                if (seq !== seqRef.current) return;
                setResults(Array.isArray(res?.photos) ? res.photos.map(mapResult) : []);
            } catch {
                if (seq === seqRef.current) setResults([]);
            } finally {
                if (seq === seqRef.current) setSearching(false);
            }
        }, 300);
        return () => window.clearTimeout(handle);
    }, [query]);

    const tokens = useMemo(() => query.toLowerCase().split(/\s+/).filter(Boolean), [query]);
    const lastToken = tokens.length ? tokens[tokens.length - 1] : '';

    const matchedPeople = useMemo(
        () => people.filter((pp) => pp.name && tokens.some((tk) => pp.name!.toLowerCase().includes(tk))),
        [people, tokens],
    );
    const matchedPlaces = useMemo(
        () => places.filter((pl) => tokens.some((tk) => pl.name.toLowerCase().includes(tk))),
        [places, tokens],
    );

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
        void (async () => {
            const id = await createAlbum(query.trim() || 'Saved search');
            if (!id) return;
            addPhotosToAlbum(id, results.map((r) => r.id));
            navigate('albums', { albumId: id });
        })();
    };

    const hasQuery = query.trim().length > 0;

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

            {!hasQuery ? (
                <p className="pt-grid-empty">Start typing to search across everything at once.</p>
            ) : (
                <>
                    {(matchedPeople.length > 0 || matchedPlaces.length > 0) && (
                        <div className="pt-match-cards">
                            {matchedPeople.map((p) => (
                                <div key={p.id} className="card-glass pt-match-card" onClick={() => navigate('person', { personId: p.id })} role="button" tabIndex={0}>
                                    <Swatch swatch={p.swatch} className="pt-match-face" />
                                    <div><b>{p.name}</b><span>{p.faceCount ?? 0} photos</span></div>
                                </div>
                            ))}
                            {matchedPlaces.map((pl) => (
                                <div key={pl.id} className="card-glass pt-match-card" onClick={() => navigate('ask', { query: pl.name })} role="button" tabIndex={0}>
                                    <Swatch swatch={pl.swatch} className="pt-match-face" />
                                    <div><b>{pl.name}</b><span>place</span></div>
                                </div>
                            ))}
                        </div>
                    )}
                    <div className="pt-menu-label">{searching ? 'Searching…' : `${results.length} result${results.length === 1 ? '' : 's'}`}</div>
                    <PhotoGrid photos={results} emptyHint={searching ? 'Searching…' : 'No photos match that search.'} />
                </>
            )}
        </div>
    );
};

export default AskPage;
