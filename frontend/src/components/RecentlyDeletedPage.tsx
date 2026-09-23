import React, { useCallback, useEffect, useState } from 'react';
import { TrashIcon, CheckIcon, ArrowUturnLeftIcon } from '@heroicons/react/24/outline';
import { get, post } from '../services/apiClient';
import faceService from '../services/faceService';
import PhotoTile from './shared/PhotoTile';
import { EmptyState } from './shared/EmptyState';
import { Loading } from './shared/Loading';
import { ErrorState } from './shared/ErrorState';
import { confirmDialog } from './shared/dialogs';
import { classifyApiError, type ApiError } from '../services/apiError';
import { notifyApiError } from '../services/requestFeedback';
import { showToast } from '../services/toast';
import { useBackendRecoveryRetry } from '../services/useBackendRecoveryRetry';
import { useThumbnailAccessResolver } from '../services/useThumbnailAccessResolver';
import { plural } from '../utils/format';
import type { Photo } from '../types/uiTypes';

interface TrashedPhoto extends Photo {
    deletedAt: string;
    purgeAt: string;
}

// Person-merge undo and album-restore are two independently-shipped
// reversible-action mechanisms with different storage shapes underneath
// (blob-snapshotted merge table vs. a plain flag on the album row) --
// unifying them at the storage layer wasn't worth it, so this normalizes
// both into one shape just for this list instead.
interface ActivityItem {
    id: string;
    kind: 'album' | 'merge';
    label: string;
    timestamp: string;
}

const daysUntil = (isoDate: string): number | null => {
    if (!isoDate) return null;
    const target = new Date(isoDate).getTime();
    if (Number.isNaN(target)) return null;
    return Math.max(0, Math.ceil((target - Date.now()) / (24 * 60 * 60 * 1000)));
};

const formatTimestamp = (value: string): string => {
    if (!value) return '';
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return '';
    return parsed.toLocaleString(undefined, {
        year: 'numeric', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit',
    });
};

