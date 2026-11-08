import { describe, expect, it } from 'vitest';
import { createHomeSnapshotStore, HOME_SNAPSHOT_LIMIT, type SnapshotBackend } from './homeSnapshot';
import type { Photo } from '../mockups/prototype/types';

const memory = (): SnapshotBackend & { data: Map<string, { meta: any; thumbs: Map<string, Blob> }> } => {
    const data = new Map<string, { meta: any; thumbs: Map<string, Blob> }>();
    return {
        data,
        read: async (library) => data.get(library) ?? null,
        write: async (library, meta, thumbs) => { data.set(library, { meta, thumbs: new Map(thumbs) }); },
        clear: async () => { data.clear(); },
    };
};

const photos = (n: number): Photo[] => Array.from({ length: n }, (_, i) => ({
    id: `p${i}.jpg`, filename: `p${i}.jpg`, swatch: 's1', dateLabel: 'Jan 1, 2024', year: 2024, rating: 0, liked: false,
    placeId: null, personIds: [], tags: [], rotation: 90, thumbnailRotation: 0,
} as Photo));

const urlsFor = (list: Photo[]) => Object.fromEntries(list.map((p) => [p.filename, `https://x/${p.filename}`]));
const okFetch = (size = 100): typeof fetch => (async () => new Response(new Uint8Array(size), { headers: { 'Content-Type': 'image/jpeg' } })) as typeof fetch;

describe('home snapshot', () => {
    it('saves the top photos with their thumbnail bytes and loads them back', async () => {
        const backend = memory();
        const store = createHomeSnapshotStore(backend, () => 1000);
        const list = photos(300);
        expect(await store.save(list, urlsFor(list), 'lib', okFetch())).toBe(HOME_SNAPSHOT_LIMIT);
        const loaded = await store.load('lib');
        expect(loaded?.photos).toHaveLength(HOME_SNAPSHOT_LIMIT);
        expect(loaded?.photos[0]).toMatchObject({ filename: 'p0.jpg', rotation: 90 });
        expect(loaded?.thumbs.get('p0.jpg')?.size).toBe(100);
    });

    it('is per library', async () => {
        const store = createHomeSnapshotStore(memory(), () => 1000);
        const list = photos(20);
        await store.save(list, urlsFor(list), 'a', okFetch());
        expect(await store.load('b')).toBeNull();
    });

    it('ignores a snapshot older than 30 days', async () => {
        const backend = memory();
        let now = 0;
        const store = createHomeSnapshotStore(backend, () => now);
        const list = photos(20);
        await store.save(list, urlsFor(list), 'lib', okFetch());
        now = 31 * 24 * 60 * 60 * 1000;
        expect(await store.load('lib')).toBeNull();
    });

    it('keeps the previous snapshot when most thumbnails fail to fetch', async () => {
        const backend = memory();
        const store = createHomeSnapshotStore(backend, () => 1000);
        const list = photos(50);
        await store.save(list, urlsFor(list), 'lib', okFetch());
        const failing = (async () => { throw new Error('offline'); }) as unknown as typeof fetch;
        expect(await store.save(list, urlsFor(list), 'lib', failing)).toBe(0);
        expect((await store.load('lib'))?.photos).toHaveLength(50);
    });

    it('skips oversized or non-image responses', async () => {
        const store = createHomeSnapshotStore(memory(), () => 1000);
        const list = photos(20);
        expect(await store.save(list, urlsFor(list), 'lib', okFetch(300 * 1024))).toBe(0);
        const html = (async () => new Response('<html>', { headers: { 'Content-Type': 'text/html' } })) as typeof fetch;
        expect(await store.save(list, urlsFor(list), 'lib', html)).toBe(0);
    });

    it('does not cache photos without a thumbnail url', async () => {
        const store = createHomeSnapshotStore(memory(), () => 1000);
        const list = photos(30);
        const urls = urlsFor(list.slice(0, 15));
        expect(await store.save(list, urls, 'lib', okFetch())).toBe(15);
    });

    it('clear removes everything', async () => {
        const backend = memory();
        const store = createHomeSnapshotStore(backend, () => 1000);
        const list = photos(20);
        await store.save(list, urlsFor(list), 'lib', okFetch());
        await store.clear();
        expect(await store.load('lib')).toBeNull();
    });
});
