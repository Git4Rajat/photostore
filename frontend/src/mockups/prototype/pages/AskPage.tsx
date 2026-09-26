import React, { useEffect, useMemo, useRef, useState } from 'react';
import { MagnifyingGlassIcon, PlusIcon } from '@heroicons/react/24/outline';
import { useStore } from '../store';
import { Swatch } from '../components/bits';
import PhotoGrid from '../components/PhotoGrid';
import { get, post } from '../../../services/apiClient';
import { getLocalSearchIndex } from '../../../services/localSearchIndex';
import { runLocalSearch } from '../../../services/localLexicalSearch';
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

// Falls back to the browser's own locally-cached lexical search index (the
// same one the legacy gallery search used) when the backend's /photos/search
// endpoint is unavailable or errors -- a browser-only-processing deployment
// may not maintain a working server-side index at all, in which case Ask
// searching "doesn't work at all" without this. Lexical-only (no semantic/CLIP
// tier): that's a bonus layer the legacy fallback also treated as optional.
// Returns null to mean "no local fallback available" (caller keeps zero
// results), distinct from a real empty result set ([]).
const tryLocalSearch = async (queryText: string): Promise<Photo[] | null> => {
    try {
        const index = await getLocalSearchIndex();
        if (!index) return null;
        const { filenames, total } = runLocalSearch(index.rows, index.peopleNameIndex, queryText, 0, 200, null, null);
        if (total === 0) return null;
        if (filenames.length === 0) return [];
        const response = await post<{ photos?: BackendPhoto[] }>('/api/photos/lookup-batch', { filenames });
        const byFilename = new Map((response?.photos ?? []).map((p) => [p.filename, p]));
        return filenames.map((f) => byFilename.get(f)).filter((p): p is BackendPhoto => Boolean(p)).map(mapResult);
    } catch {
        return null;
    }
};

/**
 * Ask — one search box fused across people / places / things / years. Results
 * come from the backend search index (GET /photos/search), falling back to a
 * local in-browser index (tryLocalSearch) if that endpoint errors out. Matched
 * people and places surface as cards, live typeahead disambiguates the
 * trailing term, and a search can be saved as an album.
 */
export const AskPage: React.FC = () => {
    const { people, places, route, createAlbum, addPhotosToAlbum, registerPhotos, navigate } = useStore();
    const [query, setQuery] = useState(route.params.query ?? '');
    const [results, setResults] = useState<Photo[]>([]);
    const [searching, setSearching] = useState(false);
    const seqRef = useRef(0);

    // Search results aren't part of the gallery's paginated photo list, so the
    // viewer can't resolve them by id unless they're registered here too --
    // otherwise clicking a result flashes the viewer open and immediately
    // closed (its resync effect finds no matching photo). See registerPhotos.
    useEffect(() => {
        registerPhotos(results);
    }, [results, registerPhotos]);

    // Clear reactively as the user backspaces the box to empty (not just on
    // Enter/route navigation) -- bumps seqRef too, so an in-flight request
    // for whatever was just cleared can't land afterward and repopulate
    // results the user already watched disappear.
    useEffect(() => {
        if (!query.trim()) {
            seqRef.current += 1;
            setResults([]);
            setSearching(false);
        }
    }, [query]);

    const runSearch = (queryOverride?: string) => {
        const trimmed = (queryOverride ?? query).trim();
        // Bump the sequence even on a no-op/empty search so a response for a
        // *previous* in-flight request (e.g. one just superseded by the user
        // clearing the box) can never land after the guard below already
        // decided this search doesn't need one -- otherwise a late response
        // could repopulate results/re-toggle "Searching…" after the UI had
        // already moved on.
        const seq = ++seqRef.current;
        if (!trimmed) {
            setResults([]);
            setSearching(false);
            return;
        }
        setSearching(true);
        void (async () => {
            try {
                const res = await get<{ photos?: BackendPhoto[] }>(`/photos/search?q=${encodeURIComponent(trimmed)}&offset=0&limit=200`);
                if (seq !== seqRef.current) return;
                setResults(Array.isArray(res?.photos) ? res.photos.map(mapResult) : []);
            } catch {
                if (seq !== seqRef.current) return;
                const local = await tryLocalSearch(trimmed);
                if (seq === seqRef.current) setResults(local ?? []);
            } finally {
                if (seq === seqRef.current) setSearching(false);
            }
        })();
    };

    // Single effect drives both the input box and the actual search off the
    // same incoming route param, passing it straight to runSearch instead of
    // relying on `query` state -- two separate effects both keyed on
    // route.params.query (one calling setQuery, the other calling runSearch)
    // raced: the search effect ran first and read `query` from *before*
    // setQuery's update had committed, so e.g. clicking a place chip updated
    // the input box to the new place name but actually searched the
    // previous text.
    useEffect(() => {
        const incoming = route.params.query;
        if (incoming === undefined) return;
        setQuery(incoming);
        runSearch(incoming);
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [route.params.query]);

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
                    onKeyDown={(e) => {
                        if (e.key === 'Enter') runSearch();
                    }}
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
                    <PhotoGrid photos={results} emptyHint={searching ? '' : 'No photos match that search.'} />
                </>
            )}
        </div>
    );
};

export default AskPage;
