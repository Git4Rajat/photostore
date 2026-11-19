import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const scope = vi.hoisted(() => ({ value: 'A' }));
vi.mock('./sessionScope', () => ({ sessionScope: () => scope.value }));

import {
    JUST_UPLOADED_MAX, JUST_UPLOADED_NOTIFY_MS, JUST_UPLOADED_TTL_MS, __resetJustUploadedForTests, addJustUploaded,
    listJustUploaded, removeJustUploaded, settleJustUploaded, subscribeJustUploaded,
} from './justUploaded';

const T0 = Date.now();
const names = () => listJustUploaded().map((e) => e.filename);

describe('just-uploaded overlay store', () => {
    let revoked: string[];
    beforeEach(() => {
        scope.value = 'A';
        __resetJustUploadedForTests();
        revoked = [];
        vi.stubGlobal('URL', Object.assign(URL, { revokeObjectURL: (u: string) => { revoked.push(u); } }));
    });
    afterEach(() => vi.unstubAllGlobals());

    it('lists newest first and ignores a repeated add of the same file', () => {
        addJustUploaded({ filename: 'a.jpg' }, T0 + 1000);
        addJustUploaded({ filename: 'b.jpg' }, T0 + 2000);
        addJustUploaded({ filename: 'a.jpg' }, T0 + 3000);
        expect(names()).toEqual(['b.jpg', 'a.jpg']);
    });

    it('returns a stable snapshot until something changes (safe for useSyncExternalStore)', () => {
        addJustUploaded({ filename: 'a.jpg' });
        expect(listJustUploaded()).toBe(listJustUploaded());
        const before = listJustUploaded();
        addJustUploaded({ filename: 'b.jpg' });
        expect(listJustUploaded()).not.toBe(before);
    });

    it('keeps libraries apart: another library never sees, settles or removes these rows', async () => {
        addJustUploaded({ filename: 'a.jpg' });
        scope.value = 'B';
        expect(names()).toEqual([]);
        addJustUploaded({ filename: 'b.jpg' });
        await settleJustUploaded(async (n) => new Set(n));
        expect(names()).toEqual([]);
        scope.value = 'A';
        expect(names()).toEqual(['a.jpg']);
    });

    it('caps a huge burst by dropping the oldest and revoking their thumbnails', () => {
        for (let i = 0; i < JUST_UPLOADED_MAX + 3; i += 1) addJustUploaded({ filename: `f${i}.jpg`, thumbnailUrl: `blob:t${i}` }, T0 + i);
        expect(listJustUploaded()).toHaveLength(JUST_UPLOADED_MAX);
        expect(names()).not.toContain('f0.jpg');
        expect(names()[0]).toBe(`f${JUST_UPLOADED_MAX + 2}.jpg`);
        expect(revoked).toEqual(['blob:t0', 'blob:t1', 'blob:t2']);
    });

    it('expires entries after the TTL without notifying during a read', () => {
        const listener = vi.fn();
        addJustUploaded({ filename: 'old.jpg', thumbnailUrl: 'blob:old' }, T0 + 0);
        subscribeJustUploaded(listener);
        expect(listJustUploaded(T0 + JUST_UPLOADED_TTL_MS + 1)).toEqual([]);
        expect(revoked).toEqual(['blob:old']);
        expect(listener).not.toHaveBeenCalled();
    });

    describe('notification rate during a burst', () => {
        beforeEach(() => vi.useFakeTimers());
        afterEach(() => vi.useRealTimers());

        it('tells listeners at once for the first photo, then at most once per interval', () => {
            const listener = vi.fn();
            subscribeJustUploaded(listener);
            addJustUploaded({ filename: 'f0.jpg' });
            expect(listener).toHaveBeenCalledTimes(1);          // first photo shows immediately
            for (let i = 1; i < 300; i += 1) addJustUploaded({ filename: `f${i}.jpg` });
            expect(listener).toHaveBeenCalledTimes(1);          // the burst is held back...
            vi.advanceTimersByTime(JUST_UPLOADED_NOTIFY_MS);
            expect(listener).toHaveBeenCalledTimes(2);          // ...and delivered as one update
            expect(listJustUploaded()).toHaveLength(300);       // nothing was dropped by coalescing
        });

        it('a removal is announced immediately, not held behind the burst timer', () => {
            const listener = vi.fn();
            addJustUploaded({ filename: 'a.jpg' });
            addJustUploaded({ filename: 'b.jpg' });
            subscribeJustUploaded(listener);
            removeJustUploaded(['a.jpg']);
            expect(listener).toHaveBeenCalledTimes(1);
        });
    });

    describe('Live Photo clips', () => {
        it('gives a clip no tile when its still is already shown', () => {
            addJustUploaded({ filename: 'IMG_1234.HEIC' });
            addJustUploaded({ filename: 'IMG_1234.MOV' });
            expect(names()).toEqual(['IMG_1234.HEIC']);
        });

        it('drops a clip that finalized first once its still arrives', () => {
            addJustUploaded({ filename: 'IMG_1234.MOV', thumbnailUrl: 'blob:clip' });
            expect(names()).toEqual(['IMG_1234.MOV']);
            addJustUploaded({ filename: 'IMG_1234.HEIC' });
            expect(names()).toEqual(['IMG_1234.HEIC']);
            expect(revoked).toEqual(['blob:clip']);
        });

        it('matches through export copy numbers and upload collision suffixes', () => {
            addJustUploaded({ filename: 'IMG_1234-1a2b3c4d.HEIC' });
            addJustUploaded({ filename: 'IMG_1234 (2).MOV' });
            expect(names()).toEqual(['IMG_1234-1a2b3c4d.HEIC']);
        });

        it('still shows an ordinary video and a clip with a different base name', () => {
            addJustUploaded({ filename: 'IMG_1.HEIC' });
            addJustUploaded({ filename: 'beach.mp4' });
            addJustUploaded({ filename: 'IMG_2.MOV' });
            expect(names().sort()).toEqual(['IMG_1.HEIC', 'IMG_2.MOV', 'beach.mp4']);
        });

        it('does not pair across libraries', () => {
            addJustUploaded({ filename: 'IMG_1.HEIC' });
            scope.value = 'B';
            addJustUploaded({ filename: 'IMG_1.MOV' });
            expect(names()).toEqual(['IMG_1.MOV']);
        });
    });

    describe('handover to the index', () => {
        it('removes only the files the index confirms and keeps the rest visible', async () => {
            addJustUploaded({ filename: 'a.jpg' }, T0 + 1);
            addJustUploaded({ filename: 'b.jpg' }, T0 + 2);
            addJustUploaded({ filename: 'c.jpg' }, T0 + 3);
            const removed = await settleJustUploaded(async () => new Set(['b.jpg']));
            expect(removed).toBe(1);
            expect(names()).toEqual(['c.jpg', 'a.jpg']);
        });

        it('keeps everything when the index cannot answer (null) or the lookup throws', async () => {
            addJustUploaded({ filename: 'a.jpg' });
            expect(await settleJustUploaded(async () => null)).toBe(0);
            expect(await settleJustUploaded(async () => { throw new Error('network'); })).toBe(0);
            expect(names()).toEqual(['a.jpg']);
        });

        it('keeps everything when the index is up but does not have the file yet (partial index)', async () => {
            addJustUploaded({ filename: 'a.jpg' });
            expect(await settleJustUploaded(async () => new Set())).toBe(0);
            expect(names()).toEqual(['a.jpg']);
        });

        it('ignores index answers for names it was not asked about', async () => {
            addJustUploaded({ filename: 'a.jpg' });
            await settleJustUploaded(async () => new Set(['zzz.jpg']));
            expect(names()).toEqual(['a.jpg']);
        });

        it('does not drop rows of the library the lookup started in if the user switched libraries meanwhile', async () => {
            addJustUploaded({ filename: 'a.jpg' });
            const pending = settleJustUploaded(async (n) => { scope.value = 'B'; return new Set(n); });
            expect(await pending).toBe(0);
            scope.value = 'A';
            expect(names()).toEqual(['a.jpg']);
        });

        it('a photo uploaded while the lookup is in flight is not swallowed by it', async () => {
            addJustUploaded({ filename: 'a.jpg' }, T0 + 1);
            const pending = settleJustUploaded(async (n) => {
                addJustUploaded({ filename: 'late.jpg' }, T0 + 2);
                return new Set(n);
            });
            await pending;
            expect(names()).toEqual(['late.jpg']);
        });

        it('revokes the local thumbnail URL when the index takes over', async () => {
            addJustUploaded({ filename: 'a.jpg', thumbnailUrl: 'blob:a' });
            await settleJustUploaded(async () => new Set(['a.jpg']));
            expect(revoked).toEqual(['blob:a']);
        });
    });
})
