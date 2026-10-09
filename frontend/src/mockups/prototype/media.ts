import { useEffect, useRef, useState } from 'react';
import { get, post, resolveApiUrl } from '../../services/apiClient';
import { isAuthEnabled } from '../../services/authClient';
import { fetchProtectedBlobUrl, fetchProtectedBlobUrlWithProgress, ProtectedFetchError } from '../../services/imageClient';
import { resolveThumbnailAccessUrls } from '../../services/thumbnailAccessCache';
import { resolveMediaAccessUrls } from '../../services/mediaAccessCache';
import { getCachedSortThumb } from '../../services/localSortIndex';
import { getCachedMediaToken, imageUrlForBlob, previewUrlForBlob, thumbnailUrlForBlob } from '../../services/mediaToken';
import { isHttpUrl, shouldFetchScopedThumbnail } from '../../components/shared/PhotoTile';
import { isRawFilename } from '../../utils/photoDisplay';
import type { Photo } from './types';

// Image resolution for the prototype's tiles and viewer. Deliberately thin:
// all the real logic (which formats need a scoped access token, how a SAS URL
// differs from a backend-proxy path, batched token minting) already lives in
// components/shared/PhotoTile + services/thumbnailAccessCache + imageClient, so
// we reuse those instead of duplicating the rules.

/** Directly-loadable thumbnail source when no scoped access token is needed. */
export const directThumbnailSource = (photo: Photo): string | undefined => {
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
 */
export function preloadMediaAccessUrls(filenames: string[]): void {
    const targets = filenames.filter(Boolean);
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
    for (const kind of order) {
        const target = await accessUrl(kind, filename);
        if (!target) continue;
        try {
            return await fetchOne(target);
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

    useEffect(() => {
        if (!filename) {
            setUrl(undefined);
            setLoading(false);
            setProgress(0);
            setStatus(undefined);
            return;
        }
        let active = true;
        let created: string | undefined;
        let retryTimer: ReturnType<typeof setTimeout> | undefined;
        setUrl(undefined);
        setProgress(0);
        setStatus(undefined);
        setLoading(fullRes);
        const controller = new AbortController();
        // 'image' is a SAS URL to the original blob -- for RAW files
        // (CR3/NEF/ARW/DNG/...) that's the undecoded raw file itself, which no
        // browser can render as an <img>. Full-res mode still has to prefer
        // the (already browser-viewable) preview render for those, or
        // toggling "FR" just shows a broken image.
        const order: Array<'preview' | 'image' | 'thumbnail'> = fullRes && !isRawFilename(filename)
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
                created = blobUrl;
                setProgress(100);
                setStatus(undefined);
                setUrl(blobUrl);
                setLoading(false);
            } catch {
                if (active) {
                    setUrl(undefined);
                    setStatus('unavailable');
                    setLoading(false);
                }
            }
        };

        void attempt(0);

        return () => {
            active = false;
            controller.abort();
            if (retryTimer) {
                clearTimeout(retryTimer);
            }
            if (created) {
                URL.revokeObjectURL(created);
            }
        };
    }, [filename, fullRes]); // eslint-disable-line react-hooks/exhaustive-deps

    return { url, loading, progress, status };
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

async function toShareFile(photo: Photo): Promise<File | null> {
    const objectUrl = await fetchDownloadBlobUrl(photo);
    if (!objectUrl) return null;
    try {
        const blob = await (await fetch(objectUrl)).blob();
        return new File([blob], photo.filename, { type: blob.type || 'application/octet-stream' });
    } finally {
        URL.revokeObjectURL(objectUrl);
    }
}

export type ShareOutcome = 'shared' | 'downloaded' | 'unsupported' | 'cancelled';

/**
 * Shares one or more photos via the Web Share API (real image files). Falls
 * back to downloading a single photo, or reports 'unsupported' for a multi-photo
 * share the platform can't handle.
 */
export async function sharePhotos(photos: Photo[]): Promise<ShareOutcome> {
    if (photos.length === 0) return 'cancelled';
    try {
        const files = (await Promise.all(photos.slice(0, 10).map(toShareFile))).filter((f): f is File => Boolean(f));
        if (files.length && navigator.canShare?.({ files }) && navigator.share) {
            await navigator.share({ files, title: files.length === 1 ? files[0].name : `${files.length} photos` });
            return 'shared';
        }
    } catch (err) {
        if (err instanceof Error && err.name === 'AbortError') {
            return 'cancelled';
        }
    }
    // No Web Share (or it rejected): a single photo can still be downloaded.
    if (photos.length === 1) {
        try {
            await downloadPhoto(photos[0]);
            return 'downloaded';
        } catch {
            return 'unsupported';
        }
    }
    return 'unsupported';
}
