import React, { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { ArrowUturnLeftIcon } from '@heroicons/react/24/outline';
import { get, post } from '../services/apiClient';
import faceService from '../services/faceService';
import { EmptyState } from './shared/EmptyState';
import { Loading } from './shared/Loading';
import { notifyApiError } from '../services/requestFeedback';
import { showToast } from '../services/toast';
import { plural } from '../utils/format';

// Person-merge undo and album-restore are two independently-shipped
// reversible-action mechanisms with different storage shapes underneath
// (blob-snapshotted merge table vs. a plain flag on the album row) --
// unifying them at the storage layer wasn't worth it, so this normalizes
// both into one shape just for this list. Formerly a slide-in drawer
// (ActivityDrawer); moved to a real page so it has its own URL like the
// rest of the mockup's full-page frames.
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

// Same formula as RecentlyDeletedPage.tsx's daysUntil().
const daysUntil = (isoDate: string): number | null => {
    if (!isoDate) return null;
    const target = new Date(isoDate).getTime();
    if (Number.isNaN(target)) return null;
    return Math.max(0, Math.ceil((target - Date.now()) / (24 * 60 * 60 * 1000)));
};

interface TrashSummary {
    total: number;
    earliestPurgeAt: string | null;
}

const ActivityPage: React.FC = () => {
    const [activity, setActivity] = useState<ActivityItem[]>([]);
    const [trashSummary, setTrashSummary] = useState<TrashSummary | null>(null);
    const [loading, setLoading] = useState<boolean>(true);
    const [busyId, setBusyId] = useState<string | null>(null);
    const [restoringAll, setRestoringAll] = useState<boolean>(false);

    // Three independent, already-working endpoints -- no new backend
    // aggregator, just interleave by timestamp client-side (all three lists
    // are small: recent merges + recently-deleted albums + one cheap
    // trash-summary page, not photo-library scale).
    const load = useCallback(async () => {
        setLoading(true);
        try {
            const [mergesResponse, albumsResponse, trashResponse] = await Promise.all([
                faceService.listMerges().catch(() => ({ merges: [] })),
                get('/albums/trash').catch(() => ({ albums: [] })),
                get('/photos/trash?limit=1').catch(() => null),
            ]);
            const merges = Array.isArray(mergesResponse?.merges) ? mergesResponse.merges : [];
            const albums = Array.isArray(albumsResponse?.albums) ? albumsResponse.albums : [];
            if (trashResponse && typeof trashResponse.total === 'number' && trashResponse.total > 0) {
                setTrashSummary({ total: trashResponse.total, earliestPurgeAt: trashResponse.earliestPurgeAt || null });
            } else {
                setTrashSummary(null);
            }

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
        }
    }, []);

    useEffect(() => {
        void load();
    }, [load]);

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

    const handleRestoreAll = async () => {
        setRestoringAll(true);
        try {
            const response = await post('/photos/trash/restore-all', {});
            const restoredCount = Array.isArray(response?.restored) ? response.restored.length : 0;
            setTrashSummary(null);
            showToast(restoredCount > 0 ? `Restored ${restoredCount === 1 ? '1 photo' : `${restoredCount} photos`}.` : 'Nothing to restore.');
        } catch (err) {
            notifyApiError(err, { context: "Couldn't restore those photos", retry: () => { void handleRestoreAll(); } });
        } finally {
            setRestoringAll(false);
        }
    };

    return (
        <section className="card-glass gallery-wrap">
            <header className="page-topline">
                <h2 className="page-topline-title">Recent Activity</h2>
            </header>

            <div className="activity-page-panel">
                {loading && <Loading label="Loading activity…" fullPage={false} />}
                {!loading && activity.length === 0 && (
                    <EmptyState
                        icon={<ArrowUturnLeftIcon />}
                        title="Nothing to undo"
                        message="Album deletes and person merges you can undo will show up here."
                    />
                )}
                {!loading && activity.length > 0 && (
                    <div className="activity-list">
                        {activity.map((item) => (
                            <div key={item.id} className="activity-row">
                                <span className="activity-row-label">{item.label}</span>
                                {formatTimestamp(item.timestamp) && (
                                    <span className="activity-row-when">{formatTimestamp(item.timestamp)}</span>
                                )}
                                <button
                                    type="button"
                                    className="activity-row-undo"
                                    disabled={busyId === item.id}
                                    onClick={() => void handleUndo(item)}
                                >
                                    {busyId === item.id ? 'Undoing…' : 'Undo'}
                                </button>
                            </div>
                        ))}
                    </div>
                )}

                {trashSummary && (
                    <div className="activity-trash-strip">
                        <span>
                            Recently Deleted — {plural(trashSummary.total, 'photo')}
                            {trashSummary.earliestPurgeAt && (() => {
                                const days = daysUntil(trashSummary.earliestPurgeAt as string);
                                return days !== null ? `, purges in ${plural(days, 'day')}` : '';
                            })()}
                        </span>
                        <button
                            type="button"
                            className="activity-trash-strip-restore"
                            disabled={restoringAll}
                            onClick={() => void handleRestoreAll()}
                        >
                            {restoringAll ? 'Restoring…' : 'Restore all'}
                        </button>
                    </div>
                )}

                <Link to="/trash" className="activity-drawer-footer-link">
                    View Recently Deleted photos →
                </Link>
            </div>
        </section>
    );
};

export default ActivityPage;
