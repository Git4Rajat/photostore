/**
 * Client-side performance instrumentation.
 *
 * Answers three questions about a session, without needing DevTools open:
 *   1. What is slow?          -> per-request timings (split into server / storage /
 *                                network+queue via Server-Timing), spans for the
 *                                index preloads, long tasks, web vitals.
 *   2. What is chatty?        -> requests per view and per endpoint, retries,
 *                                coalesced GETs.
 *   3. What is duplicated?    -> identical requests repeated inside a window, and
 *                                the same blob fetched more than once (e.g. one
 *                                thumbnail through two different SAS URLs).
 *
 * Events are buffered, summarised per view, and POSTed to /api/perf/client every
 * 20 s (and on page hide), where they are logged as `PERF event=client_*` lines
 * joinable with the backend's own on `rid` (request id) and `sess`. Everything
 * is also reachable from the console: `photostorePerf.report()`.
 *
 * Off switch: localStorage `photostore.perf = off`. Query strings are NEVER
 * recorded (they carry SAS tokens).
 */

export type PerfEvent = Record<string, string | number | boolean | undefined> & { t: string };

interface RequestRecord {
    method: string;
    path: string;
    status: number;
    ok: boolean;
    ms: number;
    bytes: number;
    rid: string;
    attempt: number;
    coalesced?: boolean;
    serverMs?: number;
    storageMs?: number;
    view: string;
    at: number;
}

interface ViewStats {
    name: string;
    startedAt: number;
    requests: number;
    netMs: number;
    bytes: number;
    dups: number;
    resources: number;
    cachedResources: number;
    longTasks: number;
    longTaskMs: number;
}

const DUP_WINDOW_MS = 15_000;
const FLUSH_INTERVAL_MS = 20_000;
const MAX_BUFFER = 600;
const SLOW_REQUEST_MS = 800;

const enabled = (): boolean => {
    try {
        return typeof window !== 'undefined' && window.localStorage.getItem('photostore.perf') !== 'off';
    } catch {
        return typeof window !== 'undefined';
    }
};

const now = (): number => (typeof performance !== 'undefined' ? performance.now() : Date.now());

export const stripQuery = (url: string): string => url.split('?', 1)[0].split('#', 1)[0];

/** Stable key for "is this the same request": method + path + sorted query. */
export const requestKey = (method: string, url: string, body?: unknown): string => {
    const [path, query = ''] = url.split('?', 2);
    const sorted = query ? query.split('&').sort().join('&') : '';
    let bodyKey = '';
    if (body !== undefined && body !== null) {
        try {
            const text = typeof body === 'string' ? body : JSON.stringify(body);
            bodyKey = `|${text.length > 2048 ? `${text.length}:${text.slice(0, 2048)}` : text}`;
        } catch {
            bodyKey = '|?';
        }
    }
    return `${method.toUpperCase()} ${path}${sorted ? `?${sorted}` : ''}${bodyKey}`;
};

/** Server-Timing: `app;dur=12, storage;dur=8;desc="3 calls"` -> {app:12, storage:8}. */
export const parseServerTiming = (header: string | undefined | null): Record<string, number> => {
    const out: Record<string, number> = {};
    if (!header) return out;
    for (const part of header.split(',')) {
        const [name, ...params] = part.trim().split(';');
        const dur = params.map((p) => p.trim()).find((p) => p.startsWith('dur='));
        if (name && dur) {
            const value = Number(dur.slice(4));
            if (Number.isFinite(value)) out[name] = value;
        }
    }
    return out;
};

const percentile = (values: number[], p: number): number => {
    if (!values.length) return 0;
    const sorted = [...values].sort((a, b) => a - b);
    return sorted[Math.min(sorted.length - 1, Math.floor((p / 100) * sorted.length))];
};

class PerfCollector {
    readonly session = Math.random().toString(36).slice(2, 6) + Math.random().toString(36).slice(2, 6);
    private counter = 0;
    private view = 'boot';
    private viewStats: ViewStats = this.newView('boot');
    private buffer: PerfEvent[] = [];
    private requests: RequestRecord[] = [];
    private recentByKey = new Map<string, number[]>();
    private reportedDup = new Map<string, number>();
    private blobFetches = new Map<string, { queries: Set<string>; network: number; cached: number; reported?: boolean }>();
    private sender: ((body: unknown) => Promise<unknown>) | null = null;
    private timer: ReturnType<typeof setInterval> | null = null;
    private started = false;
    private resourceAgg = new Map<string, { n: number; cached: number; ms: number; bytes: number }>();

    private newView(name: string): ViewStats {
        return {
            name, startedAt: now(), requests: 0, netMs: 0, bytes: 0, dups: 0,
            resources: 0, cachedResources: 0, longTasks: 0, longTaskMs: 0,
        };
    }

