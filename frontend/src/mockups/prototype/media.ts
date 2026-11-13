import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties, type RefObject } from 'react';
import { get, post, resolveApiUrl } from '../../services/apiClient';
import { isAuthEnabled } from '../../services/authClient';
import { fetchProtectedBlobUrl, fetchProtectedBlobUrlWithProgress, ProtectedFetchError } from '../../services/imageClient';
import { resolveThumbnailAccessUrls } from '../../services/thumbnailAccessCache';
import { resolveMediaAccessUrls } from '../../services/mediaAccessCache';
import { getCachedSortGrid, getCachedSortThumb } from '../../services/localSortIndex';
import { getCachedMediaToken, getMediaToken, imageUrlForBlob, previewUrlForBlob, thumbnailUrlForBlob } from '../../services/mediaToken';
import { isHttpUrl, shouldFetchScopedThumbnail } from '../../components/shared/PhotoTile';
import { isRawFilename, isVideoFilename } from '../../utils/photoDisplay';
import type { Photo } from './types';

// Image resolution for the prototype's tiles and viewer. Deliberately thin:
// all the real logic (which formats need a scoped access token, how a SAS URL
// differs from a backend-proxy path, batched token minting) already lives in
// components/shared/PhotoTile + services/thumbnailAccessCache + imageClient, so
// we reuse those instead of duplicating the rules.

/** Directly-loadable thumbnail source when no scoped access token is needed. */
const directThumbnailSource = (photo: Photo): string | undefined => {
    // Same URL the gallery uses (container token + sort-index blob name) whenever both are
    // loaded, so a thumbnail already fetched on another page is a browser-cache hit instead
    // of a second download through a per-photo SAS link.
    const tokenUrl = thumbnailUrlForBlob(getCachedSortThumb(photo.filename), getCachedMediaToken());
    if (tokenUrl) {
        return tokenUrl;
    }
    if (!photo.thumbnailUrl) {
        return undefined;
    }
    return isHttpUrl(photo.thumbnailUrl) ? photo.thumbnailUrl : resolveApiUrl(photo.thumbnailUrl);
};

/**
 * Resolves a whole page of photos to loadable thumbnail `src` strings keyed by
 * filename. Most photos carry a direct SAS URL and need no round trip; the
 * formats that do (HEIC/CR3/video, per shouldFetchScopedThumbnail) get their
 * access tokens minted in ONE batched request per page — mirrors PhotoGallery.
 */
export function usePhotoThumbnails(photos: Photo[]): Record<string, string> {
    const [scoped, setScoped] = useState<Record<string, string>>({});
    const resolvedRef = useRef<Set<string>>(new Set());
    const key = photos.map((p) => p.filename).join('|');

    useEffect(() => {
        if (!isAuthEnabled()) {
            return;
        }
        const need = photos
            .filter((p) => shouldFetchScopedThumbnail(p.filename, p.thumbnailUrl))
            .filter((p) => !thumbnailUrlForBlob(getCachedSortThumb(p.filename), getCachedMediaToken()))
            .map((p) => p.filename)
            .filter((filename) => !resolvedRef.current.has(filename));
        if (need.length === 0) {
            return;
        }
        need.forEach((filename) => resolvedRef.current.add(filename));
        let active = true;
        void resolveThumbnailAccessUrls(need).then((map) => {
            if (!active || map.size === 0) {
                return;
            }
            setScoped((prev) => {
                const next = { ...prev };
                map.forEach((url, filename) => {
                    if (url) {
                        next[filename] = url;
                    }
                });
                return next;
            });
        });
        return () => {
            active = false;
        };
    }, [key]); // eslint-disable-line react-hooks/exhaustive-deps

    const out: Record<string, string> = {};
    for (const photo of photos) {
        const tokenUrl = thumbnailUrlForBlob(getCachedSortThumb(photo.filename), getCachedMediaToken());
        if (tokenUrl) {
            out[photo.filename] = tokenUrl;
        } else if (shouldFetchScopedThumbnail(photo.filename, photo.thumbnailUrl)) {
            const url = scoped[photo.filename];
            if (url) {
                out[photo.filename] = url;
            }
        } else {
            const direct = directThumbnailSource(photo);
            if (direct) {
                out[photo.filename] = direct;
            }
        }
    }
    return out;
}

