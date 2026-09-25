import { useEffect, useRef, useState } from 'react';
import { get, resolveApiUrl } from '../../services/apiClient';
import { isAuthEnabled } from '../../services/authClient';
import { fetchProtectedBlobUrl } from '../../services/imageClient';
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

/**
 * Resolves the full-size (preview) image for the viewer to an object URL,
 * revoking it on change/unmount. Prefers a scoped preview; falls back to a
 * direct thumbnail SAS when a preview isn't available for the file.
 */
export function useMainMedia(photo?: Photo | null): string | undefined {
    const [url, setUrl] = useState<string | undefined>(undefined);
    const filename = photo?.filename;

    useEffect(() => {
        if (!filename) {
            setUrl(undefined);
            return;
        }
        let active = true;
        let created: string | undefined;
        setUrl(undefined);
        void (async () => {
            try {
                let target = '';
                try {
                    const res = await get(`/api/photos/access/preview/${encodeURIComponent(filename)}`);
                    if (res && typeof res.url === 'string') {
                        target = res.url;
                    }
                } catch {
                    // no preview available — fall back to a direct thumbnail below
                }
                if (!target && photo?.thumbnailUrl && isHttpUrl(photo.thumbnailUrl)) {
                    target = photo.thumbnailUrl;
                }
                if (!target) {
                    return;
                }
                const blobUrl = await fetchProtectedBlobUrl(target);
                if (!active) {
                    URL.revokeObjectURL(blobUrl);
                    return;
                }
                created = blobUrl;
                setUrl(blobUrl);
            } catch {
                if (active) {
                    setUrl(undefined);
                }
            }
        })();
        return () => {
            active = false;
            if (created) {
                URL.revokeObjectURL(created);
            }
        };
    }, [filename]); // eslint-disable-line react-hooks/exhaustive-deps

    return url;
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