    get currentView(): string {
        return this.view;
    }

    nextRequestId(): string {
        this.counter += 1;
        return `${this.session.slice(0, 4)}-${this.counter.toString(36)}`;
    }

    setSender(sender: (body: unknown) => Promise<unknown>): void {
        this.sender = sender;
        this.start();
    }

    private push(event: PerfEvent): void {
        if (this.buffer.length >= MAX_BUFFER) this.buffer.shift();
        this.buffer.push(event);
    }

    // --- requests -------------------------------------------------------------

    recordRequest(record: RequestRecord, key: string): void {
        if (!enabled()) return;
        if (!record.coalesced) {
            this.requests.push(record);
            if (this.requests.length > 2000) this.requests.shift();
            this.viewStats.requests += 1;
            this.viewStats.netMs += record.ms;
            this.viewStats.bytes += record.bytes;
        }
        const slow = record.ms >= SLOW_REQUEST_MS || !record.ok || record.attempt > 0 || record.coalesced;
        if (slow) {
            this.push({
                t: 'req', method: record.method, path: record.path, status: record.status, ok: record.ok,
                ms: Math.round(record.ms), bytes: record.bytes, rid: record.rid, attempt: record.attempt,
                coalesced: record.coalesced || undefined, serverMs: record.serverMs, storageMs: record.storageMs,
                view: record.view,
            });
        }
        // Duplicate = same request completed again inside the window.
        const stamps = (this.recentByKey.get(key) || []).filter((at) => record.at - at < DUP_WINDOW_MS);
        stamps.push(record.at);
        this.recentByKey.set(key, stamps);
        if (this.recentByKey.size > 1500) {
            const oldest = this.recentByKey.keys().next().value;
            if (oldest !== undefined) this.recentByKey.delete(oldest);
        }
        if (stamps.length >= 2) {
            const prev = this.reportedDup.get(key) || 0;
            if (stamps.length > prev) {
                this.reportedDup.set(key, stamps.length);
                this.viewStats.dups += 1;
                this.push({
                    t: 'dup', kind: 'request', key: stripQuery(key).slice(0, 140), n: stamps.length,
                    windowMs: DUP_WINDOW_MS, view: record.view,
                });
            }
        }
    }

    // --- spans (index preloads and other multi-step client work) -----------------

    span<T>(name: string, fn: () => Promise<T> | T, extra?: () => Record<string, string | number | boolean | undefined>): Promise<T> {
        const start = now();
        const done = (): void => {
            if (!enabled()) return;
            this.push({ t: 'span', name, ms: Math.round(now() - start), view: this.view, ...(extra ? extra() : {}) });
        };
        try {
            const result = fn();
            if (result && typeof (result as Promise<T>).then === 'function') {
                return (result as Promise<T>).then(
                    (value) => { done(); return value; },
                    (error) => { done(); throw error; },
                );
            }
            done();
            return Promise.resolve(result as T);
        } catch (error) {
            done();
            throw error;
        }
    }

    /** Manual span for code that already has start/end (e.g. between two awaits). */
    recordSpan(name: string, ms: number, extra: Record<string, string | number | boolean | undefined> = {}): void {
        if (enabled()) this.push({ t: 'span', name, ms: Math.round(ms), view: this.view, ...extra });
    }

    // --- views --------------------------------------------------------------------

    setView(name: string): void {
        if (!enabled() || name === this.view) return;
        this.closeView();
        this.view = name;
        this.viewStats = this.newView(name);
    }

    private closeView(): void {
        const v = this.viewStats;
        if (v.requests || v.resources || v.longTasks) {
            this.push({
                t: 'view', name: v.name, ms: Math.round(now() - v.startedAt), requests: v.requests,
                netMs: Math.round(v.netMs), dups: v.dups, bytes: v.bytes, resources: v.resources,
                cachedResources: v.cachedResources, longTasks: v.longTasks, longTaskMs: Math.round(v.longTaskMs),
            });
        }
    }

    // --- browser observers --------------------------------------------------------

