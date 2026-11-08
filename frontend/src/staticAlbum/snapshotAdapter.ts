import { AxiosError, type AxiosAdapter, type InternalAxiosRequestConfig } from 'axios';

interface SnapshotManifestPhoto { n: string; rot?: number; trot?: number; video?: boolean; t?: string; p?: string }
export interface SnapshotManifest {
    name?: string; total: number; cached: number; building?: boolean; expiresAt?: string; photos: SnapshotManifestPhoto[];
}

const BUILD_WAIT_MS = 120000;
const POLL_MS = 3000;
const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

const fetchManifest = async (base: string): Promise<SnapshotManifest> => {
    const res = await fetch(`${base}manifest.json`, { cache: 'no-cache' });
    if (!res.ok) throw new Error(String(res.status));
    return res.json();
};

/** Waits for a just-shared album's backfill. Resolves as soon as photos are there; rejects after BUILD_WAIT_MS. */
const loadManifest = async (base: string): Promise<SnapshotManifest> => {
    const started = Date.now();
    let manifest = await fetchManifest(base);
    while (manifest.building && Date.now() - started < BUILD_WAIT_MS) {
        await sleep(POLL_MS);
        try { manifest = await fetchManifest(base); } catch { /* keep waiting */ }
    }
    return manifest;
};

const fail = (config: InternalAxiosRequestConfig, status: number, message: string): AxiosError =>
    new AxiosError(message, 'ERR_BAD_REQUEST', config, null, {
        status, statusText: message, data: { error: message }, headers: {}, config,
    });

/** The same payload GET /public/albums/<token> returns, built from the snapshot manifest. */
export const manifestToAlbumResponse = (manifest: SnapshotManifest, base: string, token: string) => ({
    album: { name: manifest.name || 'Shared album', photoCount: manifest.total },
    photos: manifest.photos.map((p) => ({
        filename: p.n,
        // Originals (downloads, videos, photos without a cached preview) come from the real archive API.
        url: `/public/photos/${encodeURIComponent(token)}/image/${p.n.split('/').map(encodeURIComponent).join('/')}`,
        thumbnailUrl: p.t ? new URL(p.t, base).href : undefined,
        previewUrl: p.p ? new URL(p.p, base).href : undefined,
        rotation: p.rot || 0,
        thumbnailRotation: p.trot || 0,
    })),
    hasMore: false,
});

/** Wraps an axios adapter so the first-page read of the public album is served from the snapshot. */
export const snapshotAdapter = (base: string, token: string, onManifest: (m: SnapshotManifest) => void) =>
    (next: AxiosAdapter): AxiosAdapter => async (config) => {
        const url = String(config.url || '');
        const isRead = (config.method || 'get').toLowerCase() === 'get'
            && url.startsWith(`/public/albums/${encodeURIComponent(token)}?`);
        if (!isRead) return next(config);
        let manifest: SnapshotManifest;
        try {
            manifest = await loadManifest(base);
        } catch {
            throw fail(config, 404, 'This public album link is invalid or no longer available.');
        }
        const exp = manifest.expiresAt ? Date.parse(manifest.expiresAt) : NaN;
        if (!Number.isNaN(exp) && Date.now() > exp) throw fail(config, 410, 'This shared album link has expired.');
        onManifest(manifest);
        return { data: manifestToAlbumResponse(manifest, base, token), status: 200, statusText: 'OK', headers: {}, config };
    };
