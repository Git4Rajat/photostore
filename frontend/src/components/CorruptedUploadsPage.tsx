import React, { useCallback, useEffect, useState } from 'react';
import { CheckCircleIcon } from '@heroicons/react/24/outline';
import { get, post } from '../services/apiClient';
import PhotoTile from './shared/PhotoTile';
import PhotoQuickActions, { libraryFocusHref, workbenchFilenameHref } from './shared/PhotoQuickActions';
import PhotoViewer from './shared/PhotoViewer';
import { EmptyState } from './shared/EmptyState';
import { Loading } from './shared/Loading';
import { ErrorState } from './shared/ErrorState';
import { classifyApiError, type ApiError } from '../services/apiError';
import { notifyApiError } from '../services/requestFeedback';
import { useBackendRecoveryRetry } from '../services/useBackendRecoveryRetry';

interface CorruptedUpload {
    filename: string;
    reason?: string;
    uploadedAt?: string;
    mimeType?: string;
    thumbnailUrl?: string;
    url?: string;
    rotation?: number;
}

const CorruptedUploadsPage: React.FC = () => {
    const [items, setItems] = useState<CorruptedUpload[]>([]);
    const [loading, setLoading] = useState<boolean>(true);
    const [error, setError] = useState<ApiError | null>(null);
    const [clearing, setClearing] = useState<string | null>(null);
    const [viewerIndex, setViewerIndex] = useState<number | null>(null);

    const load = useCallback(async () => {
        setLoading(true);
        setError(null);
        try {
            const data = await get('/api/uploads/corrupted');
            const nextItems = Array.isArray(data?.items) ? data.items : [];
            setItems(nextItems);
        } catch (err) {
            setError(classifyApiError(err));
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => {
        void load();
    }, [load]);

    useBackendRecoveryRetry(error, load);

    const clearCorruption = async (filename: string) => {
        setClearing(filename);
        try {
            await post(`/api/uploads/corrupted/${encodeURIComponent(filename)}/clear`, {});
            setItems((current) => current.filter((item) => item.filename !== filename));
        } catch (err) {
            notifyApiError(err, { context: "Couldn't mark as not corrupted", retry: () => { void clearCorruption(filename); } });
        } finally {
            setClearing(null);
        }
    };

    const handleSaveRotation = async (filename: string, rotation: number) => {
        await post(`/photos/${encodeURIComponent(filename)}/rotation`, { rotation });
        setItems((current) => current.map((item) => (
            item.filename === filename ? { ...item, rotation } : item
        )));
    };

    return (
        <section className="card-glass gallery-wrap">
            <header className="page-topline">
                <h2 className="page-topline-title">Corrupted uploads</h2>
                <p className="gallery-meta-line">
                    <span className="gallery-meta-count">{items.length}</span>
                    <span> {items.length === 1 ? 'item' : 'items'}</span>
                    <span className="gallery-meta-dim"> · failed verification</span>
                </p>
            </header>

            {loading && <Loading label="Checking your uploads…" fullPage={false} />}
            {error && (
                <ErrorState
                    title="Couldn't check uploads"
                    message={error.message}
                    onRetry={error.retriable ? () => { void load(); } : undefined}
                />
            )}
            {!loading && !error && items.length === 0 && (
                <EmptyState
                    icon={<CheckCircleIcon />}
                    title="All clear"
                    message="No corrupted uploads detected — every file in your library looks healthy."
                />
            )}

            {!loading && !error && items.length > 0 && (
                <div className="gallery-grid">
                    {items.map((item, index) => (
                        <PhotoTile
                            key={`${item.filename}-${index}`}
                            photo={{
                                filename: item.filename,
                                url: item.url || '',
                                thumbnailUrl: item.thumbnailUrl || '',
                                rotation: item.rotation,
                            }}
                            title={item.filename}
                            kind={item.mimeType || 'unknown'}
                            onMediaClick={(e) => {
                                e.stopPropagation();
                                e.preventDefault();
                                setViewerIndex(index);
                            }}
                            mediaOverlay={(
                                <PhotoQuickActions
                                    libraryHref={libraryFocusHref(item.filename)}
                                    workbenchHref={workbenchFilenameHref(item.filename)}
                                />
                            )}
                            bodyContent={
                                <>
                                    <p className="photo-kind">
                                        {item.reason ? `Reason: ${item.reason}` : 'Reason: unknown'}
                                    </p>
                                    <button
                                        type="button"
                                        className="btn btn-soft"
                                        disabled={clearing === item.filename}
                                        onClick={() => void clearCorruption(item.filename)}
                                    >
                                        {clearing === item.filename ? 'Clearing…' : 'Mark not corrupted'}
                                    </button>
                                </>
                            }
                        />
                    ))}
                </div>
            )}
            {viewerIndex !== null && (
                <PhotoViewer
                    photos={items.map((item) => ({
                        filename: item.filename,
                        url: item.url || '',
                        thumbnailUrl: item.thumbnailUrl || '',
                        rotation: item.rotation,
                    }))}
                    index={viewerIndex}
                    onClose={() => setViewerIndex(null)}
                    onIndexChange={setViewerIndex}
                    useProtectedMedia={false}
                    onRotationSave={handleSaveRotation}
                />
            )}
        </section>
    );
};

export default CorruptedUploadsPage;