// Widths declared in tile srcsets. Tiles are square and crop with object-fit:
// cover, so what has to cover a tile is an image's SHORT edge, not its width:
// ~90 px for a typical 4:3 photo's 120 px thumbnail, and 400 px for the grid
// image (GRID_SHORT_EDGE in backend/image_utils.py). The browser therefore
// keeps the thumbnail only for tiles up to ~90 device px -- the smallest zoom
// level on a 1x screen -- and takes the grid image everywhere else.
const THUMB_COVER_WIDTH = 90;
const GRID_COVER_WIDTH = 400;

export interface TileImage {
    src: string;
    srcSet?: string;
    style?: CSSProperties;
}

const normRot = (value?: number) => { const r = Number(value || 0) % 360; return r < 0 ? r + 360 : r; };
const rotateStyle = (degrees: number): CSSProperties | undefined => (degrees ? { transform: `rotate(${degrees}deg)` } : undefined);

/**
 * What a grid tile's <img> shows, given the thumbnail src from usePhotoThumbnails:
 * the thumbnail plus, once the photo has one, the sharp grid image in srcset.
 *
 * The thumbnail bakes in `thumbnailRotation`, so its tile turns by only what
 * the user rotated on top; the grid image, like the preview, bakes in nothing
 * but EXIF orientation, so its tile turns by the full `rotation`. One <img>
 * carries one transform, so the two can share a srcset only when the
 * thumbnail has no baked rotation; otherwise the tile shows the grid image
 * alone, which is sharp at every zoom level anyway.
 */
export const tileImage = (photo: Photo, thumbSrc: string | undefined): TileImage | null => {
    const grid = thumbnailUrlForBlob(photo.gridBlob || getCachedSortGrid(photo.filename), getCachedMediaToken());
    if (!grid) {
        return thumbSrc ? { src: thumbSrc, style: rotateStyle(normRot(normRot(photo.rotation) - normRot(photo.thumbnailRotation))) } : null;
    }
    const style = rotateStyle(normRot(photo.rotation));
    if (!thumbSrc || normRot(photo.thumbnailRotation)) {
        return { src: grid, style };
    }
    return { src: thumbSrc, srcSet: `${thumbSrc} ${THUMB_COVER_WIDTH}w, ${grid} ${GRID_COVER_WIDTH}w`, style };
};

/**
 * `sizes` for a grid's tile srcsets: the rendered width of its first tile,
 * re-measured whenever that tile resizes (zoom level, window). Measured in a
 * layout effect so it lands before lazy tiles start loading. Undefined until
 * measured, which the browser reads as 100vw -- it then picks the grid image.
 */
export function useTileSizes(gridRef: RefObject<HTMLElement>, firstTileKey?: string): string | undefined {
    const [width, setWidth] = useState(0);
    useLayoutEffect(() => {
        const tile = gridRef.current?.firstElementChild;
        if (!tile) return undefined;
        const measure = () => setWidth(Math.round(tile.getBoundingClientRect().width));
        measure();
        if (typeof ResizeObserver === 'undefined') return undefined;
        const observer = new ResizeObserver(measure);
        observer.observe(tile);
        return () => observer.disconnect();
    }, [gridRef, firstTileKey]);
    return width > 0 ? `${width}px` : undefined;
}

// Direct container-token URL for one of a photo's media tiers (thumbnail/
// preview/full-res all share the same physical blob name -- just a different
// container/prefix, see /api/photos/media-token), or '' when the blob name or
// token isn't known client-side yet.
const directMediaUrl = (kind: 'preview' | 'image' | 'thumbnail', filename: string): string => {
    const blob = getCachedSortThumb(filename);
    const token = getCachedMediaToken();
    if (!blob || !token) {
        return '';
    }
    if (kind === 'thumbnail') return thumbnailUrlForBlob(blob, token);
    if (kind === 'preview') return previewUrlForBlob(blob, token);
    return imageUrlForBlob(blob, token);
};

