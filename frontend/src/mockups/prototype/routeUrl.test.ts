import { describe, expect, it } from 'vitest';
import { routeToSearch, searchToRoute } from './routeUrl';

describe('routeUrl', () => {
    it('gallery with no params produces no query string', () => {
        expect(routeToSearch({ page: 'gallery', params: {} })).toBe('');
    });

    it('a non-gallery page with a param round-trips', () => {
        const search = routeToSearch({ page: 'albums', params: { albumId: 'abc123' } });
        expect(search).toBe('?page=albums&albumId=abc123');
        expect(searchToRoute(search)).toEqual({ page: 'albums', params: { albumId: 'abc123' } });
    });

    it('empty/absent params are omitted, not encoded as empty strings', () => {
        const search = routeToSearch({ page: 'person', params: { personId: 'p1', query: '' } });
        expect(search).toBe('?page=person&personId=p1');
    });

    it('an unrecognized page in the URL falls back to gallery instead of crashing', () => {
        expect(searchToRoute('?page=not-a-real-page')).toEqual({ page: 'gallery', params: {} });
    });

    it('no query string at all resolves to plain gallery', () => {
        expect(searchToRoute('')).toEqual({ page: 'gallery', params: {} });
    });

    it('unknown query keys are ignored rather than polluting params', () => {
        expect(searchToRoute('?page=ask&query=guitar&utm_source=test')).toEqual({
            page: 'ask', params: { query: 'guitar' },
        });
    });

    it('every RouteParams key round-trips together', () => {
        const route = {
            page: 'tools' as const,
            params: { albumId: 'a', personId: 'p', query: 'q', placeId: 'pl', tag: 't', filenames: 'x.jpg,y.jpg', photo: 'z.jpg' },
        };
        expect(searchToRoute(routeToSearch(route))).toEqual(route);
    });
});
