import { beforeEach, describe, expect, it, vi } from 'vitest';
import { parseServerTiming, perf, requestKey, stripQuery } from './perf';

const rec = (over: Partial<Parameters<typeof perf.recordRequest>[0]> = {}) => ({
    method: 'GET', path: '/api/photos', status: 200, ok: true, ms: 50, bytes: 1000, rid: 'r', attempt: 0,
    view: 'gallery', at: performance.now(), ...over,
});

describe('perf helpers', () => {
    it('parses Server-Timing', () => {
        expect(parseServerTiming('app;dur=120, storage;dur=80;desc="3 calls"')).toEqual({ app: 120, storage: 80 });
        expect(parseServerTiming(undefined)).toEqual({});
    });

    it('keys requests independent of query order and never keeps the query when stripped', () => {
        expect(requestKey('get', '/a?b=1&a=2')).toBe(requestKey('GET', '/a?a=2&b=1'));
        expect(requestKey('post', '/x', { n: 1 })).not.toBe(requestKey('post', '/x', { n: 2 }));
        expect(stripQuery('https://acct.blob.core.windows.net/c/b.jpg?sig=SECRET')).toBe('https://acct.blob.core.windows.net/c/b.jpg');
    });
});

describe('perf collector', () => {
    beforeEach(() => {
        perf._reset();
        window.localStorage.removeItem('photostore.perf');
    });

    it('flags the same request completing twice inside the window as a duplicate', () => {
        const key = requestKey('GET', '/api/albums/index');
        perf.recordRequest(rec({ path: '/api/albums/index' }), key);
        perf.recordRequest(rec({ path: '/api/albums/index' }), key);
        const dups = perf._events().filter((e) => e.t === 'dup');
        expect(dups).toHaveLength(1);
        expect(dups[0]).toMatchObject({ kind: 'request', n: 2 });
        expect(perf.summary().duplicates[0]).toMatchObject({ n: 2 });
    });

    it('records slow, failed and retried requests but not every fast one', () => {
        perf.recordRequest(rec({ ms: 20, path: '/fast' }), 'k1');
        perf.recordRequest(rec({ ms: 2500, path: '/slow' }), 'k2');
        perf.recordRequest(rec({ ok: false, status: 500, path: '/bad' }), 'k3');
        perf.recordRequest(rec({ attempt: 2, path: '/retry' }), 'k4');
        const paths = perf._events().filter((e) => e.t === 'req').map((e) => e.path);
        expect(paths).toEqual(['/slow', '/bad', '/retry']);
    });

    it('coalesced GETs are visible but do not count as network requests', () => {
        perf.recordRequest(rec({ coalesced: true, ms: 0 }), 'k');
        expect(perf.summary().requests).toBe(0);
        expect(perf._events().some((e) => e.t === 'req' && e.coalesced)).toBe(true);
    });

    it('closes a view with its request totals when the view changes', () => {
        perf.setView('albums');
        perf.recordRequest(rec({ view: 'albums', ms: 100, bytes: 500 }), 'a');
        perf.recordRequest(rec({ view: 'albums', ms: 300, bytes: 700, path: '/b' }), 'b');
        perf.setView('people');
        const view = perf._events().find((e) => e.t === 'view');
        expect(view).toMatchObject({ name: 'albums', requests: 2, netMs: 400, bytes: 1200 });
    });

    it('flush posts events plus a summary and never throws when the sender fails', async () => {
        const sender = vi.fn().mockRejectedValue(new Error('offline'));
        perf.setSender(sender);
        perf.recordRequest(rec({ ms: 3000 }), 'k');
        expect(() => perf.flush()).not.toThrow();
        await Promise.resolve();
        const body = sender.mock.calls[0][0] as { session: string; events: Array<{ t: string }> };
        expect(body.events.map((e) => e.t)).toEqual(expect.arrayContaining(['req', 'summary']));
        expect(JSON.stringify(body)).not.toContain('sig=');
    });

    it('can be switched off', () => {
        window.localStorage.setItem('photostore.perf', 'off');
        perf.recordRequest(rec({ ms: 5000 }), 'k');
        expect(perf._events()).toHaveLength(0);
    });
});