// Resolves one of a photo's media tiers to a loadable URL. Prefers the direct
// container-token URL (zero backend calls, the common case once the sort
// index/media token are warm -- see directMediaUrl); falls back to the
// batched/cached access-batch resolvers only when the blob name isn't known
// client-side yet (a very recently uploaded file whose sort-index row hasn't
// synced, or proxy mode with no SAS token at all).
//
// Deliberately doesn't check *_status first: a direct URL for a not-yet-
// generated blob 404s, and the caller (useMainMedia) treats that as "still
// generating" and retries with backoff instead of asking this backend whether
// it's ready first -- that status check used to be the dominant source of
// /api/photos/access-batch traffic (see the 2026-10-09 HAR investigation).
const accessUrl = async (kind: 'preview' | 'image' | 'thumbnail', filename: string): Promise<string> => {
    const direct = directMediaUrl(kind, filename);
    if (direct) {
        return direct;
    }
    const map = kind === 'thumbnail'
        ? await resolveThumbnailAccessUrls([filename])
        : await resolveMediaAccessUrls(kind, [filename]);
    return map.get(filename) || '';
};

/**
 * Fire-and-forget cache warm for the viewer's neighbor photos (see
 * PhotoViewer.tsx's preload effect): resolves 'preview' URLs for whichever of
 * the given filenames aren't already cached, in one batched call, so
 * navigating to them next/prev finds the URL already resolved instead of
 * triggering a fresh backend round trip. Never throws -- resolveMediaAccessUrls
 * already degrades to '' per filename on failure.
 *
 * Filenames that already resolve to a direct container-token URL (the common
 * case once the sort index/media token are warm) are skipped entirely --
 * there's nothing to "preload" via access-batch for those, since the viewer
 * will construct the same URL itself on arrival. Without this, every
 * neighbor-preload call hit access-batch unconditionally regardless of
 * whether a direct URL was already resolvable, which is what was still
 * showing up as per-photo access-batch traffic even after useMainMedia
 * itself was switched to direct-first resolution (confirmed live via a HAR:
 * 21 calls, all preview-tier, all for formats whose preview isn't generated
 * yet -- i.e. this path, not useMainMedia's own resolution).
 */
export function preloadMediaAccessUrls(filenames: string[]): void {
    const targets = filenames.filter((f) => Boolean(f) && !directMediaUrl('preview', f));
    if (targets.length === 0) {
        return;
    }
    void resolveMediaAccessUrls('preview', targets);
}

// How long to wait before re-attempting a tier whose blob 404'd (not
// generated yet), and how many times -- ~1 minute of backoff total before
// giving up and showing the terminal "unavailable" state. A 404 is the
// expected, common case for a photo opened moments after upload; genuinely
// unsupported/corrupt files settle into 'unavailable' once these are
// exhausted instead of retrying forever.
const PENDING_RETRY_DELAYS_MS = [4000, 8000, 16000, 32000];

