// Presentational header/status indicators that consume the AppServicesProvider
// context via useAppServices(). Extracted from AppServicesProvider.tsx: these
// components have no dependency on the provider's internal implementation,
// only on the public useAppServices() contract, so they can live independently
// of the (much larger) provider component itself.
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Bell as BellIcon, CircleAlert as AlertIcon, CloudUpload as UploadIcon, Trash2 as TrashIcon, Users as PeopleIcon, X as XMarkIcon } from 'lucide-react';
import { IconButton } from './ui/Button';
import { formatMegabytesPerSecond } from './browserAiShared';
import { formatNotificationTime } from '../services/notificationTime';
import type { AppNotification } from '../types/browserProcessing';
import { useAppServices } from './AppServicesProvider';
import { useSwipeDismiss } from '../services/swipeDismiss';

// Problems read red; uploads and people get their own mark; the rest a bell.
type NotificationKind = 'warn' | 'upload' | 'people' | 'info';
const KIND_ICONS: Record<NotificationKind, React.ElementType> = { warn: AlertIcon, upload: UploadIcon, people: PeopleIcon, info: BellIcon };
const PROBLEM_WORDS = /\b(fail|failed|couldn.t|can.t|error|stopped|warning|limit|unavailable|reselect)\b/i;
export const notificationKind = (n: AppNotification): NotificationKind => {
    if ((n.progress?.failedCount ?? 0) > 0 || PROBLEM_WORDS.test(`${n.title} ${n.details}`)) return 'warn';
    if (n.progress || n.uploadSessionId || /upload/i.test(n.title)) return 'upload';
    if (/\b(people|person|faces?)\b/i.test(n.title)) return 'people';
    return 'info';
};

/** Progress entries are snapshots of one ongoing job: keep the newest of each
 *  title so earlier sessions' half-finished bars don't pile up. */
export const latestProgressOnly = (items: AppNotification[]): AppNotification[] => {
    const newest = new Map<string, number>();
    items.forEach((n) => { if (n.progress) newest.set(n.title, Math.max(newest.get(n.title) ?? 0, n.timestamp)); });
    return items.filter((n) => !n.progress || n.timestamp === newest.get(n.title));
};

const DAY_LABEL = new Intl.DateTimeFormat(undefined, { weekday: 'long', day: 'numeric', month: 'long' });
/** Newest first, grouped under Today / Yesterday / the date. */
export const groupByDay = (items: AppNotification[], now = new Date()): Array<[string, AppNotification[]]> => {
    const startOf = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
    const today = startOf(now);
    const groups = new Map<string, AppNotification[]>();
    [...items].sort((a, b) => b.timestamp - a.timestamp).forEach((n) => {
        const day = startOf(new Date(n.timestamp));
        const label = day === today ? 'Today' : day === today - 86_400_000 ? 'Yesterday' : DAY_LABEL.format(new Date(n.timestamp));
        groups.set(label, [...(groups.get(label) ?? []), n]);
    });
    return Array.from(groups.entries());
};

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
                            <IconButton label="Clear notifications" size="sm" onClick={clearNotifications}>
                                <TrashIcon className="toolbar-icon" />
                            </IconButton>
                            <IconButton label="Close notifications" size="sm" className="notification-close" onClick={closeNotifications}>
                                <XMarkIcon className="toolbar-icon" />
                            </IconButton>
                        </div>
                    </div>

                    {notifications.length === 0 ? (
                        <p className="status">Nothing new. Finished uploads and anything that needs you show up here.</p>
                    ) : (
                        <div className="notification-list">
                            {groupByDay(latestProgressOnly(notifications)).map(([day, items]) => (
                                <section key={day} className="notification-day" aria-label={day}>
                                    <h4 className="notification-day-label">{day}</h4>
                                    {items.map((notification) => {
                                        const kind = notificationKind(notification);
                                        const Icon = KIND_ICONS[kind];
                                        return (
                                            <article
                                                key={notification.id}
                                                className={`notification-item ${notification.unread ? 'unread' : ''}`}
                                            >
                                                <span className={`notification-icon ${kind}`} aria-hidden="true"><Icon /></span>
                                                <div className="notification-body">
                                                    <div className="notification-top">
                                                        <p className="notification-title">{notification.title}</p>
                                                        <time
                                                            className="notification-time"
                                                            dateTime={new Date(notification.timestamp).toISOString()}
                                                        >
                                                            {formatNotificationTime(notification.timestamp)}
                                                        </time>
                                                    </div>
                                                    {notification.details && <p className="notification-details">{notification.details}</p>}
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
                                                                {(notification.progress.mbPerSecond ?? 0) > 0 ? ` · ${formatMegabytesPerSecond(notification.progress.mbPerSecond)}` : ''}
                                                            </p>
                                                        </>
                                                    )}
                                                    {notification.uploadSessionId && (
                                                        <div className="notification-acts">
                                                            <button type="button" className="btn btn-primary btn-sm" disabled={uploadActive}
                                                                onClick={() => void retryPersistedUploadSession(notification.uploadSessionId)}>
                                                                Retry unfinished files
                                                            </button>
                                                        </div>
                                                    )}
                                                </div>
                                            </article>
                                        );
                                    })}
                                </section>
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


