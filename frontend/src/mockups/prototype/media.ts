import { useEffect, useRef, useState } from 'react';
import { get, post, resolveApiUrl } from '../../services/apiClient';
import { isAuthEnabled } from '../../services/authClient';
import { fetchProtectedBlobUrl, fetchProtectedBlobUrlWithProgress } from '../../services/imageClient';
import { resolveThumbnailAccessUrls } from '../../services/thumbnailAccessCache';
import { resolveMediaAccessUrls } from '../../services/mediaAccessCache';
import { getCachedSortThumb } from '../../services/localSortIndex';
import { getCachedMediaToken, thumbnailUrlForBlob } from '../../services/mediaToken';
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

// Mint a scoped access URL for one of the photo's media tiers. Returns '' when
// that tier isn't available yet (e.g. a preview that hasn't been generated).
//
// Routed through the batched, cached resolvers (mediaAccessCache.ts /
// thumbnailAccessCache.ts) instead of the single-item
// GET /api/photos/access/<kind>/<filename> route this used to call directly:
// that route did a guaranteed Table Storage point-read on every single photo
// view/navigation, never cached, never batched. Even resolving one filename
// through the batch endpoint is cheaper (it reads the already-warm gallery
// scan cache instead of a fresh point-read), and a photo re-opened within the
// session -- or preloaded as a viewer neighbor, see preloadMediaAccessUrls
// below -- resolves from the in-memory/localStorage cache with no backend
// call at all.
const accessUrl = async (kind: 'preview' | 'image' | 'thumbnail', filename: string): Promise<string> => {
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

/**
 * Resolves the viewer image for a photo to an object URL, revoking it on
 * change/unmount. In preview mode it prefers the shrunk preview tier and falls
 * back through the full image and finally a scoped thumbnail, so formats whose
 * preview blob isn't ready yet (HEIC/CR3/video) still show *something* instead
 * of a blank stage. `fullRes` flips the order to fetch the original first — used
 * by the viewer's "Full res" button.
 */
export interface MainMediaState {
    url: string | undefined;
    /** True while a `fullRes` fetch is in flight (drives the FR button's loading ring). */
    loading: boolean;
    /** 0-100 download progress for the in-flight `fullRes` fetch. */
    progress: number;
}

export function useMainMedia(photo?: Photo | null, fullRes = false): MainMediaState {
    const [url, setUrl] = useState<string | undefined>(undefined);
    const [loading, setLoading] = useState(false);
    const [progress, setProgress] = useState(0);
    const filename = photo?.filename;

    useEffect(() => {
        if (!filename) {
            setUrl(undefined);
            setLoading(false);
            setProgress(0);
            return;
        }
        let active = true;
        let created: string | undefined;
        setUrl(undefined);
        setProgress(0);
        setLoading(fullRes);
        const controller = new AbortController();
        void (async () => {
            try {
                // 'image' is a SAS URL to the original blob -- for RAW files
                // (CR3/NEF/ARW/DNG/...) that's the undecoded raw file itself,
                // which no browser can render as an <img>. Full-res mode still
                // has to prefer the (already browser-viewable) preview render
                // for those, or toggling "FR" just shows a broken image.
                const order: Array<'preview' | 'image' | 'thumbnail'> = fullRes && !isRawFilename(filename)
                    ? ['image', 'preview', 'thumbnail']
                    : ['preview', 'image', 'thumbnail'];
                let target = '';
                for (const kind of order) {
                    target = await accessUrl(kind, filename);
                    if (target) break;
                }
                if (!target && photo?.thumbnailUrl && isHttpUrl(photo.thumbnailUrl)) {
                    target = photo.thumbnailUrl;
                }
                if (!target) {
                    return;
                }
                const blobUrl = fullRes
                    ? await fetchProtectedBlobUrlWithProgress(target, {
                          signal: controller.signal,
                          onProgress: (loadedBytes, totalBytes) => {
                              if (active) {
                                  setProgress(totalBytes > 0 ? Math.min(99, Math.round((loadedBytes / totalBytes) * 100)) : 0);
                              }
                          },
                      })
                    : await fetchProtectedBlobUrl(target);
                if (!active) {
                    URL.revokeObjectURL(blobUrl);
                    return;
                }
                created = blobUrl;
                setProgress(100);
                setUrl(blobUrl);
            } catch {
                if (active) {
                    setUrl(undefined);
                }
            } finally {
                if (active) {
                    setLoading(false);
                }
            }
        })();
        return () => {
            active = false;
            controller.abort();
            if (created) {
                URL.revokeObjectURL(created);
            }
        };
    }, [filename, fullRes]); // eslint-disable-line react-hooks/exhaustive-deps

    return { url, loading, progress };
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

// Resolves the best downloadable/shareable URL for a photo: the full-size
// original if available, then the preview, then a direct thumbnail SAS.
const resolveDownloadTarget = async (photo: Photo): Promise<string> => {
    for (const kind of ['image', 'preview'] as const) {
        const map = await resolveMediaAccessUrls(kind, [photo.filename]);
        const url = map.get(photo.filename);
        if (url) {
            return url;
        }
    }
    return photo.thumbnailUrl && isHttpUrl(photo.thumbnailUrl) ? photo.thumbnailUrl : '';
};

/** Downloads a photo's file to the user's device. Throws on failure. */
export async function downloadPhoto(photo: Photo): Promise<void> {
    const target = await resolveDownloadTarget(photo);
    if (!target) {
        throw new Error('No downloadable media for this photo.');
    }
    const objectUrl = await fetchProtectedBlobUrl(target);
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
    const target = await resolveDownloadTarget(photo);
    if (!target) return null;
    const objectUrl = await fetchProtectedBlobUrl(target);
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
