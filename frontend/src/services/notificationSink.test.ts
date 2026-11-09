import { afterEach, describe, expect, it, vi } from 'vitest';
import { publishNotification, setNotificationSink } from './notificationSink';
import { showToast } from './toast';
import { formatNotificationTime } from './notificationTime';

describe('notification routing', () => {
    afterEach(() => {
        setNotificationSink(null);
        document.body.innerHTML = '';
    });

    it('reports false when no panel is registered', () => {
        expect(publishNotification('x')).toBe(false);
    });

    it('routes plain toasts into the panel and still previews them', () => {
        const sink = vi.fn();
        setNotificationSink(sink);
        showToast('Saved to panel', { dedupeKey: 'plain-1' });
        expect(sink).toHaveBeenCalledWith('Saved to panel', '');
        expect(document.querySelector('.app-toast')).not.toBeNull();
    });

    it('skips the panel entry when the caller already added one', () => {
        const sink = vi.fn();
        setNotificationSink(sink);
        showToast('Already in panel', { dedupeKey: 'nopanel-1', panel: false });
        expect(sink).not.toHaveBeenCalled();
        expect(document.querySelector('.app-toast')).not.toBeNull();
    });

    it('mirrors action toasts into the panel and keeps the popup with its action', () => {
        const sink = vi.fn();
        setNotificationSink(sink);
        showToast('Deleted', { dedupeKey: 'action-1', action: { label: 'Undo', onClick: () => {} } });
        expect(sink).toHaveBeenCalledTimes(1);
        expect(document.querySelector('.app-toast__action')).not.toBeNull();
    });

    it('falls back to the popup when no panel exists', () => {
        showToast('Public page', { dedupeKey: 'fallback-1' });
        expect(document.querySelector('.app-toast')).not.toBeNull();
    });
});

describe('formatNotificationTime', () => {
    const now = new Date(2026, 10, 9, 15, 30).getTime();
    const DAY = 24 * 60 * 60 * 1000;

    it('shows local time within the last 24 hours', () => {
        const ts = now - 3 * 60 * 60 * 1000;
        expect(formatNotificationTime(ts, now)).toBe(
            new Date(ts).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' }),
        );
    });

    it('says Yesterday between 24 and 48 hours', () => {
        expect(formatNotificationTime(now - 30 * 60 * 60 * 1000, now)).toBe('Yesterday');
    });

    it('shows a date up to 30 days', () => {
        const ts = now - 10 * DAY;
        expect(formatNotificationTime(ts, now)).toBe(
            new Date(ts).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }),
        );
        expect(formatNotificationTime(now - 30 * DAY, now)).not.toMatch(/mo/);
    });

    it('says N mo beyond 30 days', () => {
        expect(formatNotificationTime(now - 31 * DAY, now)).toBe('1 mo');
        expect(formatNotificationTime(now - 75 * DAY, now)).toBe('2 mo');
    });

    it('returns empty for invalid timestamps', () => {
        expect(formatNotificationTime(NaN)).toBe('');
    });
});
