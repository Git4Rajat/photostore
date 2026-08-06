import React from 'react';
import { getUploadJson, resolveApiUrl } from '../../services/apiClient';
import { isAuthEnabled } from '../../services/authClient';
import { useEffect, useRef, useState } from 'react';
import { PlayCircleIcon } from '@heroicons/react/24/solid';
import { isVideoFilename, requiresBackendPreview } from '../../utils/photoDisplay';
import { useLongPress } from '../../services/useLongPress';

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
    // Layer rendered on top of the thumbnail (badges, selection affordances).
    mediaOverlay?: React.ReactNode;
    // When false, the text body under the thumbnail is omitted entirely.
    showBody?: boolean;
    onCardClick?: (event: React.MouseEvent<HTMLDivElement>) => void;
    onBodyClick?: (event: React.MouseEvent<HTMLDivElement>) => void;
    onMediaClick?: (event: React.MouseEvent<HTMLImageElement>) => void;
    // Touch/pen-only (see useLongPress) -- the touch-native replacement for the
    // desktop hover corner icons, opening a PhotoActionSheet.
    onLongPress?: () => void;
    useProtectedMedia?: boolean;
}

// Neutral placeholder for photos whose thumbnail hasn't been generated yet.
// Theme-adaptive via an embedded prefers-color-scheme query so it doesn't flash
// a dark box in light mode (SVG-in-<img> follows the OS color scheme).
const PLACEHOLDER_THUMBNAIL = `data:image/svg+xml,${encodeURIComponent(
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 480">' +
    '<style>.bg{fill:#eceae4}.ic{fill:#c9c3b8}' +
    '@media(prefers-color-scheme:dark){.bg{fill:#191920}.ic{fill:#3a3a44}}</style>' +
    '<rect class="bg" width="640" height="480"/>' +
    '<g class="ic"><circle cx="404" cy="196" r="26"/>' +
    '<path d="M232 330l86-104 58 62 50-54 84 96z"/></g></svg>',
)}`;

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
    mediaOverlay,
    showBody = true,
    onCardClick,
    onBodyClick,
    onMediaClick,
    onLongPress,
    useProtectedMedia = true,
}) => {
    const longPressHandlers = useLongPress(onLongPress);
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
    const [imgLoaded, setImgLoaded] = useState(false);
    const imgElRef = useRef<HTMLImageElement | null>(null);
    const fallbackThumbnailUrl = resolveThumbnailSource(photo.thumbnailUrl);

    useEffect(() => {
        let active = true;
        if (!shouldUseProtectedMedia || !shouldFetchScopedThumbnail(photo.filename, photo.thumbnailUrl)) {
            // Listings hand out absolute SAS URLs the <img> can load directly —
            // no per-tile access round-trip needed. Anything else non-fetchable
            // renders the placeholder.
            setScopedThumbnailUrl(isHttpUrl(photo.thumbnailUrl) ? photo.thumbnailUrl : PLACEHOLDER_THUMBNAIL);
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
    const mediaClassName = ['photo-media', onMediaClick ? 'interactive' : '']
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

    // Reset the fade when the source changes; catch already-cached images whose
    // load event may have fired before the handler was attached.
    useEffect(() => {
        setImgLoaded(false);
        const el = imgElRef.current;
        if (el && el.complete && el.naturalWidth > 0) {
            setImgLoaded(true);
        }
    }, [resolvedThumbnailUrl]);

    return (
        <div className={classes} style={style} onClick={onCardClick} {...longPressHandlers}>
            <span className="photo-media-wrap">
                {!imgLoaded && <span className="photo-media-skeleton" aria-hidden="true" />}
                <img
                    ref={imgElRef}
                    src={resolvedThumbnailUrl || undefined}
                    alt={photo.filename}
                    className={`${mediaClassName} photo-media--fade${imgLoaded ? ' is-loaded' : ''}`}
                    style={mediaStyle}
                    loading="lazy"
                    onLoad={() => setImgLoaded(true)}
                    onError={() => setImgLoaded(true)}
                    onClick={onMediaClick}
                />
                {videoOverlay}
                {mediaOverlay}
            </span>

            {showBody && (
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
            )}
        </div>
    );
};

export default PhotoTile;