// Tries each media tier's direct/resolved URL in order, falling through to the
// next tier on a 404 (not generated yet) instead of giving up immediately --
// e.g. a photo whose preview isn't ready yet but whose thumbnail already is
// still shows *something*. Returns '' (not an error) when every tier 404'd
// this round; re-throws any non-404 failure (network/auth/decode error) so
// the caller can fail fast instead of retrying something that can't succeed.
const fetchFirstAvailableTier = async (
    order: Array<'preview' | 'image' | 'thumbnail'>,
    filename: string,
    fallbackUrl: string | undefined,
    fullRes: boolean,
    signal: AbortSignal,
    onProgress: (loadedBytes: number, totalBytes: number) => void,
): Promise<string> => {
    const fetchOne = (target: string) => (fullRes
        ? fetchProtectedBlobUrlWithProgress(target, { signal, onProgress })
        : fetchProtectedBlobUrl(target));

    // A 401/403 means the token baked into a direct URL was rejected server-
    // side despite the local cache still thinking it was fresh (clock skew,
    // or the background auto-refresh -- see mediaToken.ts -- not having run
    // yet). Force a real refresh and re-resolve the SAME tier once with a
    // freshly signed URL before giving up; retrying the stale `target`
    // string itself could never succeed, since its signature doesn't change.
    const fetchWithAuthRetry = async (kind: 'preview' | 'image' | 'thumbnail', target: string): Promise<string> => {
        try {
            return await fetchOne(target);
        } catch (err) {
            if (err instanceof ProtectedFetchError && (err.status === 401 || err.status === 403)) {
                await getMediaToken(true);
                const refreshed = await accessUrl(kind, filename);
                if (refreshed && refreshed !== target) {
                    return fetchOne(refreshed);
                }
            }
            throw err;
        }
    };

    for (const kind of order) {
        const target = await accessUrl(kind, filename);
        if (!target) continue;
        try {
            return await fetchWithAuthRetry(kind, target);
        } catch (err) {
            if (err instanceof ProtectedFetchError && err.status === 404) continue;
            throw err;
        }
    }
    if (fallbackUrl && isHttpUrl(fallbackUrl)) {
        try {
            return await fetchOne(fallbackUrl);
        } catch (err) {
            if (!(err instanceof ProtectedFetchError && err.status === 404)) throw err;
        }
    }
    return '';
};

/**
 * Resolves the viewer image for a photo to an object URL, revoking it on
 * change/unmount. In preview mode it prefers the shrunk preview tier and falls
 * back through the full image and finally a scoped thumbnail, so formats whose
 * preview blob isn't ready yet (HEIC/CR3/video) still show *something* instead
 * of a blank stage. `fullRes` flips the order to fetch the original first — used
 * by the viewer's "Full res" button.
 *
 * Every tier is attempted directly (no "is it ready" backend call first --
 * see accessUrl/directMediaUrl); a blob that 404s because it isn't generated
 * yet retries with backoff (`status: 'pending'`) instead of giving up, and
 * settles into a terminal `status: 'unavailable'` once retries are exhausted
 * or a non-404 error occurs, so the caller can show a graceful message instead
 * of an endless spinner.
 */
export interface MainMediaState {
    url: string | undefined;
    /** True while a `fullRes` fetch is in flight (drives the FR button's loading ring). */
    loading: boolean;
    /** 0-100 download progress for the in-flight `fullRes` fetch. */
    progress: number;
    /** 'pending': not generated yet, retrying with backoff. 'unavailable': gave up (terminal). undefined: normal. */
    status?: 'pending' | 'unavailable';
}

