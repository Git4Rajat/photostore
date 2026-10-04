import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Search as MagnifyingGlassIcon, Plus as PlusIcon } from 'lucide-react';
import { useStore } from '../store';
import { Swatch } from '../components/bits';
import PhotoGrid from '../components/PhotoGrid';
import { get, post } from '../../../services/apiClient';
import { enqueueBackgroundRequest } from '../../../services/backgroundRequestQueue';
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

const SEARCH_PAGE_LIMIT = 200;
// A new library's search database is built on the server; poll for up to ~2 minutes.
const SEARCH_BUILD_RETRIES = 12;
const SEARCH_BUILD_RETRY_MS = 10000;

// The local search scores filenames; turn them back into full photo records
// (thumbnails, ratings, dates, ...) via a point-lookup, preserving the local
// score order for whatever resolves (lookup-batch can drop a filename it can't
// resolve, e.g. one deleted between scoring and lookup).
const resolvePhotos = async (filenames: string[]): Promise<Photo[]> => {
    if (filenames.length === 0) return [];
    const response = await post<{ photos?: BackendPhoto[] }>('/api/photos/lookup-batch', { filenames });
    const byFilename = new Map((response?.photos ?? []).map((p) => [p.filename, p]));
    return filenames
        .map((f) => byFilename.get(f))
        .filter((p): p is BackendPhoto => Boolean(p))
        .map(mapResult);
};

/**
 * Ask — one search box fused across people / places / things / years. Search
 * runs entirely on the backend (/photos/search over a per-library SQLite
 * full-text database on the server's local disk); the browser downloads no
 * search index. A brand-new library whose database is still being built gets a
 * "preparing" notice and is retried automatically. Matched people and places
 * surface as cards, live typeahead disambiguates the trailing term, and a
 * search can be saved as an album.
 */
export const AskPage: React.FC = () => {
    const { people, places, route, createAlbum, addPhotosToAlbum, registerPhotos, navigate } = useStore();
    const [query, setQuery] = useState(route.params.query ?? '');
    const [results, setResults] = useState<Photo[]>([]);
    const [searching, setSearching] = useState(false);
    // True while the server is still building this library's search database
    // (new library / first search after an upgrade); searches retry automatically.
    const [indexBuilding, setIndexBuilding] = useState(false);
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
            setIndexBuilding(false);
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
            setIndexBuilding(false);
            return;
        }
        setSearching(true);
        setIndexBuilding(false);
        void (async () => {
            // Backend search. If the library's search database is still being
            // built the server says so (searchIndexBuilding) and we retry for a
            // while instead of showing a misleading empty result.
            for (let attempt = 0; attempt < SEARCH_BUILD_RETRIES; attempt += 1) {
                try {
                    const res = await get<{ photos?: BackendPhoto[]; searchIndexBuilding?: boolean }>(
                        `/photos/search?q=${encodeURIComponent(trimmed)}&offset=0&limit=${SEARCH_PAGE_LIMIT}`,
                    );
                    if (seq !== seqRef.current) return;
                    if (res?.searchIndexBuilding && attempt < SEARCH_BUILD_RETRIES - 1) {
                        setIndexBuilding(true);
                        await new Promise((resolve) => setTimeout(resolve, SEARCH_BUILD_RETRY_MS));
                        if (seq !== seqRef.current) return;
                        continue;
                    }
                    setIndexBuilding(Boolean(res?.searchIndexBuilding));
                    setResults(Array.isArray(res?.photos) ? res.photos.map(mapResult) : []);
                } catch {
                    if (seq === seqRef.current) setResults([]);
                }
                break;
            }
            if (seq === seqRef.current) setSearching(false);
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

            <form className="pt-ask-box" role="search" onSubmit={(e) => { e.preventDefault(); runSearch(); }}>
                <button type="submit" className="pt-ask-search" aria-label="Search">
                    <MagnifyingGlassIcon />
                </button>
                <input
                    className="pt-ask-input"
                    type="search"
                    enterKeyHint="search"
                    autoFocus
                    placeholder="Search people, places, things, years…"
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                />
                {results.length > 0 && (
                    <button type="button" className="btn mock-cta" onClick={saveAsAlbum}><PlusIcon className="toolbar-icon" /> Save as album</button>
                )}
            </form>

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
                            {matchedPeople.map((p) => {
                                const go = () => navigate('person', { personId: p.id });
                                return (
                                    <div key={p.id} className="card-glass pt-match-card" onClick={go} onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); } }} role="button" tabIndex={0}>
                                        <Swatch swatch={p.swatch} className="pt-match-face" />
                                        <div><b>{p.name}</b><span>{p.faceCount ?? 0} photos</span></div>
                                    </div>
                                );
                            })}
                            {matchedPlaces.map((pl) => {
                                const go = () => navigate('ask', { query: pl.name });
                                return (
                                    <div key={pl.id} className="card-glass pt-match-card" onClick={go} onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); } }} role="button" tabIndex={0}>
                                        <Swatch swatch={pl.swatch} className="pt-match-face" />
                                        <div><b>{pl.name}</b><span>place</span></div>
                                    </div>
                                );
                            })}
                        </div>
                    )}
                    <div className="pt-menu-label">
                        {searching
                            ? (indexBuilding ? 'Preparing your library for search — this can take a minute…' : 'Searching…')
                            : `${results.length} result${results.length === 1 ? '' : 's'}`}
                    </div>
                    {indexBuilding && !searching && (
                        <p className="pt-page-sub">Your library’s search is still being prepared — try again in a minute.</p>
                    )}
                    <PhotoGrid photos={results} emptyHint={searching ? '' : 'No photos match that search.'} />
                </>
            )}
        </div>
    );
};

export default AskPage;
