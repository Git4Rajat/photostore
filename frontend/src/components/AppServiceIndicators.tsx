// Presentational header/status indicators that consume the AppServicesProvider
// context via useAppServices(). Extracted from AppServicesProvider.tsx: these
// components have no dependency on the provider's internal implementation,
// only on the public useAppServices() contract, so they can live independently
// of the (much larger) provider component itself.
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { BellIcon, ServerIcon, TrashIcon, UserGroupIcon, XMarkIcon } from '@heroicons/react/24/outline';
import { formatMegabytesPerSecond } from './browserAiShared';
import { useAppServices } from './AppServicesProvider';

export const NotificationBell: React.FC = () => {
    const {
        notifications,
        unreadCount,
        clearNotifications,
        markAllNotificationsRead,
    } = useAppServices();
    const [showNotifications, setShowNotifications] = useState<boolean>(false);
    const wrapRef = useRef<HTMLDivElement | null>(null);

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
                                    <p className="notification-details">{notification.details}</p>
                                    {notification.progress && (
                                        <>
                                            <div className="progress-track notification-progress-track">
                                                <div
                                                    className="progress-bar"
                                                    style={{
                                                        width: `${(notification.progress.uploadedCount / notification.progress.totalCount) * 100}%`,
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
            {overlay}
        </div>
    );
};

// Global icon shown while a people-clustering job (initial cluster, recluster,
// or "find more faces") is queued/running on the worker. Clustering runs out of
// band, so without this the user was blind to it happening. Data comes from the
// existing /api/jobs/status poll (see pollJobStatusesOnce).
export const ClusteringActivityIndicator: React.FC = () => {
    const { clusteringActive, clusteringStatusLabel, loadBrowserAiModel, browserAiButtonDisabled } = useAppServices();
    if (!clusteringActive) {
        return null;
    }
    return (
        <button
            type="button"
            className="btn btn-soft icon-btn"
            onClick={() => void loadBrowserAiModel()}
            disabled={browserAiButtonDisabled}
            aria-live="polite"
            aria-label={clusteringStatusLabel}
            title={clusteringStatusLabel}
        >
            <UserGroupIcon className="toolbar-icon bg-activity-icon" aria-hidden="true" />
            <span className="sr-only">{clusteringStatusLabel}</span>
        </button>
    );
};

// Global icon shown while ipwork (backend/both processing mode) is generating
// thumbnails/faces/vision data for at least one photo. ipwork runs off-screen
// on a scale-to-zero server container with no other persistent signal: the
// per-tile "processing on server" badge only lights up for a tile that's both
// on-screen and actively leased, and ipwork completions are deliberately kept
// out of the notification bell (one job per photo — see jobNotifications.ts)
// to avoid a toast-per-photo storm on a bulk upload. Without this, a large
// background batch was entirely invisible. Not a button — there's no action
// to take here, just a status.
export const IpworkActivityIndicator: React.FC = () => {
    const { ipworkActive, ipworkStatusLabel } = useAppServices();
    if (!ipworkActive) {
        return null;
    }
    return (
        <span
            className="btn btn-soft icon-btn"
            role="status"
            aria-live="polite"
            aria-label={ipworkStatusLabel}
            title={ipworkStatusLabel}
        >
            <ServerIcon className="toolbar-icon bg-activity-icon" aria-hidden="true" />
            <span className="sr-only">{ipworkStatusLabel}</span>
        </span>
    );
};
