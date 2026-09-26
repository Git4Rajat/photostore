import React, { useEffect, useMemo, useRef, useState } from 'react';
import { MagnifyingGlassIcon, PlusIcon } from '@heroicons/react/24/outline';
import { useStore } from '../store';
import { Swatch } from '../components/bits';
import PhotoGrid from '../components/PhotoGrid';
import { get, post } from '../../../services/apiClient';
import { runLocalSemanticSearch } from '../../../services/localSemanticSearch';
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
 * Ask — one search box fused across people / places / things / years. Results
 * come from the full client-side search the legacy gallery built (lexical +
 * semantic CLIP tier over a locally-cached index; see localSemanticSearch.ts),
 * which avoids the slow server-side /photos/search full-scan for the common
 * case. When the local index isn't available (e.g. never built server-side, or
 * a brand-new library) it falls back to that server endpoint and tells the user
 * the search is running on the server and will take longer. Matched people and
 * places surface as cards, live typeahead disambiguates the trailing term, and
 * a search can be saved as an album.
 */
export const AskPage: React.FC = () => {
    const { people, places, route, createAlbum, addPhotosToAlbum, registerPhotos, navigate } = useStore();
    const [query, setQuery] = useState(route.params.query ?? '');
    const [results, setResults] = useState<Photo[]>([]);
    const [searching, setSearching] = useState(false);
    // True once a search has been routed to the slower server endpoint (the
    // local index couldn't answer it) -- surfaced in the UI so the user knows
    // why this particular search is taking longer.
    const [usingBackend, setUsingBackend] = useState(false);
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
            setUsingBackend(false);
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
            setUsingBackend(false);
            return;
        }
        setSearching(true);
        setUsingBackend(false);
        void (async () => {
            // Primary path: the full client-side search (lexical + semantic
            // CLIP tier) over the locally-cached index. Returns null when the
            // local index can't answer -- either it isn't available yet or it
            // scored nothing, both of which fall through to the server below.
            let local: Awaited<ReturnType<typeof runLocalSemanticSearch>>;
            try {
                local = await runLocalSemanticSearch(trimmed, 0, SEARCH_PAGE_LIMIT, null, null);
            } catch {
                local = null;
            }
            if (seq !== seqRef.current) return;

            if (local) {
                try {
                    const photos = await resolvePhotos(local.filenames);
                    if (seq !== seqRef.current) return;
                    setResults(photos);
                } catch {
                    if (seq === seqRef.current) setResults([]);
                } finally {
                    if (seq === seqRef.current) setSearching(false);
                }
                return;
            }

            // Fallback: the slower server-side full-scan search. Flag it so the
            // UI can tell the user this search is running on the server.
            setUsingBackend(true);
            try {
                const res = await get<{ photos?: BackendPhoto[] }>(`/photos/search?q=${encodeURIComponent(trimmed)}&offset=0&limit=${SEARCH_PAGE_LIMIT}`);
                if (seq !== seqRef.current) return;
                setResults(Array.isArray(res?.photos) ? res.photos.map(mapResult) : []);
            } catch {
                if (seq === seqRef.current) setResults([]);
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
                    <div className="pt-menu-label">
                        {searching
                            ? (usingBackend ? 'Searching on the server (this can take longer)…' : 'Searching…')
                            : `${results.length} result${results.length === 1 ? '' : 's'}`}
                    </div>
                    {usingBackend && !searching && (
                        <p className="pt-page-sub">Searched on the server — the fast on-device index wasn’t available for this query.</p>
                    )}
                    <PhotoGrid photos={results} emptyHint={searching ? '' : 'No photos match that search.'} />
                </>
            )}
        </div>
    );
};

export default AskPage;
