import React, { useEffect, useState } from 'react';
import { get, post } from '../services/apiClient';
import PhotoTile from './shared/PhotoTile';
import PhotoViewer from './shared/PhotoViewer';

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
    const [error, setError] = useState<string | null>(null);
    const [clearing, setClearing] = useState<string | null>(null);
    const [viewerIndex, setViewerIndex] = useState<number | null>(null);

    useEffect(() => {
        let mounted = true;
        const load = async () => {
            setLoading(true);
            setError(null);
            try {
                const data = await get('/api/uploads/corrupted');
                if (!mounted) {
                    return;
                }
                const nextItems = Array.isArray(data?.items) ? data.items : [];
                setItems(nextItems);
            } catch (err) {
                if (!mounted) {
                    return;
                }
                setError(String(err));
            } finally {
                if (mounted) {
                    setLoading(false);
                }
            }
        };
        void load();
        return () => {
            mounted = false;
        };
    }, []);

    const clearCorruption = async (filename: string) => {
        setClearing(filename);
        setError(null);
        try {
            await post(`/api/uploads/corrupted/${encodeURIComponent(filename)}/clear`, {});
            setItems((current) => current.filter((item) => item.filename !== filename));
        } catch (err) {
            setError(String(err));
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
            <div className="gallery-banner">
                <div className="gallery-banner-copy">
                    <p className="ios-kicker">UPLOAD HEALTH</p>
                    <h2 className="gallery-banner-title">Corrupted uploads</h2>
                    <p className="ios-subtitle">
                        Items that failed verification are listed here so you can re-upload or replace them.
                    </p>
                </div>
            </div>

            {loading && <p className="status">Loading corrupted uploads...</p>}
            {error && <p className="status error">{error}</p>}
            {!loading && !error && items.length === 0 && (
                <p className="status">No corrupted uploads detected.</p>
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
                            openOriginal={false}
                            onMediaClick={(e) => {
                                e.stopPropagation();
                                e.preventDefault();
                                setViewerIndex(index);
                            }}
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
                                        {clearing === item.filename ? 'Clearing...' : 'Mark not corrupted'}
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
