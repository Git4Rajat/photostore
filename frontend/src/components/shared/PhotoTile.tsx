import React from 'react';
import { getUploadJson, resolveApiUrl } from '../../services/apiClient';
import { isAuthEnabled } from '../../services/authClient';
import { useEffect, useState } from 'react';
import { PlayCircleIcon } from '@heroicons/react/24/solid';
import { isVideoFilename, requiresBackendPreview } from '../../utils/photoDisplay';

interface TilePhoto {
    filename: string;
    url: string;
    thumbnailUrl?: string;
    rotation?: number;
    thumbnailRotation?: number;
}

interface PhotoTileProps {
    photo: TilePhoto;
    selected?: boolean;
    title: string;
    kind?: string;
    animationDelayMs?: number;
    className?: string;
    selectableOverlay?: React.ReactNode;
    bodyContent?: React.ReactNode;
    onCardClick?: (event: React.MouseEvent<HTMLDivElement>) => void;
    onBodyClick?: (event: React.MouseEvent<HTMLDivElement>) => void;
    openOriginal?: boolean;
    linkTitle?: string;
    onImageClick?: (event: React.MouseEvent<HTMLAnchorElement>) => void;
    onMediaClick?: (event: React.MouseEvent<HTMLImageElement>) => void;
    useProtectedMedia?: boolean;
}

const PLACEHOLDER_THUMBNAIL = 'data:image/svg+xml;charset=UTF-8,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 640 480%22%3E%3Crect width=%22640%22 height=%22480%22 fill=%22%23111827%22/%3E%3Crect x=%2260%22 y=%2260%22 width=%22520%22 height=%22360%22 rx=%2228%22 fill=%22%231f2937%22 stroke=%22%23334155%22 stroke-width=%2212%22/%3E%3Cpath d=%22M200 312l78-92 70 74 46-48 126 132H200z%22 fill=%22%233b82f6%22 opacity=%220.88%22/%3E%3Ccircle cx=%22434%22 cy=%22198%22 r=%2234%22 fill=%22%23fbbf24%22/%3E%3Ctext x=%22320%22 y=%22390%22 text-anchor=%22middle%22 fill=%22%239ca3af%22 font-family=%22Arial,%20sans-serif%22 font-size=%2232%22%3EPending thumbnail%3C/text%3E%3C/svg%3E';

const isHttpUrl = (value?: string) => Boolean(value && value.startsWith('http'));

const resolveThumbnailSource = (thumbnailUrl?: string) => {
    if (!thumbnailUrl) {
        return PLACEHOLDER_THUMBNAIL;
    }
    return isHttpUrl(thumbnailUrl) ? thumbnailUrl : resolveApiUrl(thumbnailUrl);
};

const shouldFetchScopedThumbnail = (filename: string, thumbnailUrl?: string) => (
    Boolean(thumbnailUrl && !isHttpUrl(thumbnailUrl)) || (!thumbnailUrl && requiresBackendPreview(filename))
);

