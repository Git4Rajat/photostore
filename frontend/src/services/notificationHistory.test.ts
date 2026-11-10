import { describe, expect, it } from 'vitest';
import {
    loadNotificationHistory,
    mergeNotificationHistory,
    saveNotificationHistory,
} from './notificationHistory';
import type { AppNotification } from '../types/browserProcessing';

const note = (id: string, timestamp: number, extra: Partial<AppNotification> = {}): AppNotification => ({
    id, title: id, details: '', timestamp, unread: true, ...extra,
});

describe('notificationHistory', () => {
    it('merges history under live items, newest first, live winning on id', () => {
        const live = [note('a', 300, { title: 'live a' })];
        const history = [note('a', 100, { title: 'old a' }), note('b', 200), note('c', 400)];
        const merged = mergeNotificationHistory(live, history);
        expect(merged.map((n) => n.id)).toEqual(['c', 'a', 'b']);
        expect(merged.find((n) => n.id === 'a')?.title).toBe('live a');
    });

    it('caps the merged list at 200', () => {
        const history = Array.from({ length: 250 }, (_, i) => note(`n${i}`, i));
        expect(mergeNotificationHistory([], history)).toHaveLength(200);
    });

    it('degrades to empty when IndexedDB is unavailable', async () => {
        expect(typeof indexedDB).toBe('undefined');
        await expect(saveNotificationHistory('scope', [note('a', 1)])).resolves.toBeUndefined();
        await expect(loadNotificationHistory('scope')).resolves.toEqual([]);
    });
});
