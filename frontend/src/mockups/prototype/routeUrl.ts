import type { PageId, Route, RouteParams } from './types';

/**
 * Translates the app's in-memory `Route` to/from the browser's URL query
 * string, so navigating in-app (albums, a person, a search, a deep-linked
 * photo) is reflected in the address bar and survives a refresh/back-button
 * instead of always bouncing back to Gallery. Lives on the root path (`/`)
 * only -- the few special unauthenticated deep-link pages (public album
 * share, invite accept, password reset, confirm-library-clean) match on
 * pathname, not query string, in PrototypeApp.tsx's isPublicAlbumPath() and
 * siblings, so this never collides with those.
 */
const PAGE_IDS: readonly PageId[] = [
    'ask', 'gallery', 'albums', 'people', 'person', 'explore', 'sharing', 'tools', 'trash', 'additional',
];

const isPageId = (value: string): value is PageId => (PAGE_IDS as readonly string[]).includes(value);

const PARAM_KEYS: readonly (keyof RouteParams)[] = [
    'albumId', 'personId', 'query', 'placeId', 'tag', 'filenames', 'photo',
];

export const routeToSearch = (route: Route): string => {
    const search = new URLSearchParams();
    if (route.page !== 'gallery') {
        search.set('page', route.page);
    }
    for (const key of PARAM_KEYS) {
        const value = route.params[key];
        if (value) {
            search.set(key, value);
        }
    }
    const qs = search.toString();
    return qs ? `?${qs}` : '';
};

export const searchToRoute = (search: string): Route => {
    const parsed = new URLSearchParams(search);
    const rawPage = parsed.get('page') || 'gallery';
    const page: PageId = isPageId(rawPage) ? rawPage : 'gallery';
    const params: RouteParams = {};
    for (const key of PARAM_KEYS) {
        const value = parsed.get(key);
        if (value) {
            params[key] = value;
        }
    }
    return { page, params };
};
