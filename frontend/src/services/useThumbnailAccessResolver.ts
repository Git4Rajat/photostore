import { useCallback, useState } from 'react';
import { isAuthEnabled } from './authClient';
import { resolveThumbnailAccessUrls } from './thumbnailAccessCache';
import { shouldFetchScopedThumbnail } from '../components/shared/PhotoTile';

/** Minimal shape resolveAccessForBatch needs from a photo-like item. */
export interface ThumbnailAccessSubject {
    filename: string;
    thumbnailUrl?: string;
}

/**
 * Batches one access-token request per fetched photo list (not one per tile)
 * via thumbnailAccessCache's module-level cache -- shared across
 * PhotoGallery/AlbumsPage/ToolsPage/FaceClusters so a filename already
 * resolved on one page resolves for free on another. Extracted from the
 * near-identical `resolveAccessForBatch` previously duplicated in
 * AlbumsPage.tsx and ToolsPage.tsx.
 */
export const useThumbnailAccessResolver = () => {
    const [thumbAccessUrls, setThumbAccessUrls] = useState<Map<string, string>>(new Map());

    const resolveAccessForBatch = useCallback(<T extends ThumbnailAccessSubject>(list: T[]) => {
        if (!isAuthEnabled()) {
            return;
        }
        const needsAccess = list
            .filter((p) => shouldFetchScopedThumbnail(p.filename, p.thumbnailUrl))
            .map((p) => p.filename);
        if (needsAccess.length === 0) {
            return;
        }
        resolveThumbnailAccessUrls(needsAccess).then((resolved) => {
            setThumbAccessUrls((prev) => {
                const next = new Map(prev);
                resolved.forEach((url, filename) => next.set(filename, url));
                return next;
            });
        });
    }, []);

    return { thumbAccessUrls, setThumbAccessUrls, resolveAccessForBatch };
};
