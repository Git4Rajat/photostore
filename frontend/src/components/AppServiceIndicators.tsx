// Presentational header/status indicators that consume the AppServicesProvider
// context via useAppServices(). Extracted from AppServicesProvider.tsx: these
// components have no dependency on the provider's internal implementation,
// only on the public useAppServices() contract, so they can live independently
// of the (much larger) provider component itself.
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Bell as BellIcon, ChevronLeft as BackIcon, CloudUpload as UploadIcon, Link as LinkIcon, Mail as MailIcon, Pause as PauseIcon, RotateCw as RetryIcon, TriangleAlert as AlertIcon, Users as PeopleIcon } from 'lucide-react';
import { formatMegabytesPerSecond } from './browserAiShared';
import { formatNotificationClock, formatNotificationTime } from '../services/notificationTime';
import { getCachedSortThumb } from '../services/localSortIndex';
import { thumbnailUrlForBlob } from '../services/mediaToken';
import { resolveThumbnailAccessUrls } from '../services/thumbnailAccessCache';
import type { AppNotification, NotificationKind } from '../types/browserProcessing';
import { useAppServices } from './AppServicesProvider';
import { useSwipeDismiss } from '../services/swipeDismiss';

// Problems read red; uploads, people, shared albums and invites get their own mark; the rest a bell.
const KIND_ICONS: Record<NotificationKind, React.ElementType> = {
    warn: AlertIcon, paused: PauseIcon, upload: UploadIcon, people: PeopleIcon, share: LinkIcon, invite: MailIcon, info: BellIcon,
};
const PROBLEM_WORDS = /\b(fail|failed|couldn.t|can.t|error|stopped|warning|limit|unavailable|reselect)\b/i;
export const notificationKind = (n: AppNotification): NotificationKind => {
    // A deliberate pause is not a problem: calm colour and a pause mark, red only once something failed.
    if (n.story?.state === 'paused' && (n.progress?.failedCount ?? 0) === 0) return 'paused';
    if ((n.progress?.failedCount ?? 0) > 0 || PROBLEM_WORDS.test(`${n.title} ${n.details}`)) return 'warn';
    if (n.kind) return n.kind;
    if (n.progress || n.uploadSessionId || /upload/i.test(n.title)) return 'upload';
    if (/\b(people|person|faces?)\b/i.test(n.title)) return 'people';
    return 'info';
};

