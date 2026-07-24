import React, { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { ArrowDownTrayIcon, ArrowPathIcon, ArrowUturnLeftIcon, ArrowUturnRightIcon, CheckIcon, ChevronLeftIcon, ChevronRightIcon, InformationCircleIcon, MagnifyingGlassMinusIcon, MagnifyingGlassPlusIcon, XMarkIcon } from '@heroicons/react/24/outline';
import { postUploadJson, resolveApiUrl } from '../../services/apiClient';
import { isAuthEnabled } from '../../services/authClient';
import { fetchProtectedBlobUrl } from '../../services/imageClient';
import { getMediaKind, isVideoFilename, requiresBackendPreview } from '../../utils/photoDisplay';

export interface ViewerPhoto {
    filename: string;
    url: string;
    thumbnailUrl?: string;
    previewUrl?: string;
    rotation?: number;
    exifSummary?: {
        camera?: string;
        capturedAt?: string;
        lens?: string;
    };
    location?: {
        address?: string;
    };
    tags?: string[];
}

interface PhotoViewerProps {
    photos: ViewerPhoto[];
    index: number | null;
    onClose: () => void;
    onIndexChange: (index: number) => void;
    useProtectedMedia?: boolean;
    onRotationSave?: (filename: string, rotation: number) => Promise<void> | void;
}

const clamp = (value: number, min: number, max: number) => Math.max(min, Math.min(max, value));
const PLAYBACK_RATES = [0.25, 0.5, 1, 1.25, 1.5, 2] as const;
const TOUCH_PAN_START_THRESHOLD = 8;
const PRELOAD_NEIGHBOR_COUNT = 5;
const normalizeRotation = (value?: number | null) => {
    const rotation = Number(value || 0) % 360;
    return rotation < 0 ? rotation + 360 : rotation;
};

const getMainMediaPath = (photo?: ViewerPhoto | null) => {
    if (!photo) {
        return '';
    }
    if (photo.previewUrl) {
        return photo.previewUrl;
    }
    if (requiresBackendPreview(photo.filename)) {
        return `/api/photos/preview/${encodeURIComponent(photo.filename)}`;
    }
    if (photo.url && !photo.url.includes('/thumbnail/') && !photo.url.includes('/thumb-')) {
        return photo.url;
    }
    return photo.filename ? `/api/photos/image/${encodeURIComponent(photo.filename)}` : (photo.url || photo.thumbnailUrl || '');
};

const getThumbnailPath = (photo?: ViewerPhoto | null) => photo?.thumbnailUrl || photo?.url || '';
const getNeighborIndexes = (index: number | null, total: number, range: number) => {
    if (index === null || total <= 1) {
        return [];
    }
    const indexes: number[] = [];
    const seen = new Set<number>([index]);
    for (let offset = 1; offset <= range; offset += 1) {
        const previous = (index - offset + total) % total;
        const next = (index + offset) % total;
        if (!seen.has(previous)) {
            indexes.push(previous);
            seen.add(previous);
        }
        if (!seen.has(next)) {
            indexes.push(next);
            seen.add(next);
        }
    }
    return indexes;
};
const getAccessKindForPath = (path: string): 'image' | 'preview' | 'thumbnail' => {
    if (path.includes('/preview/')) {
        return 'preview';
    }
    if (path.includes('/thumbnail/')) {
        return 'thumbnail';
    }
    return 'image';
};

const isProtectedProxyPath = (path: string) => path.startsWith('/api/') || path.startsWith('/public/');

const fetchPublicBlobUrl = async (path: string): Promise<string> => {
    const response = await fetch(resolveApiUrl(path), { mode: 'cors', credentials: 'omit' });
    if (!response.ok) {
        const body = await response.text();
        throw new Error(body || `Failed to fetch photo: ${response.status}`);
    }
    return URL.createObjectURL(await response.blob());
};

const formatPreviewError = (filename: string, detail?: string) => {
    const kind = getMediaKind(filename);
    const normalized = detail?.trim();
    if (normalized) {
        return normalized;
    }
    return kind === 'RAW'
        ? 'We couldn’t build a preview for this RAW file — its format has no usable embedded preview or couldn’t be decoded on the server.'
        : 'We couldn’t build a preview for this image.';
};

// The backend returns a JSON body ({ error, reason, detail }) on preview failures.
// fetchProtectedBlobUrl surfaces that body as the thrown Error's message, so parse it
// back out to show the specific, human-readable reason instead of a raw JSON string.
const describePreviewFailure = (filename: string, rawMessage?: string | null): string => {
    const message = rawMessage?.trim();
    if (message) {
        try {
            const parsed = JSON.parse(message);
            if (parsed && typeof parsed.detail === 'string' && parsed.detail.trim()) {
                return parsed.detail.trim();
            }
        } catch {
            // Not JSON (e.g. a network/transport error) — fall through to the generic text.
        }
    }
    return formatPreviewError(filename);
};

const PhotoViewer: React.FC<PhotoViewerProps> = ({ photos, index, onClose, onIndexChange, useProtectedMedia = true, onRotationSave }) => {
    const [zoom, setZoom] = useState(1);
    const [pan, setPan] = useState({ x: 0, y: 0 });
    const [isPanning, setIsPanning] = useState(false);
    const [mediaLoading, setMediaLoading] = useState(false);
    const [mediaError, setMediaError] = useState<string | null>(null);
    const [mediaPathOverride, setMediaPathOverride] = useState<string | null>(null);
    const [scopedMediaUrls, setScopedMediaUrls] = useState<Record<string, string>>({});
    const [rotationDraft, setRotationDraft] = useState(0);
    const [rotationSaving, setRotationSaving] = useState(false);
    const [rotationError, setRotationError] = useState<string | null>(null);
    const [stageSize, setStageSize] = useState({ width: 0, height: 0 });
    const [resolvedUrls, setResolvedUrls] = useState<Record<string, string>>({});
    const [playbackRate, setPlaybackRate] = useState(1);
    const videoRef = useRef<HTMLVideoElement | null>(null);
    const stageRef = useRef<HTMLDivElement | null>(null);
    const panRef = useRef(pan);
    const zoomRef = useRef(zoom);
    const touchStartRef = useRef<{ x: number; y: number } | null>(null);
    const pinchDistanceRef = useRef<number | null>(null);
    const panStartRef = useRef<{ x: number; y: number; panX: number; panY: number; started: boolean; pointerType: string } | null>(null);
    const activePointerRef = useRef<number | null>(null);
    const filmstripPointerRef = useRef<{ pointerId: number; x: number; y: number; scrollLeft: number; targetIndex: number | null; moved: boolean } | null>(null);
    const suppressFilmstripClickRef = useRef(false);
    const objectUrlsRef = useRef<string[]>([]);
    const warmedPathsRef = useRef<Set<string>>(new Set());
    // Paths already resolved (or in-flight) via the scoped access-batch mechanism, kept
    // across navigation so we never re-request a signed URL we already hold.
    const scopedResolvedRef = useRef<Set<string>>(new Set());
    const filmstripRef = useRef<HTMLDivElement | null>(null);
    const filmstripItemRefs = useRef<Record<number, HTMLButtonElement | null>>({});
    const filmstripHasCenteredRef = useRef(false);

    const activePhoto = index !== null ? photos[index] : null;
    const activeIsVideo = Boolean(activePhoto && isVideoFilename(activePhoto.filename));
    const primaryMediaPath = getMainMediaPath(activePhoto);
    const mainMediaPath = mediaPathOverride || primaryMediaPath;
    const shouldProtect = useProtectedMedia && isAuthEnabled();
    const savedRotation = normalizeRotation(activePhoto?.rotation);
    const rotationChanged = activePhoto ? rotationDraft !== savedRotation : false;
    const canSaveRotation = Boolean(onRotationSave && activePhoto);
    const isQuarterTurn = rotationDraft % 180 !== 0;
    // The media normally fills the stage (width/height: 100% + object-fit: contain). For a
    // quarter-turn we swap the element's width and height to the stage's content box, so
    // that once it's rotated 90° the contained image maps back onto the full stage instead
    // of overflowing. stageSize is the stage content box (padding excluded).
    const rotationFitStyle = isQuarterTurn && stageSize.width > 0 && stageSize.height > 0
        ? { width: `${stageSize.height}px`, height: `${stageSize.width}px` }
        : undefined;
    const imageUrl = activePhoto && mainMediaPath
        ? (shouldProtect
            ? (scopedMediaUrls[mainMediaPath] || resolvedUrls[mainMediaPath])
            : resolveApiUrl(mainMediaPath))
        : '';

    const filmstripIndexes = useMemo(() => {
        if (index === null || photos.length === 0) {
            return [];
        }
        const range = 60;
        const indexes: number[] = [];
        for (let offset = -range; offset <= range; offset += 1) {
            const next = index + offset;
            if (next >= 0 && next < photos.length) {
                indexes.push(next);
            }
        }
        return indexes;
    }, [index, photos.length]);
    const previewPreloadIndexes = useMemo(
        () => getNeighborIndexes(index, photos.length, PRELOAD_NEIGHBOR_COUNT),
        [index, photos.length],
    );

    useEffect(() => {
        if (!activePhoto || !shouldProtect) {
            setScopedMediaUrls({});
            scopedResolvedRef.current.clear();
            return undefined;
        }

        // Resolve the active photo's media plus each neighbor's main media through the
        // scoped access-batch endpoint. Neighbors are resolved (and their bytes warmed)
        // ahead of time and cached across navigation, so moving to a preloaded photo
        // reuses an already-signed, already-fetched URL instead of re-downloading it.
        const targets: Array<{ path: string; filename: string }> = [];
        const pushTarget = (path: string | undefined, filename: string) => {
            if (typeof path === 'string' && path.length > 0 && !path.startsWith('http')) {
                targets.push({ path, filename });
            }
        };
        pushTarget(primaryMediaPath, activePhoto.filename);
        pushTarget(activePhoto.thumbnailUrl, activePhoto.filename);
        pushTarget(activePhoto.previewUrl, activePhoto.filename);
        // Only pre-sign/warm the original when the browser can actually decode it. For
        // RAW/HEIC the lightbox displays the backend preview proxy (primaryMediaPath),
        // never the original — warming activePhoto.url here would download the full
        // source file (often tens of MB) into an <img> that can never render, wasting
        // bandwidth and starving the real preview request.
        if (!requiresBackendPreview(activePhoto.filename)) {
            pushTarget(activePhoto.url, activePhoto.filename);
        }
        previewPreloadIndexes.forEach((photoIndex) => {
            const neighbor = photos[photoIndex];
            if (neighbor && !isVideoFilename(neighbor.filename || '')) {
                pushTarget(getMainMediaPath(neighbor), neighbor.filename);
            }
        });

        const seen = new Set<string>();
        const pending = targets.filter(({ path }) => {
            if (seen.has(path) || scopedResolvedRef.current.has(path)) {
                return false;
            }
            seen.add(path);
            return true;
        });
        if (pending.length === 0) {
            return undefined;
        }
        pending.forEach(({ path }) => scopedResolvedRef.current.add(path));

        // Gates only the surfacing of a failure message: a superseded run must still
        // commit its resolved URLs (see below), but it must not flash an error for a
        // photo the user has already navigated away from.
        let runCurrent = true;
        void (async () => {
            const entries = await Promise.all(pending.map(async ({ path, filename }) => {
                try {
                    const result = await postUploadJson('/api/photos/access-batch', {
                        kind: getAccessKindForPath(path),
                        filenames: [filename],
                    });
                    const url = typeof result?.urls?.[filename] === 'string' ? result.urls[filename] : '';
                    if (!url) {
                        return { path, url: '', error: '' };
                    }
                    if (isProtectedProxyPath(url)) {
                        const objectUrl = await fetchProtectedBlobUrl(url);
                        objectUrlsRef.current.push(objectUrl);
                        return { path, url: objectUrl, error: '' };
                    }
                    // Direct signed storage URL: warm the bytes so the eventual <img src>
                    // (identical URL string) is served from cache without a visible reload.
                    try {
                        const img = new Image();
                        img.src = url;
                    } catch {
                        /* image warming is best-effort */
                    }
                    return { path, url, error: '' };
                } catch (err) {
                    return { path, url: '', error: err instanceof Error ? err.message : '' };
                }
            }));
            // Always commit, even if this run was superseded by fast navigation. The
            // paths were already claimed in scopedResolvedRef (so they won't be
            // re-requested), and scopedMediaUrls is a path-keyed cache merged into prior
            // state — dropping a superseded run's results would leave the photo it
            // resolved marked "resolved" but with no URL, stranding it on the loading
            // spinner forever. Failed paths are released so a later pass can retry them.
            const resolved = entries.filter((entry) => Boolean(entry.url)).map((entry) => [entry.path, entry.url] as const);
            entries.filter((entry) => !entry.url).forEach((entry) => scopedResolvedRef.current.delete(entry.path));
            if (resolved.length > 0) {
                setScopedMediaUrls((prev) => ({ ...prev, ...Object.fromEntries(resolved) }));
            }
            // If the media the viewer is currently showing failed to resolve, replace the
            // indefinite loading spinner with a specific, actionable failure message.
            if (runCurrent) {
                const primaryFailure = entries.find((entry) => entry.path === primaryMediaPath && !entry.url);
                if (primaryFailure) {
                    setMediaLoading(false);
                    setMediaError(describePreviewFailure(activePhoto.filename, primaryFailure.error));
                }
            }
        })();
        return () => {
            runCurrent = false;
        };
    }, [activePhoto, primaryMediaPath, shouldProtect, previewPreloadIndexes, photos]);

    const downloadCurrentPhoto = useCallback(async () => {
        if (!activePhoto) {
            return;
        }
        try {
            const downloadPath = activePhoto.url || mainMediaPath || activePhoto.thumbnailUrl || '';
            if (!downloadPath) {
                return;
            }

            const objectUrl = shouldProtect
                ? await fetchProtectedBlobUrl(downloadPath)
                : await fetchPublicBlobUrl(downloadPath);
            objectUrlsRef.current.push(objectUrl);
            const anchor = document.createElement('a');
            anchor.href = objectUrl;
            anchor.download = activePhoto.filename;
            anchor.rel = 'noreferrer';
            anchor.style.display = 'none';
            document.body.appendChild(anchor);
            anchor.click();
            anchor.remove();
            setTimeout(() => {
                URL.revokeObjectURL(objectUrl);
            }, 5000);
        } catch {
            setMediaError(formatPreviewError(activePhoto.filename, 'Download failed.'));
        }
    }, [activePhoto, mainMediaPath, shouldProtect]);

    const close = useCallback(() => {
        onClose();
        setZoom(1);
        setPan({ x: 0, y: 0 });
        setIsPanning(false);
        setMediaLoading(false);
        setMediaError(null);
        setMediaPathOverride(null);
        setRotationError(null);
        setRotationSaving(false);
        touchStartRef.current = null;
        pinchDistanceRef.current = null;
        panStartRef.current = null;
        activePointerRef.current = null;
        warmedPathsRef.current.clear();
        filmstripHasCenteredRef.current = false;
    }, [onClose]);

    const showPrevious = useCallback(() => {
        if (index === null || photos.length === 0) {
            return;
        }
        setMediaPathOverride(null);
        onIndexChange((index - 1 + photos.length) % photos.length);
        setZoom(1);
        setPan({ x: 0, y: 0 });
        setIsPanning(false);
    }, [index, onIndexChange, photos.length]);

    const showNext = useCallback(() => {
        if (index === null || photos.length === 0) {
            return;
        }
        setMediaPathOverride(null);
        onIndexChange((index + 1) % photos.length);
        setZoom(1);
        setPan({ x: 0, y: 0 });
        setIsPanning(false);
    }, [index, onIndexChange, photos.length]);

    const changePlaybackRate = useCallback((rate: number) => {
        setPlaybackRate(rate);
        if (videoRef.current) {
            videoRef.current.playbackRate = rate;
        }
    }, []);

    const rotateBy = useCallback((delta: number) => {
        setRotationDraft((current) => normalizeRotation(current + delta));
        setRotationError(null);
    }, []);

    const saveRotation = useCallback(async () => {
        if (!activePhoto || !onRotationSave || rotationSaving || !rotationChanged) {
            return;
        }
        setRotationSaving(true);
        setRotationError(null);
        try {
            await onRotationSave(activePhoto.filename, rotationDraft);
        } catch {
            setRotationError('Rotation save failed.');
        } finally {
            setRotationSaving(false);
        }
    }, [activePhoto, onRotationSave, rotationChanged, rotationDraft, rotationSaving]);

    const selectFilmstripPhoto = useCallback((photoIndex: number) => {
        if (photoIndex < 0 || photoIndex >= photos.length) {
            return;
        }
        setMediaPathOverride(null);
        onIndexChange(photoIndex);
        setZoom(1);
        setPan({ x: 0, y: 0 });
        setIsPanning(false);
    }, [onIndexChange, photos.length]);

    const handleFilmstripPointerDown = useCallback((event: React.PointerEvent<HTMLDivElement>) => {
        const target = (event.target as HTMLElement | null)?.closest<HTMLButtonElement>('.photo-preview-thumb');
        const targetIndexValue = target?.dataset.photoIndex;
        const targetIndex = targetIndexValue ? Number(targetIndexValue) : null;
        filmstripPointerRef.current = {
            pointerId: event.pointerId,
            x: event.clientX,
            y: event.clientY,
            scrollLeft: event.currentTarget.scrollLeft,
            targetIndex: Number.isFinite(targetIndex) ? targetIndex : null,
            moved: false,
        };
        suppressFilmstripClickRef.current = false;
    }, []);

    const handleFilmstripPointerMove = useCallback((event: React.PointerEvent<HTMLDivElement>) => {
        const state = filmstripPointerRef.current;
        if (!state || state.pointerId !== event.pointerId) {
            return;
        }
        const movedByPointer = Math.hypot(event.clientX - state.x, event.clientY - state.y) > 10;
        const movedByScroll = Math.abs(event.currentTarget.scrollLeft - state.scrollLeft) > 2;
        if (movedByPointer || movedByScroll) {
            state.moved = true;
            suppressFilmstripClickRef.current = true;
        }
    }, []);

    const handleFilmstripPointerUp = useCallback((event: React.PointerEvent<HTMLDivElement>) => {
        const state = filmstripPointerRef.current;
        if (!state || state.pointerId !== event.pointerId) {
            return;
        }
        const endTarget = (event.target as HTMLElement | null)?.closest<HTMLButtonElement>('.photo-preview-thumb');
        const endTargetIndexValue = endTarget?.dataset.photoIndex;
        const endTargetIndex = endTargetIndexValue ? Number(endTargetIndexValue) : null;
        const moved = state.moved
            || Math.hypot(event.clientX - state.x, event.clientY - state.y) > 10
            || Math.abs(event.currentTarget.scrollLeft - state.scrollLeft) > 2;
        filmstripPointerRef.current = null;
        suppressFilmstripClickRef.current = true;
        if (!moved && state.targetIndex !== null && endTargetIndex === state.targetIndex) {
            selectFilmstripPhoto(state.targetIndex);
        }
        window.setTimeout(() => {
            suppressFilmstripClickRef.current = false;
        }, 350);
    }, [selectFilmstripPhoto]);

    const handleFilmstripPointerCancel = useCallback((event: React.PointerEvent<HTMLDivElement>) => {
        const state = filmstripPointerRef.current;
        if (!state || state.pointerId !== event.pointerId) {
            return;
        }
        state.moved = true;
        filmstripPointerRef.current = null;
        suppressFilmstripClickRef.current = true;
        window.setTimeout(() => {
            suppressFilmstripClickRef.current = false;
        }, 350);
    }, []);

    const handleFilmstripScroll = useCallback(() => {
        const state = filmstripPointerRef.current;
        if (!state) {
            return;
        }
        state.moved = true;
        suppressFilmstripClickRef.current = true;
    }, []);

    const stopFilmstripTouchPropagation = useCallback((event: React.TouchEvent<HTMLDivElement>) => {
        event.stopPropagation();
    }, []);

    const showPreviewFailure = useCallback((detail?: string) => {
        const fallbackPath = activePhoto?.thumbnailUrl || '';
        if (fallbackPath && fallbackPath !== mainMediaPath && mediaPathOverride !== fallbackPath) {
            setMediaPathOverride(fallbackPath);
            setMediaLoading(true);
            setMediaError(null);
            return;
        }
        if (activePhoto && requiresBackendPreview(activePhoto.filename)) {
            setMediaLoading(false);
            setMediaError(formatPreviewError(activePhoto.filename, detail));
            return;
        }
        setMediaLoading(false);
        setMediaError(formatPreviewError(activePhoto?.filename || '', detail));
    }, [activePhoto, mainMediaPath, mediaPathOverride]);

    const centerSelectedFilmstripItem = useCallback((behavior: ScrollBehavior = 'smooth') => {
        const container = filmstripRef.current;
        const selectedButton = index !== null ? filmstripItemRefs.current[index] : null;
        if (!container || !selectedButton) {
            return;
        }

        const selectedCenter = selectedButton.offsetLeft + (selectedButton.offsetWidth / 2);
        const targetLeft = selectedCenter - (container.clientWidth / 2);
        const maxLeft = Math.max(0, container.scrollWidth - container.clientWidth);
        const left = clamp(targetLeft, 0, maxLeft);

        if (typeof container.scrollTo === 'function') {
            container.scrollTo({ left, behavior });
        } else {
            container.scrollLeft = left;
        }
    }, [index]);

    useEffect(() => {
        panRef.current = pan;
    }, [pan]);

    useEffect(() => {
        zoomRef.current = zoom;
        if (zoom <= 1 && (panRef.current.x !== 0 || panRef.current.y !== 0)) {
            setPan({ x: 0, y: 0 });
            setIsPanning(false);
        }
    }, [zoom]);

    useLayoutEffect(() => {
        if (index === null || filmstripIndexes.length === 0) {
            return;
        }
        const behavior: ScrollBehavior = filmstripHasCenteredRef.current ? 'smooth' : 'auto';
        centerSelectedFilmstripItem(behavior);
        const frame = window.requestAnimationFrame(() => {
            centerSelectedFilmstripItem(behavior);
            filmstripHasCenteredRef.current = true;
        });
        return () => window.cancelAnimationFrame(frame);
    }, [centerSelectedFilmstripItem, filmstripIndexes, index]);

    useEffect(() => {
        if (index === null) {
            return;
        }
        const handleKeyDown = (event: KeyboardEvent) => {
            if (event.key === 'Escape') {
                close();
            } else if (event.key === 'ArrowLeft') {
                showPrevious();
            } else if (event.key === 'ArrowRight') {
                showNext();
            }
        };
        window.addEventListener('keydown', handleKeyDown);
        return () => window.removeEventListener('keydown', handleKeyDown);
    }, [close, index, showNext, showPrevious]);

    useEffect(() => {
        if (index === null || photos.length === 0) {
            return;
        }
        // Video blobs are streamed straight from their scoped URLs; prefetching
        // them here would download the entire file.
        // In protected mode the main images are prefetched (and byte-warmed) by the
        // scoped access-batch effect via signed URLs, so warming proxy blobs for them
        // here as well would download every neighbour twice — only warm thumbnails.
        const mainPreloadPaths = shouldProtect
            ? []
            : [
                ...(activeIsVideo ? [] : [mainMediaPath]),
                ...previewPreloadIndexes
                    .filter((photoIndex) => !isVideoFilename(photos[photoIndex]?.filename || ''))
                    .map((photoIndex) => getMainMediaPath(photos[photoIndex])),
            ];
        const paths = [
            ...mainPreloadPaths,
            ...filmstripIndexes.map((photoIndex) => getThumbnailPath(photos[photoIndex])),
        ].filter(Boolean);
        paths.forEach((path) => {
            if (warmedPathsRef.current.has(path)) {
                return;
            }
            warmedPathsRef.current.add(path);
            if (shouldProtect) {
                void fetchProtectedBlobUrl(path).then((objectUrl) => {
                    objectUrlsRef.current.push(objectUrl);
                    setResolvedUrls((prev) => (prev[path] ? prev : { ...prev, [path]: objectUrl }));
                }).catch(() => {
                    warmedPathsRef.current.delete(path);
                    if (path === mainMediaPath) {
                        showPreviewFailure('Preview fetch failed.');
                    }
                });
            } else {
                const img = new Image();
                img.src = resolveApiUrl(path);
            }
        });
    }, [activeIsVideo, activePhoto, filmstripIndexes, index, mainMediaPath, photos, previewPreloadIndexes, shouldProtect, showPreviewFailure]);

    useEffect(() => {
        if (!activePhoto) {
            setMediaLoading(false);
            setMediaError(null);
            setMediaPathOverride(null);
            setRotationDraft(0);
            setRotationError(null);
            setRotationSaving(false);
            return;
        }
        setMediaPathOverride(null);
        setMediaLoading(true);
        setMediaError(null);
        setRotationDraft(normalizeRotation(activePhoto.rotation));
        setRotationError(null);
        setRotationSaving(false);
        setZoom(1);
        setPan({ x: 0, y: 0 });
        setIsPanning(false);
    }, [activePhoto]);

    useEffect(() => {
        if (!activePhoto || !imageUrl) {
            return;
        }
        setMediaLoading(true);
        setMediaError(null);
    }, [activePhoto, imageUrl]);

    useEffect(() => () => {
        objectUrlsRef.current.forEach((url) => {
            if (url.startsWith('blob:')) {
                URL.revokeObjectURL(url);
            }
        });
    }, []);

    useEffect(() => {
        const stage = stageRef.current;
        if (!stage) {
            return undefined;
        }

        const updateStageSize = () => {
            // Report the content box (excluding padding) so it matches the area the media
            // actually fills via width/height: 100%, and so quarter-turn swapping lands the
            // rotated image on the same region rather than under the nav arrows.
            const styles = window.getComputedStyle(stage);
            const padX = parseFloat(styles.paddingLeft) + parseFloat(styles.paddingRight);
            const padY = parseFloat(styles.paddingTop) + parseFloat(styles.paddingBottom);
            setStageSize({
                width: Math.max(0, stage.clientWidth - padX),
                height: Math.max(0, stage.clientHeight - padY),
            });
        };
        updateStageSize();

        if (typeof ResizeObserver !== 'undefined') {
            const observer = new ResizeObserver(updateStageSize);
            observer.observe(stage);
            return () => observer.disconnect();
        }

        window.addEventListener('resize', updateStageSize);
        return () => window.removeEventListener('resize', updateStageSize);
    }, [activePhoto]);

    useEffect(() => {
        if (!stageRef.current) {
            return undefined;
        }
        const stage = stageRef.current;
        const onWheel = (event: WheelEvent) => {
            if (!event.ctrlKey && !event.metaKey) {
                return;
            }
            event.preventDefault();
            setZoom((current) => clamp(Number((current + (event.deltaY < 0 ? 0.18 : -0.18)).toFixed(2)), 1, 4));
        };
        const onTouchMove = (event: TouchEvent) => {
            if (event.touches.length !== 2) {
                return;
            }
            event.preventDefault();
            const [first, second] = Array.from(event.touches);
            const nextDistance = Math.hypot(first.clientX - second.clientX, first.clientY - second.clientY);
            const previousDistance = pinchDistanceRef.current;
            pinchDistanceRef.current = nextDistance;
            if (!previousDistance) {
                return;
            }
            setZoom((current) => clamp(Number((current * (nextDistance / previousDistance)).toFixed(2)), 1, 4));
        };
        const onPointerDown = (event: PointerEvent) => {
            const currentZoom = zoomRef.current;
            if ((event.pointerType === 'mouse' && event.button !== 0) || currentZoom <= 1) {
                return;
            }
            activePointerRef.current = event.pointerId;
            const currentPan = panRef.current;
            panStartRef.current = {
                x: event.clientX,
                y: event.clientY,
                panX: currentPan.x,
                panY: currentPan.y,
                started: event.pointerType !== 'touch',
                pointerType: event.pointerType,
            };
            if (event.pointerType !== 'touch') {
                event.preventDefault();
                setIsPanning(true);
                stage.setPointerCapture(event.pointerId);
            }
        };
        const onPointerMove = (event: PointerEvent) => {
            if (activePointerRef.current !== event.pointerId || !panStartRef.current || zoomRef.current <= 1) {
                return;
            }
            const dx = event.clientX - panStartRef.current.x;
            const dy = event.clientY - panStartRef.current.y;

            if (!panStartRef.current.started) {
                if (Math.hypot(dx, dy) < TOUCH_PAN_START_THRESHOLD) {
                    return;
                }
                if (Math.abs(dy) > Math.abs(dx) * 1.15) {
                    activePointerRef.current = null;
                    panStartRef.current = null;
                    setIsPanning(false);
                    return;
                }
                panStartRef.current.started = true;
                setIsPanning(true);
                try {
                    stage.setPointerCapture(event.pointerId);
                } catch {
                    // Some touch sequences are already owned by native page scroll.
                }
            }

            event.preventDefault();
            setPan({
                x: panStartRef.current.panX + dx,
                y: panStartRef.current.panY + dy,
            });
        };
        const onPointerUp = (event: PointerEvent) => {
            if (activePointerRef.current !== event.pointerId) {
                return;
            }
            activePointerRef.current = null;
            panStartRef.current = null;
            setIsPanning(false);
            if (zoomRef.current <= 1) {
                setPan({ x: 0, y: 0 });
            }
            try {
                stage.releasePointerCapture(event.pointerId);
            } catch {
                // Ignore capture release failures on older browsers / interrupted gestures.
            }
        };
        stage.addEventListener('wheel', onWheel, { passive: false });
        stage.addEventListener('touchmove', onTouchMove, { passive: false });
        stage.addEventListener('pointerdown', onPointerDown);
        stage.addEventListener('pointermove', onPointerMove, { passive: false });
        stage.addEventListener('pointerup', onPointerUp);
        stage.addEventListener('pointercancel', onPointerUp);
        return () => {
            stage.removeEventListener('wheel', onWheel);
            stage.removeEventListener('touchmove', onTouchMove);
            stage.removeEventListener('pointerdown', onPointerDown);
            stage.removeEventListener('pointermove', onPointerMove);
            stage.removeEventListener('pointerup', onPointerUp);
            stage.removeEventListener('pointercancel', onPointerUp);
        };
    }, [activePhoto]);

    if (!activePhoto) {
        return null;
    }

    return (
        <section
            className="photo-preview"
            role="region"
            aria-label={activePhoto.filename}
            onTouchStart={(event) => {
                if (event.touches.length === 2) {
                    const [first, second] = Array.from(event.touches);
                    pinchDistanceRef.current = Math.hypot(first.clientX - second.clientX, first.clientY - second.clientY);
                    touchStartRef.current = null;
                    return;
                }
                const touch = event.touches[0];
                touchStartRef.current = { x: touch.clientX, y: touch.clientY };
            }}
            onTouchEnd={(event) => {
                if (event.touches.length < 2) {
                    pinchDistanceRef.current = null;
                }
                if (zoomRef.current > 1) {
                    touchStartRef.current = null;
                    return;
                }
                const start = touchStartRef.current;
                const touch = event.changedTouches[0];
                touchStartRef.current = null;
                if (!start || !touch) {
                    return;
                }
                const dx = touch.clientX - start.x;
                const dy = touch.clientY - start.y;
                if (Math.abs(dx) < 48 || Math.abs(dx) < Math.abs(dy) * 1.2) {
                    return;
                }
                if (dx < 0) {
                    showNext();
                } else {
                    showPrevious();
                }
            }}
        >
            <div className="photo-preview-panel">
                <div className="photo-preview-top">
                    <div>
                        <p className="photo-preview-title">{activePhoto.filename}</p>
                        <p className="photo-preview-counter">
                            {index !== null ? `${index + 1}/${photos.length}` : `0/${photos.length}`}
                            {getMediaKind(activePhoto.filename) === 'RAW' ? ' · RAW preview' : ''}
                            {rotationDraft ? ` · rotated ${rotationDraft}°${rotationChanged ? ' unsaved' : ''}` : (rotationChanged ? ' · rotation unsaved' : '')}
                            {zoom > 1 ? ` · ${Math.round(zoom * 100)}%` : ''}
                        </p>
                        {rotationError && <p className="photo-preview-error">{rotationError}</p>}
                    </div>
                    <div className="photo-preview-tools">
                        <button type="button" className="photo-preview-icon" onClick={(e) => { e.stopPropagation(); void downloadCurrentPhoto(); }} aria-label="Download photo">
                            <ArrowDownTrayIcon className="toolbar-icon" />
                        </button>
                        {canSaveRotation && (
                            <>
                                <button type="button" className="photo-preview-icon" onClick={(e) => { e.stopPropagation(); rotateBy(-90); }} disabled={rotationSaving} aria-label="Rotate left">
                                    <ArrowUturnLeftIcon className="toolbar-icon" />
                                </button>
                                <button type="button" className="photo-preview-icon" onClick={(e) => { e.stopPropagation(); rotateBy(90); }} disabled={rotationSaving} aria-label="Rotate right">
                                    <ArrowUturnRightIcon className="toolbar-icon" />
                                </button>
                                <button type="button" className={`photo-preview-icon ${rotationChanged ? 'is-dirty' : ''}`} onClick={(e) => { e.stopPropagation(); void saveRotation(); }} disabled={!rotationChanged || rotationSaving} aria-label="Save rotation">
                                    {rotationSaving ? <ArrowPathIcon className="toolbar-icon spin-icon" /> : <CheckIcon className="toolbar-icon" />}
                                </button>
                            </>
                        )}
                        <button type="button" className="photo-preview-icon" onClick={(e) => { e.stopPropagation(); setZoom((current) => clamp(Number((current - 0.25).toFixed(2)), 1, 4)); }} disabled={zoom <= 1} aria-label="Zoom out">
                            <MagnifyingGlassMinusIcon className="toolbar-icon" />
                        </button>
                        <button type="button" className="photo-preview-icon" onClick={(e) => { e.stopPropagation(); setZoom((current) => clamp(Number((current + 0.25).toFixed(2)), 1, 4)); }} disabled={zoom >= 4} aria-label="Zoom in">
                            <MagnifyingGlassPlusIcon className="toolbar-icon" />
                        </button>
                        <button type="button" className="photo-preview-icon" onClick={close} aria-label="Close">
                            <XMarkIcon className="toolbar-icon" />
                        </button>
                    </div>
                </div>
                <button type="button" className="photo-preview-nav previous" onClick={showPrevious} aria-label="Previous photo">
                    <ChevronLeftIcon className="photo-preview-nav-icon" />
                </button>
                <div
                    ref={stageRef}
                    className="photo-preview-stage"
                    style={{ cursor: zoom > 1 ? (isPanning ? 'grabbing' : 'grab') : 'default' }}
                >
                    {mediaLoading && (
                        <div className="photo-preview-loading" role="status">
                            <ArrowPathIcon className="photo-preview-loading-icon" />
                            <span>Loading preview...</span>
                        </div>
                    )}
                    {mediaError && (
                        <div className="photo-preview-loading photo-preview-error-panel" role="alert">
                            <InformationCircleIcon className="photo-preview-loading-icon" />
                            <div className="photo-preview-error-body">
                                <span>{mediaError}</span>
                                <button
                                    type="button"
                                    className="btn btn-soft photo-preview-download-original"
                                    onClick={(e) => { e.stopPropagation(); void downloadCurrentPhoto(); }}
                                >
                                    <ArrowDownTrayIcon className="toolbar-icon" />
                                    Download original
                                </button>
                            </div>
                        </div>
                    )}
                    {imageUrl && !mediaError && (activeIsVideo ? (
                        <video
                            key={imageUrl}
                            ref={videoRef}
                            src={imageUrl}
                            controls
                            playsInline
                            preload="metadata"
                            poster={shouldProtect ? undefined : (activePhoto.thumbnailUrl ? resolveApiUrl(activePhoto.thumbnailUrl) : undefined)}
                            className={`photo-preview-media ${mediaLoading ? 'is-loading' : ''}`}
                            onLoadedData={(event) => {
                                event.currentTarget.playbackRate = playbackRate;
                                setMediaLoading(false);
                            }}
                            onError={() => {
                                showPreviewFailure('Video playback failed.');
                            }}
                        />
                    ) : (
                        <img
                            key={imageUrl}
                            src={imageUrl}
                            alt={activePhoto.filename}
                            draggable={false}
                            onDragStart={(event) => event.preventDefault()}
                            className={`photo-preview-media ${mediaLoading ? 'is-loading' : ''}`}
                            style={{
                                ...rotationFitStyle,
                                transform: `translate3d(${pan.x}px, ${pan.y}px, 0) rotate(${rotationDraft}deg) scale(${zoom})`,
                            }}
                            onLoad={() => setMediaLoading(false)}
                            onError={() => {
                                showPreviewFailure();
                            }}
                        />
                    ))}
                </div>
                {activeIsVideo && !mediaError && (
                    <div className="photo-preview-speed" role="group" aria-label="Playback speed">
                        <span className="photo-preview-label">Speed</span>
                        {PLAYBACK_RATES.map((rate) => (
                            <button
                                key={rate}
                                type="button"
                                className={`photo-preview-speed-btn ${playbackRate === rate ? 'active' : ''}`}
                                onClick={(e) => { e.stopPropagation(); changePlaybackRate(rate); }}
                                aria-pressed={playbackRate === rate}
                            >
                                {rate}x
                            </button>
                        ))}
                    </div>
                )}
                <button type="button" className="photo-preview-nav next" onClick={showNext} aria-label="Next photo">
                    <ChevronRightIcon className="photo-preview-nav-icon" />
                </button>
                <div
                    ref={filmstripRef}
                    className="photo-preview-filmstrip"
                    aria-label="Nearby photos"
                    onPointerDown={handleFilmstripPointerDown}
                    onPointerMove={handleFilmstripPointerMove}
                    onPointerUp={handleFilmstripPointerUp}
                    onPointerCancel={handleFilmstripPointerCancel}
                    onScroll={handleFilmstripScroll}
                    onTouchStart={stopFilmstripTouchPropagation}
                    onTouchMove={stopFilmstripTouchPropagation}
                    onTouchEnd={stopFilmstripTouchPropagation}
                    onTouchCancel={stopFilmstripTouchPropagation}
                >
                    {filmstripIndexes.map((photoIndex) => {
                        const photo = photos[photoIndex];
                        const thumbPath = getThumbnailPath(photo);
                        const thumbUrl = shouldProtect ? resolvedUrls[thumbPath] : resolveApiUrl(thumbPath);
                        const selected = photoIndex === index;
                        return (
                            <button
                                key={photo.filename}
                                type="button"
                                className={`photo-preview-thumb ${selected ? 'selected' : ''}`}
                                data-photo-index={photoIndex}
                                aria-current={selected ? 'true' : undefined}
                                aria-pressed={selected}
                                ref={(node) => {
                                    filmstripItemRefs.current[photoIndex] = node;
                                }}
                                onClick={(e) => {
                                    e.stopPropagation();
                                    if (suppressFilmstripClickRef.current) {
                                        return;
                                    }
                                    selectFilmstripPhoto(photoIndex);
                                }}
                                aria-label={`View ${photo.filename}`}
                            >
                                <img src={thumbUrl || undefined} alt={photo.filename} loading="lazy" />
                            </button>
                        );
                    })}
                </div>
                <div className="photo-preview-details">
                    <div>
                        <span className="photo-preview-label">Kind</span>
                        <span>{getMediaKind(activePhoto.filename)}</span>
                    </div>
                    {activePhoto.exifSummary?.capturedAt && (
                        <div>
                            <span className="photo-preview-label">Captured</span>
                            <span>{activePhoto.exifSummary.capturedAt}</span>
                        </div>
                    )}
                    {activePhoto.exifSummary?.camera && (
                        <div>
                            <span className="photo-preview-label">Camera</span>
                            <span>{activePhoto.exifSummary.camera}</span>
                        </div>
                    )}
                    {activePhoto.location?.address && (
                        <div>
                            <span className="photo-preview-label">Location</span>
                            <span>{activePhoto.location.address}</span>
                        </div>
                    )}
                    {(activePhoto.tags || []).length > 0 && (
                        <div className="photo-preview-tags">
                            {(activePhoto.tags || []).slice(0, 6).map((tag) => (
                                <span key={tag} className="tag-chip">{tag}</span>
                            ))}
                        </div>
                    )}
                </div>
            </div>
        </section>
    );
};

export default PhotoViewer;
