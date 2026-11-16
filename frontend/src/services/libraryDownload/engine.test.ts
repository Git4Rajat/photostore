import { createHash } from 'crypto';
import { describe, expect, it } from 'vitest';
import {
    ByteSource, Deps, Entry, Folder, HttpError, Progress, STATE_DIR, Sink, Token, blobUrl, order, run, segments,
} from './engine';

const bytes = (length: number, seed: number): Uint8Array => {
    const out = new Uint8Array(length);
    for (let i = 0; i < length; i += 1) out[i] = (i * 31 + seed) % 251;
    return out;
};
const sha = (data: Uint8Array): string => createHash('sha256').update(data).digest('hex');
const concat = (chunks: Uint8Array[]): Uint8Array => {
    const out = new Uint8Array(chunks.reduce((n, c) => n + c.byteLength, 0));
    let at = 0;
    chunks.forEach((c) => { out.set(c, at); at += c.byteLength; });
    return out;
};
const source = (data: Uint8Array, chunk = 7): ByteSource => {
    let at = 0;
    return {
        read: async () => {
            if (at >= data.byteLength) return null;
            const piece = data.slice(at, at + chunk);
            at += chunk;
            return piece;
        },
        cancel: async () => undefined,
    };
};

class MemoryFolder implements Folder {
    files = new Map<string, Uint8Array>();

    async size(path: string): Promise<number | null> {
        const file = this.files.get(path);
        return file ? file.byteLength : null;
    }

    async create(path: string): Promise<Sink> {
        const chunks: Uint8Array[] = [];
        return {
            write: async (chunk) => { chunks.push(chunk); },
            close: async () => { this.files.set(path, concat(chunks)); },
            abort: async () => undefined,
        };
    }

    async read(path: string): Promise<ByteSource> {
        return source(this.files.get(path) as Uint8Array, 1000);
    }

    async remove(path: string): Promise<void> {
        Array.from(this.files.keys()).forEach((key) => {
            if (key === path || key.startsWith(`${path}/`)) this.files.delete(key);
        });
    }
}

interface World {
    folder: MemoryFolder;
    blobs: Map<string, Uint8Array>;
    requests: Array<{ blob: string; range: [number, number] | null; sas: string }>;
    tokens: number;
    sleeps: number[];
    clock: { now: number };
    fail: Array<(blob: string, range: [number, number] | null) => Error | number | Uint8Array | null>;
    deps: Deps;
}

const world = (options: Partial<Deps> = {}): World => {
    const w = {
        folder: new MemoryFolder(), blobs: new Map<string, Uint8Array>(), requests: [], tokens: 0, sleeps: [],
        clock: { now: Date.parse('2026-11-16T00:00:00Z') }, fail: [],
    } as unknown as World;
    w.deps = {
        folder: w.folder,
        fetchBlob: async (url, range) => {
            const [path, sas] = url.split('?');
            const blob = decodeURIComponent(path.split('/').pop() as string);
            w.requests.push({ blob, range, sas });
            const injected = w.fail.length ? (w.fail.shift() as World['fail'][number])(blob, range) : null;
            if (injected instanceof Error) throw injected;
            if (typeof injected === 'number') return { status: injected, body: source(new Uint8Array(0)) };
            const data = injected || w.blobs.get(blob);
            if (!data) return { status: 404, body: source(new Uint8Array(0)) };
            return range
                ? { status: 206, body: source(data.slice(range[0], range[1] + 1)) }
                : { status: 200, body: source(data) };
        },
        getToken: async (): Promise<Token> => {
            w.tokens += 1;
            return {
                baseUrl: 'https://acct.blob/images/', sas: `sig=${w.tokens}`,
                expiresAt: new Date(w.clock.now + 12 * 3600 * 1000).toISOString(),
            };
        },
        newHasher: async () => {
            const hash = createHash('sha256');
            return { update: (chunk) => { hash.update(chunk); }, digest: () => hash.digest('hex') };
        },
        now: () => w.clock.now,
        sleep: async (ms) => { w.sleeps.push(ms); },
        connections: 3,
        segmentBytes: 100,
        ...options,
    };
    return w;
};

const put = (w: World, name: string, length: number, seed = 1, extra: Partial<Entry> = {}): Entry => {
    const data = bytes(length, seed);
    w.blobs.set(`blob-${name}`, data);
    return { name, blob: `blob-${name}`, path: `2024/05/${name}`, size: length, sha256: sha(data), ...extra };
};