const countFmt = new Intl.NumberFormat();
/** "Paused 10:19 · resumed 10:21 · cancelled 10:34": what has happened to this one upload so far. */
export const storyHistory = (story: AppNotification['story']): string => {
    if (!story) return '';
    return [
        story.pausedAt ? `Paused ${formatNotificationClock(story.pausedAt)}` : '',
        story.resumedAt ? `${story.pausedAt ? 'resumed' : 'Resumed'} ${formatNotificationClock(story.resumedAt)}` : '',
        story.cancelledAt ? `${story.pausedAt || story.resumedAt ? 'cancelled' : 'Cancelled'} ${formatNotificationClock(story.cancelledAt)}` : '',
    ].filter(Boolean).join(' · ');
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

const STRIP_RETRIES = 30;
const STRIP_RETRY_MS = 4000;

/** A finished upload's few photos, as small square thumbnails. Thumbnails the
 *  loaded sort index already knows load directly; the rest are resolved in one
 *  batch. One that isn't made yet stays an empty square. */
const NotificationStrip: React.FC<{ filenames: string[] }> = ({ filenames }) => {
    const direct = (filename: string) => thumbnailUrlForBlob(getCachedSortThumb(filename));
    const [resolved, setResolved] = useState<Record<string, string>>({});
    const [broken, setBroken] = useState<Set<string>>(() => new Set());
    const [shown, setShown] = useState<Set<string>>(() => new Set());
    const [round, setRound] = useState(0);
    const key = filenames.join('|');
    // Right after an upload the server is still making these thumbnails, so ask again every few
    // seconds (for about two minutes) instead of leaving empty squares until the panel is reopened.
    useEffect(() => {
        const need = filenames.filter((filename) => !direct(filename) && (!resolved[filename] || broken.has(filename)));
        if (need.length === 0 || round > STRIP_RETRIES) return undefined;
        let active = true;
        const timer = window.setTimeout(() => {
            void resolveThumbnailAccessUrls(need).then((urls) => {
                if (!active) return;
                setResolved((prev) => ({ ...prev, ...Object.fromEntries(Array.from(urls).filter(([, url]) => url)) }));
                setBroken((prev) => new Set(Array.from(prev).filter((filename) => !need.includes(filename))));
                setRound((n) => n + 1);
            });
        }, round === 0 ? 0 : STRIP_RETRY_MS);
        return () => { active = false; window.clearTimeout(timer); };
    }, [key, round]); // eslint-disable-line react-hooks/exhaustive-deps
    return (
        <div className="notification-strip">
            {filenames.map((filename) => {
                const src = direct(filename) || resolved[filename];
                return (
                    <span key={filename} className={`notification-thumb${src && !broken.has(filename) && shown.has(filename) ? ' ready' : ''}`}>
                        {src && !broken.has(filename) && (
                            <img key={`${filename}:${round}`} src={src} alt="" loading="lazy" decoding="async"
                                onLoad={() => setShown((prev) => new Set(prev).add(filename))}
                                onError={() => setBroken((prev) => new Set(prev).add(filename))} />
                        )}
                    </span>
                );
            })}
        </div>
    );
};

const NO_IDS: ReadonlySet<string> = new Set();

/** The bell, and the panel it opens: a popover under the bell on desktops, a page of its own on phones. */
export const NotificationBell: React.FC<{ backLabel?: string }> = ({ backLabel = 'Back' }) => {
    const {
        notifications,
        unreadCount,
        clearNotifications,
        markAllNotificationsRead,
        retryPersistedUploadSession,
        resendInvite,
        uploadActive,
        retryingUpload,
        pauseActiveUpload,
        discardPersistedUploadSession,
        requestUpload,
    } = useAppServices();
    const [showNotifications, setShowNotifications] = useState<boolean>(false);
    // Opening the panel marks everything read (and acknowledges stale uploads),
    // but what was new when it opened keeps its dot -- and the bell its dot --
    // until Mark all read or the panel closes.
    const [seenUnread, setSeenUnread] = useState<ReadonlySet<string>>(NO_IDS);
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
        setSeenUnread(NO_IDS);
    }, []);
    const markRead = () => {
        markAllNotificationsRead();
        setSeenUnread(NO_IDS);
    };

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

    const isNew = (n: AppNotification) => n.unread || seenUnread.has(n.id);
    const newCount = notifications.filter(isNew).length;

    const overlay = showNotifications
        ? (
            <div
                className="notification-pane"
                role="dialog"
                aria-label="Notifications"
            >
                    <div className="notification-head">
                        <button type="button" className="notification-back" onClick={closeNotifications} aria-label={`Back to ${backLabel}`}>
                            <BackIcon aria-hidden="true" />{backLabel}
                        </button>
                        <h3 className="notification-heading">Notifications</h3>
                        {newCount > 0 && (
                            <button type="button" className="notification-link notification-mark" onClick={markRead}>Mark all read</button>
                        )}
                    </div>

                    {notifications.length === 0 ? (
                        <div className="notification-empty">
                            <span className="notification-icon" aria-hidden="true"><BellIcon /></span>
                            <p className="notification-title">Nothing new</p>
                            <p className="notification-details">Finished uploads and anything that needs you show up here.</p>
                        </div>
                    ) : (
                        <div className="notification-list">
                            {groupByDay(latestProgressOnly(notifications)).map(([day, items]) => (
                                <section key={day} className="notification-day" aria-label={day}>
                                    <h4 className="notification-day-label">{day}</h4>
                                    {items.map((notification) => {
                                        const kind = notificationKind(notification);
                                        const Icon = KIND_ICONS[kind];
                                        const progress = notification.progress;
                                        // A bar only while the job is still going; a finished one says how it went in words.
                                        const running = progress && progress.uploadedCount + progress.failedCount < progress.totalCount;
                                        const story = notification.story;
                                        const paused = story?.state === 'paused';
                                        const history = storyHistory(story);
                                        // Duplicates found before uploading are a quieter line of their own, not part of added/total.
                                        const leftOut = story && ['uploading', 'paused', 'done'].includes(story.state) ? progress?.skippedDuplicateCount ?? 0 : 0;
                                        const title = story?.state === 'uploading' && progress
                                            ? `Uploading ${countFmt.format(progress.uploadedCount)} of ${countFmt.format(progress.totalCount)}`
                                            : notification.title;
                                        return (
                                            <article
                                                key={notification.id}
                                                className={`notification-item ${isNew(notification) ? 'unread' : ''}`}
                                            >
                                                <span className={`notification-icon ${kind}`} aria-hidden="true"><Icon /></span>
                                                <div className="notification-body">
                                                    <div className="notification-top">
                                                        <p className="notification-title">
                                                            {isNew(notification) && <span className="sr-only">New: </span>}
                                                            {title}
                                                        </p>
                                                        <time
                                                            className="notification-time"
                                                            dateTime={new Date(notification.timestamp).toISOString()}
                                                        >
                                                            {formatNotificationClock(notification.timestamp)}
                                                        </time>
                                                    </div>
                                                    {history && <p className="notification-details notification-history">{history}</p>}
                                                    {notification.details && story?.state !== 'uploading' && <p className="notification-details">{notification.details}</p>}
                                                    {running && (
                                                        <>
                                                            <div className={`notification-progress${paused ? ' paused' : ''}`} aria-hidden="true">
                                                                <i
                                                                    style={{
                                                                        width: `${progress.totalCount > 0
                                                                            ? (progress.uploadedCount / progress.totalCount) * 100
                                                                            : 100}%`,
                                                                    }}
                                                                />
                                                            </div>
                                                            {!paused && <p className="notification-details">
                                                                {progress.uploadedCount}/{progress.totalCount}
                                                                {progress.failedCount > 0
                                                                    ? ` (Failed ${progress.failedCount})`
                                                                    : ''}
                                                                {progress.skippedDuplicateCount > 0 && !story
                                                                    ? ` (Skipped duplicates ${progress.skippedDuplicateCount})`
                                                                    : ''}
                                                                {(progress.mbPerSecond ?? 0) > 0 ? ` · ${formatMegabytesPerSecond(progress.mbPerSecond)}` : ''}
                                                            </p>}
                                                        </>
                                                    )}
                                                    {leftOut > 0 && <p className="notification-details notification-history">{countFmt.format(leftOut)} already in your library, left out</p>}
                                                    {notification.photos && notification.photos.length > 0 && (
                                                        <NotificationStrip filenames={notification.photos} />
                                                    )}
                                                    {notification.inviteId && (
                                                        <div className="notification-acts">
                                                            <button type="button" className="btn btn-soft btn-sm"
                                                                onClick={() => void resendInvite(notification.id)}>
                                                                Resend
                                                            </button>
                                                        </div>
                                                    )}
                                                    {story && (
                                                        <div className="notification-acts">
                                                            {story.state === 'uploading' && (
                                                                <button type="button" className="btn btn-soft btn-sm" disabled={!uploadActive} onClick={() => pauseActiveUpload()}>Pause</button>
                                                            )}
                                                            {paused && (
                                                                <>
                                                                    <button type="button" className="btn btn-primary btn-sm" disabled={uploadActive || retryingUpload}
                                                                        onClick={() => void retryPersistedUploadSession(story.sessionId)}>Resume</button>
                                                                    <button type="button" className="btn btn-soft btn-sm" disabled={uploadActive || retryingUpload}
                                                                        onClick={() => void discardPersistedUploadSession()}>Discard</button>
                                                                </>
                                                            )}
                                                            {story.state === 'cancelled' && (
                                                                <button type="button" className="btn btn-soft btn-sm" disabled={uploadActive} onClick={() => requestUpload()}>Choose again</button>
                                                            )}
                                                            {story.state === 'failed' && (
                                                                <button type="button" className="btn btn-primary btn-sm" disabled={uploadActive || retryingUpload}
                                                                    onClick={() => void retryPersistedUploadSession(story.sessionId)}>
                                                                    <RetryIcon aria-hidden="true" />Try again
                                                                </button>
                                                            )}
                                                        </div>
                                                    )}
                                                    {notification.uploadSessionId && !story && (
                                                        <div className="notification-acts">
                                                            <button type="button" className="btn btn-primary btn-sm" disabled={uploadActive || retryingUpload}
                                                                onClick={() => void retryPersistedUploadSession(notification.uploadSessionId)}>
                                                                <RetryIcon aria-hidden="true" />Retry unfinished files
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

                    <div className="notification-foot">
                        <span>Kept for 30 days</span>
                        {notifications.length > 0 && (
                            <button type="button" className="notification-link" onClick={clearNotifications}>Clear all</button>
                        )}
                    </div>
            </div>
        )
        : null;

    const previewKind = preview ? notificationKind(preview) : 'info';
    const PreviewIcon = KIND_ICONS[previewKind];

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
                    if (showNotifications) {
                        closeNotifications();
                        return;
                    }
                    setShowNotifications(true);
                    setSeenUnread(new Set(notifications.filter((n) => n.unread).map((n) => n.id)));
                    markAllNotificationsRead();
                }}
                className={`btn btn-soft notification-bell${showNotifications ? ' open' : ''}`}
                aria-label={unreadCount > 0 ? `Open notifications, ${unreadCount} new` : "Open notifications"}
                aria-expanded={showNotifications}
                aria-haspopup="dialog"
            >
                <BellIcon className="toolbar-icon" />
                {newCount > 0 && <span className="notification-badge">{newCount}</span>}
            </button>
            {preview && !showNotifications && (
                <div
                    key={`${preview.id}:${preview.timestamp}`}
                    ref={previewRef}
                    className="notification-preview"
                    role="status"
                    aria-live="polite"
                >
                    <span className={`notification-icon ${previewKind}`} aria-hidden="true"><PreviewIcon /></span>
                    <div className="notification-body">
                        <div className="notification-top">
                            <p className="notification-title">{preview.title}</p>
                            <time className="notification-time" dateTime={new Date(preview.timestamp).toISOString()}>
                                {formatNotificationTime(preview.timestamp)}
                            </time>
                        </div>
                        {preview.details && <p className="notification-details">{preview.details}</p>}
                        {preview.action && (
                            <div className="notification-acts">
                                <button
                                    type="button"
                                    className="btn btn-soft btn-sm"
                                    onClick={() => {
                                        try { preview.action?.onClick(); } finally { setPreview(null); }
                                    }}
                                >
                                    {preview.action.label}
                                </button>
                            </div>
                        )}
                    </div>
                </div>
            )}
            {overlay}
        </div>
    );
};
