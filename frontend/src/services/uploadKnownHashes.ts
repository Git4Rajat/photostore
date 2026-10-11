import { getUpload, postUpload } from './apiClient';
import type { RequestConfig } from './httpClient';
import { perf } from './perf';

export const UPLOAD_HASH_DOWNLOAD_TIMEOUT_MS = 60000;
export const UPLOAD_HASH_DOWNLOAD_ATTEMPTS = 3;
const RETRY_DELAYS_MS = [2000, 5000];

export type UploadEndReason = 'completed' | 'stopped' | 'failed' | 'interrupted' | 'discarded';

export async function requestUploadHashRefresh(reason: UploadEndReason): Promise<void> {
    // The HTTP client retries transient failures; the build itself runs on the worker.
    await postUpload('/upload/known-hashes/refresh', { reason }, { timeout: 15000 }).catch(() => undefined);
}

function waitForRetry(ms: number, signal?: AbortSignal): Promise<void> {
    if (signal?.aborted) return Promise.resolve();
    return new Promise((resolve) => {
        const finish = () => {
            clearTimeout(timer);
            signal?.removeEventListener('abort', finish);
            resolve();
        };
        const timer = setTimeout(finish, ms);
        signal?.addEventListener('abort', finish, { once: true });
    });
}

// Background-only download: its deadline/retries never hold up upload workers.
export async function fetchKnownUploadHashes(signal?: AbortSignal): Promise<Record<string, string>> {
    for (let attempt = 0; attempt < UPLOAD_HASH_DOWNLOAD_ATTEMPTS && !signal?.aborted; attempt += 1) {
        const startedAt = performance.now();
        const controller = new AbortController();
        let timer: ReturnType<typeof setTimeout> | undefined;
        let timedOut = false;
        let cancel!: () => void;
        const deadline = new Promise<null>((resolve) => {
            cancel = () => { resolve(null); controller.abort(); };
            timer = setTimeout(() => {
                timedOut = true;
                cancel();
            }, UPLOAD_HASH_DOWNLOAD_TIMEOUT_MS);
            signal?.addEventListener('abort', cancel, { once: true });
        });
        const config: RequestConfig = {
            signal: controller.signal,
            timeout: UPLOAD_HASH_DOWNLOAD_TIMEOUT_MS,
            singleAttempt: true,
        };
        let response;
        try {
            // Also bounds auth interceptors/transports that ignore timeout or abort.
            response = await Promise.race([
                getUpload<{ hashes?: Record<string, string>; available?: boolean }>('/upload/known-hashes', config).catch(() => null),
                deadline,
            ]);
        } finally {
            clearTimeout(timer);
            signal?.removeEventListener('abort', cancel);
            controller.abort();
        }
        if (signal?.aborted) return {};
        const hashes = response?.hashes;
        const result = hashes && typeof hashes === 'object' && !Array.isArray(hashes)
            ? Object.fromEntries(Object.entries(hashes).filter(([, filename]) => typeof filename === 'string'))
            : {};
        perf.recordSpan(timedOut ? 'upload.known_hashes.timeout' : 'upload.known_hashes', performance.now() - startedAt, {
            rows: Object.keys(result).length,
            cached: response !== null && response.available !== false,
            attempt,
        });
        if (response !== null && response.available !== false) return result;
        if (attempt + 1 < UPLOAD_HASH_DOWNLOAD_ATTEMPTS) {
            await waitForRetry(RETRY_DELAYS_MS[attempt], signal);
        }
    }
    return {};
}
