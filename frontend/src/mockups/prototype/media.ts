import { useEffect, useRef, useState } from 'react';
import { get, post, resolveApiUrl } from '../../services/apiClient';
import { isAuthEnabled } from '../../services/authClient';
import { fetchProtectedBlobUrl, fetchProtectedBlobUrlWithProgress } from '../../services/imageClient';
import { resolveThumbnailAccessUrls } from '../../services/thumbnailAccessCache';
import { isHttpUrl, shouldFetchScopedThumbnail } from '../../components/shared/PhotoTile';
import type { Photo } from './types';

// Image resolution for the prototype's tiles and viewer. Deliberately thin:
// all the real logic (which formats need a scoped access token, how a SAS URL
// differs from a backend-proxy path, batched token minting) already lives in
// components/shared/PhotoTile + services/thumbnailAccessCache + imageClient, so
// we reuse those instead of duplicating the rules.

/** Directly-loadable thumbnail source when no scoped access token is needed. */
const directThumbnailSource = (photo: Photo): string | undefined => {
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
        if (shouldFetchScopedThumbnail(photo.filename, photo.thumbnailUrl)) {
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
const accessUrl = async (kind: 'preview' | 'image' | 'thumbnail', filename: string): Promise<string> => {
    try {
        const res = await get(`/api/photos/access/${kind}/${encodeURIComponent(filename)}`);
        return res && typeof res.url === 'string' ? res.url : '';
    } catch {
        return '';
    }
};

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
                const order: Array<'preview' | 'image' | 'thumbnail'> = fullRes
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
        try {
            const res = await get(`/api/photos/access/${kind}/${encodeURIComponent(photo.filename)}`);
            if (res && typeof res.url === 'string' && res.url) {
                return res.url;
            }
        } catch {
            // try the next kind
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
