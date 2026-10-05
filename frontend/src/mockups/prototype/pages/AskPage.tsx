import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Search as MagnifyingGlassIcon, Plus as PlusIcon } from 'lucide-react';
import { useStore } from '../store';
import { Swatch } from '../components/bits';
import PhotoGrid from '../components/PhotoGrid';
import { get, post } from '../../../services/apiClient';
import { getAskState, loadMoreAsk, mapSearchResult as mapResult, runAskSearch, setAskQuery, setAskScroll, useAskState } from '../askSearchStore';
import type { Photo as BackendPhoto } from '../../../types/uiTypes';
import type { Photo } from '../types';

// "Save as album" collects matches up to what one album can hold (the server enforces the real limit).
const SAVE_AS_ALBUM_MAX = 10000;
const SAVE_PAGE = 500;

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
    const ask = useAskState();
    const { query, results, total, hasMore, rankedWindow, searching, loadingMore, indexBuilding } = ask;
    const [saving, setSaving] = useState(false);
    const sentinelRef = useRef<HTMLDivElement>(null);
    const setQuery = setAskQuery;
    const runSearch = runAskSearch;
    const restoredScroll = useRef(ask.scrollY).current;

    // Coming back to Ask: put the scroll position back, and remember it again on the way out. The
    // search itself (running or finished) lives in askSearchStore and is untouched by leaving.
    useEffect(() => {
        if (restoredScroll > 0) requestAnimationFrame(() => window.scrollTo(0, restoredScroll));
        return () => setAskScroll(window.scrollY);
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    // Search results aren't part of the gallery's paginated photo list, so the
    // viewer can't resolve them by id unless they're registered here too --
    // otherwise clicking a result flashes the viewer open and immediately
    // closed (its resync effect finds no matching photo). See registerPhotos.
    useEffect(() => {
        registerPhotos(results);
    }, [results, registerPhotos]);

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
        if (incoming.trim() === getAskState().activeQuery) return;   // this search is already showing (or running)
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

    // Infinite scroll: load the next page when the bottom sentinel comes into view.
    useEffect(() => {
        const node = sentinelRef.current;
        if (!node || !hasMore) return undefined;
        const observer = new IntersectionObserver((entries) => {
            if (entries.some((e) => e.isIntersecting)) loadMoreAsk();
        }, { rootMargin: '600px' });
        observer.observe(node);
        return () => observer.disconnect();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [hasMore, results.length, loadingMore, searching]);

    // Saves the matches (as many as one album can hold), not just the pages scrolled so far.
    const saveAsAlbum = () => {
        if (saving) return;
        setSaving(true);
        void (async () => {
            try {
                const id = await createAlbum(query.trim() || 'Saved search');
                if (!id) return;
                const q = ask.activeQuery || query.trim();
                const names: string[] = results.map((r) => r.id);
                let more = hasMore;
                while (more && names.length < SAVE_AS_ALBUM_MAX) {
                    const res = await get<{ photos?: BackendPhoto[]; hasMore?: boolean }>(
                        `/photos/search?q=${encodeURIComponent(q)}&offset=${names.length}&limit=${SAVE_PAGE}`,
                    );
                    const batch = (res?.photos ?? []).map((p) => p.filename);
                    if (!batch.length) break;
                    names.push(...batch);
                    more = Boolean(res?.hasMore);
                }
                const unique = Array.from(new Set(names)).slice(0, SAVE_AS_ALBUM_MAX);
                for (let i = 0; i < unique.length; i += 2000) addPhotosToAlbum(id, unique.slice(i, i + 2000));
                navigate('albums', { albumId: id });
            } finally {
                setSaving(false);
            }
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
                    <button type="button" className="btn mock-cta" onClick={saveAsAlbum} disabled={saving}>
                        <PlusIcon className="toolbar-icon" /> {saving ? 'Saving…' : 'Save as album'}
                    </button>
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
                    {searching && (
                        <div className="pt-ask-progress" role="status" aria-live="polite">
                            <div className="pt-index-bar-track" aria-hidden="true"><div className="pt-index-bar-fill" /></div>
                        </div>
                    )}
                    <div className="pt-menu-label">
                        {searching
                            ? (indexBuilding ? 'Preparing your library for search — this can take a minute…' : 'Searching…')
                            : `${total.toLocaleString()} result${total === 1 ? '' : 's'}`}
                    </div>
                    {!searching && rankedWindow !== null && total > rankedWindow && (
                        <p className="pt-page-sub">Best matches first; after the top {rankedWindow.toLocaleString()} the rest are newest first.</p>
                    )}
                    {indexBuilding && !searching && (
                        <p className="pt-page-sub">Your library’s search is still being prepared — try again in a minute.</p>
                    )}
                    <PhotoGrid photos={results} emptyHint={searching ? '' : 'No photos match that search.'} />
                    {hasMore && <div ref={sentinelRef} className="pt-scroll-sentinel" aria-hidden="true" />}
                    {loadingMore && <p className="pt-page-sub">Loading more…</p>}
                </>
            )}
        </div>
    );
};

export default AskPage;
