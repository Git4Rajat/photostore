import React, { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { ArrowUturnLeftIcon, XMarkIcon } from '@heroicons/react/24/outline';
import { get, post } from '../../services/apiClient';
import faceService from '../../services/faceService';
import { EmptyState } from './EmptyState';
import { Loading } from './Loading';
import { notifyApiError } from '../../services/requestFeedback';
import { showToast } from '../../services/toast';

// Person-merge undo and album-restore are two independently-shipped
// reversible-action mechanisms with different storage shapes underneath
// (blob-snapshotted merge table vs. a plain flag on the album row) --
// unifying them at the storage layer wasn't worth it, so this normalizes
// both into one shape just for this list. Moved here from
// RecentlyDeletedPage.tsx, which now stays photos-only.
interface ActivityItem {
    id: string;
    kind: 'album' | 'merge';
    label: string;
    timestamp: string;
}

const formatTimestamp = (value: string): string => {
    if (!value) return '';
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return '';
    return parsed.toLocaleString(undefined, {
        year: 'numeric', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit',
    });
};

interface ActivityDrawerProps {
    open: boolean;
    onClose: () => void;
}

const ActivityDrawer: React.FC<ActivityDrawerProps> = ({ open, onClose }) => {
    const [activity, setActivity] = useState<ActivityItem[]>([]);
    const [loading, setLoading] = useState<boolean>(false);
    const [loaded, setLoaded] = useState<boolean>(false);
    const [busyId, setBusyId] = useState<string | null>(null);

    // Two independent, already-working endpoints -- no new backend aggregator,
    // just interleave by timestamp client-side (both lists are small: recent
    // merges + recently-deleted albums, not photo-library scale).
    const load = useCallback(async () => {
        setLoading(true);
        try {
            const [mergesResponse, albumsResponse] = await Promise.all([
                faceService.listMerges().catch(() => ({ merges: [] })),
                get('/albums/trash').catch(() => ({ albums: [] })),
            ]);
            const merges = Array.isArray(mergesResponse?.merges) ? mergesResponse.merges : [];
            const albums = Array.isArray(albumsResponse?.albums) ? albumsResponse.albums : [];

            const mergeItems: ActivityItem[] = merges.map((m: any) => {
                const mergedNames: string[] = Array.isArray(m?.mergedNames) ? m.mergedNames.filter(Boolean) : [];
                const targetName = m?.targetName || 'Unknown';
                const label = mergedNames.length > 0
                    ? `Merged ${mergedNames.join(', ')} → ${targetName}`
                    : `Merged into ${targetName}`;
                return { id: String(m?.mergeId || ''), kind: 'merge' as const, label, timestamp: String(m?.createdAt || '') };
            }).filter((item) => item.id);

            const albumItems: ActivityItem[] = albums.map((a: any): ActivityItem => ({
                id: String(a?.id || ''),
                kind: 'album' as const,
                label: `Album deleted: ${a?.name || 'Untitled album'}`,
                timestamp: String(a?.deletedAt || ''),
            })).filter((item: ActivityItem) => item.id);

            const combined: ActivityItem[] = [...mergeItems, ...albumItems].sort((a, b) => (a.timestamp < b.timestamp ? 1 : -1));
            setActivity(combined);
        } catch {
            setActivity([]);
        } finally {
            setLoading(false);
            setLoaded(true);
        }
    }, []);

    // Fetch lazily -- only once the drawer is actually opened, not on every
    // page load regardless of whether the user ever looks at it.
    useEffect(() => {
        if (open && !loaded) {
            void load();
        }
    }, [open, loaded, load]);

    useEffect(() => {
        if (!open || typeof document === 'undefined') {
            return undefined;
        }
        const handleKeyDown = (event: KeyboardEvent) => {
            if (event.key === 'Escape') {
                onClose();
            }
        };
        document.addEventListener('keydown', handleKeyDown);
        return () => document.removeEventListener('keydown', handleKeyDown);
    }, [open, onClose]);

    const handleUndo = async (item: ActivityItem) => {
        setBusyId(item.id);
        try {
            if (item.kind === 'merge') {
                await faceService.undoMerge(item.id);
            } else {
                await post(`/albums/${item.id}/restore`, {});
            }
            setActivity((prev) => prev.filter((i) => i.id !== item.id));
            showToast(item.kind === 'merge' ? 'Merge undone.' : 'Album restored.');
        } catch (err) {
            notifyApiError(err, { context: "Couldn't undo that action", retry: () => { void handleUndo(item); } });
        } finally {
            setBusyId(null);
        }
    };

    return (
        <div className={`activity-drawer${open ? ' open' : ''}`} aria-hidden={!open}>
            <button
                type="button"
                className="activity-drawer-backdrop"
                aria-label="Close recent activity"
                tabIndex={open ? 0 : -1}
                onClick={onClose}
            />
            <aside className="activity-drawer-panel" aria-label="Recent activity">
                <div className="activity-drawer-head">
                    <div>
                        <p className="app-menu-kicker">KEEPSAKE</p>
                        <p className="app-menu-title">Recent Activity</p>
                    </div>
                    <button
                        type="button"
                        className="btn btn-soft icon-btn"
                        onClick={onClose}
                        aria-label="Close recent activity"
                    >
                        <XMarkIcon className="toolbar-icon" />
                        <span className="sr-only">Close recent activity</span>
                    </button>
                </div>

                {loading && <Loading label="Loading activity…" fullPage={false} />}
                {!loading && activity.length === 0 && (
                    <EmptyState
                        icon={<ArrowUturnLeftIcon />}
                        title="Nothing to undo"
                        message="Album deletes and person merges you can undo will show up here."
                    />
                )}
                {!loading && activity.length > 0 && (
                    <div className="people-merge-history-list">
                        {activity.map((item) => (
                            <div key={item.id} className="people-merge-history-row">
                                <div className="people-merge-history-main">
                                    <div className="people-merge-history-title">
                                        <span className="people-merge-chip">{item.label}</span>
                                        {formatTimestamp(item.timestamp) && (
                                            <span className="people-merge-chip">{formatTimestamp(item.timestamp)}</span>
                                        )}
                                    </div>
                                </div>
                                <div className="people-merge-history-actions">
                                    <button
                                        type="button"
                                        className="btn btn-soft"
                                        disabled={busyId === item.id}
                                        onClick={() => void handleUndo(item)}
                                    >
                                        <ArrowUturnLeftIcon className="toolbar-icon" aria-hidden="true" />
                                        {busyId === item.id ? 'Undoing…' : 'Undo'}
                                    </button>
                                </div>
                            </div>
                        ))}
                    </div>
                )}

                <Link to="/trash" className="activity-drawer-footer-link" onClick={onClose}>
                    View Recently Deleted photos →
                </Link>
            </aside>
        </div>
    );
};

export default ActivityDrawer;
