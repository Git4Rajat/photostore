import React from 'react';
import { act, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AppNotification } from '../types/browserProcessing';

let services: Record<string, unknown>;
vi.mock('./AppServicesProvider', () => ({ useAppServices: () => services }));
import { NotificationBell } from './AppServiceIndicators';

const note = (over: Partial<AppNotification>): AppNotification => ({
    id: 'a', title: 'Hello', details: 'World', timestamp: Date.now(), unread: true, ...over,
});

const base = (notifications: AppNotification[]) => ({
    notifications, unreadCount: notifications.length, clearNotifications: vi.fn(),
    markAllNotificationsRead: vi.fn(), retryPersistedUploadSession: vi.fn(), uploadActive: false,
});

describe('NotificationBell preview', () => {
    beforeEach(() => vi.useFakeTimers());
    afterEach(() => vi.useRealTimers());

    it('previews a new notification under the bell, then hides it', () => {
        services = base([]);
        const view = render(<NotificationBell />);
        expect(view.container.querySelector('.notification-preview')).toBeNull();

        services = base([note({})]);
        view.rerender(<NotificationBell />);
        const preview = view.container.querySelector('.notification-preview');
        expect(preview?.textContent).toContain('Hello');
        expect(preview?.querySelector('time')).not.toBeNull();

        act(() => { vi.advanceTimersByTime(5100); });
        expect(view.container.querySelector('.notification-preview')).toBeNull();
    });

    it('does not preview notifications that existed on mount', () => {
        services = base([note({})]);
        const view = render(<NotificationBell />);
        expect(view.container.querySelector('.notification-preview')).toBeNull();
    });

    it('shows the action button on the preview', () => {
        services = base([]);
        const view = render(<NotificationBell />);
        const onClick = vi.fn();
        services = base([note({ id: 'b', title: 'Deleted', action: { label: 'Undo', onClick } })]);
        view.rerender(<NotificationBell />);
        act(() => { view.getByText('Undo').click(); });
        expect(onClick).toHaveBeenCalled();
        expect(view.container.querySelector('.notification-preview')).toBeNull();
    });
});