export function useMainMedia(photo?: Photo | null, fullRes = false): MainMediaState {
    const [url, setUrl] = useState<string | undefined>(undefined);
    const [loading, setLoading] = useState(false);
    const [progress, setProgress] = useState(0);
    const [status, setStatus] = useState<'pending' | 'unavailable' | undefined>(undefined);
    const filename = photo?.filename;
    const fallbackUrl = photo?.thumbnailUrl;
    // The object URL on screen. Switching full resolution on or off for the same
    // photo keeps it up until the other tier arrives, instead of blanking the
    // stage to a spinner for the whole download; it is revoked once replaced.
    const shownRef = useRef<{ filename: string; url: string } | null>(null);
    const release = () => {
        if (shownRef.current) URL.revokeObjectURL(shownRef.current.url);
        shownRef.current = null;
    };
    useEffect(() => release, []);

    useEffect(() => {
        if (!filename) {
            release();
            setUrl(undefined);
            setLoading(false);
            setProgress(0);
            setStatus(undefined);
            return;
        }
        let active = true;
        let retryTimer: ReturnType<typeof setTimeout> | undefined;
        if (shownRef.current?.filename !== filename) {
            release();
            setUrl(undefined);
        }
        setProgress(0);
        setStatus(undefined);
        setLoading(fullRes);
        const controller = new AbortController();
        // 'image' is a SAS URL to the original blob -- for RAW files
        // (CR3/NEF/ARW/DNG/.../SRW) that's the undecoded raw file itself,
        // which no browser can render as an <img>, REGARDLESS of fullRes: a
        // fetch of it still succeeds (200, bytes arrive) and createObjectURL
        // happily wraps raw sensor bytes into a blob: URL, so without this
        // exclusion a RAW photo whose preview isn't generated yet would
        // "succeed" into an undecodable blob instead of falling through to
        // thumbnail or retrying -- confirmed live via a Network-panel trace
        // (Samsung__NX300__nx300.SRW: blob: URL created, never rendered).
        const order: Array<'preview' | 'image' | 'thumbnail'> = isRawFilename(filename)
            ? ['preview', 'thumbnail']
            : fullRes
                ? ['image', 'preview', 'thumbnail']
                : ['preview', 'image', 'thumbnail'];

        const attempt = async (retryIndex: number) => {
            try {
                const blobUrl = await fetchFirstAvailableTier(order, filename, fallbackUrl, fullRes, controller.signal, (loadedBytes, totalBytes) => {
                    if (active) {
                        setProgress(totalBytes > 0 ? Math.min(99, Math.round((loadedBytes / totalBytes) * 100)) : 0);
                    }
                });
                if (!active) {
                    if (blobUrl) URL.revokeObjectURL(blobUrl);
                    return;
                }
                if (!blobUrl) {
                    // Every tier 404'd -- nothing generated yet. Retry with backoff
                    // (loading/FR ring stays on through retries, see below).
                    if (retryIndex < PENDING_RETRY_DELAYS_MS.length) {
                        setStatus('pending');
                        retryTimer = setTimeout(() => { void attempt(retryIndex + 1); }, PENDING_RETRY_DELAYS_MS[retryIndex]);
                        return;
                    }
                    setStatus('unavailable');
                    setLoading(false);
                    return;
                }
                release();
                shownRef.current = { filename, url: blobUrl };
                setProgress(100);
                setStatus(undefined);
                setUrl(blobUrl);
                setLoading(false);
            } catch {
                if (!active) return;
                setLoading(false);
                // A failed switch of tier leaves the photo that is already showing.
                if (shownRef.current?.filename === filename) return;
                setUrl(undefined);
                setStatus('unavailable');
            }
        };

        void attempt(0);

        return () => {
            active = false;
            controller.abort();
            if (retryTimer) {
                clearTimeout(retryTimer);
            }
        };
    }, [filename, fullRes]); // eslint-disable-line react-hooks/exhaustive-deps

    return { url, loading, progress, status };
}

export interface VideoSourceState {
    /** Streamable URL of the original; the browser range-reads it, nothing is downloaded up front. */
    src: string | undefined;
    /** Direct thumbnail URL for the player's poster (no fetch, no blob URL). */
    poster: string | undefined;
    /** True once the original can't be played (unsupported codec/container, or the URL stays rejected). */
    failed: boolean;
    /** Call from the <video> onError. */
    reportError: () => void;
}

