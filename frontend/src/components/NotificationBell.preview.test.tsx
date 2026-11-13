import React from 'react';
import { act, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AppNotification } from '../types/browserProcessing';

let services: Record<string, unknown>;
vi.mock('./AppServicesProvider', () => ({ useAppServices: () => services }));
import { NotificationBell } from './AppServiceIndicators';
import { EXIT_MS } from '../services/swipeDismiss';
import { gesture } from '../test/pointerGesture';

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

    it('closes early on a tap or a flick, and leaves the action to its button', () => {
        services = base([]);
        const view = render(<NotificationBell />);
        services = base([note({ id: 'c', title: 'Saved' })]);
        view.rerender(<NotificationBell />);
        const preview = view.container.querySelector('.notification-preview')!;
        act(() => { gesture(preview, 0, 0, 100); });
        act(() => { vi.advanceTimersByTime(EXIT_MS); });
        expect(view.container.querySelector('.notification-preview')).toBeNull();

        const onClick = vi.fn();
        services = base([note({ id: 'd', title: 'Deleted', timestamp: Date.now() + 1, action: { label: 'Undo', onClick } })]);
        view.rerender(<NotificationBell />);
        const next = view.container.querySelector('.notification-preview')!;
        act(() => { gesture(next, 0, 0, 100, view.getByText('Undo')); });
        act(() => { vi.advanceTimersByTime(EXIT_MS); });
        expect(view.container.querySelector('.notification-preview')).not.toBeNull();
        act(() => { gesture(next, 0, -90, 600); });
        act(() => { vi.advanceTimersByTime(EXIT_MS); });
        expect(view.container.querySelector('.notification-preview')).toBeNull();
        expect(onClick).not.toHaveBeenCalled();
    });
});