    private observeResources(entry: PerformanceResourceTiming): void {
        let url: URL;
        try {
            url = new URL(entry.name, window.location.href);
        } catch {
            return;
        }
        // XHR/fetch to the API are already measured (with more detail) by the HTTP client.
        if (entry.initiatorType === 'xmlhttprequest') return;
        const host = url.hostname;
        const kind = host.includes('.blob.') ? 'blob' : (entry.initiatorType === 'fetch' ? 'fetch' : entry.initiatorType || 'other');
        if (kind === 'fetch' && !host.includes('.blob.')) return;
        // Cross-origin entries without Timing-Allow-Origin report transferSize 0 even when the
        // bytes came off the network, so also treat a near-instant, connection-free load as a
        // browser-cache hit.
        const noConnection = entry.connectEnd - entry.connectStart <= 1 && entry.domainLookupEnd - entry.domainLookupStart <= 1;
        const cached = (entry.transferSize === 0 && entry.decodedBodySize > 0) || (noConnection && entry.duration < 12);
        this.viewStats.resources += 1;
        if (cached) this.viewStats.cachedResources += 1;
        const aggKey = `${kind}|${host}`;
        const agg = this.resourceAgg.get(aggKey) || { n: 0, cached: 0, ms: 0, bytes: 0 };
        agg.n += 1;
        agg.cached += cached ? 1 : 0;
        agg.ms += entry.duration;
        agg.bytes += entry.encodedBodySize || 0;
        this.resourceAgg.set(aggKey, agg);

        // Same blob (origin+path) fetched via different query strings, or re-downloaded over the
        // network more than once, is duplicate transfer.
        const blobKey = `${url.origin}${url.pathname}`;
        const seen = this.blobFetches.get(blobKey) || { queries: new Set<string>(), network: 0, cached: 0 };
        seen.queries.add(url.search);
        if (cached) seen.cached += 1; else seen.network += 1;
        this.blobFetches.set(blobKey, seen);
        if (this.blobFetches.size > 20_000) {
            const oldest = this.blobFetches.keys().next().value;
            if (oldest !== undefined) this.blobFetches.delete(oldest);
        }
        if ((seen.queries.size > 1 || seen.network > 1) && !seen.reported) {
            seen.reported = true;
            this.viewStats.dups += 1;
            this.push({
                t: 'dup', kind: seen.queries.size > 1 ? 'blob-multi-url' : 'blob-refetch',
                key: `${host}${url.pathname}`.slice(0, 140), n: seen.network + seen.cached, view: this.view,
            });
        }
    }

    private startObservers(): void {
        if (typeof PerformanceObserver === 'undefined') return;
        const observe = (type: string, handler: (entries: PerformanceEntryList) => void, buffered = true) => {
            try {
                const observer = new PerformanceObserver((list) => handler(list.getEntries()));
                observer.observe({ type, buffered } as PerformanceObserverInit);
            } catch {
                // entry type unsupported in this browser
            }
        };
        observe('resource', (entries) => entries.forEach((e) => this.observeResources(e as PerformanceResourceTiming)));
        observe('longtask', (entries) => entries.forEach((e) => {
            this.viewStats.longTasks += 1;
            this.viewStats.longTaskMs += e.duration;
        }));
        observe('paint', (entries) => entries.forEach((e) => {
            if (e.name === 'first-contentful-paint') this.push({ t: 'vital', name: 'FCP', value: Math.round(e.startTime), view: this.view });
        }));
        observe('largest-contentful-paint', (entries) => {
            const last = entries[entries.length - 1];
            if (last) this.push({ t: 'vital', name: 'LCP', value: Math.round(last.startTime), view: this.view });
        });
        try {
            const nav = performance.getEntriesByType('navigation')[0] as PerformanceNavigationTiming | undefined;
            if (nav) {
                this.push({ t: 'vital', name: 'TTFB', value: Math.round(nav.responseStart), view: 'boot' });
                this.push({ t: 'vital', name: 'DOMContentLoaded', value: Math.round(nav.domContentLoadedEventEnd), view: 'boot' });
            }
        } catch {
            // ignore
        }
    }

    // --- reporting ------------------------------------------------------------------

    /** Human-readable roll-up of the whole session so far. */
    summary() {
        const reqs = this.requests;
        const byEndpoint = new Map<string, { n: number; ms: number[]; bytes: number; failed: number }>();
        for (const r of reqs) {
            const k = `${r.method} ${r.path}`;
            const row = byEndpoint.get(k) || { n: 0, ms: [], bytes: 0, failed: 0 };
            row.n += 1;
            row.ms.push(r.ms);
            row.bytes += r.bytes;
            row.failed += r.ok ? 0 : 1;
            byEndpoint.set(k, row);
        }
        const endpoints = Array.from(byEndpoint.entries()).map(([endpoint, row]) => ({
            endpoint, count: row.n, p50: Math.round(percentile(row.ms, 50)), p95: Math.round(percentile(row.ms, 95)),
            totalMs: Math.round(row.ms.reduce((a, b) => a + b, 0)), kb: Math.round(row.bytes / 1024), failed: row.failed,
        }));
        const dupBlobs = Array.from(this.blobFetches.entries())
            .filter(([, v]) => v.queries.size > 1 || v.network > 1)
            .map(([blob, v]) => ({ blob, urls: v.queries.size, network: v.network, cached: v.cached }));
        return {
            session: this.session,
            requests: reqs.length,
            slowest: [...endpoints].sort((a, b) => b.p95 - a.p95).slice(0, 8),
            chattiest: [...endpoints].sort((a, b) => b.count - a.count).slice(0, 8),
            biggest: [...endpoints].sort((a, b) => b.kb - a.kb).slice(0, 5),
            duplicates: Array.from(this.reportedDup.entries()).filter(([, n]) => n >= 2).map(([key, n]) => ({ key: stripQuery(key), n }))
                .sort((a, b) => b.n - a.n).slice(0, 10),
            duplicateBlobs: dupBlobs.slice(0, 10),
            resources: Array.from(this.resourceAgg.entries()).map(([key, v]) => ({
                key, n: v.n, cached: v.cached, avgMs: Math.round(v.ms / v.n), mb: Number((v.bytes / 1048576).toFixed(1)),
            })),
        };
    }

