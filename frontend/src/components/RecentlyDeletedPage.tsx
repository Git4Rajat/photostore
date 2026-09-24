import React, { useCallback, useEffect, useState } from 'react';
import { TrashIcon, CheckIcon, ArrowUturnLeftIcon } from '@heroicons/react/24/outline';
import { get, post } from '../services/apiClient';
import PhotoTile from './shared/PhotoTile';
import SelectionCommandBar from './shared/SelectionCommandBar';
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

const daysUntil = (isoDate: string): number | null => {
    if (!isoDate) return null;
    const target = new Date(isoDate).getTime();
    if (Number.isNaN(target)) return null;
    return Math.max(0, Math.ceil((target - Date.now()) / (24 * 60 * 60 * 1000)));
};

const RecentlyDeletedPage: React.FC = () => {
    const [photos, setPhotos] = useState<TrashedPhoto[]>([]);
    const [total, setTotal] = useState<number>(0);
    const [loading, setLoading] = useState<boolean>(true);
    const [error, setError] = useState<ApiError | null>(null);
    const [selected, setSelected] = useState<Set<string>>(new Set());
    const [busy, setBusy] = useState<'restore' | 'purge' | null>(null);
    const { thumbAccessUrls, resolveAccessForBatch } = useThumbnailAccessResolver();

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

    useEffect(() => {
        void load();
    }, [load]);

    useBackendRecoveryRetry(error, load);

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
            {!loading && !error && photos.length === 0 && (
                <EmptyState
                    icon={<TrashIcon />}
                    title="Nothing here"
                    message="Photos you delete stay here for 30 days before they're gone for good."
                />
            )}

            {selected.size > 0 && (
                <SelectionCommandBar count={selected.size} countLabel={`${plural(selected.size, 'photo')} selected`}>
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
                </SelectionCommandBar>
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
        </section>
    );
};

export default RecentlyDeletedPage;
