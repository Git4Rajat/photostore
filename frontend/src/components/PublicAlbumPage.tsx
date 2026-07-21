import React, { useCallback, useEffect, useRef, useState } from 'react';
import { ArrowDownTrayIcon, CheckIcon, LockOpenIcon } from '@heroicons/react/24/outline';
import { useParams } from 'react-router-dom';
import { get, post, resolveApiUrl } from '../services/apiClient';
import MetricCard from './shared/MetricCard';
import PhotoTile from './shared/PhotoTile';
import PhotoViewer from './shared/PhotoViewer';
import { getMediaKind } from '../utils/photoDisplay';

interface PublicPhoto {
    filename: string;
    url: string;
    thumbnailUrl?: string;
    previewUrl?: string;
    rotation?: number;
}

interface PublicAlbum {
    name: string;
    photoCount: number;
}

const parsePublicAlbumError = (err: unknown): Record<string, unknown> => {
    if (typeof err === 'object' && err !== null) {
        return err as Record<string, unknown>;
    }
    if (typeof err === 'string') {
        try {
            const parsed = JSON.parse(err);
            return typeof parsed === 'object' && parsed !== null ? parsed as Record<string, unknown> : { error: err };
        } catch {
            return { error: err };
        }
    }
    return {};
};

const PublicAlbumPage: React.FC = () => {
    const { token } = useParams();
    const [loading, setLoading] = useState<boolean>(true);
    const [error, setError] = useState<string>('');
    const [retryAfterSeconds, setRetryAfterSeconds] = useState<number | null>(null);
    const [codeRequired, setCodeRequired] = useState<boolean>(false);
    const [accessCode, setAccessCode] = useState<string>('');
    const [album, setAlbum] = useState<PublicAlbum | null>(null);
    const [photos, setPhotos] = useState<PublicPhoto[]>([]);
    const [viewerIndex, setViewerIndex] = useState<number | null>(null);
    const [selectedPhotos, setSelectedPhotos] = useState<Set<string>>(new Set());
    const [downloading, setDownloading] = useState<boolean>(false);
    const downloadFormRef = useRef<HTMLFormElement | null>(null);

    const loadPublicAlbum = useCallback(async (code: string = '') => {
            if (!token) {
                setError('Invalid album link.');
                setLoading(false);
                return;
            }
            setLoading(true);
            setError('');
            setRetryAfterSeconds(null);
            try {
                const response = code.trim()
                    ? await post(`/public/albums/${encodeURIComponent(token)}`, { accessCode: code.trim() })
                    : await get(`/public/albums/${encodeURIComponent(token)}`);
                
                if (!response || !response.album) {
                    setError('This public album link is invalid or no longer available.');
                    setLoading(false);
                    return;
                }
                
                setAlbum(response.album || null);
                setPhotos(Array.isArray(response.photos) ? response.photos : []);
                setSelectedPhotos(new Set());
                setCodeRequired(false);
            } catch (err) {
                const payload = parsePublicAlbumError(err);
                if (payload.codeRequired === true) {
                    setCodeRequired(true);
                    const retryAfter = Number(payload.retryAfterSeconds);
                    if (Number.isFinite(retryAfter) && retryAfter > 0) {
                        setRetryAfterSeconds(Math.floor(retryAfter));
                        setError(`This album is protected. Please wait ${Math.floor(retryAfter)}s before retrying.`);
                    } else {
                        setRetryAfterSeconds(null);
                        setError('This album is protected. Enter the access code to continue.');
                    }
                } else {
                    const errorMsg = typeof payload.error === 'string' ? payload.error : 'This public album link is invalid or no longer available.';
                    setCodeRequired(false);
                    setRetryAfterSeconds(null);
                    setError(errorMsg);
                }
            } finally {
                setLoading(false);
            }
    }, [token]);

    const selectedCount = selectedPhotos.size;
    const downloadActionUrl = token
        ? resolveApiUrl(`/public/albums/${encodeURIComponent(token)}/download`)
        : '';

    const handleDownload = useCallback(async () => {
        const files = selectedCount > 0
            ? photos.filter((photo) => selectedPhotos.has(photo.filename))
            : photos;
        if (files.length === 0) {
            return;
        }

        setDownloading(true);
        try {
            const form = downloadFormRef.current;
            if (!form) {
                return;
            }
            const filenamesInput = form.querySelector<HTMLInputElement>('input[name="filenames"]');
            if (filenamesInput) {
                filenamesInput.value = selectedCount > 0 ? JSON.stringify(files.map((photo) => photo.filename)) : '';
            }
            form.submit();
        } catch {
            // Let the browser surface the failure via the current UX.
        } finally {
            setDownloading(false);
        }
    }, [photos, selectedCount, selectedPhotos]);

    useEffect(() => {
        void loadPublicAlbum('');
    }, [loadPublicAlbum]);

    return (
        <section className="gallery-wrap card-glass reveal-up delay-1 public-album-shell public-album-studio">
            <div className="public-banner">
                <div>
                    <p className="additional-kicker">SHARED VIEW</p>
                    <h2 className="gallery-banner-title">Public Album</h2>
                    {album ? (
                        <p className="photo-meta">{album.name} • {album.photoCount} photos</p>
                    ) : (
                        <p className="photo-meta">Read-only shared collection</p>
                    )}
                </div>
                <div className="albums-metrics">
                    <MetricCard value={photos.length} label="Visible Photos" />
                    <MetricCard value={codeRequired ? 'Yes' : 'No'} label="Access Code" />
                </div>
            </div>

            {!loading && !error && photos.length > 0 && (
                <div className="toolbar public-album-toolbar">
                    <div className="toolbar-left">
                        <button
                            type="button"
                            className="btn btn-soft icon-btn"
                            onClick={() => {
                                if (selectedCount > 0 && selectedCount === photos.length) {
                                    setSelectedPhotos(new Set());
                                } else {
                                    setSelectedPhotos(new Set(photos.map((photo) => photo.filename)));
                                }
                            }}
                            aria-label={selectedCount > 0 && selectedCount === photos.length ? 'Clear selection' : 'Select all photos'}
                        >
                            <CheckIcon className="toolbar-icon" />
                            <span className="sr-only">
                                {selectedCount > 0 && selectedCount === photos.length ? 'Clear selection' : 'Select all photos'}
                            </span>
                        </button>
                    </div>
                    <div className="toolbar-right">
                        <button
                            type="button"
                            className="btn btn-primary icon-btn"
                            disabled={downloading}
                            onClick={() => void handleDownload()}
                            aria-label={selectedCount > 0 ? `Download selected (${selectedCount})` : `Download all (${photos.length})`}
                        >
                            <ArrowDownTrayIcon className="toolbar-icon" />
                            <span className="sr-only">
                                {selectedCount > 0 ? `Download selected (${selectedCount})` : `Download all (${photos.length})`}
                            </span>
                        </button>
                    </div>
                </div>
            )}
            <form
                ref={downloadFormRef}
                action={downloadActionUrl}
                method="post"
                target="_blank"
                style={{ display: 'none' }}
            >
                <input type="hidden" name="filenames" defaultValue="" />
            </form>

            {loading && <p className="status">Loading public album...</p>}
            {!loading && error && <p className="status error">{error}</p>}
            {!loading && codeRequired && (
                <div className="toolbar-left public-album-lock">
                    <input
                        type="password"
                        className="field field-compact"
                        placeholder="Access code"
                        value={accessCode}
                        onChange={(e) => setAccessCode(e.target.value)}
                    />
                    <button
                        type="button"
                        className="btn btn-primary icon-btn"
                        disabled={retryAfterSeconds !== null && retryAfterSeconds > 0}
                        onClick={() => {
                            void loadPublicAlbum(accessCode);
                        }}
                        aria-label="Unlock"
                    >
                        <LockOpenIcon className="toolbar-icon" />
                        <span className="sr-only">Unlock</span>
                    </button>
                </div>
            )}
            {!loading && codeRequired && retryAfterSeconds !== null && retryAfterSeconds > 0 && (
                <p className="status">Retry available in {retryAfterSeconds}s.</p>
            )}
            {!loading && !error && photos.length === 0 && <p className="empty">No photos available in this album.</p>}

            {!loading && !error && photos.length > 0 && viewerIndex === null && (
                <div className="gallery-grid public-gallery-grid">
                    {photos.map((photo, index) => (
                        <PhotoTile
                            key={photo.filename}
                            photo={photo}
                            selected={selectedPhotos.has(photo.filename)}
                            animationDelayMs={(index % 8) * 36}
                            title={photo.filename}
                            kind={getMediaKind(photo.filename)}
                            linkTitle={`${photo.filename}\n${getMediaKind(photo.filename)}`}
                            openOriginal={false}
                            useProtectedMedia={false}
                            onCardClick={() => {
                                setSelectedPhotos((current) => {
                                    const next = new Set(current);
                                    if (next.has(photo.filename)) {
                                        next.delete(photo.filename);
                                    } else {
                                        next.add(photo.filename);
                                    }
                                    return next;
                                });
                            }}
                            selectableOverlay={(
                                <input
                                    type="checkbox"
                                    checked={selectedPhotos.has(photo.filename)}
                                    onChange={() => {
                                        setSelectedPhotos((current) => {
                                            const next = new Set(current);
                                            if (next.has(photo.filename)) {
                                                next.delete(photo.filename);
                                            } else {
                                                next.add(photo.filename);
                                            }
                                            return next;
                                        });
                                    }}
                                    onClick={(e) => e.stopPropagation()}
                                    aria-label={`Select ${photo.filename}`}
                                />
                            )}
                            onMediaClick={(e) => {
                                e.stopPropagation();
                                e.preventDefault();
                                setViewerIndex(index);
                            }}
                        />
                    ))}
                </div>
            )}
            {viewerIndex !== null && (
                <PhotoViewer
                    photos={photos}
                    index={viewerIndex}
                    onClose={() => setViewerIndex(null)}
                    onIndexChange={setViewerIndex}
                    useProtectedMedia={false}
                />
            )}
        </section>
    );
};

export default PublicAlbumPage;
