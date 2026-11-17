import { beforeEach, describe, expect, it, vi } from 'vitest';

const getMock = vi.fn();
vi.mock('./apiClient', () => ({ get: (...args: unknown[]) => getMock(...args) }));
vi.mock('./passwordAuthClient', () => ({ getActiveLibraryFromToken: () => 'lib-1' }));

import { getCachedMediaToken, getMediaToken, imageUrlForBlob, invalidateMediaToken, previewUrlForBlob, resignMediaUrls, startMediaTokenAutoRefresh, thumbnailUrlForBlob } from './mediaToken';

// The server mints short-lived tokens now (MEDIA_TOKEN_SAS_TTL_SECONDS,
// ~10 minutes) rather than day-long ones -- minutes, not hours, are the
// realistic unit here.
const futureMin = (minutes: number) => new Date(Date.now() + minutes * 60_000).toISOString();
const tokenResponse = (minutes = 9) => ({
    available: true, baseUrl: 'https://acct.blob.core.windows.net/thumbnails', sas: 'sp=r&sr=c&sig=abc', expiresAt: futureMin(minutes), previewPrefix: 'preview/',
    image: { baseUrl: 'https://acct.blob.core.windows.net/images', sas: 'sp=r&sr=c&sig=def' },
});

beforeEach(() => {
    const values = new Map<string, string>();
    const storage = { getItem: (key: string) => values.get(key) ?? null,
        setItem: (key: string, value: string) => values.set(key, value),
        removeItem: (key: string) => values.delete(key), clear: () => values.clear() };
    vi.stubGlobal('localStorage', storage);
});

describe('mediaToken', () => {
    beforeEach(() => {
        getMock.mockReset();
        invalidateMediaToken();
        window.localStorage.clear();
    });

    it('fetches once, caches, and builds direct thumbnail/preview URLs without further requests', async () => {
        getMock.mockResolvedValue(tokenResponse());
        const token = await getMediaToken();
        expect(token).not.toBeNull();
        await getMediaToken();
        expect(getMock).toHaveBeenCalledTimes(1);
        expect(getCachedMediaToken()).toEqual(token);
        expect(thumbnailUrlForBlob('1ed9-uuid')).toBe('https://acct.blob.core.windows.net/thumbnails/1ed9-uuid?sp=r&sr=c&sig=abc');
        expect(previewUrlForBlob('1ed9-uuid')).toBe('https://acct.blob.core.windows.net/thumbnails/preview/1ed9-uuid.jpg?sp=r&sr=c&sig=abc');
        expect(imageUrlForBlob('1ed9-uuid')).toBe('https://acct.blob.core.windows.net/images/1ed9-uuid?sp=r&sr=c&sig=def');
    });

    it('shares one in-flight request between concurrent callers', async () => {
        getMock.mockResolvedValue(tokenResponse());
        await Promise.all([getMediaToken(), getMediaToken(), getMediaToken()]);
        expect(getMock).toHaveBeenCalledTimes(1);
    });

    it('reuses a stored token across reloads and refetches one that is about to expire', async () => {
        getMock.mockResolvedValue(tokenResponse());
        await getMediaToken();
        invalidateMediaToken(); // simulate a page reload: memory gone, localStorage kept
        await getMediaToken();
        expect(getMock).toHaveBeenCalledTimes(1);

        window.localStorage.clear();
        invalidateMediaToken();
        getMock.mockResolvedValue(tokenResponse(1)); // < 2 min left (REFRESH_MARGIN_MS) counts as stale
        await getMediaToken();
        invalidateMediaToken();
        await getMediaToken();
        expect(getMock).toHaveBeenCalledTimes(3);
    });

    it('forceRefresh bypasses both the in-memory and stored cache even when still fresh', async () => {
        getMock.mockResolvedValue(tokenResponse());
        await getMediaToken();
        expect(getMock).toHaveBeenCalledTimes(1);
        await getMediaToken(); // still fresh, cached -- no new call
        expect(getMock).toHaveBeenCalledTimes(1);
        await getMediaToken(true); // forced -- real request despite looking fresh
        expect(getMock).toHaveBeenCalledTimes(2);
    });

    it('returns null and builds no URLs when the backend has no token (proxy mode)', async () => {
        getMock.mockResolvedValue({ available: false });
        expect(await getMediaToken()).toBeNull();
        expect(thumbnailUrlForBlob('x')).toBe('');
    });

    it('url-encodes blob names per path segment', () => {
        const token = { baseUrl: 'https://a/b', sas: 's=1', expiresAt: futureMin(9), previewPrefix: 'preview/' };
        expect(thumbnailUrlForBlob('my photo#1.jpg', token)).toBe('https://a/b/my%20photo%231.jpg?s=1');
    });
});

