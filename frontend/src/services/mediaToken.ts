import { get } from './apiClient';
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
}

interface MediaTokenResponse extends Partial<MediaToken> {
    available: boolean;
}

const STORAGE_KEY = 'photostore-media-token';
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
        return isFresh(parsed) ? parsed : null;
    } catch {
        return null;
    }
};

const writeStored = (key: string, token: MediaToken): void => {
    try {
        window.localStorage.setItem(`${STORAGE_KEY}:${key}`, JSON.stringify(token));
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
    inFlight = get<MediaTokenResponse>('/api/photos/media-token')
        .then((res) => {
            if (!res?.available || !res.baseUrl || !res.sas || !res.expiresAt) {
                cached = null;
                return null;
            }
            const token: MediaToken = {
                baseUrl: res.baseUrl, sas: res.sas, expiresAt: res.expiresAt, previewPrefix: res.previewPrefix || 'preview/',
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
