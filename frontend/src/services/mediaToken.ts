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
 * Cached in memory and in localStorage until shortly before it expires. The
 * server's tokens are short-lived (MEDIA_TOKEN_SAS_TTL_SECONDS, currently
 * 10 minutes) rather than day-aligned -- an idle tab's cached token stops
 * working on its own once the backend scales to zero on inactivity, with no
 * server-side revocation needed (which would kill every outstanding token
 * for the whole account, not just one session). startMediaTokenAutoRefresh
 * keeps the cache warm with a background refresh every ~9 minutes while the
 * tab is visible, so this is meant to be invisible during actual use; a
 * request that still hits a 401/403 (the narrow race right at expiry) should
 * call getMediaToken(true) to force past the local cache and retry.
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
// Must stay under the server's MEDIA_TOKEN_SAS_TTL_SECONDS (10 min default) --
// a margin longer than the token's own life would make every token look
// stale immediately. The background auto-refresh below is what actually
// keeps tokens from reaching this margin during active use; this mainly
// guards the moment right after a long-idle tab resumes.
const REFRESH_MARGIN_MS = 2 * 60 * 1000;
// How often a visible tab checks its token. A refresh is due once the token
// would turn stale (REFRESH_MARGIN_MS before expiry) within two checks. This
// used to be a plain refresh every 9 minutes, which only refetched a token that
// was already stale: for the last minute of every 10-minute token no fresh one
// was cached, so tiles drawn then fell back to URLs signed by an expired token
// and stayed blank until the page was reloaded.
const AUTO_REFRESH_CHECK_MS = 60 * 1000;

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

/**
 * Resolves the current media token, fetching/refreshing if needed.
 * `forceRefresh` skips the local freshness check (both the in-memory and
 * localStorage copies) and always re-fetches -- for the narrow case where a
 * request already got a 401/403 back from storage despite the local cache
 * thinking the token was still fresh (clock skew, or the server's expiry
 * enforcement landing slightly ahead of the local margin).
 */
export const getMediaToken = async (forceRefresh = false): Promise<MediaToken | null> => {
    const key = tokenKey();
    if (!forceRefresh) {
        const current = getCachedMediaToken();
        if (current) return current;
    }
    if (inFlight && cachedKey === key) return inFlight;
    cachedKey = key;
    if (!forceRefresh) {
        const stored = readStored(key);
        if (stored) {
            cached = stored;
            return stored;
        }
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

let autoRefreshStarted = false;

const refreshDue = (): boolean => {
    const token = getCachedMediaToken();
    return !token || Date.parse(token.expiresAt) - Date.now() <= REFRESH_MARGIN_MS + 2 * AUTO_REFRESH_CHECK_MS;
};

/**
 * Keeps a fresh media token cached the whole time the tab is in use: checked
 * every minute while visible and replaced shortly before it would turn stale,
 * plus checked the moment a backgrounded tab becomes visible again (covers a
 * tab that was hidden through a whole token). No-op while hidden -- a tab the
 * user has walked away from makes no requests at all, which is the point: the
 * token is allowed to actually expire, not kept alive forever in the
 * background. Idempotent; call once at app boot (see preloadLocalIndexes).
 */
export const startMediaTokenAutoRefresh = (): void => {
    if (autoRefreshStarted || typeof document === 'undefined') return;
    autoRefreshStarted = true;
    const refreshIfVisible = () => {
        if (document.visibilityState === 'visible' && refreshDue()) {
            void getMediaToken(true);
        }
    };
    setInterval(refreshIfVisible, AUTO_REFRESH_CHECK_MS);
    document.addEventListener('visibilitychange', refreshIfVisible);
};

const encodeBlobPath = (blob: string): string => blob.split('/').map(encodeURIComponent).join('/');

const resignWith = (url: string, token: MediaToken): string => {
    if (!url.startsWith(`${token.baseUrl}/`)) return url;
    const query = url.indexOf('?');
    return `${query < 0 ? url : url.slice(0, query)}?${token.sas}`;
};

/**
 * A tile image's src/srcset re-signed with the current token, or null when
 * nothing would change -- the URLs are not this container's, or already carry
 * the current signature (then the image really is missing, e.g. a thumbnail
 * still being made). Lets an <img> whose signature expired before it loaded
 * (rendered before a token refresh, lazy-loaded after it) retry instead of
 * staying blank until the page is reloaded.
 */
export const resignMediaUrls = async (src: string, srcSet?: string): Promise<{ src: string; srcSet?: string } | null> => {
    const token = await getMediaToken();
    if (!token) return null;
    const nextSrc = resignWith(src, token);
    const nextSrcSet = srcSet
        ?.split(',').map((entry) => {
            const [url, ...descriptor] = entry.trim().split(/\s+/);
            return [resignWith(url, token), ...descriptor].join(' ');
        }).join(', ');
    if (nextSrc === src && (srcSet === undefined || nextSrcSet === srcSet.split(',').map((e) => e.trim()).join(', '))) return null;
    return { src: nextSrc, ...(nextSrcSet !== undefined ? { srcSet: nextSrcSet } : {}) };
};

/** Direct thumbnail URL for a blob name, or '' when no token is loaded. */
export const thumbnailUrlForBlob = (blob: string | undefined | null, token: MediaToken | null = getCachedMediaToken()): string => (
    blob && token ? `${token.baseUrl}/${encodeBlobPath(blob)}?${token.sas}` : ''
);

const VERSIONED_PREVIEW = /^derived\/v1\/[^/]+\/[^/]+\/preview\/[^/]+$/;

/** Direct preview URL, from a photo's physical blob name (previews live under `preview/` in the
 * same container) or a versioned preview's own name (derived/v1/.../preview/<sha>.jpg). A versioned
 * thumbnail's name says nothing about its preview, so it yields ''. */
export const previewUrlForBlob = (blob: string | undefined | null, token: MediaToken | null = getCachedMediaToken()): string => {
    if (!blob || !token) return '';
    if (VERSIONED_PREVIEW.test(blob)) return `${token.baseUrl}/${encodeBlobPath(blob)}?${token.sas}`;
    return blob.startsWith('derived/v1/') ? '' : `${token.baseUrl}/${token.previewPrefix}${encodeBlobPath(blob)}.jpg?${token.sas}`;
};

/** Direct full-resolution original URL (separate container token from thumbnail/preview), or '' when unavailable. */
export const imageUrlForBlob = (blob: string | undefined | null, token: MediaToken | null = getCachedMediaToken()): string => (
    blob && !blob.startsWith('derived/v1/') && token?.image ? `${token.image.baseUrl}/${encodeBlobPath(blob)}?${token.image.sas}` : ''
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