const PhotoTile: React.FC<PhotoTileProps> = ({
    photo,
    selected = false,
    title,
    kind,
    animationDelayMs,
    className = '',
    selectableOverlay,
    bodyContent,
    onCardClick,
    onBodyClick,
    openOriginal = true,
    linkTitle,
    onImageClick,
    onMediaClick,
    useProtectedMedia = true,
}) => {
    const classes = [
        'photo-card',
        'card-appear',
        selected ? 'selected' : '',
        className,
    ]
        .filter(Boolean)
        .join(' ');

    const style = animationDelayMs !== undefined
        ? { animationDelay: `${animationDelayMs}ms` }
        : undefined;

    const shouldUseProtectedMedia = useProtectedMedia && isAuthEnabled();
    const [scopedThumbnailUrl, setScopedThumbnailUrl] = useState<string | undefined>(undefined);
    const fallbackThumbnailUrl = resolveThumbnailSource(photo.thumbnailUrl);

    useEffect(() => {
        let active = true;
        if (!shouldUseProtectedMedia || !shouldFetchScopedThumbnail(photo.filename, photo.thumbnailUrl)) {
            setScopedThumbnailUrl(PLACEHOLDER_THUMBNAIL);
            return undefined;
        }
        setScopedThumbnailUrl(undefined);
        void (async () => {
            try {
                const result = await getUploadJson(`/api/photos/access/thumbnail/${encodeURIComponent(photo.filename)}`);
                if (!active) {
                    return;
                }
                const accessUrl = typeof result?.url === 'string' ? result.url : '';
                const accessKind = typeof result?.kind === 'string' ? result.kind : '';
                // Allow http SAS URLs and non-thumbnail proxy URLs (e.g. preview for RAW files).
                // Block thumbnail proxy URLs (kind='thumbnail', non-http): <img> requests omit
                // the Bearer token so the backend can't resolve the user and returns 404.
                setScopedThumbnailUrl(
                    isHttpUrl(accessUrl) || (accessUrl && accessKind !== 'thumbnail')
                        ? accessUrl
                        : PLACEHOLDER_THUMBNAIL,
                );
            } catch {
                if (active) {
                    setScopedThumbnailUrl(fallbackThumbnailUrl || PLACEHOLDER_THUMBNAIL);
                }
            }
        })();
        return () => {
            active = false;
        };
    }, [fallbackThumbnailUrl, photo.filename, photo.thumbnailUrl, shouldUseProtectedMedia]);

    const resolvedThumbnailUrl = shouldUseProtectedMedia
        ? (scopedThumbnailUrl || PLACEHOLDER_THUMBNAIL)
        : fallbackThumbnailUrl;
    const resolvedPhotoUrl = resolveApiUrl(photo.url);
    const mediaClassName = ['photo-media', openOriginal || onMediaClick ? 'interactive' : '']
        .filter(Boolean)
        .join(' ');
    const normalizeRotation = (value?: number) => {
        const rotation = Number(value || 0) % 360;
        return rotation < 0 ? rotation + 360 : rotation;
    };
    const photoRotation = normalizeRotation(photo.rotation);
    const thumbnailRotation = normalizeRotation(photo.thumbnailRotation);
    const remainingRotation = normalizeRotation(photoRotation - thumbnailRotation);
    const mediaStyle = remainingRotation
        ? { transform: `rotate(${remainingRotation}deg) scale(${remainingRotation % 180 === 0 ? 1 : 0.74})` }
        : undefined;

    const isVideo = isVideoFilename(photo.filename);
    const videoOverlay = isVideo ? (
        <span className="photo-video-overlay" aria-hidden="true">
            <PlayCircleIcon className="photo-video-overlay-icon" />
        </span>
    ) : null;

    return (
        <div className={classes} style={style} onClick={onCardClick}>
            {openOriginal ? (
                <a href={resolvedPhotoUrl} target="_blank" rel="noreferrer" title={linkTitle} onClick={onImageClick} className="photo-media-wrap">
                    <img
                        src={resolvedThumbnailUrl || undefined}
                        alt={photo.filename}
                        className={mediaClassName}
                        style={mediaStyle}
                        loading="lazy"
                    />
                    {videoOverlay}
                </a>
            ) : (
                <span className="photo-media-wrap">
                    <img
                        src={resolvedThumbnailUrl || undefined}
                        alt={photo.filename}
                        className={mediaClassName}
                        style={mediaStyle}
                        loading="lazy"
                        onClick={onMediaClick}
                    />
                    {videoOverlay}
                </span>
            )}

            <div className="photo-body" onClick={onBodyClick}>
                <div className="photo-body-head">
                    {selectableOverlay}
                    <div className="photo-title-row">
                        <p className="photo-name">{title}</p>
                        {kind && <p className="photo-kind">{kind}</p>}
                    </div>
                </div>
                {bodyContent}
            </div>
        </div>
    );
};

export default PhotoTile;
