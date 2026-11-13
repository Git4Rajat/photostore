// Presentational header/status indicators that consume the AppServicesProvider
// context via useAppServices(). Extracted from AppServicesProvider.tsx: these
// components have no dependency on the provider's internal implementation,
// only on the public useAppServices() contract, so they can live independently
// of the (much larger) provider component itself.
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { BellIcon, TrashIcon, XMarkIcon } from '@heroicons/react/24/outline';
import { formatMegabytesPerSecond } from './browserAiShared';
import { formatNotificationTime } from '../services/notificationTime';
import type { AppNotification } from '../types/browserProcessing';
import { useAppServices } from './AppServicesProvider';
import { useSwipeDismiss } from '../services/swipeDismiss';

export const NotificationBell: React.FC = () => {
    const {
        notifications,
        unreadCount,
        clearNotifications,
        markAllNotificationsRead,
        retryPersistedUploadSession,
        uploadActive,
    } = useAppServices();
    const [showNotifications, setShowNotifications] = useState<boolean>(false);
    const wrapRef = useRef<HTMLDivElement | null>(null);

    // Every new notification previews under the bell for a few seconds, the same
    // way for all of them. Progress entries update constantly, so they're skipped.
    const [preview, setPreview] = useState<AppNotification | null>(null);
    const lastPreviewedRef = useRef<string | null>(null);
    useEffect(() => {
        const latest = notifications.reduce<AppNotification | null>(
            (best, n) => (!n.progress && n.unread && (!best || n.timestamp > best.timestamp) ? n : best), null);
        const key = latest ? `${latest.id}:${latest.timestamp}` : null;
        if (lastPreviewedRef.current === null) {
            lastPreviewedRef.current = key ?? '';
            return;
        }
        if (!latest || key === lastPreviewedRef.current) {
            return;
        }
        lastPreviewedRef.current = key;
        if (!showNotifications) {
            setPreview(latest);
        }
    }, [notifications, showNotifications]);
    useEffect(() => {
        if (!preview) {
            return undefined;
        }
        const timer = window.setTimeout(() => setPreview(null), preview.action ? 8000 : 5000);
        return () => window.clearTimeout(timer);
    }, [preview]);
    useEffect(() => {
        if (showNotifications) {
            setPreview(null);
        }
    }, [showNotifications]);
    // A tap on the preview, or a flick in any direction, closes it early.
    const previewRef = useSwipeDismiss<HTMLDivElement>(() => setPreview(null));

    const closeNotifications = useCallback(() => {
        setShowNotifications(false);
    }, []);

    // Close the pane when clicking outside of it or pressing Escape. The pane
    // itself is anchored to the bell in normal flow, so it scrolls along with
    // the header rather than floating over the page.
    useEffect(() => {
        if (!showNotifications || typeof document === 'undefined') {
            return undefined;
        }

        const handlePointerDown = (event: MouseEvent) => {
            if (wrapRef.current && !wrapRef.current.contains(event.target as Node)) {
                closeNotifications();
            }
        };

        const handleKeyDown = (event: KeyboardEvent) => {
            if (event.key === 'Escape') {
                closeNotifications();
            }
        };

        document.addEventListener('mousedown', handlePointerDown);
        document.addEventListener('keydown', handleKeyDown);

        return () => {
            document.removeEventListener('mousedown', handlePointerDown);
            document.removeEventListener('keydown', handleKeyDown);
        };
    }, [showNotifications, closeNotifications]);

    const overlay = showNotifications
        ? (
            <div
                className="notification-pane card-glass"
                role="dialog"
                aria-label="Notifications"
            >
                    <div className="notification-head">
                        <h3 className="toolbar-title">Notifications</h3>
                        <div className="notification-actions">
                            <button
                                type="button"
                                className="btn btn-soft icon-btn"
                                onClick={clearNotifications}
                                aria-label="Clear notifications"
                            >
                                <TrashIcon className="toolbar-icon" />
                                <span className="sr-only">Clear notifications</span>
                            </button>
                            <button
                                type="button"
                                className="btn btn-soft icon-btn notification-close"
                                onClick={closeNotifications}
                                aria-label="Close notifications"
                            >
                                <XMarkIcon className="toolbar-icon" />
                                <span className="sr-only">Close notifications</span>
                            </button>
                        </div>
                    </div>

                    {notifications.length === 0 ? (
                        <p className="status">No notifications yet.</p>
                    ) : (
                        <div className="notification-list">
                            {notifications.map((notification) => (
                                <article
                                    key={notification.id}
                                    className={`notification-item ${notification.unread ? 'unread' : ''}`}
                                >
                                    <p className="notification-title">{notification.title}</p>
                                    {notification.details && <p className="notification-details">{notification.details}</p>}
                                    <time
                                        className="notification-time"
                                        dateTime={new Date(notification.timestamp).toISOString()}
                                    >
                                        {formatNotificationTime(notification.timestamp)}
                                    </time>
                                    {notification.uploadSessionId && (
                                        <button type="button" className="btn btn-soft" disabled={uploadActive}
                                            onClick={() => void retryPersistedUploadSession(notification.uploadSessionId)}>
                                            Retry unfinished files
                                        </button>
                                    )}
                                    {notification.progress && (
                                        <>
                                            <div className="progress-track notification-progress-track">
                                                <div
                                                    className="progress-bar"
                                                    style={{
                                                        width: `${notification.progress.totalCount > 0
                                                            ? (notification.progress.uploadedCount / notification.progress.totalCount) * 100
                                                            : 100}%`,
                                                    }}
                                                />
                                            </div>
                                            <p className="notification-details">
                                                {notification.progress.uploadedCount}/{notification.progress.totalCount}
                                                {notification.progress.failedCount > 0
                                                    ? ` (Failed ${notification.progress.failedCount})`
                                                    : ''}
                                                {notification.progress.skippedDuplicateCount > 0
                                                    ? ` (Skipped duplicates ${notification.progress.skippedDuplicateCount})`
                                                    : ''}
                                                {` · ${formatMegabytesPerSecond(notification.progress.mbPerSecond)}`}
                                            </p>
                                        </>
                                    )}
                                </article>
                            ))}
                        </div>
                    )}
            </div>
        )
        : null;

    return (
        <div className="notification-wrap" ref={wrapRef}>
            <button
                type="button"
                onClick={() => {
                    // Deliberately not the setShowNotifications(prev => ...)
                    // updater form: React can invoke that updater outside a
                    // normal event context, and it was calling
                    // markAllNotificationsRead() (a DIFFERENT component's
                    // state setter, from AppServicesProvider) from inside it
                    // -- triggering "Cannot update a component while
                    // rendering a different component". A plain click handler
                    // always sees the latest showNotifications from this
                    // render, so there's no staleness risk in reading it
                    // directly instead.
                    const next = !showNotifications;
                    setShowNotifications(next);
                    if (next) {
                        markAllNotificationsRead();
                    }
                }}
                className="btn btn-soft notification-bell"
                aria-label="Open notifications"
            >
                <BellIcon className="toolbar-icon" />
                {unreadCount > 0 && <span className="notification-badge">{unreadCount}</span>}
            </button>
            {preview && !showNotifications && (
                <div
                    key={`${preview.id}:${preview.timestamp}`}
                    ref={previewRef}
                    className="notification-preview card-glass"
                    role="status"
                    aria-live="polite"
                >
                    <p className="notification-title">{preview.title}</p>
                    {preview.details && <p className="notification-details">{preview.details}</p>}
                    <time className="notification-time" dateTime={new Date(preview.timestamp).toISOString()}>
                        {formatNotificationTime(preview.timestamp)}
                    </time>
                    {preview.action && (
                        <button
                            type="button"
                            className="btn btn-soft"
                            onClick={() => {
                                try { preview.action?.onClick(); } finally { setPreview(null); }
                            }}
                        >
                            {preview.action.label}
                        </button>
                    )}
                </div>
            )}
            {overlay}
        </div>
    );
};


