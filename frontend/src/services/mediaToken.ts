import { get } from './apiClient';
import { perf } from './perf';
import { getActiveLibraryFromToken } from './passwordAuthClient';

/**
 * One container-scoped, read-only token for every thumbnail and preview
 * (GET /api/photos/media-token). With it the browser builds
 * `{baseUrl}/{blobName}?{sas}` itself, so loading a page of thumbnails needs no
 * backend call and no per-photo signed URLs. Blob names come from the sort
 * index (`thumb`) or photo summaries (`thumbnailBlob`).
 *
 * Cached in memory and in localStorage until shortly before it expires; the
 * server's tokens are day-aligned, so there are always >=24h of validity left.
 */
export interface MediaToken {
    baseUrl: string;
    sas: string;
    expiresAt: string;
    previewPrefix: string;
    /** Face-crop container token (People avatars); absent on older tokens or when unavailable. */
    cover?: { baseUrl: string; sas: string; prefix: string };
    /** Full-resolution original container token (separate container from thumbnail/preview); absent on older tokens or when unavailable. */
    image?: { baseUrl: string; sas: string };
}

interface MediaTokenResponse extends Partial<MediaToken> {
    available: boolean;
}

const STORAGE_KEY = 'photostore-media-token';
const STORED_VERSION = 3;
const REFRESH_MARGIN_MS = 30 * 60 * 1000;

let cached: MediaToken | null = null;
let cachedKey: string | null = null;
let inFlight: Promise<MediaToken | null> | null = null;

const tokenKey = (): string => getActiveLibraryFromToken() || '__default__';

const isFresh = (token: MediaToken | null): token is MediaToken => {
    if (!token) return false;
    const expires = Date.parse(token.expiresAt);
    return Number.isFinite(expires) && expires - Date.now() > REFRESH_MARGIN_MS;
};

const readStored = (key: string): MediaToken | null => {
    try {
        const raw = window.localStorage.getItem(`${STORAGE_KEY}:${key}`);
        if (!raw) return null;
        const parsed = JSON.parse(raw) as MediaToken;
        // Tokens stored before the face-crop token existed carry no version: fetch a fresh one.
        return isFresh(parsed) && (parsed as MediaToken & { v?: number }).v === STORED_VERSION ? parsed : null;
    } catch {
        return null;
    }
};

const writeStored = (key: string, token: MediaToken): void => {
    try {
        window.localStorage.setItem(`${STORAGE_KEY}:${key}`, JSON.stringify({ ...token, v: STORED_VERSION }));
    } catch {
        // storage unavailable (private window, quota) -- memory cache still works
    }
};

/** Synchronous accessor for already-loaded tokens (null until getMediaToken resolves). */
export const getCachedMediaToken = (): MediaToken | null => {
    const key = tokenKey();
    return cachedKey === key && isFresh(cached) ? cached : null;
};

export const getMediaToken = async (): Promise<MediaToken | null> => {
    const key = tokenKey();
    const current = getCachedMediaToken();
    if (current) return current;
    if (inFlight && cachedKey === key) return inFlight;
    cachedKey = key;
    const stored = readStored(key);
    if (stored) {
        cached = stored;
        return stored;
    }
    inFlight = perf.span('media_token.fetch', () => get<MediaTokenResponse>('/api/photos/media-token'))
        .then((res) => {
            if (!res?.available || !res.baseUrl || !res.sas || !res.expiresAt) {
                cached = null;
                return null;
            }
            const token: MediaToken = {
                baseUrl: res.baseUrl, sas: res.sas, expiresAt: res.expiresAt, previewPrefix: res.previewPrefix || 'preview/',
                cover: res.cover, image: res.image,
            };
            cached = token;
            writeStored(key, token);
            return token;
        })
        .catch(() => null)
        .finally(() => {
            inFlight = null;
        });
    return inFlight;
};

export const invalidateMediaToken = (): void => {
    cached = null;
    cachedKey = null;
};

const encodeBlobPath = (blob: string): string => blob.split('/').map(encodeURIComponent).join('/');

/** Direct thumbnail URL for a blob name, or '' when no token is loaded. */
export const thumbnailUrlForBlob = (blob: string | undefined | null, token: MediaToken | null = getCachedMediaToken()): string => (
    blob && token ? `${token.baseUrl}/${encodeBlobPath(blob)}?${token.sas}` : ''
);

/** Direct preview URL (previews live under `preview/` in the same container). */
export const previewUrlForBlob = (blob: string | undefined | null, token: MediaToken | null = getCachedMediaToken()): string => (
    blob && token ? `${token.baseUrl}/${token.previewPrefix}${encodeBlobPath(blob)}.jpg?${token.sas}` : ''
);

/** Direct full-resolution original URL (separate container token from thumbnail/preview), or '' when unavailable. */
export const imageUrlForBlob = (blob: string | undefined | null, token: MediaToken | null = getCachedMediaToken()): string => (
    blob && token?.image ? `${token.image.baseUrl}/${encodeBlobPath(blob)}?${token.image.sas}` : ''
);

const FACE_CROP_PATH = /^\/api\/faces\/crop\/([^/?#]+)$/;

/** Face id from a `/api/faces/crop/<id>` path, or null. */
export const faceIdFromCropPath = (path: string): string | null => {
    const match = FACE_CROP_PATH.exec(path);
    return match ? decodeURIComponent(match[1]) : null;
};

/** Direct URL of a face's cached crop (no backend call), or '' when the cover token isn't loaded. */
export const faceCropUrlForId = (faceId: string, token: MediaToken | null = getCachedMediaToken()): string => (
    faceId && token?.cover ? `${token.cover.baseUrl}/${token.cover.prefix}${encodeURIComponent(faceId)}.jpg?${token.cover.sas}` : ''
);