/** Direct URL of a video's original for a <video> element. */
export function useVideoSource(photo?: Photo | null): VideoSourceState {
    const [src, setSrc] = useState<string | undefined>(undefined);
    const [poster, setPoster] = useState<string | undefined>(undefined);
    const [failed, setFailed] = useState(false);
    const retriedRef = useRef(false);
    const filename = photo && isVideoFilename(photo.filename) ? photo.filename : undefined;

    useEffect(() => {
        retriedRef.current = false;
        setSrc(undefined);
        setPoster(undefined);
        setFailed(false);
        if (!filename) return;
        let active = true;
        void accessUrl('thumbnail', filename).then((url) => { if (active) setPoster(url || undefined); }).catch(() => {});
        void accessUrl('image', filename).then((url) => { if (active) setSrc(url || undefined); }).catch(() => {});
        return () => { active = false; };
    }, [filename]);

    // A <video> error carries no HTTP status, so probe the URL: a rejected token (401/403) is
    // worth one refresh; anything else (an undecodable codec such as ProRes or MXF) is final.
    const reportError = () => {
        if (!filename || !src) return;
        const current = src;
        void (async () => {
            try {
                const probe = await fetch(current, { headers: { Range: 'bytes=0-0' } });
                if ((probe.status === 401 || probe.status === 403) && !retriedRef.current) {
                    retriedRef.current = true;
                    await getMediaToken(true);
                    const refreshed = await accessUrl('image', filename);
                    if (refreshed && refreshed !== current) {
                        setSrc(refreshed);
                        return;
                    }
                }
            } catch {
                // fall through to failed
            }
            setFailed(true);
        })();
    };

    return { src, poster, failed, reportError };
}

/** Direct URL of a Live Photo's motion clip (see Photo.liveClip), resolved once the photo is shown. */
export function useLiveClipSource(clip?: string): string | undefined {
    const [src, setSrc] = useState<string | undefined>(undefined);
    useEffect(() => {
        setSrc(undefined);
        if (!clip) return;
        let active = true;
        void accessUrl('image', clip).then((url) => { if (active) setSrc(url || undefined); }).catch(() => {});
        return () => { active = false; };
    }, [clip]);
    return src;
}

// Shape of GET /api/photos/<filename>/metadata, trimmed to the fields the
// viewer's info panel renders.
export interface PhotoMetadata {
    exifSummary?: {
        camera?: string; lens?: string; fNumber?: string; exposureTime?: string;
        iso?: string; focalLength?: string; capturedAt?: string;
    };
    resolution?: { width?: number; height?: number };
    location?: { city?: string; country?: string; address?: string; latitude?: string; longitude?: string };
    tags?: string[];
    objects?: string[];
    ocrText?: string;
    caption?: string;
    uploadDate?: string;
}

/** Fetches the full metadata record for a single photo (info panel only). */
export async function fetchPhotoMetadata(filename: string): Promise<PhotoMetadata | null> {
    try {
        return await get<PhotoMetadata>(`/api/photos/${encodeURIComponent(filename)}/metadata`);
    } catch {
        return null;
    }
}

/** Persists a new rotation (normalized to 0/90/180/270) for a photo. */
export async function setPhotoRotation(filename: string, rotation: number): Promise<void> {
    const normalized = ((rotation % 360) + 360) % 360;
    await post(`/api/photos/${encodeURIComponent(filename)}/rotation`, { rotation: normalized });
}

// Resolves a photo's best downloadable media (full-size original, else
// preview, else thumbnail) directly to a fetched object URL -- same 404-
// tolerant per-tier fallback as the viewer (see fetchFirstAvailableTier), so
// a photo whose preview isn't generated yet still downloads its thumbnail
// instead of failing outright.
const fetchDownloadBlobUrl = (photo: Photo): Promise<string> => (
    fetchFirstAvailableTier(['image', 'preview', 'thumbnail'], photo.filename, photo.thumbnailUrl, false, new AbortController().signal, () => {})
);

/** Downloads a photo's file to the user's device. Throws on failure. */
export async function downloadPhoto(photo: Photo): Promise<void> {
    const objectUrl = await fetchDownloadBlobUrl(photo);
    if (!objectUrl) {
        throw new Error('No downloadable media for this photo.');
    }
    try {
        const anchor = document.createElement('a');
        anchor.href = objectUrl;
        anchor.download = photo.filename;
        anchor.rel = 'noreferrer';
        anchor.style.display = 'none';
        document.body.appendChild(anchor);
        anchor.click();
        anchor.remove();
    } finally {
        setTimeout(() => URL.revokeObjectURL(objectUrl), 5000);
    }
}


