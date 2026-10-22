import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
const identity = vi.hoisted(() => ({ library: 'A', user: 'owner' }));
vi.mock('./passwordAuthClient', () => ({ getActiveLibraryFromToken: () => identity.library, getSubjectFromToken: () => identity.user }));
beforeEach(() => {
    const values = new Map<string, string>();
    vi.stubGlobal('localStorage', { get length() { return values.size; }, key: (i: number) => Array.from(values.keys())[i] ?? null, getItem: (key: string) => values.get(key) ?? null,
        setItem: (key: string, value: string) => values.set(key, value), removeItem: (key: string) => values.delete(key) });
    vi.resetModules(); identity.library = 'A'; identity.user = 'owner'; });

afterEach(() => vi.unstubAllGlobals());

describe('pending edits across index generations', () => {
    it('keeps a saved edit across reload and stale page responses, scoped by account and library', async () => {
        let journal = await import('./pendingEdits');
        journal.confirmPendingEdit(journal.beginPendingEdit('photo', 'one.jpg', { rating: 5 }));
        vi.resetModules();
        journal = await import('./pendingEdits');
        expect(journal.applyPendingEdits('photo', 'one.jpg', { rating: 1 }).rating).toBe(5);
        identity.library = 'B';
        expect(journal.applyPendingEdits('photo', 'one.jpg', { rating: 1 }).rating).toBe(1);
        identity.library = 'A'; identity.user = 'other';
        expect(journal.applyPendingEdits('photo', 'one.jpg', { rating: 1 }).rating).toBe(1);
    });

    it('persists the last confirmed value while a newer save is still in flight', async () => {
        let j = await import('./pendingEdits');
        j.confirmPendingEdit(j.beginPendingEdit('photo', 'one.jpg', { rating: 3 }));
        j.beginPendingEdit('photo', 'one.jpg', { rating: 5 });
        j.confirmPendingEdit(j.beginPendingEdit('photo', 'one.jpg', { liked: true }));
        vi.resetModules();
        j = await import('./pendingEdits');
        expect(j.applyPendingEdits('photo', 'one.jpg', { rating: 0 }).rating).toBe(3);
    });

    it('does not treat job completion or an older read as confirmation', async () => {
        const j = await import('./pendingEdits');
        const oldRead = j.pendingEditReadTokens();
        j.confirmPendingEdit(j.beginPendingEdit('person', 'p1', { name: 'Alice' }));
        j.applyPendingEdits('person', 'p1', { name: 'Alice' }, 'v2', undefined, oldRead);
        expect(j.hasPendingEdits()).toBe(true);
        const read = j.pendingEditReadTokens();
        j.applyPendingEdits('person', 'p1', { name: 'Alice' }, 'v2', undefined, read);
        expect(j.hasPendingEdits()).toBe(false);
        expect(j.applyPendingEdits('person', 'p1', { name: 'Old' }, 'v1', undefined, read).name).toBe('Alice');
        // A projection timestamp alone cannot prove it supersedes the source write.
        expect(j.applyPendingEdits('person', 'p1', { name: 'Bob' }, 'v3', undefined, read).name).toBe('Alice');
        j.acceptSourceEdits(j.sourceEditRequests(), [{ kind: 'person', id: 'p1', values: { name: 'Bob' } }], j.pendingEditScope());
        expect(j.applyPendingEdits('person', 'p1', { name: 'Old' }).name).toBe('Bob');
    });

    it('an old failure cannot undo a newer edit; a newer failure restores an older successful save', async () => {
        const j = await import('./pendingEdits');
        const first = j.beginPendingEdit('photo', 'one.jpg', { rating: 3 });
        const second = j.beginPendingEdit('photo', 'one.jpg', { rating: 5 });
        j.confirmPendingEdit(first);
        j.rollbackPendingEdit(second);
        expect(j.applyPendingEdits('photo', 'one.jpg', { rating: 1 }).rating).toBe(3);
        const third = j.beginPendingEdit('photo', 'one.jpg', { rating: 4 });
        j.rollbackPendingEdit(first);
        expect(j.applyPendingEdits('photo', 'one.jpg', { rating: 1 }).rating).toBe(4);
        j.confirmPendingEdit(third);
    });

    it('does not resurrect a failed edit when every overlapping save fails', async () => {
        const j = await import('./pendingEdits');
        const first = j.beginPendingEdit('photo', 'one.jpg', { rating: 3 });
        const second = j.beginPendingEdit('photo', 'one.jpg', { rating: 5 });
        j.rollbackPendingEdit(first);
        j.rollbackPendingEdit(second);
        expect(j.applyPendingEdits('photo', 'one.jpg', { rating: 0 }).rating).toBe(0);
        expect(j.hasPendingEdits()).toBe(false);
    });

    it('serializes overlapping writes while unrelated photos save concurrently', async () => {
        const j = await import('./pendingEdits');
        let finish!: () => void;
        const order: string[] = [];
        const first = j.savePendingEdits([j.beginPendingEdit('photo', 'a', { rating: 1 })], async () => {
            order.push('first'); await new Promise<void>(resolve => { finish = resolve; });
        });
        const second = j.savePendingEdits([j.beginPendingEdit('photo', 'a', { rating: 5 })], async () => { order.push('second'); });
        await j.savePendingEdits([j.beginPendingEdit('photo', 'b', { rating: 2 })], async () => { order.push('other'); });
        expect(order).toEqual(['first', 'other']);
        finish(); await first; await second;
        expect(order).toEqual(['first', 'other', 'second']);
    });

    it('does not carry a rating over to a replacement upload with the same filename', async () => {
        const j = await import('./pendingEdits');
        j.observePhotoGeneration('a','blob-old', undefined, 'v1');
        j.confirmPendingEdit(j.beginPendingEdit('photo','a',{rating:5}));
        j.observePhotoGeneration('a','blob-new', undefined, 'v2');
        expect(j.applyPendingEdits('photo','a',{rating:0}).rating).toBe(0);
    });
});
