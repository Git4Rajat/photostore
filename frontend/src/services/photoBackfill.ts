import { postAdmin } from './apiClient';
import { isApiError } from './apiError';

export interface PhotoBackfillProgress {
    queued: number;
    skipped: number;
    failed: number;
}

interface PhotoBackfillBatch extends PhotoBackfillProgress {
    complete: boolean;
    continuation: string | null;
    processingMode?: 'browser' | 'backend' | 'both';
}

// Sequential, bounded requests keep a library-sized operation below HTTP
// deadlines. Never retry a successful page or fan out forced resets.
export async function queuePhotoBackfill(
    steps: string[] | undefined,
    onProgress: (progress: PhotoBackfillProgress) => void,
): Promise<PhotoBackfillProgress & { processingMode?: PhotoBackfillBatch['processingMode'] }> {
    const progress = { queued: 0, skipped: 0, failed: 0 };
    let continuation: string | null = null;
    try {
        for (;;) {
            const batch: PhotoBackfillBatch = await postAdmin('/api/admin/backfill/photos', {
                repair: true,
                confirm: 'BACKFILL_ALL_PHOTOS',
                ...(steps ? { steps } : {}),
                ...(continuation ? { continuation } : {}),
            });
            progress.queued += Number(batch.queued || 0);
            progress.skipped += Number(batch.skipped || 0);
            progress.failed += Number(batch.failed || 0);
            onProgress({ ...progress });
            // Older servers return a single full-library result.
            if (batch.complete !== false) return { ...progress, processingMode: batch.processingMode };
            if (!batch.continuation || batch.continuation === continuation) {
                throw new Error('Backfill did not return an advancing continuation cursor');
            }
            continuation = batch.continuation;
        }
    } catch (error) {
        if (isApiError(error) && error.responseData && typeof error.responseData === 'object') {
            const partial = error.responseData as Partial<PhotoBackfillBatch>;
            progress.queued += Number(partial.queued || 0);
            progress.skipped += Number(partial.skipped || 0);
            progress.failed += Number(partial.failed || 0);
        }
        throw new Error(`Queueing stopped after at least ${progress.queued} photos queued, ${progress.skipped} skipped, ${progress.failed} failed. Already queued photos remain scheduled. ${String(error)}`);
    }
}