import { useSyncExternalStore } from 'react';
import { get } from '../../services/apiClient';
import { getCachedMediaToken, thumbnailUrlForBlob } from '../../services/mediaToken';
import { getActiveLibraryFromToken } from '../../services/passwordAuthClient';
import { reportIndexBuilding } from '../../services/indexBuilding';
import type { Photo as BackendPhoto } from '../../types/uiTypes';
import type { Photo } from './types';

/**
 * The Ask page's search lives here, outside React, so it keeps running (and keeps its results) when the
 * user navigates to another page: a search started on Ask finishes in the background and is waiting
 * when they come back, along with the pages already scrolled through and the scroll position. State is
 * per library; a different account/library starts empty.
 */
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

export const mapSearchResult = (b: BackendPhoto): Photo => {
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
        // Token mode: the server sends blob names and the browser builds the (cacheable) URLs.
        thumbnailUrl: (b.thumbnailBlob ? thumbnailUrlForBlob(b.thumbnailBlob) : '') || b.thumbnailUrl,
        rotation: b.rotation,
        thumbnailRotation: b.thumbnailRotation,
        captureDate: iso,
    };
};

// Results arrive a screenful at a time and keep loading as the user scrolls.
export const SEARCH_PAGE_LIMIT = 120;
// A new library's search database is built on the server; poll for up to ~2 minutes.
const SEARCH_BUILD_RETRIES = 12;
const SEARCH_BUILD_RETRY_MS = 10000;

export interface AskState {
    library: string;
    /** Text in the search box (may be ahead of the search that last ran). */
    query: string;
    /** The query the results below belong to. */
    activeQuery: string;
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
    library: library(), query: '', activeQuery: '', results: [], total: 0, hasMore: false, rankedWindow: null,
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
        publish({ query, activeQuery: '', results: [], total: 0, hasMore: false, rankedWindow: null,
            searching: false, loadingMore: false, indexBuilding: false });
        return;
    }
    publish({ query });
};

export const setAskScroll = (scrollY: number): void => { state = { ...state, scrollY }; };

export const runAskSearch = (queryOverride?: string): void => {
    const trimmed = (queryOverride ?? getAskState().query).trim();
    const mine = ++seq;
    if (!trimmed) {
        setAskQuery('');
        return;
    }
    publish({ query: queryOverride ?? state.query, activeQuery: trimmed, searching: true, loadingMore: false,
        indexBuilding: false, hasMore: false });
    void (async () => {
        // If the library's search database is still being built the server says so (searchIndexBuilding)
        // and we retry for a while instead of showing a misleading empty result.
        for (let attempt = 0; attempt < SEARCH_BUILD_RETRIES; attempt += 1) {
            try {
                const res = await get<{ photos?: BackendPhoto[]; searchIndexBuilding?: boolean; total?: number; hasMore?: boolean; rankedWindow?: number }>(
                    `/photos/search?q=${encodeURIComponent(trimmed)}&offset=0&limit=${SEARCH_PAGE_LIMIT}${getCachedMediaToken() ? '&directMedia=1' : ''}`,
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
                `/photos/search?q=${encodeURIComponent(q)}&offset=${state.results.length}&limit=${SEARCH_PAGE_LIMIT}${getCachedMediaToken() ? '&directMedia=1' : ''}`,
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