const RecentlyDeletedPage: React.FC = () => {
    const [photos, setPhotos] = useState<TrashedPhoto[]>([]);
    const [total, setTotal] = useState<number>(0);
    const [loading, setLoading] = useState<boolean>(true);
    const [error, setError] = useState<ApiError | null>(null);
    const [selected, setSelected] = useState<Set<string>>(new Set());
    const [busy, setBusy] = useState<'restore' | 'purge' | null>(null);
    const { thumbAccessUrls, resolveAccessForBatch } = useThumbnailAccessResolver();

    const [activity, setActivity] = useState<ActivityItem[]>([]);
    const [activityLoading, setActivityLoading] = useState<boolean>(true);
    const [activityBusyId, setActivityBusyId] = useState<string | null>(null);

    const load = useCallback(async () => {
        setLoading(true);
        setError(null);
        try {
            const data = await get('/photos/trash?limit=200');
            const fetched: TrashedPhoto[] = Array.isArray(data?.photos) ? data.photos : [];
            setPhotos(fetched);
            setTotal(Number(data?.total ?? fetched.length));
            resolveAccessForBatch(fetched);
        } catch (err) {
            setError(classifyApiError(err));
        } finally {
            setLoading(false);
        }
    }, [resolveAccessForBatch]);

    // Two independent, already-working endpoints -- no new backend aggregator,
    // just interleave by timestamp client-side (both lists are small: recent
    // merges + recently-deleted albums, not photo-library scale).
    const loadActivity = useCallback(async () => {
        setActivityLoading(true);
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
            // Best-effort secondary section -- the photo-trash grid above is the
            // page's primary content and already has its own error handling.
            setActivity([]);
        } finally {
            setActivityLoading(false);
        }
    }, []);

    useEffect(() => {
        void load();
        void loadActivity();
    }, [load, loadActivity]);

    useBackendRecoveryRetry(error, load);

    const handleUndoActivity = async (item: ActivityItem) => {
        setActivityBusyId(item.id);
        try {
            if (item.kind === 'merge') {
                await faceService.undoMerge(item.id);
            } else {
                await post(`/albums/${item.id}/restore`, {});
            }
            setActivity((prev) => prev.filter((i) => i.id !== item.id));
            showToast(item.kind === 'merge' ? 'Merge undone.' : 'Album restored.');
        } catch (err) {
            notifyApiError(err, { context: "Couldn't undo that action", retry: () => { void handleUndoActivity(item); } });
        } finally {
            setActivityBusyId(null);
        }
    };

    const toggleSelect = (filename: string) => {
        setSelected((prev) => {
            const next = new Set(prev);
            if (next.has(filename)) {
                next.delete(filename);
            } else {
                next.add(filename);
            }
            return next;
        });
    };

    const handleRestore = async () => {
        if (selected.size === 0) return;
        const filenames = Array.from(selected);
        setBusy('restore');
        try {
            const response = await post('/photos/trash/restore', { filenames });
            const restored: string[] = Array.isArray(response?.restored) ? response.restored : [];
            if (restored.length > 0) {
                setPhotos((prev) => prev.filter((p) => !restored.includes(p.filename)));
                setTotal((prev) => Math.max(0, prev - restored.length));
                setSelected(new Set());
                showToast(`Restored ${plural(restored.length, 'photo')} to your gallery.`);
            }
            if (Array.isArray(response?.errors) && response.errors.length > 0) {
                showToast(`Some photos couldn't be restored: ${response.errors.slice(0, 3).join(' • ')}`, { variant: 'error' });
            }
        } catch (err) {
            notifyApiError(err, { context: "Couldn't restore selected photos", retry: () => { void handleRestore(); } });
        } finally {
            setBusy(null);
        }
    };

    const handlePurge = async () => {
        if (selected.size === 0) return;
        const count = selected.size;
        const confirmed = await confirmDialog({
            title: 'Delete forever',
            message: `Permanently delete ${plural(count, 'photo')}? This cannot be undone.`,
            confirmLabel: 'Delete forever',
            danger: true,
        });
        if (!confirmed) return;

        const filenames = Array.from(selected);
        setBusy('purge');
        try {
            const response = await post('/photos/trash/purge', { filenames });
            const deleted: string[] = Array.isArray(response?.deleted) ? response.deleted : [];
            if (deleted.length > 0) {
                setPhotos((prev) => prev.filter((p) => !deleted.includes(p.filename)));
                setTotal((prev) => Math.max(0, prev - deleted.length));
                setSelected(new Set());
                showToast(`Permanently deleted ${plural(deleted.length, 'photo')}.`);
            }
            if (Array.isArray(response?.errors) && response.errors.length > 0) {
                showToast(`Some photos couldn't be deleted: ${response.errors.slice(0, 3).join(' • ')}`, { variant: 'error' });
            }
        } catch (err) {
            notifyApiError(err, { context: "Couldn't permanently delete selected photos", retry: () => { void handlePurge(); } });
        } finally {
            setBusy(null);
        }
    };

    return (
        <section className="card-glass gallery-wrap">
            <header className="page-topline">
                <h2 className="page-topline-title">Recently Deleted</h2>
                <p className="gallery-meta-line">
                    <span className="gallery-meta-count">{total}</span>
                    <span> {total === 1 ? 'photo' : 'photos'}</span>
                    <span className="gallery-meta-dim"> · restorable, then purged automatically</span>
                </p>
            </header>

            {loading && <Loading label="Loading Recently Deleted…" fullPage={false} />}
            {error && (
                <ErrorState
                    title="Couldn't load Recently Deleted"
                    message={error.message}
                    onRetry={error.retriable ? () => { void load(); } : undefined}
                />
            )}
            {!loading && !error && !activityLoading && photos.length === 0 && activity.length === 0 && (
                <EmptyState
                    icon={<TrashIcon />}
                    title="Nothing here"
                    message="Photos and albums you delete, and merges you undo, stay here for a while before they're gone for good."
                />
            )}

            {selected.size > 0 && (
                <div className="selection-bar">
                    <div className="selection-bar-actions">
                        <span className="selection-count">{plural(selected.size, 'photo')} selected</span>
                        <button
                            type="button"
                            className="btn btn-soft"
                            disabled={busy !== null}
                            onClick={() => void handleRestore()}
                        >
                            <ArrowUturnLeftIcon className="toolbar-icon" aria-hidden="true" />
                            {busy === 'restore' ? 'Restoring…' : 'Restore'}
                        </button>
                        <button
                            type="button"
                            className="btn btn-danger"
                            disabled={busy !== null}
                            onClick={() => void handlePurge()}
                        >
                            <TrashIcon className="toolbar-icon" aria-hidden="true" />
                            {busy === 'purge' ? 'Deleting…' : 'Delete forever'}
                        </button>
                    </div>
                </div>
            )}

            {!loading && !error && photos.length > 0 && (
                <div className="gallery-grid">
                    {photos.map((photo) => {
                        const isSelected = selected.has(photo.filename);
                        const remaining = daysUntil(photo.purgeAt);
                        return (
                            <PhotoTile
                                key={photo.filename}
                                photo={photo}
                                selected={isSelected}
                                title={photo.filename}
                                useBatchedAccess
                                resolvedAccessUrl={thumbAccessUrls.get(photo.filename)}
                                onCardClick={() => toggleSelect(photo.filename)}
                                mediaOverlay={(
                                    <label
                                        className={`tile-select ${isSelected ? 'is-on' : ''}`}
                                        onClick={(e) => e.stopPropagation()}
                                        title={isSelected ? 'Selected' : 'Select photo'}
                                    >
                                        <input
                                            type="checkbox"
                                            className="tile-select-input"
                                            checked={isSelected}
                                            onChange={() => toggleSelect(photo.filename)}
                                            aria-label={`Select ${photo.filename}`}
                                        />
                                        <CheckIcon className="tile-select-icon" aria-hidden="true" />
                                    </label>
                                )}
                                bodyContent={(
                                    <p className="photo-kind">
                                        {remaining === null ? 'Purges soon' : remaining === 0 ? 'Purges today' : `Purges in ${plural(remaining, 'day')}`}
                                    </p>
                                )}
                            />
                        );
                    })}
                </div>
            )}

            {!activityLoading && activity.length > 0 && (
                <>
                    <h3 className="explore-section-title">Other activity</h3>
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
                                        disabled={activityBusyId === item.id}
                                        onClick={() => void handleUndoActivity(item)}
                                    >
                                        <ArrowUturnLeftIcon className="toolbar-icon" aria-hidden="true" />
                                        {activityBusyId === item.id ? 'Undoing…' : 'Undo'}
                                    </button>
                                </div>
                            </div>
                        ))}
                    </div>
                </>
            )}
        </section>
    );
};

export default RecentlyDeletedPage;
