import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { PersistedUploadSession } from '../types/browserProcessing';
import { UploadSessionStore, UPLOAD_CHECKPOINT_INTERVAL_MS } from './uploadSessionStore';

const KEY = 'photostore.upload.session.v1';

function session(count = 2, id = 'session'): PersistedUploadSession {
    return {
        id, createdAt: 1700000000000,
        files: Array.from({ length: count }, (_, index) => ({
            key: `${id}:${index}`, name: `original-${index}.heic`, size: 50000,
            type: 'image/heic', lastModified: 1700000000000, uploadedBytes: 0,
            skippedDuplicate: false, status: 'pending',
        })),
    };
}

function setup() {
    const values = new Map<string, string>();
    const storage: Storage = {
        get length() { return values.size; },
        clear: () => values.clear(),
        getItem: (key) => values.get(key) ?? null,
        setItem: vi.fn((key, value) => { values.set(key, value); }),
        removeItem: vi.fn((key) => { values.delete(key); }),
        key: (index) => Array.from(values.keys())[index] ?? null,
    };
    return { storage, store: new UploadSessionStore(() => storage, KEY) };
}

beforeEach(() => { vi.useFakeTimers(); });
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); });

describe('upload recovery checkpoints', () => {
    it('updates a 50,000-file queue without copying or serializing it on progress', async () => {
        const { store, storage } = setup();
        const source = session(50000);
        store.set(source);
        const liveFiles = store.current!.files;
        const map = vi.spyOn(liveFiles, 'map');
        const stringify = vi.spyOn(JSON, 'stringify');
        vi.mocked(storage.setItem).mockClear();
        for (let index = 0; index < 10000; index += 1) {
            store.update('session:12345', { uploadedBytes: index, status: 'uploading' });
        }
        expect(store.current!.files).toBe(liveFiles);
        expect(store.current!.files[12344]).toBe(source.files[12344]);
        expect(source.files[12345].uploadedBytes).toBe(0);
        expect(map).not.toHaveBeenCalled();
        expect(stringify).not.toHaveBeenCalled();
        expect(storage.setItem).not.toHaveBeenCalled();
        await vi.advanceTimersByTimeAsync(UPLOAD_CHECKPOINT_INTERVAL_MS);
        expect(storage.setItem).toHaveBeenCalledTimes(1);
        expect(stringify).toHaveBeenCalledTimes(1);
        expect(JSON.parse(storage.getItem(KEY)!).files[12345].uploadedBytes).toBe(9999);
    });

    it('does not postpone checkpoints indefinitely under continuous progress', async () => {
        const { store, storage } = setup();
        store.set(session());
        vi.mocked(storage.setItem).mockClear();
        for (let index = 0; index < 50; index += 1) {
            store.update('session:0', { uploadedBytes: index });
            await vi.advanceTimersByTimeAsync(100);
        }
        expect(storage.setItem).toHaveBeenCalledTimes(2);
        store.flush();
        expect(storage.setItem).toHaveBeenCalledTimes(3);
        expect(JSON.parse(storage.getItem(KEY)!).files[0].uploadedBytes).toBe(49);
    });

    it('coalesces completion promises and saves resume fields before resolving', async () => {
        const { store, storage } = setup();
        store.set(session());
        const oldFile = store.current!.files[0];
        store.update('session:0', { uploadId: 'upload', blobName: 'blob', chunkSizeBytes: 25000 });
        store.update('session:0', { blockIds: ['first', 'second'], uploadedBytes: 50000 });
        store.update('session:0', { error: 'network', previewDataUrl: 'data:image/jpeg;base64,abc' });
        store.update('session:0', { status: 'done' });
        const first = store.checkpoint();
        store.update('session:1', { status: 'done', skippedDuplicate: true });
        expect(store.checkpoint()).toBe(first);
        const completed = vi.fn();
        void first.then(completed);
        await vi.advanceTimersByTimeAsync(1999);
        expect(completed).not.toHaveBeenCalled();
        await vi.advanceTimersByTimeAsync(1);
        expect(await first).toBe(true);
        // blockIds/chunkSizeBytes are resume-only -- once a file is 'done' they're
        // dropped so a large session's checkpoint doesn't keep growing with dead
        // weight from every file that already finished (see update()).
        expect(JSON.parse(storage.getItem(KEY)!).files[0]).toMatchObject({
            uploadId: 'upload', blobName: 'blob', uploadedBytes: 50000, status: 'done',
        });
        expect(JSON.parse(storage.getItem(KEY)!).files[0]).not.toHaveProperty('chunkSizeBytes');
        expect(JSON.parse(storage.getItem(KEY)!).files[0]).not.toHaveProperty('blockIds');
        expect(JSON.parse(storage.getItem(KEY)!).files[0]).not.toHaveProperty('error');
        expect(JSON.parse(storage.getItem(KEY)!).files[0]).not.toHaveProperty('previewDataUrl');
        expect(JSON.parse(storage.getItem(KEY)!).files[1]).toMatchObject({ status: 'done', skippedDuplicate: true });
        expect(oldFile.status).toBe('pending');
    });

    it('drops a file entirely via remove(), not just trims it, without disturbing other entries', async () => {
        const { store, storage } = setup();
        const source = session(5);
        store.set(source);
        const untouched1 = store.current!.files[1];
        const untouched3 = store.current!.files[3];
        const splice = vi.spyOn(Array.prototype, 'splice');
        store.update('session:4', { status: 'done' });
        store.remove('session:4');
        expect(splice).not.toHaveBeenCalled();
        expect(store.current!.files).toHaveLength(4);
        expect(store.current!.files.some((file) => file.key === 'session:4')).toBe(false);
        // Swap-and-pop moves the last element into the removed slot rather
        // than shifting everything -- untouched entries keep their object
        // identity (no copy), only the swapped one's position metadata changes.
        expect(store.current!.files).toContain(untouched1);
        expect(store.current!.files).toContain(untouched3);
        await vi.advanceTimersByTimeAsync(UPLOAD_CHECKPOINT_INTERVAL_MS);
        const persisted = JSON.parse(storage.getItem(KEY)!);
        expect(persisted.files).toHaveLength(4);
        expect(persisted.files.some((file: { key: string }) => file.key === 'session:4')).toBe(false);
        // Removing a key a second time, or one that was never present, is a
        // silent no-op (mirrors update()'s own missing-key guard).
        store.remove('session:4');
        store.remove('never-existed');
        expect(store.current!.files).toHaveLength(4);
    });

    it('keeps checkpoint size bounded by the in-flight/failed working set, not the total wave size', async () => {
        const { store, storage } = setup();
        const total = 50000;
        const stillPending = 10000;
        store.set(session(total));
        for (let index = stillPending; index < total; index += 1) {
            const key = `session:${index}`;
            store.update(key, { status: 'done' });
            store.remove(key);
        }
        await vi.advanceTimersByTimeAsync(UPLOAD_CHECKPOINT_INTERVAL_MS);
        const persisted = JSON.parse(storage.getItem(KEY)!);
        expect(persisted.files).toHaveLength(stillPending);
        // A 50,000-entry blob measured ~23MB (UTF-16) in practice; bounding it
        // to the still-pending working set keeps a wave of ANY total size
        // from ever approaching that regardless of how large the wave is.
        const serializedLength = storage.getItem(KEY)!.length;
        expect(serializedLength).toBeLessThan(2_000_000);
    });

    it('flushes immediately on lifecycle boundaries without a redundant timer write', async () => {
        const { store, storage } = setup();
        store.set(session());
        vi.mocked(storage.setItem).mockClear();
        store.update('session:0', { status: 'failed', error: 'network', uploadedBytes: 25000 });
        const checkpoint = store.checkpoint();
        expect(store.flush()).toBe(true);
        expect(await checkpoint).toBe(true);
        await vi.advanceTimersByTimeAsync(10000);
        expect(storage.setItem).toHaveBeenCalledTimes(1);
        expect(JSON.parse(storage.getItem(KEY)!).files[0]).toMatchObject({ status: 'failed', error: 'network' });
    });

    it('never resurrects a discarded session from its queued checkpoint', async () => {
        const { store, storage } = setup();
        store.set(session());
        store.update('session:0', { status: 'done' });
        const checkpoint = store.checkpoint();
        store.set(null);
        expect(await checkpoint).toBe(false);
        store.update('session:0', { status: 'failed' });
        await vi.advanceTimersByTimeAsync(10000);
        expect(storage.getItem(KEY)).toBeNull();
        expect(store.current).toBeNull();
    });

    it('replaces the key index and cancels old-session completion waiters on reselection', async () => {
        const { store, storage } = setup();
        store.set(session());
        store.update('session:0', { uploadedBytes: 25000 });
        const checkpoint = store.checkpoint();
        store.set(session(1, 'new'));
        expect(await checkpoint).toBe(false);
        store.update('session:0', { uploadedBytes: 50000 });
        store.update('new:0', { uploadedBytes: 123 });
        await vi.advanceTimersByTimeAsync(2000);
        expect(JSON.parse(storage.getItem(KEY)!).files).toHaveLength(1);
        expect(store.current!.files[0].uploadedBytes).toBe(123);
    });

    it('continues in memory on quota failure and can retry the unsaved checkpoint', async () => {
        const { store, storage } = setup();
        store.set(session());
        const write = vi.mocked(storage.setItem);
        write.mockImplementationOnce(() => { throw new Error('QuotaExceededError'); });
        store.update('session:0', { status: 'done' });
        const failed = store.checkpoint();
        await vi.advanceTimersByTimeAsync(2000);
        expect(await failed).toBe(false);
        expect(store.current!.files[0].status).toBe('done');
        expect(JSON.parse(storage.getItem(KEY)!).files[0].status).toBe('pending');
        const retry = store.checkpoint();
        await vi.advanceTimersByTimeAsync(2000);
        expect(await retry).toBe(true);
        expect(JSON.parse(storage.getItem(KEY)!).files[0].status).toBe('done');
    });

    it('ignores unknown keys and does not rewrite an unchanged checkpoint', async () => {
        const { store, storage } = setup();
        store.set(session());
        vi.mocked(storage.setItem).mockClear();
        store.update('missing', { uploadedBytes: 1 });
        expect(await store.checkpoint()).toBe(true);
        await vi.advanceTimersByTimeAsync(5000);
        expect(storage.setItem).not.toHaveBeenCalled();
    });
});
