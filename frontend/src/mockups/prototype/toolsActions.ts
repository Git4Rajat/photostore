import { classifyApiError } from '../../services/apiError';

/** What actually came of a Tools action, for the toast and the History row. */
type ActionOutcome = 'success' | 'partial' | 'failed';
export interface ActionResult { outcome: ActionOutcome; message: string; detail?: string; }

/** Body of the queue-style admin repair routes (dedupe, rebuilds, vector index, ...). */
export interface QueueResponse { success?: boolean; queued?: boolean; status?: string; jobId?: string | null; }

/** One page of POST /api/admin/backfill/photos (at most 10 photos per request). */
export interface BackfillBatch {
    queued?: number; skipped?: number; failed?: number;
    complete?: boolean; continuation?: string | null;
}
export interface BackfillTotals { queued: number; skipped: number; failed: number; batches: number; }

const BACKFILL_CONFIRM = 'BACKFILL_ALL_PHOTOS';
// Each request visits at most 10 photos; this only stops a runaway loop.
export const BACKFILL_MAX_BATCHES = 5000;

/**
 * The backend's own message for a 4xx (e.g. "this library processes photos
 * through upload waves"), otherwise the caller's generic text. 5xx/network
 * errors carry no useful detail for a user, so they keep the generic text.
 */
export const failureReason = (error: unknown, fallback: string): string => {
    const classified = classifyApiError(error);
    return classified.kind === 'client' && classified.message ? `${fallback}: ${classified.message}` : fallback;
};

/** A queue-style repair returned 200, but its body can still say nothing was queued. */
export const describeQueueResult = (label: string, response: QueueResponse | null | undefined): ActionResult => {
    if (response && (response.success === false || response.queued === false)) {
        const why = response.status ? ` (${response.status})` : '';
        return { outcome: 'failed', message: `${label} wasn’t queued${why}`, detail: response.status };
    }
    return { outcome: 'success', message: `${label} queued` };
};

/** "Process now" / "Cluster now" answer 202 with a status; only 'queued' means work was sent. */
export const describeStatusResult = (
    status: string | undefined,
    messages: { queued: string; waveOwned?: string },
): ActionResult => {
    if (status === 'wave_owned' && messages.waveOwned) {
        return { outcome: 'success', message: messages.waveOwned, detail: status };
    }
    if (status && status !== 'queued') {
        return { outcome: 'failed', message: `Nothing was sent (${status})`, detail: status };
    }
    return { outcome: 'success', message: messages.queued };
};

const summarise = (totals: BackfillTotals): string =>
    `${totals.queued} queued, ${totals.skipped} skipped, ${totals.failed} failed`;

/**
 * Backfill is one page per request: the backend returns a `continuation`
 * cursor and expects the caller to repeat until `complete`. Stops early when a
 * page queued nothing and failed something, since every later page would fail
 * the same way.
 */
export const runBackfillBatches = async (
    postBatch: (body: Record<string, unknown>) => Promise<BackfillBatch>,
    onProgress: (totals: BackfillTotals) => void,
    errorReason: (error: unknown) => string,
): Promise<ActionResult> => {
    const totals: BackfillTotals = { queued: 0, skipped: 0, failed: 0, batches: 0 };
    let cursor: string | null = null;
    const finish = (stoppedBecause?: string): ActionResult => {
        const text = `Backfill: ${summarise(totals)}${stoppedBecause ? ` — ${stoppedBecause}` : ''}`;
        const outcome: ActionOutcome = !stoppedBecause && totals.failed === 0
            ? 'success'
            : totals.queued > 0 ? 'partial' : 'failed';
        return { outcome, message: text, detail: text };
    };

    while (totals.batches < BACKFILL_MAX_BATCHES) {
        let batch: BackfillBatch;
        try {
            batch = await postBatch({ repair: true, confirm: BACKFILL_CONFIRM, ...(cursor ? { continuation: cursor } : {}) });
        } catch (error) {
            return totals.batches === 0
                ? { outcome: 'failed', message: errorReason(error), detail: errorReason(error) }
                : finish(errorReason(error));
        }
        totals.batches += 1;
        totals.queued += batch.queued ?? 0;
        totals.skipped += batch.skipped ?? 0;
        totals.failed += batch.failed ?? 0;
        onProgress({ ...totals });
        if (batch.complete) return finish();
        if ((batch.queued ?? 0) === 0 && (batch.failed ?? 0) > 0) return finish('stopped, every photo in the last batch failed');
        if (!batch.continuation) return finish('stopped, no way to continue');
        cursor = batch.continuation;
    }
    return finish('stopped at the batch limit');
};