const go = (w: World, entries: Entry[], signal = new AbortController().signal): Promise<Progress> => (
    run(entries, w.deps, signal, () => undefined)
);

describe('planning', () => {
    it('puts files that need segments first, largest first, and keeps the rest in order', () => {
        const names = order([
            { name: 'a', blob: 'a', path: 'a', size: 5 }, { name: 'big', blob: 'b', path: 'b', size: 300 },
            { name: 'c', blob: 'c', path: 'c' }, { name: 'bigger', blob: 'd', path: 'd', size: 900 },
        ], 100).map((e) => e.name);
        expect(names).toEqual(['bigger', 'big', 'a', 'c']);
    });

    it('cuts a file into inclusive byte ranges', () => {
        expect(segments(250, 100)).toEqual([[0, 99], [100, 199], [200, 249]]);
        expect(segments(100, 100)).toEqual([[0, 99]]);
    });

    it('builds a blob URL with an encoded name', () => {
        expect(blobUrl({ baseUrl: 'https://a/images/', sas: 's=1', expiresAt: '' }, 'a b/c'))
            .toBe('https://a/images/a%20b%2Fc?s=1');
    });
});

describe('run', () => {
    it('downloads every file byte for byte and reports totals', async () => {
        const w = world();
        const entries = [put(w, 'a.jpg', 40), put(w, 'b.jpg', 60, 2), put(w, 'video.mov', 250, 3)];

        const result = await go(w, entries);

        entries.forEach((e) => expect(w.folder.files.get(e.path)).toEqual(w.blobs.get(e.blob)));
        expect(result).toMatchObject({ filesDone: 3, filesTotal: 3, bytesDone: 350, bytesTotal: 350, unverified: 0 });
        expect(result.failed).toEqual([]);
        expect(w.tokens).toBe(1);
    });

    it('fetches a large file in ranges and leaves no segment files behind', async () => {
        const w = world();
        const entry = put(w, 'video.mov', 250, 3);

        await go(w, [entry]);

        expect(w.requests.map((r) => r.range)).toEqual([[0, 99], [100, 199], [200, 249]]);
        expect(Array.from(w.folder.files.keys())).toEqual([entry.path]);
    });

    it('skips files already in the folder and resumes a large file from its finished segments', async () => {
        const w = world();
        const done = put(w, 'done.jpg', 40);
        const video = put(w, 'video.mov', 250, 3);
        const data = w.blobs.get(video.blob) as Uint8Array;
        w.folder.files.set(done.path, w.blobs.get(done.blob) as Uint8Array);
        w.folder.files.set(`${STATE_DIR}/parts/${video.blob}/0`, data.slice(0, 100));
        w.folder.files.set(`${STATE_DIR}/parts/${video.blob}/1`, data.slice(100, 150)); // cut short: not trusted

        const result = await go(w, [done, video]);

        expect(w.requests.map((r) => [r.blob, r.range])).toEqual([[video.blob, [100, 199]], [video.blob, [200, 249]]]);
        expect(w.folder.files.get(video.path)).toEqual(data);
        expect(result).toMatchObject({ filesDone: 2, bytesDone: 290 });
    });

    it('downloads again when the file in the folder has the wrong size', async () => {
        const w = world();
        const entry = put(w, 'a.jpg', 40);
        w.folder.files.set(entry.path, new Uint8Array(0)); // an interrupted write leaves an empty file

        await go(w, [entry]);

        expect(w.folder.files.get(entry.path)).toEqual(w.blobs.get(entry.blob));
    });

    it('retries a dropped connection and a throttled request with backoff', async () => {
        const w = world();
        const entry = put(w, 'a.jpg', 40);
        w.fail.push(() => new TypeError('Failed to fetch'), () => 503);

        const result = await go(w, [entry]);

        expect(w.sleeps).toEqual([1000, 1000]);
        expect(result).toMatchObject({ filesDone: 1, bytesDone: 40 });
        expect(result.failed).toEqual([]);
    });

    it('renews the token on a 403 and when it is close to expiry', async () => {
        const w = world({ connections: 1 });
        const entries = [put(w, 'a.jpg', 40), put(w, 'b.jpg', 40, 2), put(w, 'c.jpg', 40, 3)];
        w.fail.push(() => 403, () => null, () => { w.clock.now += 11.75 * 3600 * 1000; return null; });

        const result = await go(w, entries);

        expect(result.filesDone).toBe(3);
        expect(w.requests.map((r) => r.sas)).toEqual(['sig=1', 'sig=2', 'sig=2', 'sig=3']);
    });

    it('gives up on one file after repeated failures and carries on with the rest', async () => {
        const w = world({ connections: 1 });
        const bad = put(w, 'bad.jpg', 40);
        const good = put(w, 'good.jpg', 40, 2);
        for (let i = 0; i < 5; i += 1) w.fail.push(() => 500);

        const result = await go(w, [bad, good]);

        expect(result.failed).toEqual([{ name: 'bad.jpg', path: bad.path, message: 'Storage answered 500' }]);
        expect(result).toMatchObject({ filesDone: 1, bytesDone: 40 });
        expect(w.folder.files.has(bad.path)).toBe(false);
        expect(w.folder.files.get(good.path)).toEqual(w.blobs.get(good.blob));
    });

    it('reports a missing original without retrying it', async () => {
        const w = world();
        const entry: Entry = { name: 'gone.jpg', blob: 'nope', path: '2024/05/gone.jpg', size: 10 };

        const result = await go(w, [entry]);

        expect(w.requests).toHaveLength(1);
        expect(result.failed[0].message).toBe('The original is missing from storage');
    });

    it('refetches a corrupted transfer and saves the good bytes', async () => {
        const w = world();
        const entry = put(w, 'a.jpg', 40);
        w.fail.push(() => bytes(40, 99)); // right length, wrong content

        const result = await go(w, [entry]);

        expect(w.folder.files.get(entry.path)).toEqual(w.blobs.get(entry.blob));
        expect(result.unverified).toBe(0);
    });

    it('keeps a file whose stored bytes are consistent but differ from the recorded hash', async () => {
        const w = world();
        const entry = put(w, 'converted.heic', 40, 1, { sha256: 'f'.repeat(64) });
        const large = put(w, 'converted.mov', 250, 2, { sha256: 'e'.repeat(64) });

        const result = await go(w, [entry, large]);

        expect(w.folder.files.get(entry.path)).toEqual(w.blobs.get(entry.blob));
        expect(w.folder.files.get(large.path)).toEqual(w.blobs.get(large.blob));
        expect(result).toMatchObject({ filesDone: 2, unverified: 2 });
        expect(result.failed).toEqual([]);
    });

    it('rejects a truncated response instead of saving a short file', async () => {
        const w = world({ connections: 1 });
        const entry = put(w, 'a.jpg', 40);
        for (let i = 0; i < 5; i += 1) w.fail.push(() => bytes(10, 1));

        const result = await go(w, [entry]);

        expect(w.folder.files.has(entry.path)).toBe(false);
        expect(result.failed[0].message).toBe('Got 10 bytes, expected 40');
    });

    it('downloads a file with no recorded size or hash', async () => {
        const w = world();
        w.blobs.set('m', bytes(30, 5));

        const result = await go(w, [{ name: 'mystery', blob: 'm', path: 'undated/mystery' }]);

        expect(w.folder.files.get('undated/mystery')).toEqual(bytes(30, 5));
        expect(result).toMatchObject({ filesDone: 1, bytesDone: 30, bytesTotal: 0 });
    });

    it('stops starting work when cancelled and does not record cancellations as failures', async () => {
        const controller = new AbortController();
        const w = world({ connections: 1 });
        const entries = [put(w, 'a.jpg', 40), put(w, 'b.jpg', 40, 2)];
        w.fail.push(() => { controller.abort(); const e = new Error('aborted'); e.name = 'AbortError'; return e; });

        const result = await go(w, entries, controller.signal);

        expect(result.failed).toEqual([]);
        expect(result.filesDone).toBe(0);
        expect(w.requests).toHaveLength(1);
    });

    it('never runs more requests at once than its connection limit', async () => {
        let active = 0;
        let peak = 0;
        const w = world({ connections: 3 });
        const inner = w.deps.fetchBlob;
        w.deps.fetchBlob = async (url, range, signal) => {
            active += 1;
            peak = Math.max(peak, active);
            await new Promise((resolve) => setTimeout(resolve, 1));
            const result = await inner(url, range, signal);
            active -= 1;
            return result;
        };
        const entries = Array.from({ length: 12 }, (_, i) => put(w, `p${i}.jpg`, 20, i));

        const result = await go(w, entries);

        expect(result.filesDone).toBe(12);
        expect(peak).toBe(3);
    });
});

// Keeps the unused import honest: HttpError is part of the engine's contract with the browser fetcher.
it('exposes the storage status on HttpError', () => {
    expect(new HttpError(503).status).toBe(503);
});
