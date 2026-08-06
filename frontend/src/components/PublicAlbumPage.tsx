import React, { useCallback, useEffect, useRef, useState } from 'react';
import { ArrowDownTrayIcon, CheckIcon, LockOpenIcon, PhotoIcon } from '@heroicons/react/24/outline';
import { useParams } from 'react-router-dom';
import { get, post, resolveApiUrl } from '../services/apiClient';
import { classifyApiError, isApiError, type ApiError } from '../services/apiError';
import { notifyApiError } from '../services/requestFeedback';
import { useBackendRecoveryRetry } from '../services/useBackendRecoveryRetry';
import { getBackendStatusSnapshot } from '../services/backendStatus';
import { showToast } from '../services/toast';
import PhotoTile from './shared/PhotoTile';
import PhotoViewer from './shared/PhotoViewer';
import { Logo } from './shared/Logo';
import { EmptyState } from './shared/EmptyState';
import { Loading } from './shared/Loading';
import { ErrorState } from './shared/ErrorState';

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
    // requestJson() always throws a classified ApiError, never the raw axios
    // error or JSON body — the structured fields the backend sent (e.g.
    // `codeRequired`, `retryAfterSeconds`) only survive on `responseData`.
    if (isApiError(err)) {
        const data = err.responseData;
        if (typeof data === 'object' && data !== null) {
            const payload = data as Record<string, unknown>;
            return typeof payload.error === 'string' ? payload : { ...payload, error: err.message };
        }
        return { error: err.message };
    }
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
    const [loadError, setLoadError] = useState<ApiError | null>(null);
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
            setLoadError(null);
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
                setLoadError(classifyApiError(err));
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

    useBackendRecoveryRetry(loadError, () => { void loadPublicAlbum(accessCode); });

    const selectedCount = selectedPhotos.size;
    const downloadActionUrl = token
        ? resolveApiUrl(`/public/albums/${encodeURIComponent(token)}/download`)
        : '';

    const handleDownload = useCallback(async () => {
        const files = selectedCount > 0
            ? photos.filter((photo) => selectedPhotos.has(photo.filename))
            : photos;
        if (files.length === 0 || !token) {
            return;
        }

        setDownloading(true);
        try {
            // form.submit() below gives no programmatic success/failure signal (it
            // opens a plain browser navigation in a new tab), so a dead backend
            // previously just opened a blank/failing tab with zero feedback. If the
            // backend is already known offline, skip straight to feedback instead of
            // waiting out another full cold-start retry cycle just to rediscover it.
            if (getBackendStatusSnapshot().status === 'offline') {
                showToast("Can't reach the server right now — try again once it's back.", {
                    variant: 'error',
                    action: { label: 'Retry', onClick: () => { void handleDownload(); } },
                });
                return;
            }
            await get(`/public/albums/${encodeURIComponent(token)}/download-check`);
            const form = downloadFormRef.current;
            if (!form) {
                return;
            }
            const filenamesInput = form.querySelector<HTMLInputElement>('input[name="filenames"]');
            if (filenamesInput) {
                filenamesInput.value = selectedCount > 0 ? JSON.stringify(files.map((photo) => photo.filename)) : '';
            }
            form.submit();
        } catch (err) {
            notifyApiError(err, { context: "Couldn't start the download", retry: () => { void handleDownload(); } });
        } finally {
            setDownloading(false);
        }
    }, [photos, selectedCount, selectedPhotos, token]);

    useEffect(() => {
        void loadPublicAlbum('');
    }, [loadPublicAlbum]);

    return (
        <section className="gallery-wrap card-glass reveal-up delay-1 public-album-shell public-album-studio">
            <div className="public-banner public-banner-compact">
                <div className="public-banner-brand">
                    <Logo size={38} />
                    <div>
                        <p className="additional-kicker">SHARED VIEW</p>
                        <h2 className="page-topline-title">{album ? album.name : 'Public Album'}</h2>
                        <p className="gallery-meta-line">
                            <span className="gallery-meta-count">{photos.length}</span>
                            <span> photos</span>
                            <span className="gallery-meta-dim"> · read-only</span>
                            {codeRequired && <span className="gallery-meta-dim"> · code required</span>}
                        </p>
                    </div>
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

            {loading && <Loading label="Loading shared album…" fullPage={false} />}
            {!loading && error && !codeRequired && (
                <ErrorState
                    title="Album unavailable"
                    message={error}
                    onRetry={loadError?.retriable ? () => { void loadPublicAlbum(accessCode); } : undefined}
                />
            )}
            {!loading && error && codeRequired && <p className="status error">{error}</p>}
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
            {!loading && !error && photos.length === 0 && (
                <EmptyState icon={<PhotoIcon />} title="Nothing here yet" message="This shared album doesn't have any photos in it right now." />
            )}

            {!loading && !error && photos.length > 0 && viewerIndex === null && (
                <div className="gallery-grid public-gallery-grid">
                    {photos.map((photo, index) => {
                        const isSelected = selectedPhotos.has(photo.filename);
                        const toggle = () => {
                            setSelectedPhotos((current) => {
                                const next = new Set(current);
                                if (next.has(photo.filename)) {
                                    next.delete(photo.filename);
                                } else {
                                    next.add(photo.filename);
                                }
                                return next;
                            });
                        };
                        return (
                            <PhotoTile
                                key={photo.filename}
                                photo={photo}
                                selected={isSelected}
                                animationDelayMs={(index % 8) * 36}
                                title={photo.filename}
                                showBody={false}
                                useProtectedMedia={false}
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
                                            onChange={toggle}
                                            aria-label={`Select ${photo.filename}`}
                                        />
                                        <CheckIcon className="tile-select-icon" aria-hidden="true" />
                                    </label>
                                )}
                                onMediaClick={(e) => {
                                    e.stopPropagation();
                                    e.preventDefault();
                                    setViewerIndex(index);
                                }}
                            />
                        );
                    })}
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

            <footer className="public-album-footer">
                <Logo size={20} />
                <span>Powered by <strong>Keepsake</strong> — your own private photo library</span>
            </footer>
        </section>
    );
};

export default PublicAlbumPage;