it('loads versioned thumbnails directly but resolves other tiers independently', async () => {
    getMock.mockResolvedValue(tokenResponse());
    const token = await getMediaToken();
    const blob = 'derived/v1/source/thumbnail/digest.jpg';
    expect(thumbnailUrlForBlob(blob, token)).toContain('/derived/v1/source/thumbnail/digest.jpg?');
    expect(previewUrlForBlob(blob, token)).toBe('');
    expect(imageUrlForBlob(blob, token)).toBe('');
});

it('builds a versioned preview from its own name with the same container token', async () => {
    getMock.mockResolvedValue(tokenResponse());
    const token = await getMediaToken();
    const preview = 'derived/v1/scope/key/preview/digest.jpg';
    expect(previewUrlForBlob(preview, token)).toBe(
        'https://acct.blob.core.windows.net/thumbnails/derived/v1/scope/key/preview/digest.jpg?sp=r&sr=c&sig=abc');
    expect(previewUrlForBlob('derived/v1/scope/key/thumbnail/digest.jpg', token)).toBe('');
    expect(previewUrlForBlob('physical-id', token)).toBe(
        'https://acct.blob.core.windows.net/thumbnails/preview/physical-id.jpg?sp=r&sr=c&sig=abc');
});

describe('re-signing tile URLs after the token changes', () => {
    const base = 'https://acct.blob.core.windows.net/thumbnails';
    beforeEach(() => {
        getMock.mockReset();
        invalidateMediaToken();
        window.localStorage.clear();
    });

    it('swaps an expired signature for the current one, in src and srcset', async () => {
        getMock.mockResolvedValue(tokenResponse());
        const old = 'sp=r&sr=c&sig=OLD';
        expect(await resignMediaUrls(`${base}/abc?${old}`, `${base}/abc?${old} 90w, ${base}/grid/abc.webp?${old} 400w`)).toEqual({
            src: `${base}/abc?sp=r&sr=c&sig=abc`,
            srcSet: `${base}/abc?sp=r&sr=c&sig=abc 90w, ${base}/grid/abc.webp?sp=r&sr=c&sig=abc 400w`,
        });
    });

    it('gives up when the URL already carries the current signature or is not ours', async () => {
        getMock.mockResolvedValue(tokenResponse());
        expect(await resignMediaUrls(`${base}/abc?sp=r&sr=c&sig=abc`)).toBeNull();
        expect(await resignMediaUrls('/api/photos/thumbnail/a.jpg')).toBeNull();
        // Proxy mode: no token at all, nothing to re-sign with.
        getMock.mockResolvedValue({ available: false });
        invalidateMediaToken();
        window.localStorage.clear();
        expect(await resignMediaUrls(`${base}/abc?sig=OLD`)).toBeNull();
    });
});

// The cached token turns stale two minutes before its 10-minute life ends; the
// background refresh must replace it before then, or tiles fall back to dead URLs.
it('keeps a fresh token cached for the whole time the tab is in use', async () => {
    vi.useFakeTimers();
    try {
        getMock.mockReset().mockResolvedValue(tokenResponse(10));
        invalidateMediaToken();
        window.localStorage.clear();
        await getMediaToken();
        startMediaTokenAutoRefresh();
        getMock.mockImplementation(async () => ({ ...tokenResponse(10), sas: 'sig=next' }));
        for (let minute = 1; minute <= 25; minute += 1) {
            await vi.advanceTimersByTimeAsync(60_000);
            expect(getCachedMediaToken(), `no fresh token at minute ${minute}`).not.toBeNull();
        }
        // About one refresh per token life, not one per check.
        expect(getMock.mock.calls.length).toBeGreaterThanOrEqual(3);
        expect(getMock.mock.calls.length).toBeLessThanOrEqual(6);
    } finally {
        vi.useRealTimers();
    }
});
