import { describe, expect, it } from 'vitest';
import { groupByDay, latestProgressOnly, notificationKind } from './AppServiceIndicators';
import type { AppNotification } from '../types/browserProcessing';

const note = (id: string, timestamp: number, extra: Partial<AppNotification> = {}): AppNotification => ({
    id, title: id, details: '', timestamp, unread: true, ...extra,
});
const progress = { uploadedCount: 1, totalCount: 2, failedCount: 0, skippedDuplicateCount: 0, mbPerSecond: 0 } as AppNotification['progress'];

describe('notification panel', () => {
    it('groups newest first under Today, Yesterday and the date', () => {
        const now = new Date(2026, 10, 16, 12, 0);
        const groups = groupByDay([
            note('old', new Date(2026, 10, 10, 9).getTime()),
            note('today', new Date(2026, 10, 16, 9).getTime()),
            note('yesterday', new Date(2026, 10, 15, 22).getTime()),
        ], now);
        expect(groups.map(([label, items]) => [label, items.map((n) => n.id)])).toEqual([
            ['Today', ['today']],
            ['Yesterday', ['yesterday']],
            [expect.stringContaining('10'), ['old']],
        ]);
    });

    it('keeps only the newest progress entry of each kind', () => {
        const items = [
            note('a', 1, { title: 'Processing photos', progress }),
            note('b', 3, { title: 'Processing photos', progress }),
            note('c', 2, { title: 'Uploaded' }),
        ];
        expect(latestProgressOnly(items).map((n) => n.id)).toEqual(['b', 'c']);
    });

    it('marks problems, uploads and people', () => {
        expect(notificationKind(note('x', 1, { title: '1 video couldn’t be processed' }))).toBe('warn');
        expect(notificationKind(note('x', 1, { title: 'Upload', progress: { ...progress!, failedCount: 2 } }))).toBe('warn');
        expect(notificationKind(note('x', 1, { title: 'Upload complete' }))).toBe('upload');
        expect(notificationKind(note('x', 1, { title: 'People grouping finished' }))).toBe('people');
        expect(notificationKind(note('x', 1, { title: 'Album shared' }))).toBe('info');
    });
});
