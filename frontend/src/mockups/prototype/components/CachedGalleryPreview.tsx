import React, { useEffect, useMemo, useState } from 'react';
import { LogoLockup } from '../../../components/shared/Logo';
import { homeSnapshot, type HomeSnapshot } from '../../../services/homeSnapshot';
import { useStore } from '../store';
import { useTileSize } from './controls';

/**
 * Stands in for the "Loading Keepsake…" screen on a return visit: the last gallery the device saw, drawn
 * from local thumbnail bytes while the backend wakes. Read-only (tiles are inert) and replaced by the
 * real Shell as soon as the app is ready.
 */
export const useHomeSnapshot = (): HomeSnapshot | null | undefined => {
    const [snapshot, setSnapshot] = useState<HomeSnapshot | null | undefined>(undefined);
    useEffect(() => {
        let cancelled = false;
        void homeSnapshot.load().then((s) => { if (!cancelled) setSnapshot(s); });
        return () => { cancelled = true; };
    }, []);
    return snapshot;
};

export const CachedGalleryPreview: React.FC<{ snapshot: HomeSnapshot }> = ({ snapshot }) => {
    const [tileMin] = useTileSize('photostore.galleryTileSize', { min: 24 });
    const urls = useMemo(() => {
        const map = new Map<string, string>();
        snapshot.thumbs.forEach((blob, filename) => map.set(filename, URL.createObjectURL(blob)));
        return map;
    }, [snapshot]);
    useEffect(() => () => { urls.forEach((url) => URL.revokeObjectURL(url)); }, [urls]);

    const norm = (value: number) => { const r = value % 360; return r < 0 ? r + 360 : r; };

    return (
        <div className="pt-shell">
            <div className="mock-stage">
                <div className="mock-viewport">
                    <div className="mock-app">
                        <header className="ios-header">
                            <LogoLockup size={30} className="ios-header-wordmark" />
                        </header>
                        <div className="mock-body pt-body" aria-busy="true">
                            <div className="pt-toolbar">
                                <div>
                                    <h1 className="pt-page-title">Gallery</h1>
                                    <p className="pt-page-sub" role="status">Updating your library…</p>
                                </div>
                            </div>
                            <div className="pt-grid" style={{ ['--pt-tile-min' as string]: `${tileMin}px` } as React.CSSProperties}>
                                {snapshot.photos.map((photo) => {
                                    const remaining = norm(norm(photo.rotation) - norm(photo.thumbnailRotation));
                                    return (
                                        <div key={photo.filename} className="pt-tile mock-swatch s1" aria-hidden="true">
                                            <img
                                                className="pt-tile-img"
                                                src={urls.get(photo.filename)}
                                                alt=""
                                                draggable={false}
                                                style={remaining ? { transform: `rotate(${remaining}deg)` } : undefined}
                                            />
                                        </div>
                                    );
                                })}
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    );
};

/**
 * Keeps the cached gallery on screen after the real app mounts, until the real gallery has photos (or a few
 * seconds pass), so the grid never blinks empty between the preview and the first loaded page.
 */
export const PreviewUntilGallery: React.FC<{ snapshot: HomeSnapshot | null | undefined }> = ({ snapshot }) => {
    const { photos } = useStore();
    const [expired, setExpired] = useState(false);
    useEffect(() => {
        const timer = window.setTimeout(() => setExpired(true), 6000);
        return () => window.clearTimeout(timer);
    }, []);
    if (!snapshot || expired || photos.length > 0) return null;
    return (
        <div style={{ position: 'fixed', inset: 0, zIndex: 40, background: 'var(--bg, #0c0c0e)' }}>
            <CachedGalleryPreview snapshot={snapshot} />
        </div>
    );
};
