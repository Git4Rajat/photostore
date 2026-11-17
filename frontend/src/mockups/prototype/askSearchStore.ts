import { useSyncExternalStore } from 'react';
import { get } from '../../services/apiClient';
import { getCachedMediaToken } from '../../services/mediaToken';
import { getActiveLibraryFromToken } from '../../services/passwordAuthClient';
import { reportIndexBuilding } from '../../services/indexBuilding';
import type { Photo as BackendPhoto } from '../../types/uiTypes';
import type { Photo } from './types';
import { mapPhoto } from './store';

/**
 * The Ask page's search lives here, outside React, so it keeps running (and keeps its results) when the
 * user navigates to another page: a search started on Ask finishes in the background and is waiting
 * when they come back, along with the pages already scrolled through and the scroll position. State is
 * per library; a different account/library starts empty.
 */
// The same record the Gallery builds (thumbnail, sharp grid image, Live Photo
// clip, rotation), so search results load and look exactly like Photos.
const mapSearchResult = (b: BackendPhoto): Photo => mapPhoto(b);

// Results arrive a screenful at a time and keep loading as the user scrolls.
const SEARCH_PAGE_LIMIT = 120;
// A new library's search database is built on the server; poll for up to ~2 minutes.
const SEARCH_BUILD_RETRIES = 12;
const SEARCH_BUILD_RETRY_MS = 10000;

export interface AskState {
    library: string;
    /** Text in the search box (may be ahead of the search that last ran). */
    query: string;
    /** The query the results below belong to. */
    activeQuery: string;
    /** Set when the results are an Explore place's exact members (a place filter, not a text search). */
    activePlace: string;
    results: Photo[];
    total: number;
    hasMore: boolean;
    rankedWindow: number | null;
    searching: boolean;
    loadingMore: boolean;
    indexBuilding: boolean;
    scrollY: number;
}

const library = () => getActiveLibraryFromToken() || '__default__';
const empty = (): AskState => ({
    library: library(), query: '', activeQuery: '', activePlace: '', results: [], total: 0, hasMore: false, rankedWindow: null,
    searching: false, loadingMore: false, indexBuilding: false, scrollY: 0,
});

let state: AskState = empty();
let seq = 0;
const listeners = new Set<() => void>();

const publish = (patch: Partial<AskState>) => {
    state = { ...state, ...patch };
    listeners.forEach((l) => l());
};

export const getAskState = (): AskState => {
    if (state.library !== library()) {          // signed in as someone else: never show their search
        seq += 1;
        state = empty();
    }
    return state;
};

const subscribe = (l: () => void) => { listeners.add(l); return () => { listeners.delete(l); }; };
export const useAskState = (): AskState => useSyncExternalStore(subscribe, getAskState, getAskState);

export const setAskQuery = (query: string): void => {
    if (!query.trim()) {
        // Cleared box: drop the results, and make sure an in-flight request can't repopulate them.
        seq += 1;
        reportIndexBuilding('search', false);
        publish({ query, activeQuery: '', activePlace: '', results: [], total: 0, hasMore: false, rankedWindow: null,
            searching: false, loadingMore: false, indexBuilding: false });
        return;
    }
    publish({ query });
};

export const setAskScroll = (scrollY: number): void => { state = { ...state, scrollY }; };

/** The request path for a search: a place filter when one is active, else the text query. */
export const searchPath = (q: string, place: string, offset: number, limit: number): string =>
    `/photos/search?${place ? `place=${encodeURIComponent(place)}` : `q=${encodeURIComponent(q)}`}&offset=${offset}&limit=${limit}${getCachedMediaToken() ? '&directMedia=1' : ''}`;

/** Show exactly the photos behind an Explore place card (same membership as its count). */
export const runAskPlace = (place: string): void => runAskSearch(place, place);

export const runAskSearch = (queryOverride?: string, place = ''): void => {
    const trimmed = (queryOverride ?? getAskState().query).trim();
    const mine = ++seq;
    if (!trimmed) {
        setAskQuery('');
        return;
    }
    publish({ query: queryOverride ?? state.query, activeQuery: trimmed, activePlace: place, searching: true, loadingMore: false,
        indexBuilding: false, hasMore: false });
    void (async () => {
        // If the library's search database is still being built the server says so (searchIndexBuilding)
        // and we retry for a while instead of showing a misleading empty result.
        for (let attempt = 0; attempt < SEARCH_BUILD_RETRIES; attempt += 1) {
            try {
                const res = await get<{ photos?: BackendPhoto[]; searchIndexBuilding?: boolean; total?: number; hasMore?: boolean; rankedWindow?: number }>(
                    searchPath(trimmed, place, 0, SEARCH_PAGE_LIMIT),
                );
                if (mine !== seq) return;
                if (res?.searchIndexBuilding && attempt < SEARCH_BUILD_RETRIES - 1) {
                    publish({ indexBuilding: true });
                    reportIndexBuilding('search', true);
                    await new Promise((resolve) => setTimeout(resolve, SEARCH_BUILD_RETRY_MS));
                    if (mine !== seq) return;
                    continue;
                }
                reportIndexBuilding('search', Boolean(res?.searchIndexBuilding));
                const first = Array.isArray(res?.photos) ? res.photos.map(mapSearchResult) : [];
                publish({
                    indexBuilding: Boolean(res?.searchIndexBuilding),
                    results: first,
                    total: typeof res?.total === 'number' ? res.total : first.length,
                    hasMore: Boolean(res?.hasMore),
                    rankedWindow: typeof res?.rankedWindow === 'number' ? res.rankedWindow : null,
                });
            } catch {
                if (mine === seq) publish({ results: [], total: 0, hasMore: false });
            }
            break;
        }
        if (mine === seq) publish({ searching: false });
    })();
};

/** Next page of the same query, appended; dropped if the user has searched again or cleared the box. */
export const loadMoreAsk = (): void => {
    const current = getAskState();
    if (current.loadingMore || current.searching || !current.hasMore || !current.activeQuery) return;
    const mine = seq;
    const q = current.activeQuery;
    publish({ loadingMore: true });
    void (async () => {
        try {
            const res = await get<{ photos?: BackendPhoto[]; total?: number; hasMore?: boolean }>(
                searchPath(q, current.activePlace, state.results.length, SEARCH_PAGE_LIMIT),
            );
            if (mine !== seq) return;
            const next = Array.isArray(res?.photos) ? res.photos.map(mapSearchResult) : [];
            const seen = new Set(state.results.map((p) => p.id));
            publish({
                results: [...state.results, ...next.filter((p) => !seen.has(p.id))],
                total: typeof res?.total === 'number' ? res.total : state.total,
                hasMore: Boolean(res?.hasMore) && next.length > 0,
            });
        } catch {
            if (mine === seq) publish({ hasMore: false });
        } finally {
            if (mine === seq) publish({ loadingMore: false });
        }
    })();
};

export const __resetAskForTests = (): void => { seq += 1; state = empty(); listeners.forEach((l) => l()); };
