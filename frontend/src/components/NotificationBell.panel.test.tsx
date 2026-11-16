import React from 'react';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { AppNotification } from '../types/browserProcessing';

let services: Record<string, unknown>;
vi.mock('./AppServicesProvider', () => ({ useAppServices: () => services }));
vi.mock('../services/thumbnailAccessCache', () => ({
    resolveThumbnailAccessUrls: async (filenames: string[]) => new Map(filenames.map((f) => [f, f === 'missing.jpg' ? '' : `https://blob/${f}`])),
}));
import { NotificationBell } from './AppServiceIndicators';

const note = (over: Partial<AppNotification>): AppNotification => ({
    id: 'a', title: 'Hello', details: '', timestamp: Date.now(), unread: true, ...over,
});

// Mirrors the provider: marking read flips every entry and re-renders.
const mount = (notifications: AppNotification[]) => {
    const state = { notifications };
    services = {
        get notifications() { return state.notifications; },
        get unreadCount() { return state.notifications.filter((n) => n.unread).length; },
        clearNotifications: vi.fn(() => { state.notifications = []; }),
        markAllNotificationsRead: vi.fn(() => { state.notifications = state.notifications.map((n) => ({ ...n, unread: false })); }),
        retryPersistedUploadSession: vi.fn(),
        uploadActive: false,
    };
    const view = render(<NotificationBell backLabel="Albums" />);
    const rerender = () => view.rerender(<NotificationBell backLabel="Albums" />);
    return { view, rerender };
};

describe('notification panel', () => {
    afterEach(() => cleanup());

    it('marks everything read on open but keeps the new entries dotted until Mark all read', () => {
        const { view, rerender } = mount([note({ id: 'new' }), note({ id: 'old', unread: false, timestamp: Date.now() - 1000 })]);
        const bell = screen.getByLabelText('Open notifications, 1 new');
        act(() => { fireEvent.click(bell); });
        rerender();
        expect(services.markAllNotificationsRead).toHaveBeenCalledTimes(1);
        expect(bell.getAttribute('aria-expanded')).toBe('true');
        expect(view.container.querySelectorAll('.notification-item.unread')).toHaveLength(1);
        expect(view.container.querySelector('.notification-badge')).not.toBeNull();

        act(() => { fireEvent.click(screen.getByText('Mark all read')); });
        rerender();
        expect(view.container.querySelectorAll('.notification-item.unread')).toHaveLength(0);
        expect(view.container.querySelector('.notification-badge')).toBeNull();
        expect(screen.queryByText('Mark all read')).toBeNull();
    });

    it('draws a finished upload with its photos and no bar, and a running one with a bar', async () => {
        const done = { uploadedCount: 3, totalCount: 3, failedCount: 0, skippedDuplicateCount: 0, mbPerSecond: 0 };
        const { view } = mount([
            note({ id: 'done', title: '3 items uploaded', progress: done, photos: ['a.jpg', 'missing.jpg'] }),
            note({ id: 'run', title: 'Upload in progress', timestamp: Date.now() - 5000, progress: { ...done, uploadedCount: 1 } }),
        ]);
        act(() => { fireEvent.click(screen.getByLabelText(/^Open notifications/)); });
        await act(async () => { await Promise.resolve(); });
        const [finished, running] = Array.from(view.container.querySelectorAll('.notification-item'));
        expect(finished.querySelector('.notification-progress')).toBeNull();
        expect(Array.from(finished.querySelectorAll('.notification-thumb img')).map((img) => img.getAttribute('src'))).toEqual(['https://blob/a.jpg']);
        expect(finished.querySelectorAll('.notification-thumb')).toHaveLength(2);
        expect(running.querySelector('.notification-progress')).not.toBeNull();
    });

    it('closes from the back link, and clears from the footer', () => {
        mount([note({})]);
        act(() => { fireEvent.click(screen.getByLabelText(/^Open notifications/)); });
        act(() => { fireEvent.click(screen.getByRole('button', { name: 'Back to Albums' })); });
        expect(screen.queryByRole('dialog', { name: 'Notifications' })).toBeNull();

        act(() => { fireEvent.click(screen.getByLabelText(/^Open notifications/)); });
        expect(screen.getByText('Kept for 30 days')).not.toBeNull();
        act(() => { fireEvent.click(screen.getByText('Clear all')); });
        expect(services.clearNotifications).toHaveBeenCalledTimes(1);
    });
});
