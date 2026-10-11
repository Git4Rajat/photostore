import { getUpload } from './apiClient';
import type { RequestConfig } from './httpClient';
import { perf } from './perf';

export const UPLOAD_HASH_PREFETCH_TIMEOUT_MS = 3000;

// Early duplicate skipping is optional; backend finalization remains authoritative.
export async function fetchKnownUploadHashes(): Promise<Record<string, string>> {
    const startedAt = performance.now();
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let timedOut = false;
    const deadline = new Promise<null>((resolve) => {
        timer = setTimeout(() => {
            timedOut = true;
            resolve(null);
            controller.abort();
        }, UPLOAD_HASH_PREFETCH_TIMEOUT_MS);
    });
    const config: RequestConfig = {
        signal: controller.signal,
        timeout: UPLOAD_HASH_PREFETCH_TIMEOUT_MS,
        singleAttempt: true,
    };
    try {
        // The deadline also bounds auth interceptors or transports that ignore abort.
        const response = await Promise.race([
            getUpload<{ hashes?: Record<string, string> }>('/upload/known-hashes', config).catch(() => null),
            deadline,
        ]);
        const hashes = response?.hashes;
        const result = hashes && typeof hashes === 'object' && !Array.isArray(hashes)
            ? Object.fromEntries(Object.entries(hashes).filter(([, filename]) => typeof filename === 'string'))
            : {};
        perf.recordSpan(timedOut ? 'upload.known_hashes.timeout' : 'upload.known_hashes', performance.now() - startedAt, {
            rows: Object.keys(result).length,
            cached: response !== null,
        });
        return result;
    } finally {
        clearTimeout(timer);
        controller.abort();
    }
}
