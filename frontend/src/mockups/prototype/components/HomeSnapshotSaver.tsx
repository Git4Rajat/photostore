import { useEffect, useRef } from 'react';
import { useStore } from '../store';
import { usePhotoThumbnails } from '../media';
import { HOME_SNAPSHOT_LIMIT, homeSnapshot } from '../../../services/homeSnapshot';

// Wait for the gallery to settle (thumbnails painted from the HTTP cache) before copying them.
const SETTLE_MS = 4000;
// Re-save at most this often; the top of the gallery rarely changes between page views.
const MIN_INTERVAL_MS = 10 * 60 * 1000;

/** Renders nothing. Keeps the device-local home snapshot (see homeSnapshot.ts) fresh while the gallery is shown. */
export const HomeSnapshotSaver = (): null => {
    const { photos } = useStore();
    const top = photos.slice(0, HOME_SNAPSHOT_LIMIT);
    const urls = usePhotoThumbnails(top);
    const lastSavedAt = useRef(0);
    const key = top.map((p) => p.filename).join('|');

    useEffect(() => {
        if (top.length < 12 || Date.now() - lastSavedAt.current < MIN_INTERVAL_MS) return undefined;
        const timer = window.setTimeout(() => {
            if (document.visibilityState !== 'visible' || navigator.onLine === false) return;
            lastSavedAt.current = Date.now();
            void homeSnapshot.save(top, urls);
        }, SETTLE_MS);
        return () => window.clearTimeout(timer);
    // `urls` changes identity every render; the photo list is what decides whether a re-save is worthwhile.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [key]);

    return null;
};