    /** Console helper: `photostorePerf.report()`. */
    report(): ReturnType<PerfCollector['summary']> {
        const s = this.summary();
        /* eslint-disable no-console */
        console.group(`photostore perf — ${s.requests} requests`);
        console.log('slowest endpoints (p95)'); console.table(s.slowest);
        console.log('chattiest endpoints'); console.table(s.chattiest);
        console.log('duplicate requests'); console.table(s.duplicates);
        console.log('duplicate blob fetches'); console.table(s.duplicateBlobs);
        console.log('resources (images/blobs)'); console.table(s.resources);
        console.groupEnd();
        /* eslint-enable no-console */
        return s;
    }

    private summaryEvent(windowMs: number): PerfEvent {
        const s = this.summary();
        const fmt = (rows: { endpoint: string; count: number; p95: number }[], pick: 'p95' | 'count') =>
            rows.slice(0, 5).map((r) => `${r.endpoint}:${r[pick]}`).join(',');
        const resources = this.viewStats.resources;
        return {
            t: 'summary', windowMs: Math.round(windowMs), requests: s.requests,
            failed: this.requests.filter((r) => !r.ok).length, dups: s.duplicates.length,
            bytes: this.requests.reduce((a, r) => a + r.bytes, 0),
            slowest: fmt(s.slowest, 'p95'), chattiest: fmt(s.chattiest, 'count'),
            dupBlobs: s.duplicateBlobs.length, resources,
            cachedResources: this.viewStats.cachedResources, longTasks: this.viewStats.longTasks,
            longTaskMs: Math.round(this.viewStats.longTaskMs),
        };
    }

    flush(useBeacon = false): void {
        if (!enabled() || !this.sender) return;
        const events = this.buffer.splice(0, this.buffer.length);
        this.resourceAgg.forEach((v, key) => {
            const [kind, host] = key.split('|');
            events.push({ t: 'resource', kind, host, n: v.n, cached: v.cached, ms: Math.round(v.ms / Math.max(1, v.n)), bytes: v.bytes, view: this.view });
        });
        this.resourceAgg.clear();
        if (!events.length) return;
        events.push(this.summaryEvent(now()));
        const body = { session: this.session, events };
        void useBeacon;
        this.sender(body).catch(() => {
            // best effort: instrumentation must never surface errors or retry-storm the backend
        });
    }

    start(): void {
        if (this.started || !enabled() || typeof window === 'undefined') return;
        this.started = true;
        this.startObservers();
        this.timer = setInterval(() => this.flush(), FLUSH_INTERVAL_MS);
        document.addEventListener('visibilitychange', () => {
            if (document.visibilityState === 'hidden') {
                this.closeView();
                this.viewStats = this.newView(this.view);
                this.flush(true);
            }
        });
        (window as unknown as { photostorePerf?: unknown }).photostorePerf = {
            report: () => this.report(),
            summary: () => this.summary(),
            events: () => [...this.buffer],
            flush: () => this.flush(),
            off: () => { try { window.localStorage.setItem('photostore.perf', 'off'); } catch { /* ignore */ } },
        };
    }

    /** Test hook. */
    _reset(): void {
        this.buffer = [];
        this.requests = [];
        this.recentByKey.clear();
        this.reportedDup.clear();
        this.blobFetches.clear();
        this.resourceAgg.clear();
        this.view = 'boot';
        this.viewStats = this.newView('boot');
        if (this.timer) clearInterval(this.timer);
        this.timer = null;
        this.started = false;
    }

    /** Test hook. */
    _events(): PerfEvent[] {
        return [...this.buffer];
    }
}

export const perf = new PerfCollector();
export const perfEnabled = enabled;
export const perfNow = now;
